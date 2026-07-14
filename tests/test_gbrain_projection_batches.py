from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable

import pytest

from app.config import Settings
from app.db import connect_app, connect_app_write, init_app_db
from app.gbrain_projection import (
    ExpectedPage,
    GBrainBatchRepository,
    GBrainDeletedResult,
    GBrainPageSyncResult,
    GBrainSyncRequest,
    GBrainSyncResponse,
    ProjectionLeaseLost,
    ProjectionNetworkError,
    ProjectionProtocolError,
    ProjectionToolError,
    ProtectedMapping,
    build_segments,
)
from app.projection_jobs import ProjectionJob, ProjectionOutbox


WORKER = "gbrain-worker"
NOW = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class PageSeed:
    page_id: str
    path: str
    revision_id: str
    projection_epoch: int
    file_hash: str
    semantic_hash: str

    def expected(self) -> ExpectedPage:
        return ExpectedPage(
            page_id=self.page_id,
            revision_id=self.revision_id,
            projection_epoch=self.projection_epoch,
            path=self.path.removeprefix("wiki/"),
            file_hash=self.file_hash,
        )


class ScriptedProjectionClient:
    def __init__(self, *outcomes: Any):
        self.outcomes = list(outcomes)
        self.requests: list[GBrainSyncRequest] = []

    async def sync(self, request: GBrainSyncRequest) -> GBrainSyncResponse:
        self.requests.append(request)
        if not self.outcomes:
            raise AssertionError("projection client received an unexpected segment")
        outcome = self.outcomes.pop(0)
        if callable(outcome):
            outcome = outcome(request)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def settings(tmp_path) -> Settings:
    configured = Settings(
        database_backend="sqlite",
        database_path=tmp_path / "projection-batches.db",
        vault_path=tmp_path / "vault",
        gbrain_endpoint="https://gbrain.example/mcp",
        gbrain_projection_api_key="projection-token",
        gbrain_managed_source_id="lgdo-managed",
        gbrain_import_allowed_root=tmp_path / "vault" / "wiki",
        projection_worker_enabled=False,
        _env_file=None,
    )
    init_app_db(configured)
    return configured


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _insert_revision(
    conn: Any,
    *,
    page_id: str,
    path: str,
    revision_id: str,
    revision_number: int,
    content: str,
) -> tuple[str, str]:
    file_hash = _hash(content)
    semantic_hash = _hash(content.strip())
    conn.execute(
        """
        INSERT INTO wiki_page_revisions(
          id,page_id,page_path,revision_number,file_hash,semantic_hash,content,
          origin,base_revision_id,source_ids_json,actor,note,metadata_json,
          idempotency_key,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            revision_id,
            page_id,
            path,
            revision_number,
            file_hash,
            semantic_hash,
            content,
            "manual",
            None,
            "[]",
            "tester",
            None,
            "{}",
            f"test:{revision_id}",
            NOW.isoformat(),
        ),
    )
    return file_hash, semantic_hash


def _seed_page(
    settings: Settings,
    index: int,
    *,
    revision_number: int = 1,
    projection_epoch: int = 1,
    lifecycle_status: str = "active",
) -> PageSeed:
    page_id = f"page_{index:04d}"
    path = f"wiki/product/faq/page-{index:04d}.md"
    revision_id = f"wrev_{index:04d}_{revision_number}"
    content = f"# Page {index}\n\nRevision {revision_number}."
    with connect_app_write(settings) as conn:
        file_hash, semantic_hash = _insert_revision(
            conn,
            page_id=page_id,
            path=path,
            revision_id=revision_id,
            revision_number=revision_number,
            content=content,
        )
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,page_id,domain,page_type,title,source_ids_json,review_status,
              created_at,updated_at,current_revision_id,revision_number,file_hash,
              semantic_hash,projection_epoch,lifecycle_status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                path,
                page_id,
                "product",
                "faq",
                f"Page {index}",
                "[]",
                "draft",
                NOW.isoformat(),
                NOW.isoformat(),
                revision_id,
                revision_number,
                file_hash,
                semantic_hash,
                projection_epoch,
                lifecycle_status,
            ),
        )
    return PageSeed(page_id, path, revision_id, projection_epoch, file_hash, semantic_hash)


def _advance_page(settings: Settings, page: PageSeed) -> PageSeed:
    revision_number = int(page.revision_id.rsplit("_", 1)[-1]) + 1
    revision_id = f"{page.page_id.replace('page_', 'wrev_')}_{revision_number}"
    content = f"# Updated {page.page_id}\n\nRevision {revision_number}."
    with connect_app_write(settings) as conn:
        file_hash, semantic_hash = _insert_revision(
            conn,
            page_id=page.page_id,
            path=page.path,
            revision_id=revision_id,
            revision_number=revision_number,
            content=content,
        )
        conn.execute(
            """
            UPDATE wiki_pages
            SET current_revision_id=?, revision_number=?, file_hash=?, semantic_hash=?,
                projection_epoch=?, updated_at=?
            WHERE page_id=?
            """,
            (
                revision_id,
                revision_number,
                file_hash,
                semantic_hash,
                page.projection_epoch + 1,
                NOW.isoformat(),
                page.page_id,
            ),
        )
    return PageSeed(
        page.page_id,
        page.path,
        revision_id,
        page.projection_epoch + 1,
        file_hash,
        semantic_hash,
    )


def _enqueue(
    settings: Settings,
    *,
    operation: str,
    page: PageSeed | None,
    revision_id: str | None = None,
    projection_epoch: int | None = None,
) -> str:
    outbox = ProjectionOutbox(settings)
    with connect_app_write(settings) as conn:
        return outbox.enqueue(
            conn,
            target="gbrain",
            operation=operation,
            page_id=page.page_id if page else None,
            revision_id=revision_id if revision_id is not None else (page.revision_id if page else None),
            projection_epoch=(
                projection_epoch
                if projection_epoch is not None
                else (page.projection_epoch if page else 0)
            ),
            payload={"path": page.path if page else None},
        )


def _claim(settings: Settings, limit: int = 500) -> list[ProjectionJob]:
    return ProjectionOutbox(settings).claim(
        target="gbrain",
        worker_id=WORKER,
        limit=limit,
        lease_seconds=180,
        now=NOW,
    )


def _seed_mapping(
    settings: Settings,
    page: PageSeed,
    *,
    slug: str | None = None,
    status: str = "current",
) -> str:
    mapping_id = f"gproj_{page.page_id}_{slug or 'default'}"
    resolved_slug = slug or page.path.removeprefix("wiki/").removesuffix(".md")
    with connect_app_write(settings) as conn:
        conn.execute(
            """
            INSERT INTO gbrain_page_projections(
              id,page_id,revision_id,projection_epoch,page_path,file_hash,semantic_hash,
              gbrain_source_id,slug,source_path,gbrain_content_hash,
              gbrain_page_generation,status,imported_at,invalidated_at,last_job_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                mapping_id,
                page.page_id,
                page.revision_id,
                page.projection_epoch,
                page.path,
                page.file_hash,
                page.semantic_hash,
                "lgdo-managed",
                resolved_slug,
                page.path.removeprefix("wiki/"),
                _hash(f"content:{page.page_id}"),
                1,
                status,
                NOW.isoformat(),
                None,
                "seed-job",
            ),
        )
    return resolved_slug


