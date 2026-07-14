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
from app.wiki_markdown import (
    FileObservationInput,
    FrontmatterLimits,
    ObservationChanged,
    capture_file_observation,
)


RECOVERY_OBSERVATION_LIMITS = FrontmatterLimits()


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


@dataclass(frozen=True)
class _RecoveryFile:
    kind: Literal["expected", "intended", "unknown", "missing"]
    observation: FileObservationInput | None


def _fsync_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _sync_directory_posix(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directory_windows(path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = (wintypes.HANDLE,)
    flush_file_buffers.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    generic_write = 0x40000000
    share_read_write_delete = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    invalid_handle_value = ctypes.c_void_p(-1).value
    handle = create_file(
        str(path),
        generic_write,
        share_read_write_delete,
        None,
        open_existing,
        file_flag_backup_semantics,
        None,
    )
    if handle == invalid_handle_value:
        raise ctypes.WinError(ctypes.get_last_error())

    flush_error = (
        0 if flush_file_buffers(handle) else ctypes.get_last_error()
    )
    close_error = 0 if close_handle(handle) else ctypes.get_last_error()
    error_code = flush_error or close_error
    if error_code:
        raise ctypes.WinError(error_code)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        _sync_directory_windows(path)
    else:
        _sync_directory_posix(path)


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
        self._ensure_directory_durable(path)
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

    def capture_claim_path(self, intent_id: str) -> Path:
        return self._intent_dir(intent_id) / "capture-claim.md"

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
    def _fsync_directory(path: Path, *, strict: bool = False) -> None:
        try:
            _sync_directory(path)
        except OSError:
            if strict:
                raise

    @staticmethod
    def _fsync_existing_file(path: Path) -> None:
        with path.open("r+b") as handle:
            os.fsync(handle.fileno())

    def _ensure_directory_durable(self, path: Path) -> None:
        missing: list[Path] = []
        current = path
        while not current.exists():
            missing.append(current)
            parent = current.parent
            if parent == current:
                break
            current = parent
        if current.exists() and not current.is_dir():
            raise NotADirectoryError(current)
        for directory in reversed(missing):
            try:
                directory.mkdir()
            except FileExistsError:
                if not directory.is_dir():
                    raise
            self._fsync_directory(directory.parent, strict=True)

    def _sync_existing_path(self, path: Path) -> None:
        self._fsync_existing_file(path)
        self._fsync_directory(path.parent, strict=True)

    @staticmethod
    def _same_file(first: Path, second: Path) -> bool:
        try:
            return os.path.samefile(first, second)
        except OSError:
            return False

    def capture(
        self,
        intent_id: str,
        target_path: str,
        expected_file_hash: str | None,
    ) -> CapturedFile:
        target = self._safe_target(target_path)
        backup = self.backup_path(intent_id)
        claim = self.capture_claim_path(intent_id)
        claim_duplicates_backup = (
            claim.exists()
            and backup.exists()
            and self._same_file(claim, backup)
        )
        claim_duplicates_target = (
            claim.exists()
            and target.exists()
            and self._same_file(claim, target)
        )
        if claim.exists() and not (
            claim_duplicates_backup or claim_duplicates_target
        ):
            self._sync_existing_path(claim)
            claim_hash = self._stream_hash(claim)
            if not target.exists():
                try:
                    os.link(claim, target)
                except FileExistsError as exc:
                    observed = self._stream_hash(target)
                    raise TargetChanged(target_path, observed) from exc
                except OSError as exc:
                    raise TargetChanged(target_path, claim_hash) from exc
                self._sync_existing_path(target)
            raise TargetChanged(target_path, claim_hash)
        if backup.exists():
            self._sync_existing_path(backup)
            observed = self._stream_hash(backup)
            if expected_file_hash is not None and observed != expected_file_hash:
                raise TargetChanged(target_path, observed)
            if not target.exists():
                if claim_duplicates_backup and claim.exists():
                    self._fsync_directory(target.parent, strict=True)
                    claim.unlink()
                    self._fsync_directory(claim.parent, strict=True)
                return CapturedFile(backup, observed)
            if not self._same_file(target, backup):
                return CapturedFile(backup, observed)
        if not target.exists():
            if expected_file_hash is not None:
                raise TargetMissing(target_path)
            return CapturedFile(backup, None)
        self._ensure_directory_durable(backup.parent)
        if not backup.exists():
            try:
                os.link(target, backup)
            except FileExistsError as exc:
                observed = self._stream_hash(backup)
                raise TargetChanged(str(backup), observed) from exc
            self._sync_existing_path(backup)
        if claim.exists():
            raise TargetChanged(str(claim), self._stream_hash(claim))
        try:
            os.rename(target, claim)
        except FileExistsError as exc:
            observed = self._stream_hash(claim)
            raise TargetChanged(str(claim), observed) from exc
        self._sync_existing_path(claim)
        self._fsync_directory(target.parent, strict=True)

        if not self._same_file(claim, backup):
            claim_hash = self._stream_hash(claim)
            try:
                os.link(claim, target)
            except FileExistsError as exc:
                observed = self._stream_hash(target)
                raise TargetChanged(target_path, observed) from exc
            except OSError as exc:
                raise TargetChanged(target_path, claim_hash) from exc
            self._sync_existing_path(target)
            raise TargetChanged(target_path, claim_hash)
        if target.exists():
            raise TargetChanged(target_path, self._stream_hash(target))
        claim.unlink()
        self._fsync_directory(claim.parent, strict=True)
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
            self._fsync_directory(staged.parent, strict=True)
        else:
            self._sync_existing_path(staged)
            staged_hash = self._stream_hash(staged)
            if staged_hash != expected_hash:
                raise TargetChanged(str(staged), staged_hash)
        self._ensure_directory_durable(target.parent)
        try:
            os.link(staged, target)
        except FileExistsError as exc:
            raise TargetChanged(target_path, self._stream_hash(target)) from exc
        self._fsync_directory(target.parent, strict=True)
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

    @staticmethod
    def _classify_recovery_file(
        observation: FileObservationInput | None,
        *,
        expected_hash: str | None,
        intended_hash: str,
    ) -> _RecoveryFile:
        if observation is None:
            return _RecoveryFile("missing", None)
        if observation.file_hash == intended_hash:
            return _RecoveryFile("intended", observation)
        if observation.file_hash == expected_hash:
            return _RecoveryFile("expected", observation)
        return _RecoveryFile("unknown", observation)

    @staticmethod
    def _observe_recovery_path(path: Path) -> FileObservationInput | None:
        try:
            return capture_file_observation(
                path,
                max_content_bytes=RECOVERY_OBSERVATION_LIMITS.max_file_bytes,
                prefix_bytes=RECOVERY_OBSERVATION_LIMITS.max_prefix_bytes,
            )
        except FileNotFoundError:
            return None

    def _reconcile_recovery(
        self,
        intent: dict[str, Any],
        revision: dict[str, Any],
    ) -> IntentReconcileResult | None:
        from app.wiki_revisions import WikiRevisionError

        target = self.writer._safe_target(intent["target_path"])
        backup = self.writer.backup_path(intent["id"])
        staged = self.writer.staged_path(intent["id"])
        claim = self.writer.capture_claim_path(intent["id"])
        target_file = self._classify_recovery_file(
            self._observe_recovery_path(target),
            expected_hash=intent["expected_file_hash"],
            intended_hash=revision["file_hash"],
        )
        backup_file = self._classify_recovery_file(
            self._observe_recovery_path(backup),
            expected_hash=intent["expected_file_hash"],
            intended_hash=revision["file_hash"],
        )
        staged_file = self._classify_recovery_file(
            self._observe_recovery_path(staged),
            expected_hash=intent["expected_file_hash"],
            intended_hash=revision["file_hash"],
        )
        claim_file = self._classify_recovery_file(
            self._observe_recovery_path(claim),
            expected_hash=intent["expected_file_hash"],
            intended_hash=revision["file_hash"],
        )
        claim_is_duplicate = claim_file.kind != "missing" and any(
            self.writer._same_file(claim, path)
            for path in (target, backup)
            if path.exists()
        )
        claim_is_unknown = (
            claim_file.kind == "unknown" and not claim_is_duplicate
        )
        staged_is_unknown = staged_file.kind not in {"intended", "missing"}
        has_external_unknown = (
            target_file.kind == "unknown"
            or backup_file.kind == "unknown"
            or staged_is_unknown
            or claim_is_unknown
        )
        intended_is_installed = target_file.kind == "intended"
        intended_can_be_installed = (
            target_file.kind == "missing"
            and backup_file.kind == "intended"
        )
        if not has_external_unknown and (
            intended_is_installed or intended_can_be_installed
        ):
            self._require_lease(intent["id"])
            if intended_can_be_installed:
                self.writer.install(
                    intent["id"],
                    intent["target_path"],
                    revision["content"].encode("utf-8"),
                )
            else:
                self.writer._sync_existing_path(target)
                observed_hash = self.writer._stream_hash(target)
                if observed_hash != revision["file_hash"]:
                    raise TargetChanged(intent["target_path"], observed_hash)
            self._require_lease(intent["id"])
            captured_hash = (
                backup_file.observation.file_hash
                if backup_file.observation is not None
                else None
            )
            with connect_app_write(self.settings) as conn:
                installed = conn.execute(
                    """
                    UPDATE vault_write_intents
                    SET status='installed',backup_path=?,captured_file_hash=?,
                        backup_retention_status=?,updated_at=?
                    WHERE id=? AND status='recovery_required'
                      AND executor_owner=?
                    """,
                    (
                        str(backup),
                        captured_hash,
                        "retained" if captured_hash is not None else "none",
                        _utc_iso(),
                        intent["id"],
                        self.owner,
                    ),
                )
                if installed.rowcount != 1:
                    raise VaultWriteError(
                        "known intended recovery status CAS failed"
                    )
            mutation = self.revisions.finalize_intent(
                intent["id"],
                self.owner,
            )
            return IntentReconcileResult(
                intent_id=intent["id"],
                intent_status="applied",
                current_revision_id=mutation.current_revision_id,
                current_kind="intended",
            )
        if (
            target_file.kind != "unknown"
            and backup_file.kind != "unknown"
            and not staged_is_unknown
            and not claim_is_unknown
        ):
            if target_file.kind == backup_file.kind == "missing":
                outcome = self.revisions.fail_missing_recovery(
                    intent["id"],
                    self.owner,
                )
                return IntentReconcileResult(
                    intent_id=intent["id"],
                    intent_status="failed",
                    current_revision_id=outcome["current_revision_id"],
                    current_kind="unchanged",
                    observation_ids=tuple(outcome["observation_ids"]),
                )
            return None

        try:
            outcome = self.revisions.recover_write_intent(
                intent["id"],
                self.owner,
                target_kind=target_file.kind,
                target_observation=(
                    target_file.observation
                    if target_file.kind == "unknown"
                    else None
                ),
                backup_kind=backup_file.kind,
                backup_observation=(
                    backup_file.observation
                    if backup_file.kind == "unknown"
                    else None
                ),
                staged_kind=(
                    "unknown" if staged_is_unknown else staged_file.kind
                ),
                staged_observation=(
                    staged_file.observation if staged_is_unknown else None
                ),
                claim_kind=(
                    "missing" if claim_is_duplicate else claim_file.kind
                ),
                claim_observation=(
                    claim_file.observation if claim_is_unknown else None
                ),
            )
        except (VaultWriteError, WikiRevisionError) as exc:
            return self._mark_recovery(intent["id"], exc)
        return IntentReconcileResult(
            intent_id=intent["id"],
            intent_status=outcome["intent_status"],
            current_revision_id=outcome["current_revision_id"],
            current_kind=outcome["current_kind"],
            successor_intent_id=outcome.get("successor_intent_id"),
            observation_ids=tuple(outcome.get("observation_ids") or ()),
        )

    def capture_and_install(
        self,
        intent_id: str,
        *,
        stop_after: Literal["captured", "installed"] | None = None,
    ) -> IntentReconcileResult:
        from app.wiki_revisions import RevisionConflict, WikiRevisionError

        intent, revision = self._load_intent(intent_id)
        if intent["executor_owner"] != self.owner:
            raise VaultWriteError("intent is not owned by this executor")
        try:
            if intent["status"] == "recovery_required":
                self._require_lease(intent_id)
                recovered = self._reconcile_recovery(intent, revision)
                if recovered is not None:
                    return recovered
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
                    self.writer._sync_existing_path(target)
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
                self.writer._sync_existing_path(target)
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
        except (
            ObservationChanged,
            TargetChanged,
            TargetMissing,
            RevisionConflict,
            WikiRevisionError,
        ) as exc:
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
            if intent["status"] == "superseded":
                outcome = self.revisions.replay_recovery_handoff(intent_id)
                return IntentReconcileResult(
                    intent_id=intent_id,
                    intent_status="superseded",
                    current_revision_id=outcome["current_revision_id"],
                    current_kind=outcome["current_kind"],
                    successor_intent_id=outcome["successor_intent_id"],
                    observation_ids=tuple(outcome.get("observation_ids") or ()),
                )
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

    def reconcile_retained_backups(self) -> list[str]:
        with connect_app(self.settings) as conn:
            rows = conn.execute(
                """
                SELECT id FROM vault_write_intents
                WHERE status='applied'
                  AND backup_retention_status IN ('retained','change_detected')
                ORDER BY created_at,id
                """
            ).fetchall()
        changed: list[str] = []
        for row in rows:
            intent_id = str(row["id"])
            backup = self.writer.backup_path(intent_id)
            try:
                observation = capture_file_observation(
                    backup,
                    max_content_bytes=RECOVERY_OBSERVATION_LIMITS.max_file_bytes,
                    prefix_bytes=RECOVERY_OBSERVATION_LIMITS.max_prefix_bytes,
                )
                if self.revisions.reconcile_retained_backup(
                    intent_id,
                    observation,
                ):
                    changed.append(intent_id)
            except Exception:
                continue
        return changed

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
