from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

import app.main as main_module
import app.projection_worker as projection_worker
from app.config import Settings
from app.db import connect_app, connect_app_write, init_app_db
from app.projection_jobs import ProjectionJob, ProjectionOutbox
from app.wiki_rag_projection import ProjectionLeaseLost as RagProjectionLeaseLost


NOW = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)


def _job(job_id: str, target: str) -> ProjectionJob:
    return ProjectionJob(
        id=job_id,
        idempotency_key=f"key:{job_id}",
        target=target,
        operation="upsert",
        page_id=f"page:{job_id}",
        revision_id=f"revision:{job_id}",
        projection_epoch=1,
        payload={"path": f"wiki/{job_id}.md"},
        status="running",
        attempts=1,
        available_at=NOW.isoformat(),
        lease_owner="placeholder",
        lease_expires_at=(NOW + timedelta(seconds=180)).isoformat(),
        last_error=None,
        created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
    )


@pytest.fixture
def settings(tmp_path) -> Settings:
    configured = Settings(
        database_backend="sqlite",
        database_path=tmp_path / "projection-worker.db",
        vault_path=tmp_path / "vault",
        gbrain_endpoint="https://gbrain.example/mcp",
        gbrain_projection_api_key="projection-token",
        gbrain_managed_source_id="lgdo-managed",
        projection_worker_enabled=True,
        projection_poll_seconds=0.01,
        _env_file=None,
    )
    init_app_db(configured)
    return configured