def _page_result(
    expected: ExpectedPage,
    *,
    status: str = "imported",
    protections: tuple[ProtectedMapping, ...] = (),
    error: str | None = None,
) -> GBrainPageSyncResult:
    projected = status in {"imported", "skipped"}
    return GBrainPageSyncResult(
        page_id=expected.page_id,
        revision_id=expected.revision_id,
        projection_epoch=expected.projection_epoch,
        path=expected.path,
        file_hash=expected.file_hash,
        source_id="lgdo-managed",
        slug=expected.path.removesuffix(".md"),
        source_path=expected.path,
        raw_file_hash_before=expected.file_hash,
        raw_file_hash_after=expected.file_hash,
        content_hash=_hash(f"gbrain:{expected.page_id}") if projected else None,
        page_generation=7 if projected else None,
        status=status,
        error=error,
        protected_mappings=protections,
    )


def _response(
    request: GBrainSyncRequest,
    *,
    statuses: dict[str, tuple[str, tuple[ProtectedMapping, ...], str | None]] | None = None,
    deleted: tuple[GBrainDeletedResult, ...] = (),
    protections: tuple[ProtectedMapping, ...] = (),
) -> GBrainSyncResponse:
    pages: list[GBrainPageSyncResult] = []
    for expected in request.expected_pages:
        status, page_protections, error = (statuses or {}).get(
            expected.page_id,
            ("skipped", (), None),
        )
        pages.append(
            _page_result(
                expected,
                status=status,
                protections=page_protections,
                error=error,
            )
        )
    imported = sum(page.status == "imported" for page in pages)
    skipped = sum(page.status == "skipped" for page in pages)
    return GBrainSyncResponse(
        source_id=request.source_id,
        mode=request.mode,
        idempotency_key=request.idempotency_key,
        pages=tuple(pages),
        deleted=deleted,
        protected_mappings=protections,
        imported=imported,
        skipped=skipped,
        errors=len(pages) - imported - skipped,
        chunks=imported,
        duration_ms=1.0,
    )


def _generation(settings: Settings) -> int:
    with connect_app(settings) as conn:
        row = conn.execute(
            "SELECT value FROM projection_state WHERE key='gbrain_projection_generation'"
        ).fetchone()
    return int(row["value"])


def _job_statuses(settings: Settings, *job_ids: str) -> dict[str, str]:
    with connect_app(settings) as conn:
        rows = conn.execute(
            "SELECT id,status FROM knowledge_projection_jobs"
        ).fetchall()
    wanted = set(job_ids)
    return {str(row["id"]): str(row["status"]) for row in rows if row["id"] in wanted}


def _expected_pages(count: int) -> tuple[ExpectedPage, ...]:
    return tuple(
        ExpectedPage(
            page_id=f"page_{index:04d}",
            revision_id=f"wrev_{index:04d}",
            projection_epoch=1,
            path=f"product/faq/page-{index:04d}.md",
            file_hash=_hash(str(index)),
        )
        for index in reversed(range(count))
    )


@pytest.mark.parametrize(
    ("count", "sizes", "modes"),
    [
        (201, [100, 100, 1], ["incremental", "incremental", "reconcile"]),
        (200, [100, 100], ["incremental", "reconcile"]),
    ],
)
def test_reconcile_segments_are_sorted_capped_and_only_final_reconciles(count, sizes, modes):
    segments = build_segments("batch-demo", "reconcile", _expected_pages(count))

    assert [len(segment.expected_pages) for segment in segments] == sizes
    assert [segment.mode for segment in segments] == modes
    assert [page.page_id for segment in segments for page in segment.expected_pages] == sorted(
        page.page_id for page in _expected_pages(count)
    )


def test_empty_reconcile_has_one_control_segment_but_empty_incremental_has_none():
    reconcile = build_segments("batch-empty", "reconcile", ())

    assert len(reconcile) == 1
    assert reconcile[0].mode == "reconcile"
    assert reconcile[0].expected_pages == ()
    assert build_segments("batch-empty-incremental", "incremental", ()) == []


def test_batch_construction_supersedes_old_epoch_and_requeues_current_state(settings):
    old = _seed_page(settings, 1)
    current = _advance_page(settings, old)
    old_job = _enqueue(
        settings,
        operation="upsert",
        page=old,
        revision_id=old.revision_id,
        projection_epoch=old.projection_epoch,
    )
    claimed = _claim(settings)

    batch = GBrainBatchRepository(settings).create_batch(
        claimed,
        worker_id=WORKER,
        lease_seconds=180,
        now=NOW,
    )

    assert batch is None
    with connect_app(settings) as conn:
        old_row = conn.execute(
            "SELECT status FROM knowledge_projection_jobs WHERE id=?",
            (old_job,),
        ).fetchone()
        current_row = conn.execute(
            """
            SELECT status FROM knowledge_projection_jobs
            WHERE target='gbrain' AND operation='upsert' AND page_id=?
              AND revision_id=? AND projection_epoch=?
            """,
            (current.page_id, current.revision_id, current.projection_epoch),
        ).fetchone()
    assert old_row["status"] == "superseded"
    assert current_row["status"] == "pending"


@pytest.mark.parametrize("trigger_operation", ["rename", "delete"])
def test_reconcile_snapshot_is_full_valid_and_batch_membership_is_watermarked(
    settings,
    trigger_operation,
):
    stable = _seed_page(settings, 1)
    rename_target = _seed_page(settings, 2)
    deleted = _seed_page(settings, 3, lifecycle_status="deleted")
    invalid = _seed_page(settings, 4)
    with connect_app_write(settings) as conn:
        conn.execute(
            "UPDATE wiki_pages SET current_revision_id=NULL WHERE page_id=?",
            (invalid.page_id,),
        )
    upsert_job = _enqueue(settings, operation="upsert", page=stable)
    trigger_page = rename_target if trigger_operation == "rename" else deleted
    trigger_job = _enqueue(settings, operation=trigger_operation, page=trigger_page)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )

    assert batch.mode == "reconcile"
    assert {page.page_id for page in batch.expected_pages} == {
        stable.page_id,
        rename_target.page_id,
    }
    with connect_app(settings) as conn:
        members = conn.execute(
            """
            SELECT job_id,revision_id,projection_epoch,operation
            FROM gbrain_projection_batch_jobs WHERE batch_id=? ORDER BY job_id
            """,
            (batch.id,),
        ).fetchall()
    assert {
        (
            row["job_id"],
            row["revision_id"],
            row["projection_epoch"],
            row["operation"],
        )
        for row in members
    } == {
        (upsert_job, stable.revision_id, stable.projection_epoch, "upsert"),
        (
            trigger_job,
            trigger_page.revision_id,
            trigger_page.projection_epoch,
            trigger_operation,
        ),
    }

    late = _seed_page(settings, 5)
    late_job = _enqueue(settings, operation="upsert", page=late)
    _claim(settings)
    with connect_app(settings) as conn:
        late_member = conn.execute(
            """
            SELECT 1 FROM gbrain_projection_batch_jobs
            WHERE batch_id=? AND job_id=?
            """,
            (batch.id, late_job),
        ).fetchone()
        snapshot = conn.execute(
            "SELECT included_snapshot_json FROM gbrain_projection_batches WHERE id=?",
            (batch.id,),
        ).fetchone()["included_snapshot_json"]
    assert late_member is None
    assert late.page_id not in snapshot


