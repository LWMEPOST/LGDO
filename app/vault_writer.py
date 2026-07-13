from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import portalocker

from app.config import Settings
from app.db import connect_app, connect_app_write


def _utc_iso(value: datetime | None = None) -> str:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat(timespec="seconds")


class VaultWriteError(RuntimeError):
    pass


class TargetMissing(VaultWriteError):
    def __init__(self, target_path: str):
        super().__init__(f"target missing: {target_path}")
        self.target_path = target_path


class TargetChanged(VaultWriteError):
    def __init__(self, target_path: str, observed_hash: str):
        super().__init__(f"target changed: {target_path}")
        self.target_path = target_path
        self.observed_hash = observed_hash


@dataclass(frozen=True)
class CapturedFile:
    backup_path: Path
    captured_hash: str | None


@dataclass(frozen=True)
class InstalledFile:
    target_path: Path
    target_hash: str


@dataclass(frozen=True)
class IntentReconcileResult:
    intent_id: str
    intent_status: Literal[
        "pending",
        "captured",
        "installed",
        "recovery_required",
        "applied",
        "superseded",
        "aborted",
        "failed",
    ]
    current_revision_id: str | None
    current_kind: Literal["intended", "external_target", "external_backup", "unchanged"]
    successor_intent_id: str | None = None
    observation_ids: tuple[str, ...] = ()


def _fsync_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


class AtomicVaultWriter:
    def __init__(self, vault_path: Path):
        self.vault_path = vault_path.resolve()
        self.pending_root = (self.vault_path / ".lgdo" / "pending").resolve()

    def _intent_dir(self, intent_id: str) -> Path:
        if not intent_id or any(char in intent_id for char in ("/", "\\")) or intent_id in {".", ".."}:
            raise VaultWriteError("invalid write intent id")
        path = (self.pending_root / intent_id).resolve()
        try:
            path.relative_to(self.pending_root)
        except ValueError as exc:
            raise VaultWriteError("write intent path escapes pending root") from exc
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _safe_target(self, target_path: str) -> Path:
        if "\\" in target_path or target_path.startswith("/"):
            raise VaultWriteError("target path must be Vault-relative")
        parts = PurePosixPath(target_path).parts
        if not parts or parts[0] != "wiki" or any(part in {"", ".", ".."} for part in parts):
            raise VaultWriteError("target path is outside managed Wiki")
        target = (self.vault_path / Path(*parts)).resolve()
        try:
            target.relative_to(self.vault_path)
        except ValueError as exc:
            raise VaultWriteError("target path escapes Vault") from exc
        return target

    def staged_path(self, intent_id: str) -> Path:
        return self._intent_dir(intent_id) / "new.md"

    def backup_path(self, intent_id: str) -> Path:
        return self._intent_dir(intent_id) / "backup.md"

    def lock_path(self, intent_id: str) -> Path:
        return self._intent_dir(intent_id) / "executor.lock"

    @staticmethod
    def _stream_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def capture(
        self,
        intent_id: str,
        target_path: str,
        expected_file_hash: str | None,
    ) -> CapturedFile:
        target = self._safe_target(target_path)
        backup = self.backup_path(intent_id)
        if backup.exists():
            observed = self._stream_hash(backup)
            if expected_file_hash is not None and observed != expected_file_hash:
                raise TargetChanged(target_path, observed)
            return CapturedFile(backup, observed)
        if not target.exists():
            if expected_file_hash is not None:
                raise TargetMissing(target_path)
            return CapturedFile(backup, None)
        backup.parent.mkdir(parents=True, exist_ok=True)
        os.rename(target, backup)
        self._fsync_directory(target.parent)
        observed = self._stream_hash(backup)
        if observed != expected_file_hash:
            raise TargetChanged(target_path, observed)
        return CapturedFile(backup, observed)

    def install(self, intent_id: str, target_path: str, content: bytes) -> InstalledFile:
        target = self._safe_target(target_path)
        staged = self.staged_path(intent_id)
        expected_hash = hashlib.sha256(content).hexdigest()
        if not staged.exists():
            _fsync_file(staged, content)
        else:
            staged_hash = self._stream_hash(staged)
            if staged_hash != expected_hash:
                raise TargetChanged(str(staged), staged_hash)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(staged, target)
        except FileExistsError as exc:
            raise TargetChanged(target_path, self._stream_hash(target)) from exc
        self._fsync_directory(target.parent)
        observed = self._stream_hash(target)
        if observed != expected_hash:
            raise TargetChanged(target_path, observed)
        return InstalledFile(target, observed)