class ScriptedOutbox:
    def __init__(self, **batches: list[list[ProjectionJob]]):
        self.batches = {target: list(values) for target, values in batches.items()}
        self.claims: list[dict[str, Any]] = []
        self.renewals: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []

    def claim(self, **kwargs: Any) -> list[ProjectionJob]:
        self.claims.append(kwargs)
        target_batches = self.batches.setdefault(str(kwargs["target"]), [])
        return target_batches.pop(0) if target_batches else []

    def renew_lease(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        self.renewals.append(
            {
                "job_id": job_id,
                "worker_id": worker_id,
                "lease_seconds": lease_seconds,
                "now": now,
            }
        )
        return True

    def mark_failed(
        self,
        job_id: str,
        worker_id: str,
        last_error: str,
        now: datetime | None = None,
    ) -> bool:
        self.failures.append(
            {
                "job_id": job_id,
                "worker_id": worker_id,
                "last_error": last_error,
                "now": now,
            }
        )
        return True


def _patch_gbrain_repository(monkeypatch, repository_type: type) -> None:
    monkeypatch.setattr(projection_worker, "GBrainBatchRepository", repository_type)
    monkeypatch.setattr(
        projection_worker,
        "GBrainProjectionClient",
        lambda configured: SimpleNamespace(settings=configured),
    )


def test_start_and_stop_are_idempotent(settings, monkeypatch):
    worker = projection_worker.ProjectionWorker(settings, outbox=ScriptedOutbox())
    loop_started: list[str] = []

    async def idle_loop(target: str) -> None:
        loop_started.append(target)
        await worker._stop.wait()

    monkeypatch.setattr(worker, "_loop", idle_loop)

    async def exercise() -> None:
        await worker.start()
        first_tasks = tuple(worker._tasks)
        await asyncio.sleep(0)
        await worker.start()
        assert tuple(worker._tasks) == first_tasks
        assert len(first_tasks) == 2
        await worker.stop()
        await worker.stop()
        assert worker._tasks == []

    asyncio.run(exercise())
    assert sorted(loop_started) == ["gbrain", "rag"]


def test_stop_cancels_an_active_projection_operation(settings, monkeypatch):
    outbox = ScriptedOutbox(gbrain=[[_job("active", "gbrain")]])

    class Repository:
        started: asyncio.Event
        cancelled: asyncio.Event

        def __init__(self, configured, claimed_outbox):
            pass

        def create_batch(self, jobs, **kwargs):
            return SimpleNamespace(id="active-batch")

        async def execute_batch(self, batch_id, **kwargs):
            Repository.started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                Repository.cancelled.set()
                raise

    _patch_gbrain_repository(monkeypatch, Repository)
    worker = projection_worker.ProjectionWorker(settings, outbox=outbox, now=lambda: NOW)

    async def exercise() -> None:
        Repository.started = asyncio.Event()
        Repository.cancelled = asyncio.Event()
        await worker.start()
        await asyncio.wait_for(Repository.started.wait(), timeout=1)
        await worker.stop()
        assert Repository.cancelled.is_set()

    asyncio.run(exercise())


def test_only_one_gbrain_batch_runs_at_a_time(settings, monkeypatch):
    outbox = ScriptedOutbox(
        gbrain=[[_job("gbrain-1", "gbrain")], [_job("gbrain-2", "gbrain")]]
    )
    started = asyncio.Event()
    release = asyncio.Event()

    class Repository:
        active = 0
        maximum_active = 0
        executions = 0

        def __init__(self, configured, claimed_outbox):
            assert configured is settings
            assert claimed_outbox is outbox

        def create_batch(self, jobs, **kwargs):
            return SimpleNamespace(id=f"batch:{jobs[0].id}")

        async def execute_batch(self, batch_id, **kwargs):
            Repository.executions += 1
            Repository.active += 1
            Repository.maximum_active = max(Repository.maximum_active, Repository.active)
            started.set()
            await release.wait()
            await asyncio.sleep(0)
            Repository.active -= 1

    _patch_gbrain_repository(monkeypatch, Repository)
    worker = projection_worker.ProjectionWorker(settings, outbox=outbox, now=lambda: NOW)

    async def exercise() -> None:
        first = asyncio.create_task(worker.run_once("gbrain"))
        await started.wait()
        second = asyncio.create_task(worker.run_once("gbrain"))
        await asyncio.sleep(0)
        assert Repository.executions == 1
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(exercise())
    assert Repository.executions == 2
    assert Repository.maximum_active == 1


def test_rag_progresses_while_gbrain_batch_is_active(settings, monkeypatch):
    outbox = ScriptedOutbox(
        gbrain=[[_job("gbrain", "gbrain")]],
        rag=[[_job("rag", "rag")]],
    )
    gbrain_started = asyncio.Event()
    release_gbrain = asyncio.Event()
    rag_calls: list[str] = []

    class Repository:
        def __init__(self, configured, claimed_outbox):
            pass

        def create_batch(self, jobs, **kwargs):
            return SimpleNamespace(id="batch")

        async def execute_batch(self, batch_id, **kwargs):
            gbrain_started.set()
            await release_gbrain.wait()

    class Projector:
        def __init__(self, configured, claimed_outbox):
            pass

        def project(self, job, worker_id):
            rag_calls.append(job.id)

    _patch_gbrain_repository(monkeypatch, Repository)
    monkeypatch.setattr(projection_worker, "WikiRagProjector", Projector)
    worker = projection_worker.ProjectionWorker(settings, outbox=outbox, now=lambda: NOW)

    async def exercise() -> None:
        gbrain = asyncio.create_task(worker.run_once("gbrain"))
        await gbrain_started.wait()
        rag = await asyncio.wait_for(worker.run_once("rag"), timeout=1)
        assert rag.succeeded == 1
        assert not gbrain.done()
        release_gbrain.set()
        await gbrain

    asyncio.run(exercise())
    assert rag_calls == ["rag"]


def test_claims_use_180_second_leases_and_renew_every_60_seconds(
    settings, monkeypatch
):
    assert projection_worker.PROJECTION_LEASE_SECONDS == 180
    assert projection_worker.PROJECTION_LEASE_RENEW_SECONDS == 60
    outbox = ScriptedOutbox(gbrain=[[_job("renewed", "gbrain")]])

    class Repository:
        def __init__(self, configured, claimed_outbox):
            pass

        def create_batch(self, jobs, **kwargs):
            assert kwargs["lease_seconds"] == 180
            return SimpleNamespace(id="batch")

        async def execute_batch(self, batch_id, **kwargs):
            assert kwargs["lease_seconds"] == 180
            while not outbox.renewals:
                await asyncio.sleep(0)

    _patch_gbrain_repository(monkeypatch, Repository)
    monkeypatch.setattr(projection_worker, "PROJECTION_LEASE_RENEW_SECONDS", 0.001)
    worker = projection_worker.ProjectionWorker(settings, outbox=outbox, now=lambda: NOW)

    result = asyncio.run(worker.run_once("gbrain"))

    assert result.succeeded == 1
    assert outbox.claims[0]["lease_seconds"] == 180
    assert outbox.renewals[0]["lease_seconds"] == 180
    assert outbox.renewals[0]["now"] is NOW


def test_rag_renews_every_claimed_job_while_the_first_projection_is_active(
    settings, monkeypatch
):
    outbox = ScriptedOutbox(
        rag=[[_job("rag-first", "rag"), _job("rag-waiting", "rag")]]
    )
    first_started = threading.Event()
    release_first = threading.Event()

    class Projector:
        def __init__(self, configured, claimed_outbox):
            pass

        def project(self, job, worker_id):
            if job.id == "rag-first":
                first_started.set()
                if not release_first.wait(timeout=2):
                    raise AssertionError("test did not release the first RAG projection")

    monkeypatch.setattr(projection_worker, "WikiRagProjector", Projector)
    monkeypatch.setattr(projection_worker, "PROJECTION_LEASE_RENEW_SECONDS", 0.001)
    worker = projection_worker.ProjectionWorker(settings, outbox=outbox, now=lambda: NOW)

    async def wait_for_waiting_job_renewal() -> None:
        while not any(
            renewal["job_id"] == "rag-waiting" for renewal in outbox.renewals
        ):
            await asyncio.sleep(0)

    async def exercise() -> None:
        active = asyncio.create_task(worker.run_once("rag"))
        while not first_started.is_set():
            await asyncio.sleep(0)
        try:
            await asyncio.wait_for(wait_for_waiting_job_renewal(), timeout=0.1)
        finally:
            release_first.set()
        result = await active
        assert result.succeeded == 2

    asyncio.run(exercise())


def test_worker_recovers_an_expired_running_job(settings, monkeypatch):
    outbox = ProjectionOutbox(settings)
    with connect_app_write(settings) as conn:
        job_id = outbox.enqueue(
            conn,
            target="rag",
            operation="upsert",
            page_id="page-expired",
            revision_id="revision-expired",
            projection_epoch=1,
            payload={"path": "wiki/expired.md"},
        )
        conn.execute(
            """
            UPDATE knowledge_projection_jobs
            SET status='running',attempts=1,lease_owner='dead-worker',lease_expires_at=?
            WHERE id=?
            """,
            ((NOW - timedelta(seconds=1)).isoformat(), job_id),
        )

    class Projector:
        def __init__(self, configured, claimed_outbox):
            self.outbox = claimed_outbox

        def project(self, job, worker_id):
            assert job.attempts == 2
            assert self.outbox.mark_succeeded(job.id, worker_id) is True

    monkeypatch.setattr(projection_worker, "WikiRagProjector", Projector)
    worker = projection_worker.ProjectionWorker(settings, outbox=outbox, now=lambda: NOW)

    result = asyncio.run(worker.run_once("rag"))

    with connect_app(settings) as conn:
        row = conn.execute(
            "SELECT status,attempts,lease_owner FROM knowledge_projection_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    assert result.succeeded == 1
    assert (row["status"], row["attempts"], row["lease_owner"]) == (
        "succeeded",
        2,
        None,
    )


def test_worker_rejects_a_late_rag_result_after_lease_loss(settings, monkeypatch):
    outbox = ProjectionOutbox(settings)
    with connect_app_write(settings) as conn:
        job_id = outbox.enqueue(
            conn,
            target="rag",
            operation="upsert",
            page_id="page-late",
            revision_id="revision-late",
            projection_epoch=1,
            payload={"path": "wiki/late.md"},
        )

    class Projector:
        def __init__(self, configured, claimed_outbox):
            pass

        def project(self, job, worker_id):
            with connect_app_write(settings) as conn:
                conn.execute(
                    "UPDATE knowledge_projection_jobs SET lease_owner='new-worker' WHERE id=?",
                    (job.id,),
                )
            raise RagProjectionLeaseLost(job.id)

    monkeypatch.setattr(projection_worker, "WikiRagProjector", Projector)
    worker = projection_worker.ProjectionWorker(settings, outbox=outbox, now=lambda: NOW)

    result = asyncio.run(worker.run_once("rag"))

    with connect_app(settings) as conn:
        row = conn.execute(
            "SELECT status,lease_owner,last_error FROM knowledge_projection_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    assert (result.succeeded, result.failed) == (0, 0)
    assert (row["status"], row["lease_owner"], row["last_error"]) == (
        "running",
        "new-worker",
        None,
    )


def test_job_enqueued_during_active_gbrain_batch_remains_pending(settings, monkeypatch):
    first = _job("first", "gbrain")
    second = _job("second", "gbrain")
    outbox = ScriptedOutbox(gbrain=[[first]])
    started = asyncio.Event()
    release = asyncio.Event()

    class Repository:
        def __init__(self, configured, claimed_outbox):
            pass

        def create_batch(self, jobs, **kwargs):
            assert [job.id for job in jobs] == ["first"]
            return SimpleNamespace(id="batch")

        async def execute_batch(self, batch_id, **kwargs):
            started.set()
            await release.wait()

    _patch_gbrain_repository(monkeypatch, Repository)
    worker = projection_worker.ProjectionWorker(settings, outbox=outbox, now=lambda: NOW)

    async def exercise() -> None:
        active = asyncio.create_task(worker.run_once("gbrain"))
        await started.wait()
        outbox.batches["gbrain"].append([second])
        await asyncio.sleep(0)
        assert len(outbox.claims) == 1
        assert outbox.batches["gbrain"] == [[second]]
        release.set()
        await active

    asyncio.run(exercise())


def test_worker_passes_injected_now_unchanged_to_mark_failed(settings, monkeypatch):
    outbox = ScriptedOutbox(rag=[[_job("broken", "rag")]])

    class Projector:
        def __init__(self, configured, claimed_outbox):
            pass

        def project(self, job, worker_id):
            raise RuntimeError("projection failed")

    monkeypatch.setattr(projection_worker, "WikiRagProjector", Projector)
    worker = projection_worker.ProjectionWorker(settings, outbox=outbox, now=lambda: NOW)

    result = asyncio.run(worker.run_once("rag"))

    assert result.failed == 1
    assert outbox.failures[0]["now"] is NOW
    assert "available_at" not in outbox.failures[0]


def test_missing_gbrain_endpoint_marks_failed_and_reports_degraded(settings, monkeypatch):
    settings.gbrain_endpoint = None
    outbox = ProjectionOutbox(settings)
    with connect_app_write(settings) as conn:
        job_id = outbox.enqueue(
            conn,
            target="gbrain",
            operation="upsert",
            page_id="page-unconfigured",
            revision_id="revision-unconfigured",
            projection_epoch=1,
            payload={"path": "wiki/unconfigured.md"},
        )

    def forbidden_client(configured):
        raise AssertionError("unconfigured GBrain must not create a projection client")

    monkeypatch.setattr(projection_worker, "GBrainProjectionClient", forbidden_client)
    worker = projection_worker.ProjectionWorker(settings, outbox=outbox, now=lambda: NOW)

    result = asyncio.run(worker.run_once("gbrain"))
    status = worker.snapshot()

    with connect_app(settings) as conn:
        row = conn.execute(
            "SELECT status,last_error,available_at FROM knowledge_projection_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    assert result.failed == 1
    assert row["status"] == "failed"
    assert "endpoint" in row["last_error"].lower()
    assert datetime.fromisoformat(row["available_at"]) == NOW + timedelta(seconds=5)
    assert status.gbrain["configured"] is False
    assert status.gbrain["degraded"] is True
    assert status.gbrain["failed"] == 1


def test_fastapi_lifespan_owns_worker(settings, monkeypatch):
    events: list[str] = []

    class Worker:
        def __init__(self, configured):
            assert configured is settings

        async def start(self):
            events.append("start")

        async def stop(self):
            events.append("stop")

    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(main_module, "ProjectionWorker", Worker)

    async def exercise() -> None:
        async with main_module.lifespan(main_module.app):
            assert isinstance(main_module.app.state.projection_worker, Worker)
            assert events == ["start"]

    asyncio.run(exercise())
    assert events == ["start", "stop"]