@pytest.mark.parametrize(
    "transport_error",
    [
        ProjectionNetworkError("timeout"),
        ProjectionProtocolError("malformed response"),
        ProjectionToolError("tool rejected"),
    ],
)
def test_nonfinal_transport_failure_never_calls_reconcile_finalization(
    settings,
    transport_error,
):
    for index in range(101):
        _seed_page(settings, index)
    job_id = _enqueue(settings, operation="reconcile", page=None)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings),
        worker_id=WORKER,
        lease_seconds=180,
        now=NOW,
    )
    client = ScriptedProjectionClient(transport_error)

    with pytest.raises(type(transport_error), match=str(transport_error)):
        asyncio.run(
            GBrainBatchRepository(settings).execute_batch(
                batch.id,
                worker_id=WORKER,
                client=client,
                lease_seconds=180,
                now=NOW,
            )
        )

    assert [request.mode for request in client.requests] == ["incremental"]
    with connect_app(settings) as conn:
        batch_row = conn.execute(
            "SELECT status,last_error FROM gbrain_projection_batches WHERE id=?",
            (batch.id,),
        ).fetchone()
        segments = conn.execute(
            """
            SELECT segment_index,status FROM gbrain_projection_segments
            WHERE batch_id=? ORDER BY segment_index
            """,
            (batch.id,),
        ).fetchall()
        job_row = conn.execute(
            "SELECT status,lease_owner FROM knowledge_projection_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    assert [(row["segment_index"], row["status"]) for row in segments] == [
        (0, "failed"),
        (1, "pending"),
    ]
    assert batch_row["status"] == "failed"
    assert str(transport_error) in batch_row["last_error"]
    assert (job_row["status"], job_row["lease_owner"]) == ("running", WORKER)


def test_deterministic_protections_accumulate_into_only_final_reconcile(settings):
    pages = [_seed_page(settings, index) for index in range(104)]
    _enqueue(settings, operation="reconcile", page=None)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings),
        worker_id=WORKER,
        lease_seconds=180,
        now=NOW,
    )
    reasons = (
        "page_error",
        "page_superseded",
        "rename_compensated",
        "rename_recovery_required",
    )
    expected_reasons = (*reasons, "rename_recovery_required_new")

    def first(request: GBrainSyncRequest) -> GBrainSyncResponse:
        statuses: dict[str, tuple[str, tuple[ProtectedMapping, ...], str | None]] = {}
        for page, status, reason in zip(
            request.expected_pages[:4],
            ("error", "superseded", "error", "recovery_required"),
            reasons,
        ):
            page_mappings = (
                ProtectedMapping(
                    source_id="lgdo-managed",
                    slug=page.path.removesuffix(".md"),
                    source_path=page.path,
                    reason=reason,
                ),
            )
            if status == "recovery_required":
                page_mappings += (
                    ProtectedMapping(
                        source_id="lgdo-managed",
                        slug=f"{page.path.removesuffix('.md')}-new",
                        source_path=f"renamed/{page.path.rsplit('/', 1)[-1]}",
                        reason="rename_recovery_required_new",
                    ),
                )
            statuses[page.page_id] = (
                status,
                page_mappings,
                reason,
            )
        return _response(request, statuses=statuses)

    def final(request: GBrainSyncRequest) -> GBrainSyncResponse:
        assert request.mode == "reconcile"
        assert expected_reasons == tuple(mapping.reason for mapping in request.protected_mappings)
        return _response(request)

    client = ScriptedProjectionClient(first, final)
    asyncio.run(
        GBrainBatchRepository(settings).execute_batch(
            batch.id,
            worker_id=WORKER,
            client=client,
            lease_seconds=180,
            now=NOW,
        )
    )

    assert [request.mode for request in client.requests] == ["incremental", "reconcile"]
    with connect_app(settings) as conn:
        persisted = conn.execute(
            """
            SELECT slug,source_path,reason FROM gbrain_projection_protections
            WHERE active=1 ORDER BY created_at,id
            """
        ).fetchall()
    assert sorted(expected_reasons) == sorted(row["reason"] for row in persisted)
    recovery = [
        row for row in persisted if row["reason"].startswith("rename_recovery_required")
    ]
    assert len(recovery) == 2
    assert recovery[0]["slug"] != recovery[1]["slug"]
    assert recovery[0]["source_path"] != recovery[1]["source_path"]


def test_persisted_recovery_protection_is_loaded_by_a_later_reconcile(settings):
    page = _seed_page(settings, 1)
    job_id = _enqueue(settings, operation="upsert", page=page)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )
    protection = ProtectedMapping(
        source_id="lgdo-managed",
        slug="product/faq/page-0001",
        source_path="product/faq/page-0001.md",
        reason="rename_recovery_required",
    )
    first_client = ScriptedProjectionClient(
        lambda request: _response(
            request,
            statuses={page.page_id: ("recovery_required", (protection,), "repair required")},
        )
    )
    asyncio.run(
        GBrainBatchRepository(settings).execute_batch(
            batch.id,
            worker_id=WORKER,
            client=first_client,
            lease_seconds=180,
            now=NOW,
        )
    )
    assert _job_statuses(settings, job_id)[job_id] == "failed"

    _enqueue(settings, operation="reconcile", page=None, projection_epoch=1)
    later = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )

    def assert_persisted(request: GBrainSyncRequest) -> GBrainSyncResponse:
        assert protection in request.protected_mappings
        return _response(request)

    asyncio.run(
        GBrainBatchRepository(settings).execute_batch(
            later.id,
            worker_id=WORKER,
            client=ScriptedProjectionClient(assert_persisted),
            lease_seconds=180,
            now=NOW,
        )
    )


