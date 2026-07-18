from __future__ import annotations

import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import httpx

from app.config import Settings
from app.db import connect_app_write, json_dump
from app.projection_jobs import TERMINAL_STATUSES, ProjectionJob, ProjectionOutbox


MCP_PROTOCOL_VERSION = "2025-11-25"
PAGE_STATUSES = {"imported", "skipped", "error", "superseded", "recovery_required"}


class ProjectionConfigurationError(RuntimeError):
    """Projection endpoint, credential, source, or root configuration is invalid."""


class ProjectionNetworkError(RuntimeError):
    """The HTTP MCP exchange failed before a trustworthy result was decoded."""


class ProjectionProtocolError(RuntimeError):
    """The MCP envelope or lgdo_vault_sync payload violated the locked contract."""


class ProjectionToolError(RuntimeError):
    """The MCP server returned a structured tool error."""


class ProjectionLeaseLost(RuntimeError):
    def __init__(self, owner_type: str, owner_id: str):
        self.owner_type = owner_type
        self.owner_id = owner_id
        super().__init__(f"GBrain projection {owner_type} lease lost: {owner_id}")


def to_gbrain_manifest_path(page_path: str) -> str:
    prefix = "wiki/"
    if "\\" in page_path or not page_path.startswith(prefix):
        raise ProjectionProtocolError("wiki page path must begin with 'wiki/'")
    relative = page_path[len(prefix) :]
    raw_parts = relative.split("/")
    if not relative or relative.startswith("/") or any(part in {"", ".", ".."} for part in raw_parts):
        raise ProjectionProtocolError("wiki page path is not a safe GBrain source path")
    parts = PurePosixPath(relative).parts
    if "/".join(parts) != relative:
        raise ProjectionProtocolError("wiki page path is not a safe GBrain source path")
    return relative


@dataclass(frozen=True)
class ExpectedPage:
    page_id: str
    revision_id: str
    projection_epoch: int
    path: str
    file_hash: str


@dataclass(frozen=True)
class ProtectedMapping:
    source_id: str
    slug: str | None
    source_path: str | None
    reason: str


@dataclass(frozen=True)
class GBrainSyncRequest:
    source_id: str
    root: str
    mode: Literal["incremental", "reconcile"]
    expected_pages: Sequence[ExpectedPage]
    protected_mappings: Sequence[ProtectedMapping]
    no_embed: bool
    idempotency_key: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "root": self.root,
            "mode": self.mode,
            "expected_pages": [asdict(page) for page in self.expected_pages],
            "protected_mappings": [asdict(mapping) for mapping in self.protected_mappings],
            "no_embed": self.no_embed,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class GBrainPageSyncResult:
    page_id: str
    revision_id: str
    projection_epoch: int
    path: str
    file_hash: str
    source_id: str
    slug: str | None
    source_path: str
    raw_file_hash_before: str | None
    raw_file_hash_after: str | None
    content_hash: str | None
    page_generation: int | None
    status: str
    error: str | None
    protected_mappings: Sequence[ProtectedMapping]


@dataclass(frozen=True)
class GBrainDeletedResult:
    source_id: str
    slug: str


@dataclass(frozen=True)
class GBrainSyncResponse:
    source_id: str
    mode: Literal["incremental", "reconcile"]
    idempotency_key: str
    pages: Sequence[GBrainPageSyncResult]
    deleted: Sequence[GBrainDeletedResult]
    protected_mappings: Sequence[ProtectedMapping]
    imported: int
    skipped: int
    errors: int
    chunks: int
    duration_ms: float

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "GBrainSyncResponse":
        aggregate_keys = {
            "source_id",
            "mode",
            "idempotency_key",
            "pages",
            "deleted",
            "protected_mappings",
            "imported",
            "skipped",
            "errors",
            "chunks",
            "duration_ms",
        }
        _require_exact_keys(payload, aggregate_keys, "sync result")
        mode = _required_string(payload, "mode", "sync result")
        if mode not in {"incremental", "reconcile"}:
            raise ValueError(f"invalid sync mode: {mode}")

        pages: list[GBrainPageSyncResult] = []
        for index, item in enumerate(_required_list(payload, "pages", "sync result")):
            page = _required_mapping(item, f"pages[{index}]")
            page_keys = {
                "page_id",
                "revision_id",
                "projection_epoch",
                "path",
                "file_hash",
                "source_id",
                "slug",
                "source_path",
                "raw_file_hash_before",
                "raw_file_hash_after",
                "content_hash",
                "page_generation",
                "status",
                "error",
                "protected_mappings",
            }
            _require_exact_keys(page, page_keys, f"pages[{index}]")
            status = _required_string(page, "status", f"pages[{index}]")
            if status not in PAGE_STATUSES:
                raise ValueError(f"invalid page status: {status}")
            pages.append(
                GBrainPageSyncResult(
                    page_id=_required_string(page, "page_id", f"pages[{index}]"),
                    revision_id=_required_string(page, "revision_id", f"pages[{index}]"),
                    projection_epoch=_required_int(page, "projection_epoch", f"pages[{index}]"),
                    path=_required_string(page, "path", f"pages[{index}]"),
                    file_hash=_required_string(page, "file_hash", f"pages[{index}]"),
                    source_id=_required_string(page, "source_id", f"pages[{index}]"),
                    slug=_optional_string(page, "slug", f"pages[{index}]"),
                    source_path=_required_string(page, "source_path", f"pages[{index}]"),
                    raw_file_hash_before=_optional_string(page, "raw_file_hash_before", f"pages[{index}]"),
                    raw_file_hash_after=_optional_string(page, "raw_file_hash_after", f"pages[{index}]"),
                    content_hash=_optional_string(page, "content_hash", f"pages[{index}]"),
                    page_generation=_optional_int(page, "page_generation", f"pages[{index}]"),
                    status=status,
                    error=_optional_string(page, "error", f"pages[{index}]"),
                    protected_mappings=_parse_protected_mappings(
                        page["protected_mappings"],
                        f"pages[{index}].protected_mappings",
                    ),
                )
            )

        deleted: list[GBrainDeletedResult] = []
        for index, item in enumerate(_required_list(payload, "deleted", "sync result")):
            deleted_item = _required_mapping(item, f"deleted[{index}]")
            _require_exact_keys(deleted_item, {"source_id", "slug"}, f"deleted[{index}]")
            deleted.append(
                GBrainDeletedResult(
                    source_id=_required_string(deleted_item, "source_id", f"deleted[{index}]"),
                    slug=_required_string(deleted_item, "slug", f"deleted[{index}]"),
                )
            )

        duration_ms = _required_float(payload, "duration_ms", "sync result")
        if not math.isfinite(duration_ms) or duration_ms < 0:
            raise ValueError("sync result.duration_ms must be a finite non-negative number")
        return cls(
            source_id=_required_string(payload, "source_id", "sync result"),
            mode=mode,
            idempotency_key=_required_string(payload, "idempotency_key", "sync result"),
            pages=tuple(pages),
            deleted=tuple(deleted),
            protected_mappings=_parse_protected_mappings(
                payload["protected_mappings"],
                "sync result.protected_mappings",
            ),
            imported=_required_non_negative_int(payload, "imported", "sync result"),
            skipped=_required_non_negative_int(payload, "skipped", "sync result"),
            errors=_required_non_negative_int(payload, "errors", "sync result"),
            chunks=_required_non_negative_int(payload, "chunks", "sync result"),
            duration_ms=duration_ms,
        )


@dataclass(frozen=True)
class GBrainSegment:
    id: str
    batch_id: str
    index: int
    mode: Literal["incremental", "reconcile"]
    expected_pages: Sequence[ExpectedPage]
    idempotency_key: str


@dataclass(frozen=True)
class GBrainBatch:
    id: str
    mode: Literal["incremental", "reconcile"]
    worker_id: str
    expected_pages: Sequence[ExpectedPage]
    segments: Sequence[GBrainSegment]


@dataclass(frozen=True)
class GBrainBatchExecutionResult:
    batch_id: str
    status: Literal["succeeded", "failed"]
    segments: int


def build_segments(
    batch_id: str,
    mode: str,
    pages: Sequence[ExpectedPage],
) -> list[GBrainSegment]:
    if mode not in {"incremental", "reconcile"}:
        raise ValueError(f"invalid GBrain batch mode: {mode}")
    ordered = sorted(pages, key=lambda page: page.page_id)
    groups = [ordered[index : index + 100] for index in range(0, len(ordered), 100)]
    if not groups and mode == "reconcile":
        groups = [[]]
    if not groups:
        return []
    segments: list[GBrainSegment] = []
    for index, group in enumerate(groups):
        segment_mode = "reconcile" if mode == "reconcile" and index == len(groups) - 1 else "incremental"
        digest = sha256(json_dump([asdict(page) for page in group]).encode("utf-8")).hexdigest()
        segments.append(
            GBrainSegment(
                id=f"gbseg_{uuid.uuid4().hex}",
                batch_id=batch_id,
                index=index,
                mode=segment_mode,
                expected_pages=tuple(group),
                idempotency_key=f"{batch_id}:{index}:{digest}",
            )
        )
    return segments


