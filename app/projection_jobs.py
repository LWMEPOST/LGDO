from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping

from app.config import Settings
from app.db import connect_app_write, json_dump

RETRY_DELAYS = {1: 5, 2: 30, 3: 120, 4: 600}
TERMINAL_STATUSES = {"succeeded", "failed", "superseded"}


def _utc_iso(value: datetime | None = None) -> str:
    timestamp = value or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ProjectionJob:
    id: str
    idempotency_key: str
    target: str
    operation: str
    page_id: str | None
    revision_id: str | None
    projection_epoch: int
    payload: dict[str, Any]
    status: str
    attempts: int
    available_at: str
    lease_owner: str | None
    lease_expires_at: str | None
    last_error: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ProjectionJob:
        payload = row.get("payload_json")
        return cls(
            id=str(row["id"]),
            idempotency_key=str(row["idempotency_key"]),
            target=str(row["target"]),
            operation=str(row["operation"]),
            page_id=row.get("page_id"),
            revision_id=row.get("revision_id"),
            projection_epoch=int(row["projection_epoch"]),
            payload=json.loads(payload or "{}"),
            status=str(row["status"]),
            attempts=int(row["attempts"]),
            available_at=str(row["available_at"]),
            lease_owner=row.get("lease_owner"),
            lease_expires_at=row.get("lease_expires_at"),
            last_error=row.get("last_error"),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


class ProjectionOutbox:
    def __init__(self, settings: Settings):
        self.settings = settings

    def enqueue(
        self,
        conn: Any,
        *,
        target: str,
        operation: str,
        page_id: str | None,
        revision_id: str | None,
        projection_epoch: int,
        payload: dict[str, Any],
    ) -> str:
        timestamp = _utc_iso()
        key = f"{target}:{operation}:{page_id or '-'}:{revision_id or '-'}:{projection_epoch}"
        job_id = f"pjob_{uuid.uuid4().hex}"
        conn.execute(
            """
            INSERT INTO knowledge_projection_jobs(
              id,idempotency_key,target,operation,page_id,revision_id,projection_epoch,
              payload_json,status,attempts,available_at,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?, 'pending',0,?,?,?)
            ON CONFLICT(idempotency_key) DO NOTHING
            """,
            (
                job_id,
                key,
                target,
                operation,
                page_id,
                revision_id,
                projection_epoch,
                json_dump(payload),
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        row = conn.execute(
            "SELECT id FROM knowledge_projection_jobs WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("projection job enqueue did not produce a row")
        return str(row["id"])

    def enqueue_pair_for_state(
        self,
        conn: Any,
        page: Mapping[str, Any],
        operation: str,
        payload: dict[str, Any],
    ) -> list[str]:
        values = dict(page)
        return [
            self.enqueue(
                conn,
                target=target,
                operation=operation,
                page_id=values.get("page_id"),
                revision_id=values.get("current_revision_id"),
                projection_epoch=int(values.get("projection_epoch") or 0),
                payload=payload,
            )
            for target in ("rag", "gbrain")
        ]

    def claim(
        self,
        *,
        target: str,
        worker_id: str,
        limit: int,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> list[ProjectionJob]:
        if limit <= 0:
            return []
        claimed_at = now or datetime.now(timezone.utc)
        claimed_iso = _utc_iso(claimed_at)
        lease_expires_at = _utc_iso(claimed_at + timedelta(seconds=max(1, lease_seconds)))
        suffix = " FOR UPDATE SKIP LOCKED" if self.settings.database_backend == "postgres" else ""
        jobs: list[ProjectionJob] = []
        with connect_app_write(self.settings) as conn:
            rows = conn.execute(
                """
                SELECT * FROM knowledge_projection_jobs
                WHERE target = ?
                  AND attempts < 5
                  AND (
                    (status IN ('pending','failed') AND available_at <= ?)
                    OR (status = 'running' AND lease_expires_at < ?)
                  )
                ORDER BY available_at, created_at
                LIMIT ?
                """
                + suffix,
                (target, claimed_iso, claimed_iso, limit),
            ).fetchall()
            for row in rows:
                updated = conn.execute(
                    """
                    UPDATE knowledge_projection_jobs
                    SET status='running', attempts=attempts+1, lease_owner=?,
                        lease_expires_at=?, updated_at=?
                    WHERE id=? AND attempts < 5
                      AND ((status IN ('pending','failed') AND available_at <= ?)
                           OR (status='running' AND lease_expires_at < ?))
                    """,
                    (
                        worker_id,
                        lease_expires_at,
                        claimed_iso,
                        row["id"],
                        claimed_iso,
                        claimed_iso,
                    ),
                )
                if updated.rowcount == 1:
                    data = dict(row)
                    data.update(
                        status="running",
                        attempts=int(row["attempts"]) + 1,
                        lease_owner=worker_id,
                        lease_expires_at=lease_expires_at,
                        updated_at=claimed_iso,
                    )
                    jobs.append(ProjectionJob.from_row(data))
        return jobs

    def renew_lease(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        renewed_at = now or datetime.now(timezone.utc)
        renewed_iso = _utc_iso(renewed_at)
        lease_expires_at = _utc_iso(renewed_at + timedelta(seconds=max(1, lease_seconds)))
        with connect_app_write(self.settings) as conn:
            cursor = conn.execute(
                """
                UPDATE knowledge_projection_jobs
                SET lease_expires_at=?, updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?
                """,
                (lease_expires_at, renewed_iso, job_id, worker_id),
            )
            return cursor.rowcount == 1

    def finish_claimed(
        self,
        conn: Any,
        *,
        job_id: str,
        worker_id: str,
        status: Literal["succeeded", "failed", "superseded"],
        last_error: str | None = None,
        available_at: str | None = None,
    ) -> bool:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"invalid terminal projection status: {status}")
        cursor = conn.execute(
            """
            UPDATE knowledge_projection_jobs
            SET status=?, last_error=?, available_at=COALESCE(?, available_at),
                lease_owner=NULL, lease_expires_at=NULL, updated_at=?
            WHERE id=? AND status='running' AND lease_owner=?
            """,
            (status, (last_error or "")[:500] or None, available_at, _utc_iso(), job_id, worker_id),
        )
        return cursor.rowcount == 1

    def mark_succeeded(self, job_id: str, worker_id: str) -> bool:
        with connect_app_write(self.settings) as conn:
            return self.finish_claimed(
                conn,
                job_id=job_id,
                worker_id=worker_id,
                status="succeeded",
            )

    def mark_failed(
        self,
        job_id: str,
        worker_id: str,
        last_error: str,
        now: datetime | None = None,
    ) -> bool:
        failed_at = now or datetime.now(timezone.utc)
        with connect_app_write(self.settings) as conn:
            suffix = " FOR UPDATE" if self.settings.database_backend == "postgres" else ""
            job = conn.execute(
                "SELECT * FROM knowledge_projection_jobs WHERE id=?" + suffix,
                (job_id,),
            ).fetchone()
            if job is None or job["status"] != "running" or job["lease_owner"] != worker_id:
                return False
            delay = RETRY_DELAYS.get(int(job["attempts"]))
            available_at = (
                _utc_iso(failed_at + timedelta(seconds=delay))
                if delay is not None
                else None
            )
            return self.finish_claimed(
                conn,
                job_id=job_id,
                worker_id=worker_id,
                status="failed",
                last_error=last_error,
                available_at=available_at,
            )

    def supersede_stale(self, conn: Any, page_id: str, projection_epoch: int) -> int:
        cursor = conn.execute(
            """
            UPDATE knowledge_projection_jobs
            SET status='superseded', lease_owner=NULL, lease_expires_at=NULL, updated_at=?
            WHERE page_id=? AND projection_epoch<? AND status IN ('pending','failed','running')
            """,
            (_utc_iso(), page_id, projection_epoch),
        )
        return cursor.rowcount