def test_page_success_and_error_are_attributed_independently(settings):
    good = _seed_page(settings, 1)
    bad = _seed_page(settings, 2)
    good_job = _enqueue(settings, operation="upsert", page=good)
    bad_job = _enqueue(settings, operation="upsert", page=bad)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )
    bad_protection = ProtectedMapping(
        source_id="lgdo-managed",
        slug=bad.expected().path.removesuffix(".md"),
        source_path=bad.expected().path,
        reason="frontmatter_mismatch",
    )
    client = ScriptedProjectionClient(
        lambda request: _response(
            request,
            statuses={
                good.page_id: ("imported", (), None),
                bad.page_id: ("error", (bad_protection,), "frontmatter mismatch"),
            },
        )
    )

    asyncio.run(
        GBrainBatchRepository(settings).execute_batch(
            batch.id,
            worker_id=WORKER,
            client=client,
            lease_seconds=180,
            now=NOW,
        )
    )

    assert _job_statuses(settings, good_job, bad_job) == {
        good_job: "succeeded",
        bad_job: "failed",
    }
    with connect_app(settings) as conn:
        mappings = conn.execute(
            "SELECT * FROM gbrain_page_projections ORDER BY page_id"
        ).fetchall()
        protection = conn.execute(
            "SELECT reason,active FROM gbrain_projection_protections"
        ).fetchone()
    assert len(mappings) == 1
    mapping = mappings[0]
    assert mapping["page_id"] == good.page_id
    assert mapping["revision_id"] == good.revision_id
    assert mapping["projection_epoch"] == good.projection_epoch
    assert mapping["page_path"] == good.path
    assert mapping["file_hash"] == good.file_hash
    assert mapping["semantic_hash"] == good.semantic_hash
    assert mapping["gbrain_source_id"] == "lgdo-managed"
    assert mapping["slug"] == good.expected().path.removesuffix(".md")
    assert mapping["source_path"] == good.expected().path
    assert mapping["gbrain_content_hash"] == _hash(f"gbrain:{good.page_id}")
    assert mapping["gbrain_page_generation"] == 7
    assert mapping["status"] == "current"
    assert mapping["last_job_id"] == good_job
    assert (protection["reason"], protection["active"]) == ("frontmatter_mismatch", 1)
    assert _generation(settings) == 1


@pytest.mark.parametrize(
    ("status", "expected_job_status"),
    [
        ("error", "failed"),
        ("recovery_required", "failed"),
        ("superseded", "superseded"),
    ],
)
def test_deterministic_page_failure_allows_non_success_raw_hashes(
    settings,
    status,
    expected_job_status,
):
    page = _seed_page(settings, 1)
    job_id = _enqueue(settings, operation="upsert", page=page)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )

    def failed_result(request: GBrainSyncRequest) -> GBrainSyncResponse:
        response = _response(
            request,
            statuses={page.page_id: (status, (), "deterministic failure")},
        )
        failed_page = replace(
            response.pages[0],
            raw_file_hash_before=None,
            raw_file_hash_after=_hash("observed-mutated-bytes"),
        )
        return replace(response, pages=(failed_page,))

    asyncio.run(
        GBrainBatchRepository(settings).execute_batch(
            batch.id,
            worker_id=WORKER,
            client=ScriptedProjectionClient(failed_result),
            lease_seconds=180,
            now=NOW,
        )
    )

    assert _job_statuses(settings, job_id) == {job_id: expected_job_status}


@pytest.mark.parametrize(
    ("identity_field", "wrong_value"),
    [
        ("revision_id", "wrev_wrong"),
        ("projection_epoch", 999),
        ("path", "wrong/path.md"),
        ("source_id", "wrong-source"),
        ("source_path", "wrong/path.md"),
    ],
)
def test_deterministic_page_failure_still_requires_request_identity(
    settings,
    identity_field,
    wrong_value,
):
    page = _seed_page(settings, 1)
    job_id = _enqueue(settings, operation="upsert", page=page)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )

    def wrong_identity(request: GBrainSyncRequest) -> GBrainSyncResponse:
        response = _response(
            request,
            statuses={page.page_id: ("error", (), "deterministic failure")},
        )
        failed_page = replace(
            response.pages[0],
            **{identity_field: wrong_value},
        )
        return replace(response, pages=(failed_page,))

    with pytest.raises(ProjectionProtocolError, match="mismatch"):
        asyncio.run(
            GBrainBatchRepository(settings).execute_batch(
                batch.id,
                worker_id=WORKER,
                client=ScriptedProjectionClient(wrong_identity),
                lease_seconds=180,
                now=NOW,
            )
        )

    assert _job_statuses(settings, job_id) == {job_id: "running"}


@pytest.mark.parametrize(
    "mismatch_field",
    ["file_hash", "raw_file_hash_before", "raw_file_hash_after"],
)
def test_mismatched_page_hash_cannot_advance_mapping_or_job(settings, mismatch_field):
    page = _seed_page(settings, 1)
    job_id = _enqueue(settings, operation="upsert", page=page)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )

    def mismatched(request: GBrainSyncRequest) -> GBrainSyncResponse:
        response = _response(
            request,
            statuses={page.page_id: ("imported", (), None)},
        )
        bad_page = replace(response.pages[0], **{mismatch_field: _hash("other-bytes")})
        return replace(response, pages=(bad_page,))

    with pytest.raises(ProjectionProtocolError, match="hash"):
        asyncio.run(
            GBrainBatchRepository(settings).execute_batch(
                batch.id,
                worker_id=WORKER,
                client=ScriptedProjectionClient(mismatched),
                lease_seconds=180,
                now=NOW,
            )
        )

    with connect_app(settings) as conn:
        mapping_count = conn.execute(
            "SELECT COUNT(*) AS count FROM gbrain_page_projections"
        ).fetchone()["count"]
        job = conn.execute(
            "SELECT status,lease_owner FROM knowledge_projection_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    assert mapping_count == 0
    assert (job["status"], job["lease_owner"]) == ("running", WORKER)
    assert _generation(settings) == 0


def test_successful_rename_stales_old_current_mapping_before_new_upsert(settings):
    page = _seed_page(settings, 1)
    old_slug = _seed_mapping(settings, page, slug="product/faq/legacy-page")
    job_id = _enqueue(settings, operation="rename", page=page)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )

    asyncio.run(
        GBrainBatchRepository(settings).execute_batch(
            batch.id,
            worker_id=WORKER,
            client=ScriptedProjectionClient(
                lambda request: _response(
                    request,
                    statuses={page.page_id: ("imported", (), None)},
                )
            ),
            lease_seconds=180,
            now=NOW,
        )
    )

    with connect_app(settings) as conn:
        mappings = conn.execute(
            """
            SELECT slug,status,last_job_id FROM gbrain_page_projections
            WHERE page_id=? ORDER BY slug
            """,
            (page.page_id,),
        ).fetchall()
    assert [(row["slug"], row["status"]) for row in mappings] == [
        (old_slug, "stale"),
        (page.expected().path.removesuffix(".md"), "current"),
    ]
    assert mappings[-1]["last_job_id"] == job_id
    assert _job_statuses(settings, job_id)[job_id] == "succeeded"
    assert _generation(settings) == 1