class GBrainBatchRepository:
    def __init__(self, settings: Settings, outbox: ProjectionOutbox | None = None):
        self.settings = settings
        self.outbox = outbox or ProjectionOutbox(settings)

    def create_batch(
        self,
        jobs: Sequence[ProjectionJob],
        *,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> GBrainBatch | None:
        if lease_seconds <= 0:
            raise ValueError("GBrain batch lease must be positive")
        source_id = (self.settings.gbrain_managed_source_id or "").strip()
        if not source_id:
            raise ProjectionConfigurationError("GBrain managed source is not configured")
        if not jobs:
            return None

        batch_mode: Literal["incremental", "reconcile"] = (
            "reconcile"
            if any(job.operation in {"delete", "rename", "reconcile"} for job in jobs)
            else "incremental"
        )
        instant = now or datetime.now(timezone.utc)
        timestamp = instant.isoformat()
        lease_expires_at = (instant + timedelta(seconds=lease_seconds)).isoformat()
        batch_id = f"gbbatch_{uuid.uuid4().hex}"

        with connect_app_write(self.settings) as conn:
            resumed = self._resume_batch(
                conn,
                jobs,
                worker_id=worker_id,
                lease_expires_at=lease_expires_at,
                timestamp=timestamp,
            )
            if resumed is not None:
                return resumed
            retained: list[ProjectionJob] = []
            for claimed in sorted(jobs, key=lambda item: (item.created_at, item.id)):
                row = conn.execute(
                    "SELECT * FROM knowledge_projection_jobs WHERE id=?",
                    (claimed.id,),
                ).fetchone()
                if row is None or row["status"] != "running" or row["lease_owner"] != worker_id:
                    raise ProjectionLeaseLost("job", claimed.id)
                current = ProjectionJob.from_row(dict(row))
                if current.operation in {"upsert", "rename", "restore"}:
                    page = self._page_snapshot(
                        conn,
                        current.page_id,
                        for_update=True,
                    )
                    if not self._page_matches_job(page, current):
                        if not self.outbox.finish_claimed(
                            conn,
                            job_id=current.id,
                            worker_id=worker_id,
                            status="superseded",
                            last_error="desired revision or epoch changed before batch construction",
                        ):
                            raise ProjectionLeaseLost("job", current.id)
                        self._enqueue_current_page(conn, page)
                        continue
                retained.append(current)

            if not retained and batch_mode == "incremental":
                return None

            if batch_mode == "reconcile":
                expected_pages = self._active_expected_pages(conn)
            else:
                expected_pages = tuple(
                    expected
                    for job in retained
                    if job.operation in {"upsert", "rename", "restore"}
                    for expected in [self._expected_for_job(conn, job)]
                    if expected is not None
                )
            expected_pages = tuple(
                {page.page_id: page for page in expected_pages}.values()
            )
            expected_pages = tuple(sorted(expected_pages, key=lambda page: page.page_id))
            mapping_snapshot = self._mapping_snapshot(conn, source_id)
            protections = self._active_protections(conn, source_id)
            segments = build_segments(batch_id, batch_mode, expected_pages)
            if not segments:
                return None

            included_snapshot = {
                "jobs": [self._job_snapshot(job) for job in retained],
                "expected_pages": [asdict(page) for page in expected_pages],
                "mappings": mapping_snapshot,
                "protected_mappings": [asdict(mapping) for mapping in protections],
            }
            conn.execute(
                """
                INSERT INTO gbrain_projection_batches(
                  id,mode,status,batch_watermark_json,included_snapshot_json,
                  lease_owner,lease_expires_at,result_json,last_error,started_at,
                  finished_at,created_at,updated_at)
                VALUES (?,?, 'running',?,?,?,?, '{}',NULL,?,NULL,?,?)
                """,
                (
                    batch_id,
                    batch_mode,
                    json_dump({"created_at_lte": timestamp}),
                    json_dump(included_snapshot),
                    worker_id,
                    lease_expires_at,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            for job in retained:
                conn.execute(
                    """
                    INSERT INTO gbrain_projection_batch_jobs(
                      batch_id,job_id,page_id,revision_id,projection_epoch,operation)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        batch_id,
                        job.id,
                        job.page_id,
                        job.revision_id,
                        job.projection_epoch,
                        job.operation,
                    ),
                )
            for segment in segments:
                conn.execute(
                    """
                    INSERT INTO gbrain_projection_segments(
                      id,batch_id,segment_index,mode,idempotency_key,
                      expected_pages_json,protected_mappings_json,status,
                      lease_owner,lease_expires_at,result_json,last_error,
                      started_at,finished_at)
                    VALUES (?,?,?,?,?,?,?, 'pending',?,?, '{}',NULL,NULL,NULL)
                    """,
                    (
                        segment.id,
                        batch_id,
                        segment.index,
                        segment.mode,
                        segment.idempotency_key,
                        json_dump([asdict(page) for page in segment.expected_pages]),
                        json_dump([asdict(mapping) for mapping in protections]),
                        worker_id,
                        lease_expires_at,
                    ),
                )

        return GBrainBatch(
            id=batch_id,
            mode=batch_mode,
            worker_id=worker_id,
            expected_pages=expected_pages,
            segments=tuple(segments),
        )

    def _resume_batch(
        self,
        conn: Any,
        jobs: Sequence[ProjectionJob],
        *,
        worker_id: str,
        lease_expires_at: str,
        timestamp: str,
    ) -> GBrainBatch | None:
        job_ids = {job.id for job in jobs}
        if not job_ids:
            return None
        placeholders = ",".join("?" for _ in job_ids)
        rows = conn.execute(
            f"""
            SELECT b.* FROM gbrain_projection_batches b
            JOIN gbrain_projection_batch_jobs bj ON bj.batch_id=b.id
            WHERE bj.job_id IN ({placeholders})
              AND b.status IN ('running','failed')
            ORDER BY b.created_at,b.id
            """,
            tuple(sorted(job_ids)),
        ).fetchall()
        batch_ids = {str(row["id"]) for row in rows}
        if not batch_ids:
            return None
        if len(batch_ids) != 1:
            raise ProjectionProtocolError("claimed jobs belong to multiple resumable GBrain batches")
        batch_id = next(iter(batch_ids))
        batch = dict(rows[0])
        member_rows = conn.execute(
            """
            SELECT bj.job_id,j.status,j.lease_owner
            FROM gbrain_projection_batch_jobs bj
            JOIN knowledge_projection_jobs j ON j.id=bj.job_id
            WHERE bj.batch_id=? ORDER BY bj.job_id
            """,
            (batch_id,),
        ).fetchall()
        nonterminal_member_ids = {
            str(row["job_id"])
            for row in member_rows
            if row["status"] not in TERMINAL_STATUSES
        }
        if nonterminal_member_ids != job_ids:
            raise ProjectionLeaseLost("batch members", batch_id)
        for row in member_rows:
            if str(row["job_id"]) not in job_ids:
                continue
            if row["status"] != "running" or row["lease_owner"] != worker_id:
                raise ProjectionLeaseLost("job", str(row["job_id"]))

        renewed = conn.execute(
            """
            UPDATE gbrain_projection_batches
            SET status='running',lease_owner=?,lease_expires_at=?,last_error=NULL,
                finished_at=NULL,updated_at=?
            WHERE id=? AND status IN ('running','failed')
            """,
            (worker_id, lease_expires_at, timestamp, batch_id),
        )
        if renewed.rowcount != 1:
            raise ProjectionLeaseLost("batch", batch_id)
        conn.execute(
            """
            UPDATE gbrain_projection_segments
            SET status=CASE WHEN status='succeeded' THEN status ELSE 'pending' END,
                lease_owner=?,lease_expires_at=?,last_error=NULL,
                started_at=CASE WHEN status='succeeded' THEN started_at ELSE NULL END,
                finished_at=CASE WHEN status='succeeded' THEN finished_at ELSE NULL END
            WHERE batch_id=?
            """,
            (worker_id, lease_expires_at, batch_id),
        )
        segments = conn.execute(
            """
            SELECT * FROM gbrain_projection_segments
            WHERE batch_id=? ORDER BY segment_index
            """,
            (batch_id,),
        ).fetchall()
        try:
            expected_values = json.loads(batch["included_snapshot_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProjectionProtocolError("stored GBrain batch snapshot is invalid") from exc
        if not isinstance(expected_values, dict):
            raise ProjectionProtocolError("stored GBrain batch snapshot must be an object")
        expected_pages = self._decode_expected_pages(expected_values.get("expected_pages"))
        return GBrainBatch(
            id=batch_id,
            mode=str(batch["mode"]),
            worker_id=worker_id,
            expected_pages=expected_pages,
            segments=tuple(
                GBrainSegment(
                    id=str(row["id"]),
                    batch_id=batch_id,
                    index=int(row["segment_index"]),
                    mode=str(row["mode"]),
                    expected_pages=self._decode_expected_pages(row["expected_pages_json"]),
                    idempotency_key=str(row["idempotency_key"]),
                )
                for row in segments
            ),
        )

    async def execute_batch(
        self,
        batch_id: str,
        *,
        worker_id: str,
        client: Any,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> GBrainBatchExecutionResult:
        if lease_seconds <= 0:
            raise ValueError("GBrain batch lease must be positive")
        instant = now or datetime.now(timezone.utc)
        timestamp = instant.isoformat()
        batch, snapshot, segment_rows = self._load_execution(batch_id, worker_id)
        source_id = (self.settings.gbrain_managed_source_id or "").strip()
        if not source_id:
            raise ProjectionConfigurationError("GBrain managed source is not configured")
        root = str((self.settings.vault_path / "wiki").expanduser().resolve())
        accumulated = self._dedupe_protections(
            [
                *self._snapshot_protections(snapshot),
                *self._load_active_protections(source_id),
            ]
        )
        completed = 0

        for segment_row in segment_rows:
            segment_id = str(segment_row["id"])
            if segment_row["status"] == "succeeded":
                accumulated = self._dedupe_protections(
                    [
                        *accumulated,
                        *self._decode_protections(segment_row["protected_mappings_json"]),
                    ]
                )
                completed += 1
                continue
            expected_pages = self._decode_expected_pages(segment_row["expected_pages_json"])
            self._renew_execution_leases(
                batch_id,
                segment_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                instant=instant,
                start_segment=True,
            )
            request_mode = str(segment_row["mode"])
            request_protections = tuple(accumulated)
            if request_mode == "reconcile":
                request_protections = self._reconcile_request_protections(
                    batch_id,
                    segment_id,
                    worker_id=worker_id,
                    source_id=source_id,
                    snapshot=snapshot,
                    protections=accumulated,
                )
            request = GBrainSyncRequest(
                source_id=source_id,
                root=root,
                mode=request_mode,
                expected_pages=expected_pages,
                protected_mappings=request_protections,
                no_embed=bool(self.settings.gbrain_import_no_embed),
                idempotency_key=str(segment_row["idempotency_key"]),
            )
            try:
                response = await client.sync(request)
                member_job_ids = self._renew_execution_leases(
                    batch_id,
                    segment_id,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                    instant=instant,
                    start_segment=False,
                )
                self._validate_result_hashes(request, response)
            except (ProjectionNetworkError, ProjectionProtocolError, ProjectionToolError) as exc:
                self._record_transport_failure(
                    batch_id,
                    segment_id,
                    worker_id=worker_id,
                    timestamp=timestamp,
                    error=str(exc),
                )
                raise

            response_protections = self._dedupe_protections(
                [
                    *response.protected_mappings,
                    *(
                        mapping
                        for page in response.pages
                        for mapping in page.protected_mappings
                    ),
                ]
            )
            page_response_protections = self._dedupe_protections(
                [
                    mapping
                    for page in response.pages
                    for mapping in page.protected_mappings
                ]
            )
            accumulated = self._dedupe_protections(
                [*accumulated, *response_protections]
            )
            request_keys = {
                self._protection_key(mapping)
                for mapping in request.protected_mappings
            }
            page_response_keys = {
                self._protection_key(mapping)
                for mapping in page_response_protections
            }
            segment_protections = tuple(
                mapping
                for mapping in response.protected_mappings
                if self._protection_key(mapping) not in request_keys
                and self._protection_key(mapping) not in page_response_keys
            )
            self._persist_segment_protections(
                batch_id,
                segment_id,
                worker_id=worker_id,
                protections=segment_protections,
                member_job_ids=member_job_ids,
                timestamp=timestamp,
            )
            self._attribute_segment(
                batch_id,
                segment_id,
                worker_id=worker_id,
                snapshot=snapshot,
                request=request,
                response=response,
                timestamp=timestamp,
            )
            self._finish_segment(
                batch_id,
                segment_id,
                worker_id=worker_id,
                response=response,
                protections=accumulated,
                timestamp=timestamp,
            )
            completed += 1

        self._finish_remaining_jobs(batch_id, worker_id, timestamp)
        self._finish_batch(
            batch_id,
            worker_id=worker_id,
            timestamp=timestamp,
            result={"segments": completed},
        )
        return GBrainBatchExecutionResult(
            batch_id=batch_id,
            status="succeeded",
            segments=completed,
        )

    def _load_execution(
        self,
        batch_id: str,
        worker_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        with connect_app_write(self.settings) as conn:
            row = conn.execute(
                "SELECT * FROM gbrain_projection_batches WHERE id=?",
                (batch_id,),
            ).fetchone()
            if row is None or row["status"] != "running" or row["lease_owner"] != worker_id:
                raise ProjectionLeaseLost("batch", batch_id)
            segments = conn.execute(
                """
                SELECT * FROM gbrain_projection_segments
                WHERE batch_id=? ORDER BY segment_index
                """,
                (batch_id,),
            ).fetchall()
        batch = dict(row)
        try:
            snapshot = json.loads(batch["included_snapshot_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProjectionProtocolError("stored GBrain batch snapshot is invalid") from exc
        if not isinstance(snapshot, dict):
            raise ProjectionProtocolError("stored GBrain batch snapshot must be an object")
        return batch, snapshot, [dict(segment) for segment in segments]

    def _renew_execution_leases(
        self,
        batch_id: str,
        segment_id: str,
        *,
        worker_id: str,
        lease_seconds: int,
        instant: datetime,
        start_segment: bool,
    ) -> tuple[str, ...]:
        timestamp = instant.isoformat()
        lease_expires_at = (instant + timedelta(seconds=lease_seconds)).isoformat()
        with connect_app_write(self.settings) as conn:
            renewed_batch = conn.execute(
                """
                UPDATE gbrain_projection_batches
                SET lease_expires_at=?,updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?
                """,
                (lease_expires_at, timestamp, batch_id, worker_id),
            )
            if renewed_batch.rowcount != 1:
                raise ProjectionLeaseLost("batch", batch_id)
            segment_statuses = "('pending','running')" if start_segment else "('running')"
            renewed_segment = conn.execute(
                f"""
                UPDATE gbrain_projection_segments
                SET status='running',lease_expires_at=?,
                    started_at=COALESCE(started_at,?)
                WHERE id=? AND batch_id=? AND status IN {segment_statuses}
                  AND lease_owner=?
                """,
                (lease_expires_at, timestamp, segment_id, batch_id, worker_id),
            )
            if renewed_segment.rowcount != 1:
                raise ProjectionLeaseLost("segment", segment_id)
            members = conn.execute(
                """
                SELECT j.id,j.status,j.lease_owner
                FROM gbrain_projection_batch_jobs bj
                JOIN knowledge_projection_jobs j ON j.id=bj.job_id
                WHERE bj.batch_id=?
                ORDER BY j.id
                """,
                (batch_id,),
            ).fetchall()
            renewed_member_ids: list[str] = []
            for member in members:
                if member["status"] != "running":
                    continue
                renewed_job = conn.execute(
                    """
                    UPDATE knowledge_projection_jobs
                    SET lease_expires_at=?,updated_at=?
                    WHERE id=? AND status='running' AND lease_owner=?
                    """,
                    (lease_expires_at, timestamp, member["id"], worker_id),
                )
                if renewed_job.rowcount != 1:
                    raise ProjectionLeaseLost("job", str(member["id"]))
                renewed_member_ids.append(str(member["id"]))
        return tuple(renewed_member_ids)

    def _record_transport_failure(
        self,
        batch_id: str,
        segment_id: str,
        *,
        worker_id: str,
        timestamp: str,
        error: str,
    ) -> None:
        detail = error[:500]
        with connect_app_write(self.settings) as conn:
            segment = conn.execute(
                """
                UPDATE gbrain_projection_segments
                SET status='failed',last_error=?,finished_at=?
                WHERE id=? AND batch_id=? AND status='running' AND lease_owner=?
                """,
                (detail, timestamp, segment_id, batch_id, worker_id),
            )
            if segment.rowcount != 1:
                raise ProjectionLeaseLost("segment", segment_id)
            batch = conn.execute(
                """
                UPDATE gbrain_projection_batches
                SET status='failed',last_error=?,finished_at=?,updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?
                """,
                (detail, timestamp, timestamp, batch_id, worker_id),
            )
            if batch.rowcount != 1:
                raise ProjectionLeaseLost("batch", batch_id)

    @staticmethod
    def _validate_result_hashes(
        request: GBrainSyncRequest,
        response: GBrainSyncResponse,
    ) -> None:
        GBrainProjectionClient._validate_response(request, response)
        expected_by_id = {page.page_id: page for page in request.expected_pages}
        for actual in response.pages:
            expected = expected_by_id[actual.page_id]
            if actual.status in {"imported", "skipped"} and (
                actual.raw_file_hash_before != expected.file_hash
                or actual.raw_file_hash_after != expected.file_hash
            ):
                raise ProjectionProtocolError(
                    f"GBrain page result hash mismatch for {actual.page_id}"
                )

    def _persist_segment_protections(
        self,
        batch_id: str,
        segment_id: str,
        *,
        worker_id: str,
        protections: Sequence[ProtectedMapping],
        member_job_ids: Sequence[str],
        timestamp: str,
    ) -> None:
        if not protections:
            return
        with connect_app_write(self.settings) as conn:
            self._require_persistence_owners(
                conn,
                batch_id,
                segment_id,
                worker_id,
                member_job_ids,
            )
            self._persist_protections(conn, protections, timestamp)
            self._require_persistence_owners(
                conn,
                batch_id,
                segment_id,
                worker_id,
                member_job_ids,
            )

    def _finish_segment(
        self,
        batch_id: str,
        segment_id: str,
        *,
        worker_id: str,
        response: GBrainSyncResponse,
        protections: Sequence[ProtectedMapping],
        timestamp: str,
    ) -> None:
        with connect_app_write(self.settings) as conn:
            self._require_batch_segment(conn, batch_id, segment_id, worker_id)
            updated = conn.execute(
                """
                UPDATE gbrain_projection_segments
                SET status='succeeded',result_json=?,protected_mappings_json=?,
                    last_error=NULL,finished_at=?
                WHERE id=? AND batch_id=? AND status='running' AND lease_owner=?
                """,
                (
                    json_dump(asdict(response)),
                    json_dump([asdict(mapping) for mapping in protections]),
                    timestamp,
                    segment_id,
                    batch_id,
                    worker_id,
                ),
            )
            if updated.rowcount != 1:
                raise ProjectionLeaseLost("segment", segment_id)

    def _finish_batch(
        self,
        batch_id: str,
        *,
        worker_id: str,
        timestamp: str,
        result: Mapping[str, Any],
    ) -> None:
        with connect_app_write(self.settings) as conn:
            updated = conn.execute(
                """
                UPDATE gbrain_projection_batches
                SET status='succeeded',result_json=?,last_error=NULL,
                    finished_at=?,updated_at=?
                WHERE id=? AND status='running' AND lease_owner=?
                """,
                (json_dump(dict(result)), timestamp, timestamp, batch_id, worker_id),
            )
            if updated.rowcount != 1:
                raise ProjectionLeaseLost("batch", batch_id)

    def _require_batch_segment(
        self,
        conn: Any,
        batch_id: str,
        segment_id: str,
        worker_id: str,
    ) -> None:
        lock_clause = (
            " FOR UPDATE" if self.settings.database_backend == "postgres" else ""
        )
        batch = conn.execute(
            f"""
            SELECT 1 FROM gbrain_projection_batches
            WHERE id=? AND status='running' AND lease_owner=?
            {lock_clause}
            """,
            (batch_id, worker_id),
        ).fetchone()
        if batch is None:
            raise ProjectionLeaseLost("batch", batch_id)
        segment = conn.execute(
            f"""
            SELECT 1 FROM gbrain_projection_segments
            WHERE id=? AND batch_id=? AND status='running' AND lease_owner=?
            {lock_clause}
            """,
            (segment_id, batch_id, worker_id),
        ).fetchone()
        if segment is None:
            raise ProjectionLeaseLost("segment", segment_id)

    def _require_persistence_owners(
        self,
        conn: Any,
        batch_id: str,
        segment_id: str,
        worker_id: str,
        member_job_ids: Sequence[str],
    ) -> None:
        self._require_batch_segment(conn, batch_id, segment_id, worker_id)
        if not member_job_ids:
            return
        rows = conn.execute(
            """
            SELECT j.* FROM gbrain_projection_batch_jobs bj
            JOIN knowledge_projection_jobs j ON j.id=bj.job_id
            WHERE bj.batch_id=?
            ORDER BY j.id
            """,
            (batch_id,),
        ).fetchall()
        members = {
            str(row["id"]): ProjectionJob.from_row(dict(row))
            for row in rows
            if str(row["id"]) in member_job_ids
        }
        for job_id in member_job_ids:
            job = members.get(job_id)
            if job is None:
                raise ProjectionLeaseLost("job", job_id)
            self._require_job_owners((job,), worker_id)

    def _load_active_protections(self, source_id: str) -> tuple[ProtectedMapping, ...]:
        with connect_app_write(self.settings) as conn:
            return self._active_protections(conn, source_id)

    def _reconcile_request_protections(
        self,
        batch_id: str,
        segment_id: str,
        *,
        worker_id: str,
        source_id: str,
        snapshot: Mapping[str, Any],
        protections: Sequence[ProtectedMapping],
    ) -> tuple[ProtectedMapping, ...]:
        raw_jobs = snapshot.get("jobs")
        raw_mappings = snapshot.get("mappings")
        with connect_app_write(self.settings) as conn:
            self._require_batch_segment(conn, batch_id, segment_id, worker_id)
            protection_rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id,gbrain_source_id,page_id,slug,source_path,reason
                    FROM gbrain_projection_protections
                    WHERE gbrain_source_id=? AND active=1
                    ORDER BY created_at,id
                    """,
                    (source_id,),
                ).fetchall()
            ]
            active = self._dedupe_protections(
                tuple(
                    ProtectedMapping(
                        source_id=str(row["gbrain_source_id"]),
                        slug=row.get("slug"),
                        source_path=row.get("source_path"),
                        reason=str(row["reason"]),
                    )
                    for row in protection_rows
                )
            )
            active_keys = {
                (mapping.source_id, mapping.slug, mapping.source_path, mapping.reason)
                for mapping in active
            }
            active = self._dedupe_protections(
                (
                    *(
                        mapping
                        for mapping in protections
                        if (
                            mapping.source_id,
                            mapping.slug,
                            mapping.source_path,
                            mapping.reason,
                        )
                        in active_keys
                    ),
                    *active,
                )
            )
            if not isinstance(raw_jobs, list) or not isinstance(raw_mappings, list):
                return active

            snapshot_jobs = [item for item in raw_jobs if isinstance(item, dict)]
            snapshot_mappings = [item for item in raw_mappings if isinstance(item, dict)]
            current_rows = conn.execute(
                """
                SELECT j.* FROM gbrain_projection_batch_jobs bj
                JOIN knowledge_projection_jobs j ON j.id=bj.job_id
                WHERE bj.batch_id=? AND bj.operation='delete'
                ORDER BY j.created_at,j.id
                """,
                (batch_id,),
            ).fetchall()
            current_jobs = [ProjectionJob.from_row(dict(row)) for row in current_rows]
            authoritative: dict[str, list[dict[str, Any]]] = {}
            page_ids = {
                str(row["page_id"])
                for row in protection_rows
                if row.get("page_id") is not None
            }
            for page_id in page_ids:
                desired = [
                    item
                    for item in snapshot_jobs
                    if item.get("operation") == "delete"
                    and str(item.get("page_id")) == page_id
                ]
                owners = [job for job in current_jobs if str(job.page_id) == page_id]
                if len(desired) != 1 or len(owners) != 1:
                    continue
                owner = owners[0]
                if (
                    not self._job_matches_snapshot(owner, desired[0])
                    or owner.status != "running"
                    or owner.lease_owner != worker_id
                    or not self._page_matches_delete_job(
                        self._page_snapshot(conn, page_id), owner
                    )
                ):
                    continue
                mappings = [
                    item
                    for item in snapshot_mappings
                    if str(item.get("page_id")) == page_id
                    and item.get("gbrain_source_id") == source_id
                ]
                if mappings:
                    authoritative[page_id] = mappings

            grouped: dict[
                tuple[str, str | None, str | None, str], list[dict[str, Any]]
            ] = {}
            for row in protection_rows:
                key = (
                    str(row["gbrain_source_id"]),
                    row.get("slug"),
                    row.get("source_path"),
                    str(row["reason"]),
                )
                grouped.setdefault(key, []).append(row)
            releasable: set[tuple[str, str | None, str | None, str]] = set()
            for key, rows in grouped.items():
                matches: list[bool] = []
                for row in rows:
                    page_id = row.get("page_id")
                    candidates = authoritative.get(str(page_id), []) if page_id is not None else []
                    matched = [
                        mapping
                        for mapping in candidates
                        if self._protection_matches_mapping(row, mapping)
                        and self._mapping_matches_snapshot(conn, mapping)
                    ]
                    matches.append(len(matched) == 1)
                if matches and all(matches):
                    releasable.add(key)
            return tuple(
                mapping
                for mapping in active
                if (
                    mapping.source_id,
                    mapping.slug,
                    mapping.source_path,
                    mapping.reason,
                )
                not in releasable
            )

    @staticmethod
    def _snapshot_protections(snapshot: Mapping[str, Any]) -> tuple[ProtectedMapping, ...]:
        return GBrainBatchRepository._decode_protections(
            snapshot.get("protected_mappings", [])
        )

    @staticmethod
    def _decode_expected_pages(value: Any) -> tuple[ExpectedPage, ...]:
        raw = json.loads(value) if isinstance(value, str) else value
        if not isinstance(raw, list):
            raise ProjectionProtocolError("stored segment expected pages must be an array")
        try:
            return tuple(ExpectedPage(**item) for item in raw)
        except (TypeError, KeyError) as exc:
            raise ProjectionProtocolError("stored segment expected page is invalid") from exc

    @staticmethod
    def _decode_protections(value: Any) -> tuple[ProtectedMapping, ...]:
        raw = json.loads(value) if isinstance(value, str) else value
        if not isinstance(raw, list):
            raise ProjectionProtocolError("stored protections must be an array")
        try:
            return tuple(ProtectedMapping(**item) for item in raw)
        except (TypeError, KeyError) as exc:
            raise ProjectionProtocolError("stored protection is invalid") from exc

    @staticmethod
    def _dedupe_protections(
        protections: Sequence[ProtectedMapping],
    ) -> tuple[ProtectedMapping, ...]:
        unique: dict[tuple[str, str | None, str | None, str], ProtectedMapping] = {}
        for mapping in protections:
            key = GBrainBatchRepository._protection_key(mapping)
            unique.setdefault(key, mapping)
        return tuple(unique.values())

    @staticmethod
    def _protection_key(
        mapping: ProtectedMapping,
    ) -> tuple[str, str | None, str | None, str]:
        return (mapping.source_id, mapping.slug, mapping.source_path, mapping.reason)

    @staticmethod
    def _persist_protections(
        conn: Any,
        protections: Sequence[ProtectedMapping],
        timestamp: str,
        page_id: str | None = None,
    ) -> None:
        for mapping in protections:
            existing = conn.execute(
                """
                SELECT id FROM gbrain_projection_protections
                WHERE gbrain_source_id=? AND COALESCE(slug,'')=COALESCE(?, '')
                  AND COALESCE(source_path,'')=COALESCE(?, '')
                  AND reason=? AND active=1
                  AND (
                    page_id=?
                    OR (page_id IS NULL AND CAST(? AS TEXT) IS NULL)
                  )
                """,
                (
                    mapping.source_id,
                    mapping.slug,
                    mapping.source_path,
                    mapping.reason,
                    page_id,
                    page_id,
                ),
            ).fetchone()
            if existing is not None:
                continue
            conn.execute(
                """
                INSERT INTO gbrain_projection_protections(
                  id,gbrain_source_id,page_id,slug,source_path,reason,
                  active,created_at,resolved_at)
                VALUES (?,?,?,?,?,?,1,?,NULL)
                """,
                (
                    f"gbprotect_{uuid.uuid4().hex}",
                    mapping.source_id,
                    page_id,
                    mapping.slug,
                    mapping.source_path,
                    mapping.reason,
                    timestamp,
                ),
            )

    def _attribute_segment(
        self,
        batch_id: str,
        segment_id: str,
        *,
        worker_id: str,
        snapshot: Mapping[str, Any],
        request: GBrainSyncRequest,
        response: GBrainSyncResponse,
        timestamp: str,
    ) -> None:
        expected_by_id = {page.page_id: page for page in request.expected_pages}
        for result in response.pages:
            expected = expected_by_id[result.page_id]
            if result.status in {"imported", "skipped"}:
                self._attribute_page_success(
                    batch_id,
                    segment_id,
                    worker_id=worker_id,
                    expected=expected,
                    result=result,
                    timestamp=timestamp,
                )
            else:
                self._attribute_page_failure(
                    batch_id,
                    segment_id,
                    worker_id=worker_id,
                    expected=expected,
                    result=result,
                    timestamp=timestamp,
                )
        if request.mode == "reconcile":
            self._attribute_deletions(
                batch_id,
                segment_id,
                worker_id=worker_id,
                snapshot=snapshot,
                deleted=response.deleted,
                timestamp=timestamp,
            )

    def _finish_remaining_jobs(
        self,
        batch_id: str,
        worker_id: str,
        timestamp: str,
    ) -> None:
        with connect_app_write(self.settings) as conn:
            batch = conn.execute(
                """
                SELECT 1 FROM gbrain_projection_batches
                WHERE id=? AND status='running' AND lease_owner=?
                """,
                (batch_id, worker_id),
            ).fetchone()
            if batch is None:
                raise ProjectionLeaseLost("batch", batch_id)
            rows = conn.execute(
                """
                SELECT j.* FROM gbrain_projection_batch_jobs bj
                JOIN knowledge_projection_jobs j ON j.id=bj.job_id
                WHERE bj.batch_id=? AND j.status='running'
                ORDER BY j.id
                """,
                (batch_id,),
            ).fetchall()
            for row in rows:
                job = ProjectionJob.from_row(dict(row))
                if job.lease_owner != worker_id:
                    raise ProjectionLeaseLost("job", job.id)
                if job.operation == "reconcile":
                    status: Literal["succeeded", "failed"] = "succeeded"
                    error = None
                else:
                    status = "failed"
                    error = f"GBrain {job.operation} result was not uniquely attributable"
                if not self.outbox.finish_claimed(
                    conn,
                    job_id=job.id,
                    worker_id=worker_id,
                    status=status,
                    last_error=error,
                ):
                    raise ProjectionLeaseLost("job", job.id)

    def _attribute_page_success(
        self,
        batch_id: str,
        segment_id: str,
        *,
        worker_id: str,
        expected: ExpectedPage,
        result: GBrainPageSyncResult,
        timestamp: str,
    ) -> None:
        source_id = (self.settings.gbrain_managed_source_id or "").strip()
        with connect_app_write(self.settings) as conn:
            self._require_batch_segment(conn, batch_id, segment_id, worker_id)
            jobs = self._page_member_jobs(conn, batch_id, expected.page_id)
            self._require_job_owners(jobs, worker_id)
            self._persist_protections(
                conn,
                result.protected_mappings,
                timestamp,
                expected.page_id,
            )
            page = self._page_snapshot(
                conn,
                expected.page_id,
                for_update=True,
            )
            if not self._page_matches_expected(page, expected):
                self._supersede_jobs(conn, jobs, worker_id, page)
                self._require_batch_segment(conn, batch_id, segment_id, worker_id)
                return
            if not result.slug or not result.content_hash or result.page_generation is None:
                self._fail_page_attribution(
                    conn,
                    jobs,
                    worker_id,
                    expected,
                    result,
                    timestamp,
                    "GBrain page result omitted mapping identity",
                )
                self._require_batch_segment(conn, batch_id, segment_id, worker_id)
                return
            occupying = conn.execute(
                """
                SELECT * FROM gbrain_page_projections
                WHERE gbrain_source_id=? AND slug=? AND status<>'deleted'
                ORDER BY id
                """,
                (source_id, result.slug),
            ).fetchall()
            if any(str(row["page_id"]) != expected.page_id for row in occupying):
                self._fail_page_attribution(
                    conn,
                    jobs,
                    worker_id,
                    expected,
                    result,
                    timestamp,
                    "ambiguous GBrain slug attribution",
                )
                self._require_batch_segment(conn, batch_id, segment_id, worker_id)
                return
            desired_states = {
                (job.page_id, job.revision_id, job.projection_epoch)
                for job in jobs
            }
            expected_state = (
                expected.page_id, expected.revision_id, expected.projection_epoch
            )
            if len(desired_states) > 1 or (desired_states and expected_state not in desired_states):
                self._fail_page_attribution(
                    conn,
                    jobs,
                    worker_id,
                    expected,
                    result,
                    timestamp,
                    "ambiguous GBrain page job attribution",
                )
                self._require_batch_segment(conn, batch_id, segment_id, worker_id)
                return

            conn.execute(
                """
                UPDATE gbrain_page_projections
                SET status='stale',invalidated_at=?
                WHERE gbrain_source_id=? AND page_id=? AND status='current' AND slug<>?
                """,
                (timestamp, source_id, expected.page_id, result.slug),
            )
            existing = conn.execute(
                """
                SELECT id FROM gbrain_page_projections
                WHERE gbrain_source_id=? AND slug=?
                """,
                (source_id, result.slug),
            ).fetchone()
            mapping_id = str(existing["id"]) if existing is not None else f"gproj_{uuid.uuid4().hex}"
            last_job_id = jobs[-1].id if jobs else batch_id
            conn.execute(
                """
                INSERT INTO gbrain_page_projections(
                  id,page_id,revision_id,projection_epoch,page_path,file_hash,
                  semantic_hash,gbrain_source_id,slug,source_path,
                  gbrain_content_hash,gbrain_page_generation,status,imported_at,
                  invalidated_at,last_job_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'current',?,NULL,?)
                ON CONFLICT(gbrain_source_id,slug) DO UPDATE SET
                  page_id=excluded.page_id,
                  revision_id=excluded.revision_id,
                  projection_epoch=excluded.projection_epoch,
                  page_path=excluded.page_path,
                  file_hash=excluded.file_hash,
                  semantic_hash=excluded.semantic_hash,
                  source_path=excluded.source_path,
                  gbrain_content_hash=excluded.gbrain_content_hash,
                  gbrain_page_generation=excluded.gbrain_page_generation,
                  status='current',
                  imported_at=excluded.imported_at,
                  invalidated_at=NULL,
                  last_job_id=excluded.last_job_id
                """,
                (
                    mapping_id,
                    expected.page_id,
                    expected.revision_id,
                    expected.projection_epoch,
                    str(page["path"]),
                    expected.file_hash,
                    str(page["revision_semantic_hash"]),
                    source_id,
                    result.slug,
                    result.source_path,
                    result.content_hash,
                    result.page_generation,
                    timestamp,
                    last_job_id,
                ),
            )
            bump_gbrain_projection_generation(conn, timestamp)
            for job in jobs:
                if not self.outbox.finish_claimed(
                    conn,
                    job_id=job.id,
                    worker_id=worker_id,
                    status="succeeded",
                ):
                    raise ProjectionLeaseLost("job", job.id)
            conn.execute(
                """
                UPDATE gbrain_projection_protections
                SET active=0,resolved_at=?
                WHERE gbrain_source_id=? AND page_id=? AND active=1
                  AND (slug=? OR source_path=?)
                """,
                (
                    timestamp,
                    source_id,
                    expected.page_id,
                    result.slug,
                    result.source_path,
                ),
            )
            self._require_batch_segment(conn, batch_id, segment_id, worker_id)

    def _attribute_page_failure(
        self,
        batch_id: str,
        segment_id: str,
        *,
        worker_id: str,
        expected: ExpectedPage,
        result: GBrainPageSyncResult,
        timestamp: str,
    ) -> None:
        with connect_app_write(self.settings) as conn:
            self._require_batch_segment(conn, batch_id, segment_id, worker_id)
            jobs = self._page_member_jobs(conn, batch_id, expected.page_id)
            self._require_job_owners(jobs, worker_id)
            protections = tuple(result.protected_mappings) or (
                ProtectedMapping(
                    source_id=result.source_id,
                    slug=result.slug,
                    source_path=result.source_path,
                    reason=result.error or result.status,
                ),
            )
            self._persist_protections(conn, protections, timestamp, expected.page_id)
            page = self._page_snapshot(
                conn,
                expected.page_id,
                for_update=True,
            )
            stale_expected = not self._page_matches_expected(page, expected)
            for job in jobs:
                status: Literal["failed", "superseded"] = (
                    "superseded"
                    if stale_expected or result.status == "superseded"
                    else "failed"
                )
                if not self.outbox.finish_claimed(
                    conn,
                    job_id=job.id,
                    worker_id=worker_id,
                    status=status,
                    last_error=result.error or f"GBrain page result: {result.status}",
                ):
                    raise ProjectionLeaseLost("job", job.id)
            if stale_expected or result.status == "superseded":
                self._enqueue_current_page(conn, page)
            self._require_batch_segment(conn, batch_id, segment_id, worker_id)

    def _fail_page_attribution(
        self,
        conn: Any,
        jobs: Sequence[ProjectionJob],
        worker_id: str,
        expected: ExpectedPage,
        result: GBrainPageSyncResult,
        timestamp: str,
        reason: str,
    ) -> None:
        protection = ProtectedMapping(
            source_id=result.source_id,
            slug=result.slug,
            source_path=result.source_path,
            reason=reason,
        )
        self._persist_protections(conn, (protection,), timestamp, expected.page_id)
        for job in jobs:
            if not self.outbox.finish_claimed(
                conn,
                job_id=job.id,
                worker_id=worker_id,
                status="failed",
                last_error=reason,
            ):
                raise ProjectionLeaseLost("job", job.id)

    def _attribute_deletions(
        self,
        batch_id: str,
        segment_id: str,
        *,
        worker_id: str,
        snapshot: Mapping[str, Any],
        deleted: Sequence[GBrainDeletedResult],
        timestamp: str,
    ) -> None:
        mapping_snapshot = snapshot.get("mappings", [])
        job_snapshot = snapshot.get("jobs", [])
        mappings = mapping_snapshot if isinstance(mapping_snapshot, list) else []
        for deletion in deleted:
            candidates = [
                mapping
                for mapping in mappings
                if isinstance(mapping, dict)
                and mapping.get("gbrain_source_id") == deletion.source_id
                and mapping.get("slug") == deletion.slug
            ]
            page_ids = {
                str(mapping["page_id"])
                for mapping in candidates
                if mapping.get("page_id") is not None
            }
            with connect_app_write(self.settings) as conn:
                self._require_batch_segment(conn, batch_id, segment_id, worker_id)
                owners = self._delete_member_jobs(conn, batch_id, page_ids)
                self._require_job_owners(owners, worker_id)
                self._require_job_snapshots(owners, job_snapshot)
                if not candidates:
                    continue
                if len(candidates) == 1 and len(owners) <= 1:
                    mapping = candidates[0]
                    page = self._page_snapshot(
                        conn,
                        mapping.get("page_id"),
                        for_update=True,
                    )
                    if owners and not self._page_matches_delete_job(page, owners[0]):
                        self._supersede_jobs(
                            conn,
                            owners,
                            worker_id,
                            page,
                        )
                        continue
                    if (
                        not owners
                        and mapping.get("page_id") is not None
                        and not self._page_matches_deleted_mapping(page, mapping)
                    ):
                        self._enqueue_current_page(conn, page)
                        continue
                    self._set_mapping_status_from_snapshot(
                        conn,
                        mapping,
                        status="deleted",
                        timestamp=timestamp,
                    )
                    bump_gbrain_projection_generation(conn, timestamp)
                    if owners:
                        owner = owners[0]
                        if not self.outbox.finish_claimed(
                            conn,
                            job_id=owner.id,
                            worker_id=worker_id,
                            status="succeeded",
                        ):
                            raise ProjectionLeaseLost("job", owner.id)
                        conn.execute(
                            """
                            UPDATE gbrain_projection_protections
                            SET active=0,resolved_at=?
                            WHERE gbrain_source_id=? AND page_id=? AND active=1
                              AND (slug=? OR source_path=?)
                            """,
                            (timestamp, deletion.source_id, owner.page_id, deletion.slug, mapping.get("source_path")),
                        )
                    continue

                for mapping in candidates:
                    self._set_mapping_status_from_snapshot(
                        conn,
                        mapping,
                        status="stale",
                        timestamp=timestamp,
                    )
                bump_gbrain_projection_generation(conn, timestamp)
                first = candidates[0]
                reason = "ambiguous GBrain delete attribution"
                self._persist_protections(
                    conn,
                    (
                        ProtectedMapping(
                            source_id=deletion.source_id,
                            slug=deletion.slug,
                            source_path=first.get("source_path"),
                            reason=reason,
                        ),
                    ),
                    timestamp,
                    first.get("page_id"),
                )
                for owner in owners:
                    if not self.outbox.finish_claimed(
                        conn,
                        job_id=owner.id,
                        worker_id=worker_id,
                        status="failed",
                        last_error=reason,
                    ):
                        raise ProjectionLeaseLost("job", owner.id)

    @staticmethod
    def _set_mapping_status_from_snapshot(
        conn: Any,
        mapping: Mapping[str, Any],
        *,
        status: Literal["deleted", "stale"],
        timestamp: str,
    ) -> None:
        updated = conn.execute(
            """
            UPDATE gbrain_page_projections
            SET status=?,invalidated_at=?
            WHERE id=? AND page_id=? AND revision_id=? AND projection_epoch=?
              AND page_path=? AND file_hash=? AND semantic_hash=?
              AND gbrain_source_id=? AND slug=? AND source_path=?
              AND gbrain_content_hash=? AND gbrain_page_generation=?
              AND status=?
              AND (
                imported_at=?
                OR (imported_at IS NULL AND CAST(? AS TEXT) IS NULL)
              )
              AND (
                invalidated_at=?
                OR (invalidated_at IS NULL AND CAST(? AS TEXT) IS NULL)
              )
              AND last_job_id=?
            """,
            (
                status,
                timestamp,
                mapping["id"],
                mapping["page_id"],
                mapping["revision_id"],
                mapping["projection_epoch"],
                mapping["page_path"],
                mapping["file_hash"],
                mapping["semantic_hash"],
                mapping["gbrain_source_id"],
                mapping["slug"],
                mapping["source_path"],
                mapping["gbrain_content_hash"],
                mapping["gbrain_page_generation"],
                mapping["status"],
                mapping.get("imported_at"),
                mapping.get("imported_at"),
                mapping.get("invalidated_at"),
                mapping.get("invalidated_at"),
                mapping["last_job_id"],
            ),
        )
        if updated.rowcount != 1:
            raise ProjectionLeaseLost("mapping snapshot", str(mapping["id"]))

    @staticmethod
    def _page_member_jobs(
        conn: Any,
        batch_id: str,
        page_id: str,
    ) -> list[ProjectionJob]:
        rows = conn.execute(
            """
            SELECT j.* FROM gbrain_projection_batch_jobs bj
            JOIN knowledge_projection_jobs j ON j.id=bj.job_id
            WHERE bj.batch_id=? AND bj.page_id=?
              AND bj.operation IN ('upsert','rename','restore')
            ORDER BY j.created_at,j.id
            """,
            (batch_id, page_id),
        ).fetchall()
        return [ProjectionJob.from_row(dict(row)) for row in rows]

    @staticmethod
    def _delete_member_jobs(
        conn: Any,
        batch_id: str,
        page_ids: set[str],
    ) -> list[ProjectionJob]:
        if not page_ids:
            return []
        rows = conn.execute(
            """
            SELECT j.* FROM gbrain_projection_batch_jobs bj
            JOIN knowledge_projection_jobs j ON j.id=bj.job_id
            WHERE bj.batch_id=? AND bj.operation='delete'
            ORDER BY j.id
            """,
            (batch_id,),
        ).fetchall()
        return [
            ProjectionJob.from_row(dict(row))
            for row in rows
            if str(row["page_id"]) in page_ids
        ]

    @staticmethod
    def _job_matches_snapshot(job: ProjectionJob, snapshot: Mapping[str, Any]) -> bool:
        return bool(
            snapshot.get("id") == job.id
            and snapshot.get("page_id") == job.page_id
            and snapshot.get("revision_id") == job.revision_id
            and int(snapshot.get("projection_epoch") or 0) == job.projection_epoch
            and snapshot.get("operation") == job.operation
        )

    @classmethod
    def _require_job_snapshots(
        cls,
        jobs: Sequence[ProjectionJob],
        snapshots: Any,
    ) -> None:
        raw = snapshots if isinstance(snapshots, list) else []
        for job in jobs:
            matches = [
                item
                for item in raw
                if isinstance(item, dict) and cls._job_matches_snapshot(job, item)
            ]
            if len(matches) != 1:
                raise ProjectionLeaseLost("job snapshot", job.id)

    @staticmethod
    def _page_matches_delete_job(
        page: Mapping[str, Any] | None,
        job: ProjectionJob,
    ) -> bool:
        return bool(
            page
            and page.get("lifecycle_status") in {"deleted", "invalid"}
            and int(page.get("projection_epoch") or 0) == job.projection_epoch
            and (
                job.revision_id is None
                or page.get("current_revision_id") == job.revision_id
            )
        )

    @staticmethod
    def _page_matches_deleted_mapping(
        page: Mapping[str, Any] | None,
        mapping: Mapping[str, Any],
    ) -> bool:
        del mapping
        return bool(page and page.get("lifecycle_status") in {"deleted", "invalid"})

    @staticmethod
    def _protection_matches_mapping(
        protection: Mapping[str, Any],
        mapping: Mapping[str, Any],
    ) -> bool:
        slug = protection.get("slug")
        source_path = protection.get("source_path")
        return bool(
            (slug is not None or source_path is not None)
            and (slug is None or slug == mapping.get("slug"))
            and (source_path is None or source_path == mapping.get("source_path"))
        )

    @staticmethod
    def _mapping_matches_snapshot(conn: Any, snapshot: Mapping[str, Any]) -> bool:
        mapping_id = snapshot.get("id")
        if mapping_id is None:
            return False
        row = conn.execute(
            "SELECT * FROM gbrain_page_projections WHERE id=?",
            (mapping_id,),
        ).fetchone()
        if row is None:
            return False
        current = dict(row)
        return all(current.get(key) == value for key, value in snapshot.items())

    @staticmethod
    def _require_job_owners(jobs: Sequence[ProjectionJob], worker_id: str) -> None:
        for job in jobs:
            if job.status != "running" or job.lease_owner != worker_id:
                raise ProjectionLeaseLost("job", job.id)

    def _supersede_jobs(
        self,
        conn: Any,
        jobs: Sequence[ProjectionJob],
        worker_id: str,
        page: Mapping[str, Any] | None,
    ) -> None:
        for job in jobs:
            if not self.outbox.finish_claimed(
                conn,
                job_id=job.id,
                worker_id=worker_id,
                status="superseded",
                last_error="desired revision or epoch changed before attribution",
            ):
                raise ProjectionLeaseLost("job", job.id)
        self._enqueue_current_page(conn, page)

    @staticmethod
    def _page_matches_expected(
        page: Mapping[str, Any] | None,
        expected: ExpectedPage,
    ) -> bool:
        return bool(
            page
            and page.get("lifecycle_status") == "active"
            and page.get("current_revision_id") == expected.revision_id
            and int(page.get("projection_epoch") or 0) == expected.projection_epoch
            and page.get("revision_file_hash") == expected.file_hash
            and page.get("revision_semantic_hash")
        )

    def _page_snapshot(
        self,
        conn: Any,
        page_id: str | None,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        if page_id is None:
            return None
        lock_clause = (
            " FOR UPDATE OF p"
            if for_update and self.settings.database_backend == "postgres"
            else ""
        )
        row = conn.execute(
            f"""
            SELECT p.*,r.file_hash AS revision_file_hash,
                   r.semantic_hash AS revision_semantic_hash
            FROM wiki_pages p
            LEFT JOIN wiki_page_revisions r ON r.id=p.current_revision_id
            WHERE p.page_id=?
            {lock_clause}
            """,
            (page_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _page_matches_job(page: Mapping[str, Any] | None, job: ProjectionJob) -> bool:
        return bool(
            page
            and page.get("lifecycle_status") == "active"
            and page.get("current_revision_id") == job.revision_id
            and int(page.get("projection_epoch") or 0) == job.projection_epoch
            and page.get("revision_file_hash")
        )

    def _enqueue_current_page(self, conn: Any, page: Mapping[str, Any] | None) -> None:
        if not page or page.get("lifecycle_status") != "active" or not page.get("current_revision_id"):
            return
        self.outbox.enqueue(
            conn,
            target="gbrain",
            operation="upsert",
            page_id=str(page["page_id"]),
            revision_id=str(page["current_revision_id"]),
            projection_epoch=int(page.get("projection_epoch") or 0),
            payload={"path": str(page["path"])},
        )

    def _expected_for_job(self, conn: Any, job: ProjectionJob) -> ExpectedPage | None:
        page = self._page_snapshot(conn, job.page_id, for_update=True)
        if not self._page_matches_job(page, job):
            return None
        return self._expected_from_page(page)

    def _active_expected_pages(self, conn: Any) -> tuple[ExpectedPage, ...]:
        rows = conn.execute(
            """
            SELECT p.*,r.file_hash AS revision_file_hash,
                   r.semantic_hash AS revision_semantic_hash
            FROM wiki_pages p
            JOIN wiki_page_revisions r ON r.id=p.current_revision_id
            WHERE p.lifecycle_status='active' AND p.current_revision_id IS NOT NULL
            ORDER BY p.page_id
            """
        ).fetchall()
        pages: list[ExpectedPage] = []
        for row in rows:
            page = dict(row)
            try:
                pages.append(self._expected_from_page(page))
            except ProjectionProtocolError:
                continue
        return tuple(pages)

    @staticmethod
    def _expected_from_page(page: Mapping[str, Any]) -> ExpectedPage:
        return ExpectedPage(
            page_id=str(page["page_id"]),
            revision_id=str(page["current_revision_id"]),
            projection_epoch=int(page.get("projection_epoch") or 0),
            path=to_gbrain_manifest_path(str(page["path"])),
            file_hash=str(page["revision_file_hash"]),
        )

    @staticmethod
    def _mapping_snapshot(conn: Any, source_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT * FROM gbrain_page_projections
            WHERE gbrain_source_id=? AND status<>'deleted'
            ORDER BY page_id,slug
            """,
            (source_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _active_protections(conn: Any, source_id: str) -> tuple[ProtectedMapping, ...]:
        rows = conn.execute(
            """
            SELECT gbrain_source_id,slug,source_path,reason
            FROM gbrain_projection_protections
            WHERE gbrain_source_id=? AND active=1
            ORDER BY created_at,id
            """,
            (source_id,),
        ).fetchall()
        return tuple(
            ProtectedMapping(
                source_id=str(row["gbrain_source_id"]),
                slug=row["slug"],
                source_path=row["source_path"],
                reason=str(row["reason"]),
            )
            for row in rows
        )

    @staticmethod
    def _job_snapshot(job: ProjectionJob) -> dict[str, Any]:
        return {
            "id": job.id,
            "page_id": job.page_id,
            "revision_id": job.revision_id,
            "projection_epoch": job.projection_epoch,
            "operation": job.operation,
        }


class GBrainProjectionClient:
    def __init__(self, settings: Settings):
        endpoint = (settings.gbrain_endpoint or "").strip()
        projection_token = (settings.gbrain_projection_api_key or "").strip()
        if not endpoint or not projection_token:
            raise ProjectionConfigurationError("GBrain projection endpoint/token is not configured")
        try:
            parsed_endpoint = httpx.URL(endpoint)
        except (TypeError, ValueError) as exc:
            raise ProjectionConfigurationError("GBrain projection endpoint is invalid") from exc
        if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.host:
            raise ProjectionConfigurationError("GBrain projection endpoint must be an HTTP(S) URL")

        self.settings = settings
        self.endpoint = endpoint
        self.projection_token = projection_token
        self.root = (settings.vault_path / "wiki").expanduser().resolve()
        if settings.gbrain_import_allowed_root is not None:
            allowed_root = settings.gbrain_import_allowed_root.expanduser().resolve()
            if allowed_root != self.root:
                raise ProjectionConfigurationError("GBrain projection root does not match the configured allowed root")

    async def sync(self, request: GBrainSyncRequest) -> GBrainSyncResponse:
        self._validate_request(request)
        timeout_seconds = (
            self.settings.gbrain_reconcile_timeout_seconds
            if request.mode == "reconcile"
            else self.settings.gbrain_incremental_timeout_seconds
        )
        if timeout_seconds <= 0:
            raise ProjectionConfigurationError("GBrain projection timeout must be positive")

        async with httpx.AsyncClient(timeout=httpx.Timeout(float(timeout_seconds))) as client:
            await self._initialize(client)
            payload = await self._call(client, "lgdo_vault_sync", request.to_payload())
        try:
            response = GBrainSyncResponse.from_payload(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectionProtocolError(f"invalid lgdo_vault_sync result: {exc}") from exc
        self._validate_response(request, response)
        return response

    async def _initialize(self, client: httpx.AsyncClient) -> None:
        envelope = await self._post_envelope(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "lgdo-projection", "version": "1"},
                },
            },
            expected_request_id=1,
        )
        self._raise_envelope_error(envelope, "initialize")
        if not isinstance(envelope.get("result"), Mapping):
            raise ProjectionProtocolError("MCP initialize response omitted a result object")

    async def _call(
        self,
        client: httpx.AsyncClient,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        envelope = await self._post_envelope(
            client,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
            expected_request_id=2,
        )
        self._raise_envelope_error(envelope, tool_name)
        result = envelope.get("result")
        if not isinstance(result, Mapping):
            raise ProjectionProtocolError(f"MCP tool {tool_name} response omitted a result object")
        is_error = result.get("isError", False)
        if not isinstance(is_error, bool):
            raise ProjectionProtocolError(f"MCP tool {tool_name} returned a non-boolean isError")
        text = _tool_text(result)
        if is_error:
            raise ProjectionToolError(text or f"MCP tool {tool_name} returned isError")

        structured = result.get("structuredContent")
        if structured is not None:
            if not isinstance(structured, Mapping):
                raise ProjectionProtocolError(f"MCP tool {tool_name} returned invalid structuredContent")
            return dict(structured)
        if not text:
            raise ProjectionProtocolError(f"MCP tool {tool_name} returned no JSON result content")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProjectionProtocolError(f"MCP tool {tool_name} returned malformed JSON content") from exc
        if not isinstance(payload, dict):
            raise ProjectionProtocolError(f"MCP tool {tool_name} result must be a JSON object")
        return payload

    async def _post_envelope(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
        *,
        expected_request_id: int,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.projection_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Mcp-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        try:
            response = await client.post(self.endpoint, json=payload, headers=headers)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ProjectionNetworkError("GBrain projection HTTP request timed out") from exc
        except httpx.HTTPError as exc:
            raise ProjectionNetworkError(f"GBrain projection HTTP request failed: {exc}") from exc

        content_type = response.headers.get("content-type", "").lower()
        if "text/event-stream" in content_type:
            envelopes: Any = _parse_sse_envelopes(response.text)
        else:
            try:
                envelopes = response.json()
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                raise ProjectionProtocolError("GBrain projection returned malformed JSON") from exc
        return _select_response_envelope(envelopes, expected_request_id)

    @staticmethod
    def _raise_envelope_error(envelope: Mapping[str, Any], operation: str) -> None:
        if "error" not in envelope:
            return
        error = envelope["error"]
        if isinstance(error, Mapping):
            code = error.get("code")
            message = error.get("message")
            detail = f"{code}: {message}" if code is not None else str(message or error)
        else:
            detail = str(error)
        raise ProjectionToolError(f"MCP {operation} failed: {detail}")

    def _validate_request(self, request: GBrainSyncRequest) -> None:
        if request.mode not in {"incremental", "reconcile"}:
            raise ProjectionConfigurationError("GBrain sync mode must be incremental or reconcile")
        if not request.source_id or not request.idempotency_key:
            raise ProjectionConfigurationError("GBrain sync source and idempotency key are required")
        managed_source = (self.settings.gbrain_managed_source_id or "").strip()
        if managed_source and request.source_id != managed_source:
            raise ProjectionConfigurationError("GBrain sync source does not match the managed source")
        try:
            request_root = Path(request.root).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise ProjectionConfigurationError("GBrain sync root is invalid") from exc
        if request_root != self.root:
            raise ProjectionConfigurationError("GBrain sync root does not match the trusted Vault root")

    @staticmethod
    def _validate_response(request: GBrainSyncRequest, response: GBrainSyncResponse) -> None:
        if response.source_id != request.source_id:
            raise ProjectionProtocolError("lgdo_vault_sync response source_id mismatch")
        if response.mode != request.mode:
            raise ProjectionProtocolError("lgdo_vault_sync response mode mismatch")
        if response.idempotency_key != request.idempotency_key:
            raise ProjectionProtocolError("lgdo_vault_sync response idempotency_key mismatch")

        expected_by_id = {page.page_id: page for page in request.expected_pages}
        actual_by_id = {page.page_id: page for page in response.pages}
        if len(expected_by_id) != len(request.expected_pages) or len(actual_by_id) != len(response.pages):
            raise ProjectionProtocolError("lgdo_vault_sync returned duplicate page results")
        if set(actual_by_id) != set(expected_by_id):
            raise ProjectionProtocolError("lgdo_vault_sync page results do not match the request")
        for page_id, expected in expected_by_id.items():
            actual = actual_by_id[page_id]
            if actual.revision_id != expected.revision_id:
                raise ProjectionProtocolError(f"page result revision_id mismatch for {page_id}")
            if actual.projection_epoch != expected.projection_epoch:
                raise ProjectionProtocolError(f"page result projection_epoch mismatch for {page_id}")
            if actual.path != expected.path:
                raise ProjectionProtocolError(f"page result path mismatch for {page_id}")
            if actual.file_hash != expected.file_hash:
                raise ProjectionProtocolError(f"page result file_hash mismatch for {page_id}")
            if actual.source_path != expected.path:
                raise ProjectionProtocolError(f"page result source_path mismatch for {page_id}")
            if actual.source_id != request.source_id:
                raise ProjectionProtocolError(f"page result source_id mismatch for {page_id}")
            if any(mapping.source_id != request.source_id for mapping in actual.protected_mappings):
                raise ProjectionProtocolError(f"page result protection source_id mismatch for {page_id}")

        imported = sum(page.status == "imported" for page in response.pages)
        skipped = sum(page.status == "skipped" for page in response.pages)
        errors = len(response.pages) - imported - skipped
        if (response.imported, response.skipped, response.errors) != (imported, skipped, errors):
            raise ProjectionProtocolError("lgdo_vault_sync aggregate page counts do not match page results")
        if any(item.source_id != request.source_id for item in response.deleted):
            raise ProjectionProtocolError("lgdo_vault_sync deletion source_id mismatch")
        if any(mapping.source_id != request.source_id for mapping in response.protected_mappings):
            raise ProjectionProtocolError("lgdo_vault_sync protection source_id mismatch")


def bump_gbrain_projection_generation(conn: Any, timestamp: str) -> int:
    conn.execute(
        "UPDATE projection_state SET value = value + 1, updated_at = ? WHERE key = ?",
        (timestamp, "gbrain_projection_generation"),
    )
    row = conn.execute(
        "SELECT value FROM projection_state WHERE key = ?",
        ("gbrain_projection_generation",),
    ).fetchone()
    if row is None:
        raise RuntimeError("gbrain_projection_generation is not initialized")
    return int(row["value"])


def _parse_sse_envelopes(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for line in raw.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif not line.strip() and data_lines:
            events.append(_parse_sse_data(data_lines))
            data_lines = []
    if data_lines:
        events.append(_parse_sse_data(data_lines))
    if not events:
        raise ProjectionProtocolError("GBrain projection returned an empty SSE response")
    return events


def _select_response_envelope(value: Any, expected_request_id: int) -> dict[str, Any]:
    envelopes = value if isinstance(value, list) else [value]
    if not envelopes:
        raise ProjectionProtocolError("GBrain projection returned an empty JSON-RPC batch")
    normalized: list[dict[str, Any]] = []
    for envelope in envelopes:
        if not isinstance(envelope, dict):
            raise ProjectionProtocolError("GBrain projection returned a non-object JSON-RPC envelope")
        if envelope.get("jsonrpc") != "2.0":
            raise ProjectionProtocolError("GBrain projection returned an invalid JSON-RPC version")
        normalized.append(envelope)
    matching = [
        envelope
        for envelope in normalized
        if type(envelope.get("id")) is type(expected_request_id)
        and envelope.get("id") == expected_request_id
    ]
    if not matching:
        raise ProjectionProtocolError(
            f"GBrain projection response id did not match request id {expected_request_id}"
        )
    if len(matching) != 1:
        raise ProjectionProtocolError(
            f"GBrain projection returned duplicate response id {expected_request_id}"
        )
    return matching[0]


def _parse_sse_data(data_lines: Sequence[str]) -> dict[str, Any]:
    try:
        parsed = json.loads("\n".join(data_lines))
    except json.JSONDecodeError as exc:
        raise ProjectionProtocolError("GBrain projection returned malformed SSE JSON") from exc
    if not isinstance(parsed, dict):
        raise ProjectionProtocolError("GBrain projection SSE data must be a JSON object")
    return parsed


def _tool_text(result: Mapping[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        item["text"]
        for item in content
        if isinstance(item, Mapping) and item.get("type") == "text" and isinstance(item.get("text"), str)
    ]
    return "\n".join(parts).strip()


def _required_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be an object")
    return dict(value)


def _require_exact_keys(value: Mapping[str, Any], keys: set[str], field: str) -> None:
    missing = keys - set(value)
    extra = set(value) - keys
    if missing:
        raise KeyError(f"{field} missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise TypeError(f"{field} has unexpected fields: {', '.join(sorted(extra))}")


def _required_list(value: Mapping[str, Any], key: str, field: str) -> list[Any]:
    item = value[key]
    if not isinstance(item, list):
        raise TypeError(f"{field}.{key} must be an array")
    return item


def _required_string(value: Mapping[str, Any], key: str, field: str) -> str:
    item = value[key]
    if not isinstance(item, str) or not item:
        raise TypeError(f"{field}.{key} must be a non-empty string")
    return item


def _optional_string(value: Mapping[str, Any], key: str, field: str) -> str | None:
    item = value[key]
    if item is not None and not isinstance(item, str):
        raise TypeError(f"{field}.{key} must be a string or null")
    return item


def _required_int(value: Mapping[str, Any], key: str, field: str) -> int:
    item = value[key]
    if isinstance(item, int) and not isinstance(item, bool):
        return item
    if isinstance(item, str):
        try:
            return int(item)
        except ValueError as exc:
            raise TypeError(f"{field}.{key} must be an integer") from exc
    raise TypeError(f"{field}.{key} must be an integer")


def _optional_int(value: Mapping[str, Any], key: str, field: str) -> int | None:
    if value[key] is None:
        return None
    return _required_int(value, key, field)


def _required_non_negative_int(value: Mapping[str, Any], key: str, field: str) -> int:
    item = _required_int(value, key, field)
    if item < 0:
        raise ValueError(f"{field}.{key} must be non-negative")
    return item


def _required_float(value: Mapping[str, Any], key: str, field: str) -> float:
    item = value[key]
    if isinstance(item, bool):
        raise TypeError(f"{field}.{key} must be a number")
    try:
        return float(item)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field}.{key} must be a number") from exc


def _parse_protected_mappings(value: Any, field: str) -> tuple[ProtectedMapping, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be an array")
    mappings: list[ProtectedMapping] = []
    for index, item in enumerate(value):
        mapping = _required_mapping(item, f"{field}[{index}]")
        allowed = {"source_id", "slug", "source_path", "reason"}
        required = {"source_id", "reason"}
        missing = required - set(mapping)
        extra = set(mapping) - allowed
        if missing:
            raise KeyError(f"{field}[{index}] missing fields: {', '.join(sorted(missing))}")
        if extra:
            raise TypeError(f"{field}[{index}] has unexpected fields: {', '.join(sorted(extra))}")
        normalized = {**mapping, "slug": mapping.get("slug"), "source_path": mapping.get("source_path")}
        mappings.append(
            ProtectedMapping(
                source_id=_required_string(normalized, "source_id", f"{field}[{index}]"),
                slug=_optional_string(normalized, "slug", f"{field}[{index}]"),
                source_path=_optional_string(normalized, "source_path", f"{field}[{index}]"),
                reason=_required_string(normalized, "reason", f"{field}[{index}]"),
            )
        )
    return tuple(mappings)