class IntentExecutor:
    NONTERMINAL = ("pending", "captured", "installed", "recovery_required")

    def __init__(self, settings: Settings, owner: str | None = None):
        self.settings = settings
        self.owner = owner or f"executor_{uuid.uuid4().hex}"
        self._lease_seconds_by_intent: dict[str, int] = {}
        self.writer = AtomicVaultWriter(settings.vault_path)
        from app.wiki_revisions import WikiRevisionService

        self.revisions = WikiRevisionService(settings, writer=self.writer)

    def claim(
        self,
        intent_id: str,
        *,
        now: datetime | None = None,
        lease_seconds: int = 30,
    ) -> bool:
        lease_seconds = max(1, lease_seconds)
        claimed_at = now or datetime.now(timezone.utc)
        claimed_iso = _utc_iso(claimed_at)
        lease_expires_at = _utc_iso(claimed_at + timedelta(seconds=lease_seconds))
        with connect_app_write(self.settings) as conn:
            claimed = conn.execute(
                """
                UPDATE vault_write_intents
                SET executor_owner=?, lease_expires_at=?, attempts=attempts+1, updated_at=?
                WHERE id=?
                  AND status IN ('pending','captured','installed','recovery_required')
                  AND (executor_owner IS NULL OR executor_owner=? OR lease_expires_at<?)
                """,
                (
                    self.owner,
                    lease_expires_at,
                    claimed_iso,
                    intent_id,
                    self.owner,
                    claimed_iso,
                ),
            )
            claimed_ok = claimed.rowcount == 1
        if claimed_ok:
            self._lease_seconds_by_intent[intent_id] = lease_seconds
        return claimed_ok

    def renew_lease(
        self,
        intent_id: str,
        *,
        now: datetime | None = None,
        lease_seconds: int | None = None,
    ) -> bool:
        lease_seconds = max(
            1,
            lease_seconds
            if lease_seconds is not None
            else self._lease_seconds_by_intent.get(intent_id, 30),
        )
        renewed_at = now or datetime.now(timezone.utc)
        with connect_app_write(self.settings) as conn:
            cursor = conn.execute(
                """
                UPDATE vault_write_intents
                SET lease_expires_at=?,updated_at=?
                WHERE id=? AND executor_owner=?
                  AND status IN ('pending','captured','installed','recovery_required')
                """,
                (
                    _utc_iso(renewed_at + timedelta(seconds=lease_seconds)),
                    _utc_iso(renewed_at),
                    intent_id,
                    self.owner,
                ),
            )
            return cursor.rowcount == 1

    def advance_phase(self, intent_id: str, *, expected_status: str, status: str) -> bool:
        with connect_app_write(self.settings) as conn:
            cursor = conn.execute(
                """
                UPDATE vault_write_intents
                SET status=?,updated_at=?
                WHERE id=? AND status=? AND executor_owner=?
                """,
                (status, _utc_iso(), intent_id, expected_status, self.owner),
            )
            return cursor.rowcount == 1

    def _require_lease(
        self,
        intent_id: str,
        *,
        lease_seconds: int | None = None,
    ) -> None:
        if not self.renew_lease(intent_id, lease_seconds=lease_seconds):
            raise VaultWriteError("intent lease ownership was lost")

    def _load_intent(self, intent_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        with connect_app(self.settings) as conn:
            intent = conn.execute(
                "SELECT * FROM vault_write_intents WHERE id=?",
                (intent_id,),
            ).fetchone()
            if intent is None:
                raise VaultWriteError(f"write intent not found: {intent_id}")
            revision = conn.execute(
                "SELECT * FROM wiki_page_revisions WHERE id=?",
                (intent["revision_id"],),
            ).fetchone()
            if revision is None:
                raise VaultWriteError(f"write revision not found: {intent['revision_id']}")
        return dict(intent), dict(revision)

    def _result(self, intent_id: str, status: str, current_kind: str) -> IntentReconcileResult:
        intent, _ = self._load_intent(intent_id)
        with connect_app(self.settings) as conn:
            page = conn.execute(
                "SELECT current_revision_id FROM wiki_pages WHERE page_id=?",
                (intent["page_id"],),
            ).fetchone()
        return IntentReconcileResult(
            intent_id=intent_id,
            intent_status=status,
            current_revision_id=page["current_revision_id"] if page is not None else None,
            current_kind=current_kind,
        )

    def _mark_recovery(self, intent_id: str, error: Exception) -> IntentReconcileResult:
        with connect_app_write(self.settings) as conn:
            cursor = conn.execute(
                """
                UPDATE vault_write_intents
                SET status='recovery_required',last_error=?,updated_at=?
                WHERE id=? AND executor_owner=?
                  AND status IN ('pending','captured','installed','recovery_required')
                """,
                (str(error)[:500], _utc_iso(), intent_id, self.owner),
            )
            if cursor.rowcount != 1:
                raise VaultWriteError("lost intent ownership while marking recovery")
        return self._result(intent_id, "recovery_required", "unchanged")

    def capture_and_install(
        self,
        intent_id: str,
        *,
        stop_after: Literal["captured", "installed"] | None = None,
    ) -> IntentReconcileResult:
        from app.wiki_revisions import RevisionConflict

        intent, revision = self._load_intent(intent_id)
        if intent["executor_owner"] != self.owner:
            raise VaultWriteError("intent is not owned by this executor")
        try:
            if intent["status"] in {"pending", "recovery_required"}:
                self._require_lease(intent_id)
                captured = self.writer.capture(
                    intent_id,
                    intent["target_path"],
                    intent["expected_file_hash"],
                )
                self._require_lease(intent_id)
                with connect_app_write(self.settings) as conn:
                    advanced = conn.execute(
                        """
                        UPDATE vault_write_intents
                        SET status='captured',backup_path=?,captured_file_hash=?,
                            backup_retention_status=?,updated_at=?
                        WHERE id=? AND status IN ('pending','recovery_required') AND executor_owner=?
                        """,
                        (
                            str(captured.backup_path),
                            captured.captured_hash,
                            "retained" if captured.captured_hash is not None else "none",
                            _utc_iso(),
                            intent_id,
                            self.owner,
                        ),
                    )
                    if advanced.rowcount != 1:
                        raise VaultWriteError("lost intent ownership after capture")
                intent["status"] = "captured"
                if stop_after == "captured":
                    return self._result(intent_id, "captured", "unchanged")

            if intent["status"] == "captured":
                self._require_lease(intent_id)
                target = self.writer._safe_target(intent["target_path"])
                if target.exists():
                    observed_hash = self.writer._stream_hash(target)
                    if observed_hash != revision["file_hash"]:
                        raise TargetChanged(intent["target_path"], observed_hash)
                else:
                    self.writer.install(
                        intent_id,
                        intent["target_path"],
                        revision["content"].encode("utf-8"),
                    )
                self._require_lease(intent_id)
                if not self.advance_phase(intent_id, expected_status="captured", status="installed"):
                    raise VaultWriteError("lost intent ownership after install")
                intent["status"] = "installed"
                if stop_after == "installed":
                    return self._result(intent_id, "installed", "unchanged")

            if intent["status"] == "installed":
                self._require_lease(intent_id)
                target = self.writer._safe_target(intent["target_path"])
                if not target.exists():
                    raise TargetMissing(intent["target_path"])
                observed_hash = self.writer._stream_hash(target)
                if observed_hash != revision["file_hash"]:
                    raise TargetChanged(intent["target_path"], observed_hash)
                mutation = self.revisions.finalize_intent(intent_id, self.owner)
                return IntentReconcileResult(
                    intent_id=intent_id,
                    intent_status="applied",
                    current_revision_id=mutation.current_revision_id,
                    current_kind="intended",
                )
        except FileNotFoundError:
            return self._mark_recovery(intent_id, TargetMissing(intent["target_path"]))
        except (TargetChanged, TargetMissing, RevisionConflict) as exc:
            return self._mark_recovery(intent_id, exc)
        return self._result(intent_id, intent["status"], "unchanged")

    def execute(
        self,
        intent_id: str,
        *,
        stop_after: Literal["intent_created", "captured", "installed"] | None = None,
        lease_seconds: int = 30,
    ) -> IntentReconcileResult | None:
        if stop_after == "intent_created":
            return self._result(intent_id, "pending", "unchanged")
        if not self.claim(intent_id, lease_seconds=lease_seconds):
            return None
        try:
            with portalocker.Lock(str(self.writer.lock_path(intent_id)), mode="a+", timeout=0):
                self._require_lease(intent_id, lease_seconds=lease_seconds)
                return self.capture_and_install(intent_id, stop_after=stop_after)
        except portalocker.exceptions.LockException:
            return None

    def reconcile_one(
        self,
        intent_id: str,
        *,
        now: datetime | None = None,
    ) -> IntentReconcileResult:
        if not self.claim(intent_id, now=now):
            intent, _ = self._load_intent(intent_id)
            if intent["executor_owner"] != self.owner:
                raise VaultWriteError("intent lease is held by another executor")
        try:
            with portalocker.Lock(str(self.writer.lock_path(intent_id)), mode="a+", timeout=0):
                return self.capture_and_install(intent_id)
        except portalocker.exceptions.LockException as exc:
            raise VaultWriteError("intent OS lock is held by another executor") from exc

    def reconcile_all(self, *, now: datetime | None = None) -> list[str]:
        reconcile_at = now or datetime.now(timezone.utc)
        with connect_app(self.settings) as conn:
            rows = conn.execute(
                """
                SELECT id FROM vault_write_intents
                WHERE status IN ('pending','captured','installed','recovery_required')
                  AND (executor_owner IS NULL OR executor_owner=? OR lease_expires_at<?)
                ORDER BY created_at,id
                """,
                (self.owner, _utc_iso(reconcile_at)),
            ).fetchall()
        completed: list[str] = []
        for row in rows:
            intent_id = row["id"]
            if not self.claim(intent_id, now=reconcile_at):
                continue
            try:
                with portalocker.Lock(str(self.writer.lock_path(intent_id)), mode="a+", timeout=0):
                    result = self.capture_and_install(intent_id)
            except portalocker.exceptions.LockException:
                continue
            if result.intent_status == "applied":
                completed.append(intent_id)
        return completed

    def clear_terminal(self, intent_id: str, status: str, error: str | None = None) -> bool:
        if status not in {"aborted", "failed"}:
            raise ValueError("terminal clear status must be aborted or failed")
        intent, _ = self._load_intent(intent_id)
        with self.revisions.coordinator.lock_page(page_id=intent["page_id"]) as locked:
            if locked.page.get("pending_write_intent_id") != intent_id:
                return False
            suffix = " FOR UPDATE" if self.settings.database_backend == "postgres" else ""
            owned_intent = locked.conn.execute(
                "SELECT status,executor_owner FROM vault_write_intents WHERE id=?" + suffix,
                (intent_id,),
            ).fetchone()
            if (
                owned_intent is None
                or owned_intent["executor_owner"] != self.owner
                or owned_intent["status"] not in self.NONTERMINAL
            ):
                return False
            cleared_intent = locked.conn.execute(
                """
                UPDATE vault_write_intents
                SET status=?,last_error=?,executor_owner=NULL,lease_expires_at=NULL,updated_at=?
                WHERE id=? AND executor_owner=?
                  AND status IN ('pending','captured','installed','recovery_required')
                """,
                (status, (error or "")[:500] or None, _utc_iso(), intent_id, self.owner),
            )
            if cleared_intent.rowcount != 1:
                raise VaultWriteError("intent owner/status CAS failed while clearing terminal state")
            cleared_page = locked.conn.execute(
                """
                UPDATE wiki_pages SET pending_write_intent_id=NULL,updated_at=?
                WHERE page_id=? AND pending_write_intent_id=?
                """,
                (_utc_iso(), intent["page_id"], intent_id),
            )
            if cleared_page.rowcount != 1:
                raise VaultWriteError("pending intent CAS failed while clearing terminal state")
            return True
