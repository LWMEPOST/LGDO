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
import app.vault_sync as vault_sync_module
from app.config import Settings
from app.db import connect_app, connect_app_write, init_app_db, json_dump
from app.projection_jobs import ProjectionJob, ProjectionOutbox
from app.vault_events import VaultEventStore
from app.vault_sync import VaultSyncService
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


def _persist_claimed_jobs(
    settings: Settings,
    jobs: list[ProjectionJob],
    worker_id: str,
) -> None:
    with connect_app_write(settings) as conn:
        for job in jobs:
            existing = conn.execute(
                "SELECT status,lease_owner FROM knowledge_projection_jobs WHERE id=?",
                (job.id,),
            ).fetchone()
            if existing is not None:
                assert (existing["status"], existing["lease_owner"]) == (
                    "running",
                    worker_id,
                )
                continue
            conn.execute(
                """
                INSERT INTO knowledge_projection_jobs(
                  id,idempotency_key,target,operation,page_id,revision_id,
                  projection_epoch,payload_json,status,attempts,available_at,
                  lease_owner,lease_expires_at,last_error,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job.id,
                    job.idempotency_key,
                    job.target,
                    job.operation,
                    job.page_id,
                    job.revision_id,
                    job.projection_epoch,
                    json_dump(job.payload),
                    "running",
                    job.attempts,
                    job.available_at,
                    worker_id,
                    job.lease_expires_at,
                    None,
                    job.created_at,
                    job.updated_at,
                ),
            )


def _finish_test_repository_jobs(
    settings: Settings,
    jobs: list[ProjectionJob],
    worker_id: str,
) -> None:
    outbox = ProjectionOutbox(settings)
    with connect_app_write(settings) as conn:
        for job in jobs:
            row = conn.execute(
                "SELECT status,lease_owner FROM knowledge_projection_jobs WHERE id=?",
                (job.id,),
            ).fetchone()
            if row["status"] != "running":
                continue
            assert row["lease_owner"] == worker_id
            assert outbox.finish_claimed(
                conn,
                job_id=job.id,
                worker_id=worker_id,
                status="succeeded",
            )


def _patch_gbrain_repository(
    monkeypatch,
    repository_type: type,
    settings: Settings,
) -> None:
    class PersistingRepository(repository_type):
        def create_batch(self, jobs, **kwargs):
            self._claimed_jobs = list(jobs)
            self._worker_id = str(kwargs["worker_id"])
            _persist_claimed_jobs(settings, self._claimed_jobs, self._worker_id)
            return super().create_batch(jobs, **kwargs)

        async def execute_batch(self, batch_id, **kwargs):
            result = await super().execute_batch(batch_id, **kwargs)
            _finish_test_repository_jobs(
                settings,
                self._claimed_jobs,
                self._worker_id,
            )
            return result

    monkeypatch.setattr(
        projection_worker,
        "GBrainBatchRepository",
        PersistingRepository,
    )
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

    _patch_gbrain_repository(monkeypatch, Repository, settings)
    worker = projection_worker.ProjectionWorker(settings, outbox=outbox, now=lambda: NOW)

    async def exercise() -> None:
        Repository.started = asyncio.Event()
        Repository.cancelled = asyncio.Event()
        await worker.start()
        await asyncio.wait_for(Repository.started.wait(), timeout=1)
        await worker.stop()
        assert Repository.cancelled.is_set()

    asyncio.run(exercise())


def test_stop_waits_for_active_rag_thread_without_blocking_loop(settings, monkeypatch):
    outbox = ScriptedOutbox(rag=[[_job("active-rag", "rag")]])
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class Projector:
        def __init__(self, configured, claimed_outbox):
            pass

        def project(self, job, worker_id):
            started.set()
            if not release.wait(timeout=2):
                raise AssertionError("test did not release the RAG projection")
            finished.set()

    monkeypatch.setattr(projection_worker, "WikiRagProjector", Projector)
    worker = projection_worker.ProjectionWorker(settings, outbox=outbox, now=lambda: NOW)

    async def exercise() -> None:
        await worker.start()
        while not started.is_set():
            await asyncio.sleep(0)
        stopping = asyncio.create_task(worker.stop())
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(stopping), timeout=0.05)
            probe_ran = False

            async def probe() -> None:
                nonlocal probe_ran
                await asyncio.sleep(0)
                probe_ran = True

            await asyncio.wait_for(probe(), timeout=0.1)
            assert probe_ran is True
        finally:
            release.set()
            await asyncio.wait_for(stopping, timeout=1)
        assert finished.is_set()

    asyncio.run(exercise())


def test_stop_renews_rag_claim_until_physical_thread_exits(settings, monkeypatch):
    initial_now = NOW
    renewed_at = NOW + timedelta(seconds=2)
    competitor_at = NOW + timedelta(seconds=4)
    clock = [initial_now]
    renewed_before_stop = threading.Event()
    renewed_after_stop = threading.Event()

    class ObservedOutbox(ProjectionOutbox):
        def renew_lease(
            self,
            job_id,
            worker_id,
            lease_seconds,
            now=None,
        ):
            renewed = super().renew_lease(
                job_id,
                worker_id,
                lease_seconds,
                now=now,
            )
            if renewed and now == initial_now:
                renewed_before_stop.set()
            if renewed and now == renewed_at:
                renewed_after_stop.set()
            return renewed

    outbox = ObservedOutbox(settings)
    with connect_app_write(settings) as conn:
        job_id = outbox.enqueue(
            conn,
            target="rag",
            operation="upsert",
            page_id="page-stop-renewal",
            revision_id="revision-stop-renewal",
            projection_epoch=1,
            payload={"path": "wiki/stop-renewal.md"},
        )

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    finish_results: list[bool] = []

    class Projector:
        def __init__(self, configured, claimed_outbox):
            self.outbox = claimed_outbox

        def project(self, job, worker_id):
            started.set()
            if not release.wait(timeout=2):
                raise AssertionError("test did not release the RAG projection")
            finish_results.append(self.outbox.mark_succeeded(job.id, worker_id))
            finished.set()

    monkeypatch.setattr(projection_worker, "WikiRagProjector", Projector)
    monkeypatch.setattr(projection_worker, "PROJECTION_LEASE_SECONDS", 3)
    monkeypatch.setattr(projection_worker, "PROJECTION_LEASE_RENEW_SECONDS", 0.001)
    worker = projection_worker.ProjectionWorker(
        settings,
        outbox=outbox,
        now=lambda: clock[0],
    )

    async def wait_for_threading_event(event: threading.Event) -> None:
        while not event.is_set():
            await asyncio.sleep(0)

    async def exercise() -> None:
        await worker.start()
        worker_tasks = tuple(worker._tasks)
        rag_task = next(
            task for task in worker_tasks if task.get_name() == "projection-rag"
        )
        while not started.is_set():
            await asyncio.sleep(0)
        await asyncio.wait_for(
            wait_for_threading_event(renewed_before_stop),
            timeout=1,
        )

        stopping = asyncio.create_task(worker.stop())
        try:
            while worker._tasks or rag_task.cancelling() == 0:
                await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert stopping.done() is False
            assert rag_task.cancel() is True
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert stopping.done() is False

            clock[0] = renewed_at
            try:
                await asyncio.wait_for(
                    wait_for_threading_event(renewed_after_stop),
                    timeout=0.1,
                )
            except TimeoutError:
                pass

            claimed = ProjectionOutbox(settings).claim(
                target="rag",
                worker_id="worker-b",
                limit=1,
                lease_seconds=30,
                now=competitor_at,
            )
            assert claimed == []
            assert renewed_after_stop.is_set()
        finally:
            release.set()
            await asyncio.wait_for(asyncio.shield(stopping), timeout=1)

        assert finished.is_set()
        assert finish_results == [True]
        assert all(task.done() for task in worker_tasks)
        assert worker._tasks == []

    asyncio.run(exercise())

    with connect_app(settings) as conn:
        row = conn.execute(
            """
            SELECT status,attempts,lease_owner,lease_expires_at
            FROM knowledge_projection_jobs WHERE id=?
            """,
            (job_id,),
        ).fetchone()
    assert tuple(row) == ("succeeded", 1, None, None)


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

    _patch_gbrain_repository(monkeypatch, Repository, settings)
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

    _patch_gbrain_repository(monkeypatch, Repository, settings)
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

    _patch_gbrain_repository(monkeypatch, Repository, settings)
    monkeypatch.setattr(projection_worker, "PROJECTION_LEASE_RENEW_SECONDS", 0.001)
    worker = projection_worker.ProjectionWorker(settings, outbox=outbox, now=lambda: NOW)

    result = asyncio.run(worker.run_once("gbrain"))

    assert result.succeeded == 1
    assert outbox.claims[0]["lease_seconds"] == 180
    assert outbox.renewals[0]["lease_seconds"] == 180
    assert outbox.renewals[0]["now"] is NOW


def test_gbrain_renewal_ignores_members_completed_by_an_earlier_segment(
    settings,
    monkeypatch,
):
    outbox = ProjectionOutbox(settings)
    with connect_app_write(settings) as conn:
        job_ids = [
            outbox.enqueue(
                conn,
                target="gbrain",
                operation="upsert",
                page_id=f"page-{index}",
                revision_id=f"revision-{index}",
                projection_epoch=1,
                payload={"path": f"wiki/page-{index}.md"},
            )
            for index in range(2)
        ]

    class Repository:
        cancelled = False

        def __init__(self, configured, claimed_outbox):
            self.outbox = claimed_outbox
            self.jobs = []

        def create_batch(self, jobs, **kwargs):
            self.jobs = list(jobs)
            return SimpleNamespace(id="segmented-batch")

        async def execute_batch(self, batch_id, **kwargs):
            worker_id = kwargs["worker_id"]
            with connect_app_write(settings) as conn:
                assert self.outbox.finish_claimed(
                    conn,
                    job_id=self.jobs[0].id,
                    worker_id=worker_id,
                    status="succeeded",
                )
            try:
                await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                Repository.cancelled = True
                raise
            with connect_app_write(settings) as conn:
                assert self.outbox.finish_claimed(
                    conn,
                    job_id=self.jobs[1].id,
                    worker_id=worker_id,
                    status="succeeded",
                )

    _patch_gbrain_repository(monkeypatch, Repository, settings)
    monkeypatch.setattr(projection_worker, "PROJECTION_LEASE_RENEW_SECONDS", 0.001)
    worker = projection_worker.ProjectionWorker(settings, outbox=outbox, now=lambda: NOW)

    result = asyncio.run(worker.run_once("gbrain"))

    with connect_app(settings) as conn:
        rows = conn.execute(
            "SELECT id,status FROM knowledge_projection_jobs ORDER BY id"
        ).fetchall()
    assert Repository.cancelled is False
    assert result.succeeded == 2
    assert {str(row["id"]): str(row["status"]) for row in rows} == {
        job_id: "succeeded" for job_id in job_ids
    }


def test_gbrain_terminal_result_rejects_a_missing_claimed_row(settings):
    outbox = ProjectionOutbox(settings)
    worker = projection_worker.ProjectionWorker(settings, outbox=outbox, now=lambda: NOW)
    with connect_app_write(settings) as conn:
        job_id = outbox.enqueue(
            conn,
            target="gbrain",
            operation="upsert",
            page_id="page-present",
            revision_id="revision-present",
            projection_epoch=1,
            payload={"path": "wiki/present.md"},
        )
    present = outbox.claim(
        target="gbrain",
        worker_id=worker.worker_id,
        limit=1,
        lease_seconds=180,
        now=NOW,
    )[0]
    assert outbox.mark_succeeded(job_id, worker.worker_id) is True

    with pytest.raises(RuntimeError, match="missing.*gbrain-missing"):
        worker._gbrain_terminal_result(
            [present, _job("gbrain-missing", "gbrain")]
        )


def test_gbrain_terminal_result_rejects_a_non_terminal_claimed_row(settings):
    outbox = ProjectionOutbox(settings)
    worker = projection_worker.ProjectionWorker(settings, outbox=outbox, now=lambda: NOW)
    with connect_app_write(settings) as conn:
        outbox.enqueue(
            conn,
            target="gbrain",
            operation="upsert",
            page_id="page-running",
            revision_id="revision-running",
            projection_epoch=1,
            payload={"path": "wiki/running.md"},
        )
    running = outbox.claim(
        target="gbrain",
        worker_id=worker.worker_id,
        limit=1,
        lease_seconds=180,
        now=NOW,
    )[0]

    with pytest.raises(RuntimeError, match="non-terminal.*running"):
        worker._gbrain_terminal_result([running])


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

    _patch_gbrain_repository(monkeypatch, Repository, settings)
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
    settings.gbrain_enabled = True
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


def test_reconcile_lease_loss_cancels_inventory_before_finish(
    settings,
    monkeypatch,
):
    settings.vault_reconcile_lease_seconds = 0.03
    events = VaultEventStore(settings)
    service = VaultSyncService(settings, events=events)
    job = events.request_reconcile("admin")
    renew_called = threading.Event()
    finishes: list[str] = []

    def lose_lease(*_args, **_kwargs):
        renew_called.set()
        return False

    original_finish = events.finish_reconcile

    def record_finish(job_id, owner, result):
        finishes.append(job_id)
        return original_finish(job_id, owner, result)

    monkeypatch.setattr(events, "renew_reconcile", lose_lease)
    monkeypatch.setattr(events, "finish_reconcile", record_finish)

    async def exercise() -> None:
        inventory_started = asyncio.Event()
        inventory_cancelled = asyncio.Event()

        async def blocked_inventory(**_kwargs):
            inventory_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                inventory_cancelled.set()
                raise

        monkeypatch.setattr(service, "reconcile_startup", blocked_inventory)
        task = asyncio.create_task(service._run_reconcile_job(job.id))
        await inventory_started.wait()
        with pytest.raises(vault_sync_module.ReconcileLeaseLost):
            await task
        assert inventory_cancelled.is_set()

    asyncio.run(exercise())
    persisted = events.get_reconcile(job.id)
    assert renew_called.is_set()
    assert finishes == []
    assert persisted is not None
    assert persisted.status == "running"
    assert persisted.lease_owner == service._reconcile_owner


def test_reconcile_heartbeat_error_is_lease_loss_without_failing_job(
    settings,
    monkeypatch,
):
    settings.vault_reconcile_lease_seconds = 0.03
    events = VaultEventStore(settings)
    service = VaultSyncService(settings, events=events)
    job = events.request_reconcile("admin")
    inventory_cancelled = threading.Event()
    failures: list[str] = []
    finishes: list[str] = []

    def broken_renew(*_args, **_kwargs):
        raise RuntimeError("heartbeat database unavailable")

    def forbidden_fail(job_id, *_args, **_kwargs):
        failures.append(job_id)
        return True

    def forbidden_finish(job_id, *_args, **_kwargs):
        finishes.append(job_id)
        return True

    monkeypatch.setattr(events, "renew_reconcile", broken_renew)
    monkeypatch.setattr(events, "fail_reconcile", forbidden_fail)
    monkeypatch.setattr(events, "finish_reconcile", forbidden_finish)

    async def blocked_inventory(**_kwargs):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            inventory_cancelled.set()
            raise

    monkeypatch.setattr(service, "reconcile_startup", blocked_inventory)

    async def exercise() -> None:
        with pytest.raises(vault_sync_module.ReconcileLeaseLost):
            await service._run_reconcile_job(job.id)

    asyncio.run(exercise())
    persisted = events.get_reconcile(job.id)
    assert inventory_cancelled.is_set()
    assert failures == []
    assert finishes == []
    assert persisted is not None
    assert persisted.status == "running"
    assert persisted.lease_owner == service._reconcile_owner


def test_reconcile_heartbeat_prevents_takeover_during_long_inventory(
    settings,
    monkeypatch,
):
    settings.vault_reconcile_lease_seconds = 1
    events = VaultEventStore(settings)
    service = VaultSyncService(settings, events=events)
    job = events.request_reconcile("admin")
    renewed_four_times = threading.Event()
    renew_times: list[datetime] = []
    original_renew = events.renew_reconcile

    def tracked_renew(job_id, owner, *, now, lease_seconds):
        renewed = original_renew(
            job_id,
            owner,
            now=now,
            lease_seconds=lease_seconds,
        )
        if renewed:
            renew_times.append(now)
            if len(renew_times) >= 4:
                renewed_four_times.set()
        return renewed

    monkeypatch.setattr(events, "renew_reconcile", tracked_renew)

    async def exercise() -> None:
        release_inventory = asyncio.Event()

        async def blocked_inventory(**_kwargs):
            await release_inventory.wait()
            return {"ingested": 1, "failed": 0, "projection_jobs": 0}

        monkeypatch.setattr(service, "reconcile_startup", blocked_inventory)
        task = asyncio.create_task(service._run_reconcile_job(job.id))
        assert await asyncio.to_thread(renewed_four_times.wait, 5)
        takeover = await asyncio.to_thread(
            events.claim_reconcile,
            job.id,
            "owner-b",
            now=renew_times[-1],
            lease_seconds=1,
        )
        assert takeover is False
        release_inventory.set()
        await task

    asyncio.run(exercise())
    completed = events.get_reconcile(job.id)
    assert len(renew_times) >= 4
    assert completed is not None
    assert completed.status == "succeeded"
    assert completed.result == {
        "ingested": 1,
        "failed": 0,
        "projection_jobs": 0,
    }


@pytest.mark.parametrize("prequeued", [False, True])
def test_startup_and_persisted_reconcile_share_one_global_authority(
    settings,
    monkeypatch,
    prequeued,
):
    if prequeued:
        VaultEventStore(settings).request_reconcile("admin")
    first = VaultSyncService(settings)
    second = VaultSyncService(settings)
    inventory_calls: list[str] = []
    claim_attempts = 0
    two_claims = threading.Event()
    original_claim = VaultEventStore.claim_reconcile

    def tracked_claim(store, *args, **kwargs):
        nonlocal claim_attempts
        claimed = original_claim(store, *args, **kwargs)
        claim_attempts += 1
        if claim_attempts >= 2:
            two_claims.set()
        return claimed

    monkeypatch.setattr(VaultEventStore, "claim_reconcile", tracked_claim)

    async def fake_inventory(label: str, **_kwargs):
        inventory_calls.append(label)
        assert await asyncio.to_thread(two_claims.wait, 2)
        return {"ingested": 1, "failed": 0, "projection_jobs": 0}

    monkeypatch.setattr(
        first,
        "reconcile_startup",
        lambda **kwargs: fake_inventory("first", **kwargs),
    )
    monkeypatch.setattr(
        second,
        "reconcile_startup",
        lambda **kwargs: fake_inventory("second", **kwargs),
    )
    watcher_starts: list[str] = []

    class Watcher:
        def __init__(self, label: str, store: VaultEventStore):
            self.label = label
            self.store = store

        async def start(self):
            latest = self.store.latest_reconcile()
            assert latest is not None
            watcher_starts.append(f"{self.label}:{latest.status}")

        async def stop(self):
            return None

    first._watcher = Watcher("first", first.events)
    second._watcher = Watcher("second", second.events)

    async def boot(service: VaultSyncService):
        result = await service.reconcile_before_watcher_start()
        await service.start()
        return result

    async def exercise() -> tuple[dict[str, int], dict[str, int]]:
        results = await asyncio.gather(boot(first), boot(second))
        await asyncio.gather(first.stop(), second.stop())
        return results[0], results[1]

    first_result, second_result = asyncio.run(exercise())
    assert inventory_calls in (["first"], ["second"])
    assert first_result == second_result == {
        "ingested": 1,
        "failed": 0,
        "projection_jobs": 0,
    }
    assert sorted(watcher_starts) == ["first:succeeded", "second:succeeded"]
    assert first._reconcile_tasks == {}
    assert second._reconcile_tasks == {}


def test_stop_cancels_owned_reconcile_and_requeues_its_lease(
    settings,
    monkeypatch,
):
    events = VaultEventStore(settings)
    service = VaultSyncService(settings, events=events)
    inventory_started = threading.Event()
    inventory_cancelled = threading.Event()

    async def blocked_inventory(**_kwargs):
        inventory_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            inventory_cancelled.set()
            raise

    monkeypatch.setattr(service, "reconcile_startup", blocked_inventory)

    async def exercise():
        job = service.request_reconcile("admin")
        assert await asyncio.to_thread(inventory_started.wait, 2)
        await service.stop()
        await service.stop()
        return job

    job = asyncio.run(exercise())
    persisted = events.get_reconcile(job.id)
    assert inventory_cancelled.is_set()
    assert persisted is not None
    assert persisted.status == "queued"
    assert persisted.lease_owner is None
    assert service._reconcile_tasks == {}


def test_vault_runtime_reports_idle_watcher_as_running(settings):
    service = VaultSyncService(settings)
    calls: list[str] = []

    class Watcher:
        async def start(self):
            calls.append("start")

        async def stop(self):
            calls.append("stop")

    service._watcher = Watcher()

    async def exercise() -> None:
        await service.start()
        assert service.snapshot()["running"] is True
        await service.stop()
        assert service.snapshot()["running"] is False

    asyncio.run(exercise())
    assert calls == ["start", "stop"]


def test_cancel_during_reconcile_claim_requeues_the_acquired_lease(
    settings,
    monkeypatch,
):
    events = VaultEventStore(settings)
    service = VaultSyncService(settings, events=events)
    job = events.request_reconcile("admin")
    claim_entered = threading.Event()
    release_claim = threading.Event()
    original_claim = events.claim_reconcile

    def delayed_claim(*args, **kwargs):
        claim_entered.set()
        assert release_claim.wait(timeout=2)
        return original_claim(*args, **kwargs)

    monkeypatch.setattr(events, "claim_reconcile", delayed_claim)

    async def exercise() -> None:
        task = asyncio.create_task(service._run_reconcile_job(job.id))
        assert await asyncio.to_thread(claim_entered.wait, 2)
        task.cancel()
        release_claim.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    persisted = events.get_reconcile(job.id)
    assert persisted is not None
    assert persisted.status == "queued"
    assert persisted.lease_owner is None


def test_cancel_during_reconcile_failure_fencing_preserves_cancellation(
    settings,
    monkeypatch,
):
    events = VaultEventStore(settings)
    service = VaultSyncService(settings, events=events)
    job = events.request_reconcile("admin")
    fail_entered = threading.Event()
    release_fail = threading.Event()
    original_fail = events.fail_reconcile

    async def failed_inventory(**_kwargs):
        raise RuntimeError("inventory failed")

    def delayed_fail(*args, **kwargs):
        fail_entered.set()
        assert release_fail.wait(timeout=2)
        return original_fail(*args, **kwargs)

    monkeypatch.setattr(service, "reconcile_startup", failed_inventory)
    monkeypatch.setattr(events, "fail_reconcile", delayed_fail)

    async def exercise() -> None:
        task = asyncio.create_task(service._run_reconcile_job(job.id))
        assert await asyncio.to_thread(fail_entered.wait, 2)
        task.cancel()
        release_fail.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    persisted = events.get_reconcile(job.id)
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.error_summary == "inventory failed"


def test_cancel_during_heartbeat_cleanup_requeues_successful_inventory(
    settings,
    monkeypatch,
):
    events = VaultEventStore(settings)
    service = VaultSyncService(settings, events=events)
    job = events.request_reconcile("admin")

    async def completed_inventory(**_kwargs):
        return {"ingested": 1, "failed": 0, "projection_jobs": 0}

    async def exercise() -> None:
        heartbeat_cleanup_started = asyncio.Event()
        release_heartbeat = asyncio.Event()

        async def heartbeat(_job_id):
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                heartbeat_cleanup_started.set()
                await release_heartbeat.wait()

        monkeypatch.setattr(service, "reconcile_startup", completed_inventory)
        monkeypatch.setattr(service, "_reconcile_heartbeat", heartbeat)
        task = asyncio.create_task(service._run_reconcile_job(job.id))
        await heartbeat_cleanup_started.wait()
        task.cancel()
        release_heartbeat.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    persisted = events.get_reconcile(job.id)
    assert persisted is not None
    assert persisted.status == "queued"
    assert persisted.lease_owner is None


def test_reconcile_failure_fence_false_reports_lease_loss(
    settings,
    monkeypatch,
):
    events = VaultEventStore(settings)
    service = VaultSyncService(settings, events=events)
    job = events.request_reconcile("admin")
    original_fail = events.fail_reconcile

    async def failed_inventory(**_kwargs):
        raise RuntimeError("inventory failed after takeover")

    def takeover_before_fail(job_id, owner, error):
        with connect_app_write(settings) as conn:
            conn.execute(
                """
                UPDATE vault_reconcile_jobs
                SET lease_owner='owner-b'
                WHERE id=? AND status='running'
                """,
                (job_id,),
            )
        return original_fail(job_id, owner, error)

    monkeypatch.setattr(service, "reconcile_startup", failed_inventory)
    monkeypatch.setattr(events, "fail_reconcile", takeover_before_fail)

    async def exercise() -> None:
        with pytest.raises(vault_sync_module.ReconcileLeaseLost):
            await service._run_reconcile_job(job.id)

    asyncio.run(exercise())
    persisted = events.get_reconcile(job.id)
    assert persisted is not None
    assert persisted.status == "running"
    assert persisted.lease_owner == "owner-b"


def test_reconcile_finally_propagates_cancel_queued_after_finish(
    settings,
    monkeypatch,
):
    events = VaultEventStore(settings)
    service = VaultSyncService(settings, events=events)
    job = events.request_reconcile("admin")
    original_store_call = service._store_call

    async def completed_inventory(**_kwargs):
        return {"ingested": 1, "failed": 0, "projection_jobs": 0}

    async def cancel_after_finish(operation, *args, **kwargs):
        result = await original_store_call(operation, *args, **kwargs)
        if operation == events.finish_reconcile:
            task = asyncio.current_task()
            assert task is not None
            asyncio.get_running_loop().call_soon(task.cancel)
        return result

    monkeypatch.setattr(service, "reconcile_startup", completed_inventory)
    monkeypatch.setattr(service, "_store_call", cancel_after_finish)

    async def exercise() -> None:
        with pytest.raises(asyncio.CancelledError):
            await service._run_reconcile_job(job.id)

    asyncio.run(exercise())
    persisted = events.get_reconcile(job.id)
    assert persisted is not None
    assert persisted.status == "succeeded"


@pytest.mark.parametrize(
    "failure_point",
    [
        "intent_reconcile",
        "vault_reconcile",
        "projection_start",
        "vault_start",
    ],
)
def test_lifespan_cleans_both_runtime_owners_at_every_failure_point(
    settings,
    monkeypatch,
    failure_point,
):
    settings.projection_worker_enabled = True
    settings.vault_watch_enabled = True
    events: list[str] = []
    instances: dict[str, Any] = {}
    previous_state = dict(main_module.app.state._state)
    main_module.app.state._state.clear()

    class Executor:
        def __init__(self, configured):
            assert configured is settings

        def reconcile_all(self):
            events.append("intent.reconcile")
            if failure_point == "intent_reconcile":
                raise RuntimeError("intent reconcile failed")
            return []

    class VaultRuntime:
        def __init__(self, configured, *, intent_executor):
            assert configured is settings
            assert isinstance(intent_executor, Executor)
            self.stop_calls = 0
            self._reconcile_tasks = {"partial": object()}
            instances["vault"] = self

        async def reconcile_before_watcher_start(self):
            events.append("vault.reconcile")
            if failure_point == "vault_reconcile":
                raise RuntimeError("vault reconcile failed")
            return {"failed": 0}

        async def start(self):
            events.append("vault.start")
            if failure_point == "vault_start":
                raise RuntimeError("vault start failed")

        async def stop(self):
            events.append("vault.stop")
            self.stop_calls += 1
            self._reconcile_tasks.clear()

    class Worker:
        def __init__(self, configured):
            assert configured is settings
            self.stop_calls = 0
            self._tasks = [object()]
            instances["projection"] = self

        async def start(self):
            events.append("projection.start")
            if failure_point == "projection_start":
                raise RuntimeError("projection start failed")

        async def stop(self):
            events.append("projection.stop")
            self.stop_calls += 1
            self._tasks.clear()

    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(main_module, "IntentExecutor", Executor, raising=False)
    monkeypatch.setattr(main_module, "VaultSyncService", VaultRuntime, raising=False)
    monkeypatch.setattr(main_module, "ProjectionWorker", Worker)
    monkeypatch.setattr(main_module, "ensure_vault", lambda _path: None)
    monkeypatch.setattr(main_module, "init_app_db", lambda _settings: None)
    monkeypatch.setattr(
        main_module,
        "ensure_obsidian_vault",
        lambda _path: SimpleNamespace(
            installed=[".obsidian/app.json"],
            drifted=[],
            backup_dir=settings.vault_path / ".lgdo" / "secret-backup",
        ),
        raising=False,
    )

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="failed"):
            async with main_module.lifespan(main_module.app):
                raise AssertionError("failure point did not abort startup")

    try:
        asyncio.run(exercise())
        vault = instances["vault"]
        projection = instances["projection"]
        assert vault.stop_calls == 1
        assert projection.stop_calls == 1
        assert vault._reconcile_tasks == {}
        assert projection._tasks == []
        assert events[-2:] == ["vault.stop", "projection.stop"]
        assert "vault_sync" not in main_module.app.state._state
        assert "projection_worker" not in main_module.app.state._state
        assert "obsidian_status" not in main_module.app.state._state
    finally:
        main_module.app.state._state.clear()
        main_module.app.state._state.update(previous_state)