def test_slug_owned_by_another_page_fails_closed_and_persists_protection(settings):
    target = _seed_page(settings, 1)
    other = _seed_page(settings, 2)
    target_slug = target.expected().path.removesuffix(".md")
    _seed_mapping(settings, other, slug=target_slug)
    job_id = _enqueue(settings, operation="upsert", page=target)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )

    asyncio.run(
        GBrainBatchRepository(settings).execute_batch(
            batch.id,
            worker_id=WORKER,
            client=ScriptedProjectionClient(
                lambda request: _response(
                    request,
                    statuses={target.page_id: ("imported", (), None)},
                )
            ),
            lease_seconds=180,
            now=NOW,
        )
    )

    with connect_app(settings) as conn:
        mappings = conn.execute(
            "SELECT page_id,status FROM gbrain_page_projections WHERE slug=?",
            (target_slug,),
        ).fetchall()
        protection = conn.execute(
            "SELECT reason,active FROM gbrain_projection_protections"
        ).fetchone()
    assert [(row["page_id"], row["status"]) for row in mappings] == [
        (other.page_id, "current")
    ]
    assert _job_statuses(settings, job_id)[job_id] == "failed"
    assert "ambiguous" in protection["reason"]
    assert protection["active"] == 1
    assert _generation(settings) == 0


def test_result_for_old_epoch_is_superseded_without_mapping_and_requeues_current(settings):
    old = _seed_page(settings, 1)
    old_job = _enqueue(settings, operation="upsert", page=old)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )
    current = _advance_page(settings, old)

    asyncio.run(
        GBrainBatchRepository(settings).execute_batch(
            batch.id,
            worker_id=WORKER,
            client=ScriptedProjectionClient(
                lambda request: _response(
                    request,
                    statuses={old.page_id: ("imported", (), None)},
                )
            ),
            lease_seconds=180,
            now=NOW,
        )
    )

    with connect_app(settings) as conn:
        mapping_count = conn.execute(
            "SELECT COUNT(*) AS count FROM gbrain_page_projections"
        ).fetchone()["count"]
        current_job = conn.execute(
            """
            SELECT status FROM knowledge_projection_jobs
            WHERE target='gbrain' AND operation='upsert' AND page_id=?
              AND revision_id=? AND projection_epoch=?
            """,
            (current.page_id, current.revision_id, current.projection_epoch),
        ).fetchone()
    assert _job_statuses(settings, old_job)[old_job] == "superseded"
    assert mapping_count == 0
    assert current_job["status"] == "pending"
    assert _generation(settings) == 0


def test_unique_delete_marks_mapping_deleted_bumps_generation_and_finishes_owner(settings):
    page = _seed_page(settings, 1, lifecycle_status="deleted")
    slug = _seed_mapping(settings, page)
    job_id = _enqueue(settings, operation="delete", page=page)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )
    client = ScriptedProjectionClient(
        lambda request: _response(
            request,
            deleted=(GBrainDeletedResult(source_id="lgdo-managed", slug=slug),),
        )
    )

    asyncio.run(
        GBrainBatchRepository(settings).execute_batch(
            batch.id,
            worker_id=WORKER,
            client=client,
            lease_seconds=180,
            now=NOW,
        )
    )

    with connect_app(settings) as conn:
        mapping = conn.execute(
            "SELECT status,invalidated_at FROM gbrain_page_projections WHERE page_id=?",
            (page.page_id,),
        ).fetchone()
    assert mapping["status"] == "deleted"
    assert mapping["invalidated_at"] is not None
    assert _job_statuses(settings, job_id)[job_id] == "succeeded"
    assert _generation(settings) == 1


@pytest.mark.parametrize(
    ("changed_column", "changed_value"),
    [
        ("page_id", "page_concurrent"),
        ("revision_id", "wrev_concurrent"),
        ("projection_epoch", 999),
        ("status", "stale"),
        ("gbrain_page_generation", 999),
    ],
)
def test_unique_delete_rejects_mapping_changed_since_batch_snapshot(
    settings,
    monkeypatch,
    changed_column,
    changed_value,
):
    page = _seed_page(settings, 1, lifecycle_status="deleted")
    slug = _seed_mapping(settings, page)
    job_id = _enqueue(settings, operation="delete", page=page)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )
    with connect_app(settings) as conn:
        mapping_id = conn.execute(
            "SELECT id FROM gbrain_page_projections WHERE slug=?",
            (slug,),
        ).fetchone()["id"]
    repository = GBrainBatchRepository(settings)
    persist_protections = repository._persist_segment_protections

    def mutate_after_protection_check(batch_id, segment_id, **kwargs):
        persist_protections(batch_id, segment_id, **kwargs)
        with connect_app_write(settings) as conn:
            conn.execute(
                f"UPDATE gbrain_page_projections SET {changed_column}=? WHERE id=?",
                (changed_value, mapping_id),
            )

    monkeypatch.setattr(
        repository,
        "_persist_segment_protections",
        mutate_after_protection_check,
    )

    with pytest.raises(ProjectionLeaseLost, match=mapping_id):
        asyncio.run(
            repository.execute_batch(
                batch.id,
                worker_id=WORKER,
                client=ScriptedProjectionClient(
                    lambda request: _response(
                        request,
                        deleted=(
                            GBrainDeletedResult(source_id="lgdo-managed", slug=slug),
                        ),
                    )
                ),
                lease_seconds=180,
                now=NOW,
            )
        )

    assert _job_statuses(settings, job_id) == {job_id: "running"}
    assert _generation(settings) == 0


@pytest.mark.parametrize("nullable_column", ["imported_at", "invalidated_at"])
def test_unique_delete_cas_distinguishes_null_from_empty_text(
    settings,
    monkeypatch,
    nullable_column,
):
    page = _seed_page(settings, 1, lifecycle_status="deleted")
    slug = _seed_mapping(settings, page)
    with connect_app_write(settings) as conn:
        conn.execute(
            f"UPDATE gbrain_page_projections SET {nullable_column}=NULL WHERE slug=?",
            (slug,),
        )
    job_id = _enqueue(settings, operation="delete", page=page)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )
    with connect_app(settings) as conn:
        mapping_id = conn.execute(
            "SELECT id FROM gbrain_page_projections WHERE slug=?",
            (slug,),
        ).fetchone()["id"]
    repository = GBrainBatchRepository(settings)
    persist_protections = repository._persist_segment_protections

    def mutate_after_protection_check(batch_id, segment_id, **kwargs):
        persist_protections(batch_id, segment_id, **kwargs)
        with connect_app_write(settings) as conn:
            conn.execute(
                f"UPDATE gbrain_page_projections SET {nullable_column}='' WHERE id=?",
                (mapping_id,),
            )

    monkeypatch.setattr(
        repository,
        "_persist_segment_protections",
        mutate_after_protection_check,
    )

    with pytest.raises(ProjectionLeaseLost, match=mapping_id):
        asyncio.run(
            repository.execute_batch(
                batch.id,
                worker_id=WORKER,
                client=ScriptedProjectionClient(
                    lambda request: _response(
                        request,
                        deleted=(
                            GBrainDeletedResult(source_id="lgdo-managed", slug=slug),
                        ),
                    )
                ),
                lease_seconds=180,
                now=NOW,
            )
        )

    assert _job_statuses(settings, job_id) == {job_id: "running"}
    assert _generation(settings) == 0


