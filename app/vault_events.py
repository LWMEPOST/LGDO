from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from app.config import Settings
from app.db import connect_app, connect_app_write
from app.timeutil import now_iso


TERMINAL_OCCURRENCE_STATUSES = frozenset(
    {
        "applied",
        "ignored",
        "invalid",
        "deferred",
        "deleted",
        "renamed",
        "resolved",
        "conflicted",
        "failed",
    }
)


class VaultOccurrenceConflict(RuntimeError):
    pass


def occurrence_payload_digest(
    kind: str,
    page_path: str,
    old_page_path: str | None,
) -> str:
    payload = json.dumps(
        {
            "kind": kind,
            "old_page_path": old_page_path,
            "page_path": page_path,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _datetime_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class VaultWatchOccurrence:
    id: str
    kind: str
    page_path: str
    old_page_path: str | None
    payload_digest: str
    status: str
    detected_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> VaultWatchOccurrence:
        values = dict(row)
        return cls(
            id=str(values["id"]),
            kind=str(values["kind"]),
            page_path=str(values["page_path"]),
            old_page_path=values.get("old_page_path"),
            payload_digest=str(values["payload_digest"]),
            status=str(values["status"]),
            detected_at=_parse_datetime(values["detected_at"]),
        )


@dataclass(frozen=True)
class PendingVaultDelete:
    id: str
    occurrence_id: str
    page_id: str
    old_page_path: str
    file_hash: str | None
    semantic_hash: str | None
    detected_at: datetime
    expires_at: datetime
    claim_owner: str | None = None
    claim_updated_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> PendingVaultDelete:
        values = dict(row)
        claim_owner = (
            str(values["matched_occurrence_id"])
            if values.get("status") == "pending"
            and values.get("matched_occurrence_id") is not None
            else None
        )
        return cls(
            id=str(values["id"]),
            occurrence_id=str(values["occurrence_id"]),
            page_id=str(values["page_id"]),
            old_page_path=str(values["old_page_path"]),
            file_hash=values.get("file_hash"),
            semantic_hash=values.get("semantic_hash"),
            detected_at=_parse_datetime(values["detected_at"]),
            expires_at=_parse_datetime(values["expires_at"]),
            claim_owner=claim_owner,
            claim_updated_at=(
                _parse_datetime(values["updated_at"])
                if claim_owner is not None
                else None
            ),
        )


@dataclass(frozen=True)
class VaultReconcileJob:
    id: str
    status: str
    requested_by: str
    attempts: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    result: dict[str, int] | None
    error_summary: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> VaultReconcileJob:
        values = dict(row)
        result_json = values.get("result_json")
        return cls(
            id=str(values["id"]),
            status=str(values["status"]),
            requested_by=str(values["requested_by"]),
            attempts=int(values["attempts"]),
            lease_owner=values.get("lease_owner"),
            lease_expires_at=(
                _parse_datetime(values["lease_expires_at"])
                if values.get("lease_expires_at") is not None
                else None
            ),
            result=json.loads(str(result_json)) if result_json is not None else None,
            error_summary=values.get("error_summary"),
        )


@dataclass(frozen=True)
class SyncIssueRef:
    id: str
    generation: int


class VaultEventStore:
    def __init__(self, settings: Settings):
        self.settings = settings

    def begin_occurrence(
        self,
        kind: str,
        page_path: str,
        *,
        detected_at: datetime,
        old_page_path: str | None = None,
        occurrence_id: str | None = None,
    ) -> VaultWatchOccurrence:
        if occurrence_id is None:
            occurrence_id = f"vocc_{uuid.uuid4().hex}"
        digest = occurrence_payload_digest(kind, page_path, old_page_path)
        detected_iso = _datetime_iso(detected_at)
        with connect_app_write(self.settings) as conn:
            conn.execute(
                """
                INSERT INTO vault_watch_occurrences(
                  id,kind,page_path,old_page_path,payload_digest,status,
                  detected_at,updated_at
                ) VALUES (?,?,?,?,?,'pending',?,?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    occurrence_id,
                    kind,
                    page_path,
                    old_page_path,
                    digest,
                    detected_iso,
                    detected_iso,
                ),
            )
            row = conn.execute(
                "SELECT * FROM vault_watch_occurrences WHERE id=?",
                (occurrence_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("watcher occurrence insert did not produce a row")
            if str(row["payload_digest"]) != digest:
                raise VaultOccurrenceConflict(
                    "watch occurrence payload changed"
                )
            return VaultWatchOccurrence.from_row(row)

    def finish_occurrence(
        self,
        occurrence_id: str,
        status: str,
        *,
        page_id: str | None = None,
        revision_id: str | None = None,
        sync_issue_id: str | None = None,
        error_summary: str | None = None,
    ) -> bool:
        if status not in TERMINAL_OCCURRENCE_STATUSES:
            raise VaultOccurrenceConflict(
                "watch occurrence requires a terminal status"
            )
        with connect_app_write(self.settings) as conn:
            updated = conn.execute(
                """
                UPDATE vault_watch_occurrences
                SET status=?,result_page_id=?,result_revision_id=?,sync_issue_id=?,
                    error_summary=?,updated_at=?
                WHERE id=? AND status='pending'
                """,
                (
                    status,
                    page_id,
                    revision_id,
                    sync_issue_id,
                    error_summary,
                    now_iso(),
                    occurrence_id,
                ),
            )
            if updated.rowcount == 1:
                return True
            row = conn.execute(
                """
                SELECT status,result_page_id,result_revision_id,sync_issue_id,
                       error_summary
                FROM vault_watch_occurrences WHERE id=?
                """,
                (occurrence_id,),
            ).fetchone()
            if row is None:
                raise VaultOccurrenceConflict("watch occurrence does not exist")
            stored = tuple(row[column] for column in row.keys())
            expected = (
                status,
                page_id,
                revision_id,
                sync_issue_id,
                error_summary,
            )
            if stored == expected:
                return False
            raise VaultOccurrenceConflict(
                "watch occurrence terminal result changed"
            )

    def pending_occurrences(self) -> list[VaultWatchOccurrence]:
        with connect_app(self.settings) as conn:
            rows = conn.execute(
                """
                SELECT * FROM vault_watch_occurrences
                WHERE status='pending' ORDER BY detected_at,id
                """
            ).fetchall()
        return [VaultWatchOccurrence.from_row(row) for row in rows]

    def has_unclassified_add_before(self, cutoff: datetime) -> bool:
        with connect_app(self.settings) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM vault_watch_occurrences
                WHERE kind='add' AND status='pending' AND detected_at<=?
                LIMIT 1
                """,
                (_datetime_iso(cutoff),),
            ).fetchone()
        return row is not None

    def page_by_path(self, page_path: str) -> dict[str, Any] | None:
        with connect_app(self.settings) as conn:
            row = conn.execute(
                "SELECT * FROM wiki_pages WHERE path=?",
                (page_path,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_wiki_pages(
        self,
        *,
        lifecycle_statuses: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        if not lifecycle_statuses:
            return []
        placeholders = ",".join("?" for _ in lifecycle_statuses)
        with connect_app(self.settings) as conn:
            rows = conn.execute(
                f"""
                SELECT path,page_id,file_hash,semantic_hash,lifecycle_status
                FROM wiki_pages
                WHERE lifecycle_status IN ({placeholders})
                ORDER BY path
                """,
                lifecycle_statuses,
            ).fetchall()
        return [dict(row) for row in rows]

    def has_active_intent(self, page_id: str) -> bool:
        with connect_app(self.settings) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM vault_write_intents
                WHERE page_id=?
                  AND status IN (
                    'pending','captured','installed','recovery_required'
                  )
                LIMIT 1
                """,
                (page_id,),
            ).fetchone()
        return row is not None

    def get_or_create_pending_delete(
        self,
        *,
        occurrence_id: str,
        page_id: str,
        old_page_path: str,
        file_hash: str | None,
        semantic_hash: str | None,
        detected_at: datetime,
        expires_at: datetime,
    ) -> PendingVaultDelete:
        delete_id = f"vdel_{uuid.uuid4().hex}"
        detected_iso = _datetime_iso(detected_at)
        expires_iso = _datetime_iso(expires_at)
        with connect_app_write(self.settings) as conn:
            conn.execute(
                """
                INSERT INTO pending_vault_deletes(
                  id,occurrence_id,page_id,old_page_path,file_hash,semantic_hash,
                  detected_at,expires_at,status,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,'pending',?)
                ON CONFLICT DO NOTHING
                """,
                (
                    delete_id,
                    occurrence_id,
                    page_id,
                    old_page_path,
                    file_hash,
                    semantic_hash,
                    detected_iso,
                    expires_iso,
                    detected_iso,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM pending_vault_deletes
                WHERE occurrence_id=?
                   OR (page_id=? AND old_page_path=? AND status='pending')
                ORDER BY CASE WHEN occurrence_id=? THEN 0 ELSE 1 END,id
                LIMIT 1
                """,
                (occurrence_id, page_id, old_page_path, occurrence_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("pending delete insert did not produce a row")
            return PendingVaultDelete.from_row(row)

    def list_due_deletes(self, now: datetime) -> list[PendingVaultDelete]:
        with connect_app(self.settings) as conn:
            rows = conn.execute(
                """
                SELECT * FROM pending_vault_deletes
                WHERE status='pending' AND expires_at<=?
                ORDER BY expires_at,detected_at,id
                """,
                (_datetime_iso(now),),
            ).fetchall()
        return [PendingVaultDelete.from_row(row) for row in rows]

    def get_pending_delete(self, delete_id: str) -> PendingVaultDelete | None:
        with connect_app(self.settings) as conn:
            row = conn.execute(
                """
                SELECT * FROM pending_vault_deletes
                WHERE id=? AND status='pending'
                """,
                (delete_id,),
            ).fetchone()
        return PendingVaultDelete.from_row(row) if row is not None else None

    def find_pending_delete_for_path(
        self,
        page_path: str,
    ) -> PendingVaultDelete | None:
        with connect_app(self.settings) as conn:
            row = conn.execute(
                """
                SELECT * FROM pending_vault_deletes
                WHERE old_page_path=? AND status='pending'
                ORDER BY detected_at,id LIMIT 1
                """,
                (page_path,),
            ).fetchone()
        return PendingVaultDelete.from_row(row) if row is not None else None

    def find_pending_deletes(self, *, page_id: str) -> list[PendingVaultDelete]:
        with connect_app(self.settings) as conn:
            rows = conn.execute(
                """
                SELECT * FROM pending_vault_deletes
                WHERE page_id=? AND status='pending'
                ORDER BY detected_at,id
                """,
                (page_id,),
            ).fetchall()
        return [PendingVaultDelete.from_row(row) for row in rows]

    def claim_delete(
        self,
        delete_id: str,
        owner: str,
        *,
        now: datetime,
        due_at: datetime | None = None,
    ) -> bool:
        claimed_at = _datetime_iso(now)
        stale_before = _datetime_iso(
            now - timedelta(seconds=self.settings.vault_reconcile_lease_seconds)
        )
        due_before = _datetime_iso(due_at or now)
        with connect_app_write(self.settings) as conn:
            updated = conn.execute(
                """
                UPDATE pending_vault_deletes
                SET matched_occurrence_id=?,updated_at=?
                WHERE id=? AND status='pending' AND expires_at<=? AND (
                  matched_occurrence_id IS NULL
                  OR updated_at<=?
                )
                """,
                (owner, claimed_at, delete_id, due_before, stale_before),
            )
            return updated.rowcount == 1

    def renew_delete_claim(
        self,
        delete_id: str,
        owner: str,
        *,
        now: datetime,
    ) -> bool:
        stale_before = _datetime_iso(
            now - timedelta(seconds=self.settings.vault_reconcile_lease_seconds)
        )
        with connect_app_write(self.settings) as conn:
            updated = conn.execute(
                """
                UPDATE pending_vault_deletes
                SET updated_at=?
                WHERE id=? AND status='pending' AND matched_occurrence_id=?
                  AND updated_at>?
                """,
                (_datetime_iso(now), delete_id, owner, stale_before),
            )
            return updated.rowcount == 1

    def release_delete_claim(
        self,
        delete_id: str,
        owner: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        released_at = now or datetime.now(timezone.utc)
        with connect_app_write(self.settings) as conn:
            updated = conn.execute(
                """
                UPDATE pending_vault_deletes
                SET matched_occurrence_id=NULL,updated_at=?
                WHERE id=? AND status='pending' AND matched_occurrence_id=?
                """,
                (_datetime_iso(released_at), delete_id, owner),
            )
            return updated.rowcount == 1

    def cancel_delete(self, delete_id: str, occurrence_id: str) -> bool:
        cancelled_at = datetime.now(timezone.utc)
        stale_before = cancelled_at - timedelta(
            seconds=self.settings.vault_reconcile_lease_seconds
        )
        with connect_app_write(self.settings) as conn:
            updated = conn.execute(
                """
                UPDATE pending_vault_deletes
                SET status='cancelled',matched_occurrence_id=?,updated_at=?
                WHERE id=? AND status='pending' AND (
                  matched_occurrence_id IS NULL OR updated_at<=?
                )
                """,
                (
                    occurrence_id,
                    _datetime_iso(cancelled_at),
                    delete_id,
                    _datetime_iso(stale_before),
                ),
            )
            return updated.rowcount == 1

    def cancel_claimed_delete(
        self,
        delete_id: str,
        owner: str,
        occurrence_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        cancelled_at = now or datetime.now(timezone.utc)
        stale_before = cancelled_at - timedelta(
            seconds=self.settings.vault_reconcile_lease_seconds
        )
        with connect_app_write(self.settings) as conn:
            updated = conn.execute(
                """
                UPDATE pending_vault_deletes
                SET status='cancelled',matched_occurrence_id=?,updated_at=?
                WHERE id=? AND status='pending' AND matched_occurrence_id=?
                  AND updated_at>?
                """,
                (
                    occurrence_id,
                    _datetime_iso(cancelled_at),
                    delete_id,
                    owner,
                    _datetime_iso(stale_before),
                ),
            )
            return updated.rowcount == 1

    def complete_delete(
        self,
        delete_id: str,
        owner: str | None = None,
        *,
        now: datetime | None = None,
    ) -> bool:
        completed_at = now or datetime.now(timezone.utc)
        stale_before = completed_at - timedelta(
            seconds=self.settings.vault_reconcile_lease_seconds
        )
        with connect_app_write(self.settings) as conn:
            updated = conn.execute(
                """
                UPDATE pending_vault_deletes
                SET status='completed',matched_occurrence_id=NULL,updated_at=?
                WHERE id=? AND status='pending' AND (
                  (? IS NULL AND matched_occurrence_id IS NULL)
                  OR (
                    ? IS NOT NULL AND matched_occurrence_id=? AND updated_at>?
                  )
                )
                """,
                (
                    _datetime_iso(completed_at),
                    delete_id,
                    owner,
                    owner,
                    owner,
                    _datetime_iso(stale_before),
                ),
            )
            return updated.rowcount == 1

    def request_reconcile(self, requested_by: str) -> VaultReconcileJob:
        job_id = f"vrec_{uuid.uuid4().hex}"
        timestamp = now_iso()
        with connect_app_write(self.settings) as conn:
            conn.execute(
                """
                INSERT INTO vault_reconcile_jobs(
                  id,scope,requested_by,status,attempts,created_at,updated_at
                ) VALUES (?,'full',?,'queued',0,?,?)
                ON CONFLICT DO NOTHING
                """,
                (job_id, requested_by, timestamp, timestamp),
            )
            row = conn.execute(
                """
                SELECT * FROM vault_reconcile_jobs
                WHERE scope='full' AND status IN ('queued','running')
                ORDER BY created_at,id LIMIT 1
                """
            ).fetchone()
            if row is None:
                raise RuntimeError("reconcile request did not produce an active job")
            return VaultReconcileJob.from_row(row)

    def claim_reconcile(
        self,
        job_id: str,
        owner: str,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        claimed_at = _datetime_iso(now)
        lease_expires_at = _datetime_iso(
            now + timedelta(seconds=lease_seconds)
        )
        with connect_app_write(self.settings) as conn:
            updated = conn.execute(
                """
                UPDATE vault_reconcile_jobs
                SET status='running',attempts=attempts+1,lease_owner=?,
                    lease_expires_at=?,started_at=COALESCE(started_at,?),updated_at=?
                WHERE id=? AND (
                  status='queued'
                  OR (
                    status='running'
                    AND (lease_expires_at IS NULL OR lease_expires_at<=?)
                  )
                )
                """,
                (
                    owner,
                    lease_expires_at,
                    claimed_at,
                    claimed_at,
                    job_id,
                    claimed_at,
                ),
            )
            return updated.rowcount == 1

    def renew_reconcile(
        self,
        job_id: str,
        owner: str,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        renewed_at = _datetime_iso(now)
        lease_expires_at = _datetime_iso(
            now + timedelta(seconds=lease_seconds)
        )
        with connect_app_write(self.settings) as conn:
            updated = conn.execute(
                """
                UPDATE vault_reconcile_jobs
                SET lease_expires_at=?,updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?
                """,
                (lease_expires_at, renewed_at, job_id, owner),
            )
            return updated.rowcount == 1

    def finish_reconcile(
        self,
        job_id: str,
        owner: str,
        result: dict[str, int],
    ) -> bool:
        timestamp = now_iso()
        result_json = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with connect_app_write(self.settings) as conn:
            updated = conn.execute(
                """
                UPDATE vault_reconcile_jobs
                SET status='succeeded',result_json=?,error_summary=NULL,
                    lease_owner=NULL,lease_expires_at=NULL,finished_at=?,updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?
                """,
                (result_json, timestamp, timestamp, job_id, owner),
            )
            return updated.rowcount == 1

    def fail_reconcile(self, job_id: str, owner: str, error: str) -> bool:
        timestamp = now_iso()
        with connect_app_write(self.settings) as conn:
            updated = conn.execute(
                """
                UPDATE vault_reconcile_jobs
                SET status='failed',error_summary=?,lease_owner=NULL,
                    lease_expires_at=NULL,finished_at=?,updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?
                """,
                (error, timestamp, timestamp, job_id, owner),
            )
            return updated.rowcount == 1

    def requeue_reconcile(self, job_id: str, owner: str) -> bool:
        with connect_app_write(self.settings) as conn:
            updated = conn.execute(
                """
                UPDATE vault_reconcile_jobs
                SET status='queued',lease_owner=NULL,lease_expires_at=NULL,updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?
                """,
                (now_iso(), job_id, owner),
            )
            return updated.rowcount == 1

    def get_reconcile(self, job_id: str) -> VaultReconcileJob | None:
        with connect_app(self.settings) as conn:
            row = conn.execute(
                "SELECT * FROM vault_reconcile_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        return VaultReconcileJob.from_row(row) if row is not None else None

    def active_reconcile(self) -> VaultReconcileJob | None:
        with connect_app(self.settings) as conn:
            row = conn.execute(
                """
                SELECT * FROM vault_reconcile_jobs
                WHERE scope='full' AND status IN ('queued','running')
                ORDER BY created_at,id LIMIT 1
                """
            ).fetchone()
        return VaultReconcileJob.from_row(row) if row is not None else None

    def latest_reconcile(self) -> VaultReconcileJob | None:
        with connect_app(self.settings) as conn:
            row = conn.execute(
                """
                SELECT * FROM vault_reconcile_jobs
                WHERE scope='full' ORDER BY created_at DESC,id DESC LIMIT 1
                """
            ).fetchone()
        return VaultReconcileJob.from_row(row) if row is not None else None

    def status_snapshot(self) -> dict[str, int | str | None]:
        with connect_app(self.settings) as conn:
            occurrence = conn.execute(
                """
                SELECT
                  SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                  MAX(updated_at) AS last_event_at
                FROM vault_watch_occurrences
                """
            ).fetchone()
            pending_deletes = conn.execute(
                """
                SELECT COUNT(*) FROM pending_vault_deletes
                WHERE status='pending'
                """
            ).fetchone()[0]
            open_issues = conn.execute(
                "SELECT COUNT(*) FROM vault_sync_issues WHERE status='open'"
            ).fetchone()[0]
            invalid_pages = conn.execute(
                """
                SELECT COUNT(*) FROM wiki_pages
                WHERE lifecycle_status='invalid'
                """
            ).fetchone()[0]
            latest_error = conn.execute(
                """
                SELECT error_summary FROM vault_watch_occurrences
                WHERE error_summary IS NOT NULL
                ORDER BY updated_at DESC,id DESC LIMIT 1
                """
            ).fetchone()
        return {
            "pending_occurrences": int(occurrence["pending"] or 0),
            "failed_occurrences": int(occurrence["failed"] or 0),
            "pending_deletes": int(pending_deletes),
            "open_issues": int(open_issues),
            "invalid_pages": int(invalid_pages),
            "last_event_at": occurrence["last_event_at"],
            "last_error": (
                latest_error["error_summary"]
                if latest_error is not None
                else None
            ),
        }

    def upsert_sync_issue(
        self,
        *,
        page_path: str,
        file_hash: str,
        page_id: str | None,
        issue_type: str,
        error_summary: str,
    ) -> SyncIssueRef:
        from app.wiki_revisions import _upsert_sync_issue_locked

        with connect_app_write(self.settings) as conn:
            issue_id, generation = _upsert_sync_issue_locked(
                conn,
                page_path=page_path,
                file_hash=file_hash,
                page_id=page_id,
                issue_type=issue_type,
                error_summary=error_summary,
            )
        return SyncIssueRef(issue_id, generation)

    def list_open_issue_refs(self, page_path: str) -> tuple[SyncIssueRef, ...]:
        with connect_app(self.settings) as conn:
            rows = conn.execute(
                """
                SELECT id,generation FROM vault_sync_issues
                WHERE page_path=? AND status='open' ORDER BY id
                """,
                (page_path,),
            ).fetchall()
        return tuple(
            SyncIssueRef(str(row["id"]), int(row["generation"]))
            for row in rows
        )

    def resolve_issue(self, issue: SyncIssueRef) -> bool:
        timestamp = now_iso()
        with connect_app(self.settings) as conn:
            changed = conn.execute(
                """
                UPDATE vault_sync_issues
                SET status='resolved',resolved_at=?,last_seen_at=?
                WHERE id=? AND generation=? AND status='open'
                """,
                (timestamp, timestamp, issue.id, issue.generation),
            )
            return changed.rowcount == 1