def test_orphan_mapping_and_unmanaged_ghost_deletions_are_legal(settings):
    orphan = _seed_page(settings, 1, lifecycle_status="deleted")
    orphan_slug = _seed_mapping(settings, orphan)
    control_job = _enqueue(settings, operation="reconcile", page=None)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )
    ghost = GBrainDeletedResult(source_id="lgdo-managed", slug="unmanaged/ghost")
    client = ScriptedProjectionClient(
        lambda request: _response(
            request,
            deleted=(
                GBrainDeletedResult(source_id="lgdo-managed", slug=orphan_slug),
                ghost,
            ),
        )
    )

    asyncio.run(
        GBrainBatchRepository(settings).execute_batch(
            batch.id,
            worker_id=WORKER,
            client=client,
            lease_seconds=180,
            now=NOW,
        )
    )

    with connect_app(settings) as conn:
        orphan_status = conn.execute(
            "SELECT status FROM gbrain_page_projections WHERE page_id=?",
            (orphan.page_id,),
        ).fetchone()["status"]
    assert orphan_status == "deleted"
    assert _job_statuses(settings, control_job)[control_job] == "succeeded"
    assert _generation(settings) == 1


def test_ambiguous_delete_owners_fail_and_protect_stale_mapping(settings):
    page = _seed_page(settings, 1, lifecycle_status="deleted")
    slug = _seed_mapping(settings, page)
    first_job = _enqueue(
        settings,
        operation="delete",
        page=page,
        revision_id=page.revision_id,
        projection_epoch=page.projection_epoch,
    )
    second_job = _enqueue(
        settings,
        operation="delete",
        page=page,
        revision_id=f"{page.revision_id}-retry",
        projection_epoch=page.projection_epoch + 1,
    )
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )

    asyncio.run(
        GBrainBatchRepository(settings).execute_batch(
            batch.id,
            worker_id=WORKER,
            client=ScriptedProjectionClient(
                lambda request: _response(
                    request,
                    deleted=(GBrainDeletedResult(source_id="lgdo-managed", slug=slug),),
                )
            ),
            lease_seconds=180,
            now=NOW,
        )
    )

    with connect_app(settings) as conn:
        mapping = conn.execute(
            "SELECT status FROM gbrain_page_projections WHERE page_id=?",
            (page.page_id,),
        ).fetchone()
        protection = conn.execute(
            "SELECT reason,active FROM gbrain_projection_protections"
        ).fetchone()
    assert mapping["status"] == "stale"
    assert _job_statuses(settings, first_job, second_job) == {
        first_job: "failed",
        second_job: "failed",
    }
    assert "ambiguous" in protection["reason"]
    assert protection["active"] == 1
    assert _generation(settings) == 1


def test_legacy_duplicate_delete_candidates_are_all_staled_and_protected(settings):
    first = _seed_page(settings, 1, lifecycle_status="deleted")
    second = _seed_page(settings, 2, lifecycle_status="deleted")
    shared_slug = _seed_mapping(settings, first, slug="legacy/shared")
    with connect_app_write(settings) as conn:
        conn.execute("DROP INDEX idx_gbrain_projection_slug_unique")
    _seed_mapping(settings, second, slug=shared_slug)
    job_id = _enqueue(settings, operation="delete", page=first)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )

    asyncio.run(
        GBrainBatchRepository(settings).execute_batch(
            batch.id,
            worker_id=WORKER,
            client=ScriptedProjectionClient(
                lambda request: _response(
                    request,
                    deleted=(
                        GBrainDeletedResult(source_id="lgdo-managed", slug=shared_slug),
                    ),
                )
            ),
            lease_seconds=180,
            now=NOW,
        )
    )

    with connect_app(settings) as conn:
        statuses = conn.execute(
            "SELECT status FROM gbrain_page_projections WHERE slug=? ORDER BY page_id",
            (shared_slug,),
        ).fetchall()
        protection = conn.execute(
            "SELECT reason,active FROM gbrain_projection_protections"
        ).fetchone()
    assert [row["status"] for row in statuses] == ["stale", "stale"]
    assert _job_statuses(settings, job_id)[job_id] == "failed"
    assert "ambiguous" in protection["reason"]
    assert protection["active"] == 1
    assert _generation(settings) == 1


def test_ambiguous_delete_rejects_mapping_changed_since_batch_snapshot(
    settings,
    monkeypatch,
):
    first = _seed_page(settings, 1, lifecycle_status="deleted")
    second = _seed_page(settings, 2, lifecycle_status="deleted")
    shared_slug = _seed_mapping(settings, first, slug="legacy/shared-cas")
    with connect_app_write(settings) as conn:
        conn.execute("DROP INDEX idx_gbrain_projection_slug_unique")
    _seed_mapping(settings, second, slug=shared_slug)
    job_id = _enqueue(settings, operation="delete", page=first)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )
    with connect_app(settings) as conn:
        changed_mapping_id = conn.execute(
            """
            SELECT id FROM gbrain_page_projections
            WHERE slug=? ORDER BY page_id LIMIT 1
            """,
            (shared_slug,),
        ).fetchone()["id"]
    repository = GBrainBatchRepository(settings)
    persist_protections = repository._persist_segment_protections

    def mutate_after_protection_check(batch_id, segment_id, **kwargs):
        persist_protections(batch_id, segment_id, **kwargs)
        with connect_app_write(settings) as conn:
            conn.execute(
                """
                UPDATE gbrain_page_projections
                SET gbrain_content_hash=? WHERE id=?
                """,
                (_hash("concurrent-content"), changed_mapping_id),
            )

    monkeypatch.setattr(
        repository,
        "_persist_segment_protections",
        mutate_after_protection_check,
    )

    with pytest.raises(ProjectionLeaseLost, match=changed_mapping_id):
        asyncio.run(
            repository.execute_batch(
                batch.id,
                worker_id=WORKER,
                client=ScriptedProjectionClient(
                    lambda request: _response(
                        request,
                        deleted=(
                            GBrainDeletedResult(
                                source_id="lgdo-managed",
                                slug=shared_slug,
                            ),
                        ),
                    )
                ),
                lease_seconds=180,
                now=NOW,
            )
        )

    with connect_app(settings) as conn:
        statuses = conn.execute(
            "SELECT status FROM gbrain_page_projections WHERE slug=? ORDER BY page_id",
            (shared_slug,),
        ).fetchall()
    assert [row["status"] for row in statuses] == ["current", "current"]
    assert _job_statuses(settings, job_id) == {job_id: "running"}
    assert _generation(settings) == 0


def test_empty_vault_reconcile_completes_each_unique_delete_job(settings):
    pages = [
        _seed_page(settings, index, lifecycle_status="deleted")
        for index in range(2)
    ]
    slugs = [_seed_mapping(settings, page) for page in pages]
    job_ids = [_enqueue(settings, operation="delete", page=page) for page in pages]
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )

    def delete_all(request: GBrainSyncRequest) -> GBrainSyncResponse:
        assert request.mode == "reconcile"
        assert request.expected_pages == ()
        return _response(
            request,
            deleted=tuple(
                GBrainDeletedResult(source_id="lgdo-managed", slug=slug)
                for slug in slugs
            ),
        )

    asyncio.run(
        GBrainBatchRepository(settings).execute_batch(
            batch.id,
            worker_id=WORKER,
            client=ScriptedProjectionClient(delete_all),
            lease_seconds=180,
            now=NOW,
        )
    )

    assert _job_statuses(settings, *job_ids) == {
        job_id: "succeeded" for job_id in job_ids
    }
    with connect_app(settings) as conn:
        statuses = conn.execute(
            "SELECT status FROM gbrain_page_projections ORDER BY page_id"
        ).fetchall()
    assert [row["status"] for row in statuses] == ["deleted", "deleted"]
    assert _generation(settings) == 2


def test_terminal_page_batch_member_is_not_treated_as_ownerless_repair(
    settings,
    monkeypatch,
):
    page = _seed_page(settings, 1)
    job_id = _enqueue(settings, operation="upsert", page=page)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )
    repository = GBrainBatchRepository(settings)
    persist_protections = repository._persist_segment_protections

    def terminalize_after_protection_check(batch_id, segment_id, **kwargs):
        persist_protections(batch_id, segment_id, **kwargs)
        with connect_app_write(settings) as conn:
            conn.execute(
                """
                UPDATE knowledge_projection_jobs
                SET status='failed',lease_owner=NULL,lease_expires_at=NULL
                WHERE id=?
                """,
                (job_id,),
            )

    monkeypatch.setattr(
        repository,
        "_persist_segment_protections",
        terminalize_after_protection_check,
    )

    with pytest.raises(ProjectionLeaseLost, match=job_id):
        asyncio.run(
            repository.execute_batch(
                batch.id,
                worker_id=WORKER,
                client=ScriptedProjectionClient(
                    lambda request: _response(
                        request,
                        statuses={page.page_id: ("imported", (), None)},
                    )
                ),
                lease_seconds=180,
                now=NOW,
            )
        )

    with connect_app(settings) as conn:
        mapping_count = conn.execute(
            "SELECT COUNT(*) AS count FROM gbrain_page_projections"
        ).fetchone()["count"]
    assert mapping_count == 0


def test_terminal_delete_batch_member_is_not_treated_as_orphan_cleanup(
    settings,
    monkeypatch,
):
    page = _seed_page(settings, 1, lifecycle_status="deleted")
    slug = _seed_mapping(settings, page)
    job_id = _enqueue(settings, operation="delete", page=page)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )
    repository = GBrainBatchRepository(settings)
    persist_protections = repository._persist_segment_protections

    def terminalize_after_protection_check(batch_id, segment_id, **kwargs):
        persist_protections(batch_id, segment_id, **kwargs)
        with connect_app_write(settings) as conn:
            conn.execute(
                """
                UPDATE knowledge_projection_jobs
                SET status='failed',lease_owner=NULL,lease_expires_at=NULL
                WHERE id=?
                """,
                (job_id,),
            )

    monkeypatch.setattr(
        repository,
        "_persist_segment_protections",
        terminalize_after_protection_check,
    )

    with pytest.raises(ProjectionLeaseLost, match=job_id):
        asyncio.run(
            repository.execute_batch(
                batch.id,
                worker_id=WORKER,
                client=ScriptedProjectionClient(
                    lambda request: _response(
                        request,
                        deleted=(
                            GBrainDeletedResult(source_id="lgdo-managed", slug=slug),
                        ),
                    )
                ),
                lease_seconds=180,
                now=NOW,
            )
        )

    with connect_app(settings) as conn:
        mapping_status = conn.execute(
            "SELECT status FROM gbrain_page_projections WHERE page_id=?",
            (page.page_id,),
        ).fetchone()["status"]
    assert mapping_status == "current"


def test_member_lease_lost_before_protection_persistence_writes_nothing(
    settings,
    monkeypatch,
):
    page = _seed_page(settings, 1)
    job_id = _enqueue(settings, operation="upsert", page=page)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )
    protection = ProtectedMapping(
        source_id="lgdo-managed",
        slug=page.expected().path.removesuffix(".md"),
        source_path=page.expected().path,
        reason="deterministic failure",
    )
    repository = GBrainBatchRepository(settings)
    validate_result = repository._validate_result_hashes

    def steal_after_validation(request, response):
        validate_result(request, response)
        with connect_app_write(settings) as conn:
            conn.execute(
                """
                UPDATE knowledge_projection_jobs SET lease_owner='other'
                WHERE id=?
                """,
                (job_id,),
            )

    monkeypatch.setattr(repository, "_validate_result_hashes", steal_after_validation)

    with pytest.raises(ProjectionLeaseLost, match=job_id):
        asyncio.run(
            repository.execute_batch(
                batch.id,
                worker_id=WORKER,
                client=ScriptedProjectionClient(
                    lambda request: _response(
                        request,
                        statuses={
                            page.page_id: (
                                "error",
                                (protection,),
                                "deterministic failure",
                            )
                        },
                    )
                ),
                lease_seconds=180,
                now=NOW,
            )
        )

    with connect_app(settings) as conn:
        protection_count = conn.execute(
            "SELECT COUNT(*) AS count FROM gbrain_projection_protections"
        ).fetchone()["count"]
        job_owner = conn.execute(
            "SELECT lease_owner FROM knowledge_projection_jobs WHERE id=?",
            (job_id,),
        ).fetchone()["lease_owner"]
    assert protection_count == 0
    assert job_owner == "other"


@pytest.mark.parametrize("lost_lease", ["batch", "segment", "job"])
def test_lease_lost_during_protection_persistence_rolls_back_write(
    settings,
    monkeypatch,
    lost_lease,
):
    page = _seed_page(settings, 1)
    job_id = _enqueue(settings, operation="upsert", page=page)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )
    protection = ProtectedMapping(
        source_id="lgdo-managed",
        slug=page.expected().path.removesuffix(".md"),
        source_path=page.expected().path,
        reason="deterministic failure",
    )
    repository = GBrainBatchRepository(settings)
    persist_protections = repository._persist_protections

    def steal_during_persistence(conn, protections, timestamp, page_id=None):
        persist_protections(conn, protections, timestamp, page_id)
        if lost_lease == "batch":
            conn.execute(
                """
                UPDATE gbrain_projection_batches SET lease_owner='other'
                WHERE id=?
                """,
                (batch.id,),
            )
        elif lost_lease == "segment":
            conn.execute(
                """
                UPDATE gbrain_projection_segments SET lease_owner='other'
                WHERE batch_id=? AND segment_index=0
                """,
                (batch.id,),
            )
        else:
            conn.execute(
                """
                UPDATE knowledge_projection_jobs SET lease_owner='other'
                WHERE id=?
                """,
                (job_id,),
            )

    monkeypatch.setattr(repository, "_persist_protections", steal_during_persistence)

    with pytest.raises(ProjectionLeaseLost):
        asyncio.run(
            repository.execute_batch(
                batch.id,
                worker_id=WORKER,
                client=ScriptedProjectionClient(
                    lambda request: _response(
                        request,
                        statuses={
                            page.page_id: (
                                "error",
                                (protection,),
                                "deterministic failure",
                            )
                        },
                    )
                ),
                lease_seconds=180,
                now=NOW,
            )
        )

    with connect_app(settings) as conn:
        protection_count = conn.execute(
            "SELECT COUNT(*) AS count FROM gbrain_projection_protections"
        ).fetchone()["count"]
        batch_owner = conn.execute(
            "SELECT lease_owner FROM gbrain_projection_batches WHERE id=?",
            (batch.id,),
        ).fetchone()["lease_owner"]
        segment_owner = conn.execute(
            """
            SELECT lease_owner FROM gbrain_projection_segments
            WHERE batch_id=? AND segment_index=0
            """,
            (batch.id,),
        ).fetchone()["lease_owner"]
        job_owner = conn.execute(
            "SELECT lease_owner FROM knowledge_projection_jobs WHERE id=?",
            (job_id,),
        ).fetchone()["lease_owner"]
    assert protection_count == 0
    assert (batch_owner, segment_owner, job_owner) == (WORKER, WORKER, WORKER)


@pytest.mark.parametrize("lost_lease", ["batch", "segment", "job"])
def test_pre_call_lease_loss_makes_no_request_or_state_transition(settings, lost_lease):
    page = _seed_page(settings, 1)
    job_id = _enqueue(settings, operation="upsert", page=page)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )
    with connect_app_write(settings) as conn:
        if lost_lease == "batch":
            conn.execute(
                "UPDATE gbrain_projection_batches SET lease_owner='other' WHERE id=?",
                (batch.id,),
            )
        elif lost_lease == "segment":
            conn.execute(
                """
                UPDATE gbrain_projection_segments SET lease_owner='other'
                WHERE batch_id=? AND segment_index=0
                """,
                (batch.id,),
            )
        else:
            conn.execute(
                "UPDATE knowledge_projection_jobs SET lease_owner='other' WHERE id=?",
                (job_id,),
            )
    client = ScriptedProjectionClient()

    with pytest.raises(ProjectionLeaseLost):
        asyncio.run(
            GBrainBatchRepository(settings).execute_batch(
                batch.id,
                worker_id=WORKER,
                client=client,
                lease_seconds=180,
                now=NOW,
            )
        )

    assert client.requests == []
    with connect_app(settings) as conn:
        batch_row = conn.execute(
            "SELECT status,lease_owner FROM gbrain_projection_batches WHERE id=?",
            (batch.id,),
        ).fetchone()
        segment_row = conn.execute(
            """
            SELECT status,lease_owner FROM gbrain_projection_segments
            WHERE batch_id=? AND segment_index=0
            """,
            (batch.id,),
        ).fetchone()
        job_row = conn.execute(
            "SELECT status,lease_owner FROM knowledge_projection_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        mapping_count = conn.execute(
            "SELECT COUNT(*) AS count FROM gbrain_page_projections"
        ).fetchone()["count"]
        protection_count = conn.execute(
            "SELECT COUNT(*) AS count FROM gbrain_projection_protections"
        ).fetchone()["count"]
    assert batch_row["status"] == "running"
    assert batch_row["lease_owner"] == ("other" if lost_lease == "batch" else WORKER)
    assert segment_row["status"] == "pending"
    assert segment_row["lease_owner"] == ("other" if lost_lease == "segment" else WORKER)
    assert job_row["status"] == "running"
    assert job_row["lease_owner"] == ("other" if lost_lease == "job" else WORKER)
    assert mapping_count == protection_count == 0
    assert _generation(settings) == 0


@pytest.mark.parametrize("lost_lease", ["batch", "segment", "job"])
def test_lost_batch_segment_or_job_lease_discards_late_result(settings, lost_lease):
    page = _seed_page(settings, 1)
    job_id = _enqueue(settings, operation="upsert", page=page)
    batch = GBrainBatchRepository(settings).create_batch(
        _claim(settings), worker_id=WORKER, lease_seconds=180, now=NOW
    )

    def steal_lease(request: GBrainSyncRequest) -> GBrainSyncResponse:
        with connect_app_write(settings) as conn:
            if lost_lease == "batch":
                conn.execute(
                    "UPDATE gbrain_projection_batches SET lease_owner='other' WHERE id=?",
                    (batch.id,),
                )
            elif lost_lease == "segment":
                conn.execute(
                    """
                    UPDATE gbrain_projection_segments SET lease_owner='other'
                    WHERE batch_id=? AND segment_index=0
                    """,
                    (batch.id,),
                )
            else:
                conn.execute(
                    """
                    UPDATE knowledge_projection_jobs SET lease_owner='other'
                    WHERE id=?
                    """,
                    (job_id,),
                )
        return _response(
            request,
            statuses={page.page_id: ("imported", (), None)},
        )

    with pytest.raises(ProjectionLeaseLost):
        asyncio.run(
            GBrainBatchRepository(settings).execute_batch(
                batch.id,
                worker_id=WORKER,
                client=ScriptedProjectionClient(steal_lease),
                lease_seconds=180,
                now=NOW,
            )
        )

    with connect_app(settings) as conn:
        batch_row = conn.execute(
            "SELECT status,lease_owner FROM gbrain_projection_batches WHERE id=?",
            (batch.id,),
        ).fetchone()
        segment_row = conn.execute(
            """
            SELECT status,lease_owner FROM gbrain_projection_segments
            WHERE batch_id=? AND segment_index=0
            """,
            (batch.id,),
        ).fetchone()
        job_row = conn.execute(
            "SELECT status,lease_owner FROM knowledge_projection_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
        mapping_count = conn.execute(
            "SELECT COUNT(*) AS count FROM gbrain_page_projections"
        ).fetchone()["count"]
        protection_count = conn.execute(
            "SELECT COUNT(*) AS count FROM gbrain_projection_protections"
        ).fetchone()["count"]
    assert batch_row["status"] == "running"
    assert batch_row["lease_owner"] == ("other" if lost_lease == "batch" else WORKER)
    assert segment_row["status"] == "running"
    assert segment_row["lease_owner"] == ("other" if lost_lease == "segment" else WORKER)
    assert job_row["status"] == "running"
    assert job_row["lease_owner"] == ("other" if lost_lease == "job" else WORKER)
    assert mapping_count == 0
    assert protection_count == 0
    assert _generation(settings) == 0
