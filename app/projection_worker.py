from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, TypeVar

from app.config import Settings
from app.db import connect_app
from app.gbrain_projection import (
    GBrainBatchRepository,
    GBrainProjectionClient,
    ProjectionLeaseLost as GBrainProjectionLeaseLost,
)
from app.projection_jobs import TERMINAL_STATUSES, ProjectionJob, ProjectionOutbox
from app.wiki_rag_projection import (
    ProjectionLeaseLost as RagProjectionLeaseLost,
    ProjectionSuperseded,
    WikiRagProjector,
)


logger = logging.getLogger(__name__)

PROJECTION_LEASE_SECONDS = 180
PROJECTION_LEASE_RENEW_SECONDS = 60

_T = TypeVar("_T")


@dataclass(frozen=True)
class WorkerRunResult:
    target: Literal["rag", "gbrain", "combined"]
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    superseded: int = 0

    @classmethod
    def combine(
        cls,
        rag: "WorkerRunResult",
        gbrain: "WorkerRunResult",
    ) -> "WorkerRunResult":
        return cls(
            target="combined",
            claimed=rag.claimed + gbrain.claimed,
            succeeded=rag.succeeded + gbrain.succeeded,
            failed=rag.failed + gbrain.failed,
            superseded=rag.superseded + gbrain.superseded,
        )


@dataclass(frozen=True)
class ProjectionWorkerStatus:
    worker_id: str
    running: bool
    rag: dict[str, int]
    gbrain: dict[str, int | bool]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def projection_worker_status(
    settings: Settings,
    worker_id: str,
    running: bool,
) -> ProjectionWorkerStatus:
    counts = {
        target: {status: 0 for status in ("pending", "running", "failed")}
        for target in ("rag", "gbrain")
    }
    with connect_app(settings) as conn:
        rows = conn.execute(
            """
            SELECT target,status,COUNT(*) AS count
            FROM knowledge_projection_jobs
            WHERE target IN ('rag','gbrain')
              AND status IN ('pending','running','failed')
            GROUP BY target,status
            """
        ).fetchall()
    for row in rows:
        target = str(row["target"])
        status = str(row["status"])
        if target in counts and status in counts[target]:
            counts[target][status] = int(row["count"])

    configured = bool(
        settings.gbrain_endpoint
        and settings.gbrain_projection_api_key
        and settings.gbrain_managed_source_id
    )
    gbrain: dict[str, int | bool] = {
        **counts["gbrain"],
        "configured": configured,
        "degraded": bool(counts["gbrain"]["failed"] or not configured),
    }
    return ProjectionWorkerStatus(
        worker_id=worker_id,
        running=running,
        rag=counts["rag"],
        gbrain=gbrain,
    )


class _ClaimLeaseLost(RuntimeError):
    def __init__(self, job_ids: Sequence[str]):
        self.job_ids = tuple(job_ids)
        super().__init__(f"projection claim lease lost: {', '.join(self.job_ids)}")


class ProjectionWorker:
    def __init__(
        self,
        settings: Settings,
        outbox: ProjectionOutbox | None = None,
        now: Callable[[], datetime] = utc_now,
    ):
        self.settings = settings
        self.outbox = outbox or ProjectionOutbox(settings)
        self._now = now
        self.worker_id = f"projection-{uuid.uuid4().hex}"
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._gbrain_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._tasks or not self.settings.projection_worker_enabled:
            return
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._loop("rag"), name="projection-rag"),
            asyncio.create_task(self._loop("gbrain"), name="projection-gbrain"),
        ]

    async def stop(self) -> None:
        self._stop.set()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run_once(self, target: str | None = None) -> WorkerRunResult:
        if target == "rag":
            return await self._run_rag_once()
        if target == "gbrain":
            async with self._gbrain_lock:
                return await self._run_gbrain_once()
        if target is not None:
            raise ValueError(f"invalid projection target: {target}")
        rag = await self._run_rag_once()
        async with self._gbrain_lock:
            gbrain = await self._run_gbrain_once()
        return WorkerRunResult.combine(rag, gbrain)

    def snapshot(self) -> ProjectionWorkerStatus:
        return projection_worker_status(self.settings, self.worker_id, bool(self._tasks))

    async def _loop(self, target: Literal["rag", "gbrain"]) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once(target)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("projection worker loop failed for target %s", target)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(0.01, float(self.settings.projection_poll_seconds)),
                )
            except TimeoutError:
                pass

    async def _run_rag_once(self) -> WorkerRunResult:
        jobs = self._claim("rag")
        if not jobs:
            return WorkerRunResult(target="rag")

        projector = WikiRagProjector(self.settings, self.outbox)
        succeeded = 0
        failed = 0
        superseded = 0
        active_jobs = list(jobs)

        async def project_claims() -> None:
            nonlocal succeeded, failed, superseded
            for job in jobs:
                try:
                    projection_task = asyncio.create_task(
                        asyncio.to_thread(projector.project, job, self.worker_id)
                    )
                    try:
                        await asyncio.shield(projection_task)
                    except asyncio.CancelledError:
                        await asyncio.gather(projection_task, return_exceptions=True)
                        raise
                    succeeded += 1
                except ProjectionSuperseded:
                    superseded += 1
                except RagProjectionLeaseLost:
                    continue
                except Exception as exc:
                    if self.outbox.mark_failed(
                        job.id,
                        self.worker_id,
                        str(exc),
                        now=self._now(),
                    ):
                        failed += 1
                finally:
                    active_jobs.remove(job)

        try:
            await self._with_lease_renewal(active_jobs, project_claims())
        except _ClaimLeaseLost:
            pass
        return WorkerRunResult(
            target="rag",
            claimed=len(jobs),
            succeeded=succeeded,
            failed=failed,
            superseded=superseded,
        )

    async def _run_gbrain_once(self) -> WorkerRunResult:
        jobs = self._claim("gbrain")
        if not jobs:
            return WorkerRunResult(target="gbrain")

        configuration_error = self._gbrain_configuration_error()
        if configuration_error is not None:
            failed = self._fail_claims(jobs, configuration_error)
            return WorkerRunResult(target="gbrain", claimed=len(jobs), failed=failed)

        repository = GBrainBatchRepository(self.settings, self.outbox)
        try:
            batch = repository.create_batch(
                jobs,
                worker_id=self.worker_id,
                lease_seconds=PROJECTION_LEASE_SECONDS,
                now=self._now(),
            )
            if batch is None:
                return WorkerRunResult(
                    target="gbrain",
                    claimed=len(jobs),
                    superseded=len(jobs),
                )
            client = GBrainProjectionClient(self.settings)
            await self._with_lease_renewal(
                jobs,
                repository.execute_batch(
                    batch.id,
                    worker_id=self.worker_id,
                    client=client,
                    lease_seconds=PROJECTION_LEASE_SECONDS,
                    now=self._now(),
                ),
            )
        except (GBrainProjectionLeaseLost, _ClaimLeaseLost):
            return WorkerRunResult(target="gbrain", claimed=len(jobs))
        except Exception as exc:
            failed = self._fail_claims(jobs, str(exc))
            return WorkerRunResult(target="gbrain", claimed=len(jobs), failed=failed)
        return self._gbrain_terminal_result(jobs)

    def _claim(self, target: Literal["rag", "gbrain"]) -> list[ProjectionJob]:
        return self.outbox.claim(
            target=target,
            worker_id=self.worker_id,
            limit=max(1, int(self.settings.projection_claim_limit)),
            lease_seconds=PROJECTION_LEASE_SECONDS,
            now=self._now(),
        )

    def _fail_claims(self, jobs: Sequence[ProjectionJob], error: str) -> int:
        failed = 0
        for job in jobs:
            if self.outbox.mark_failed(
                job.id,
                self.worker_id,
                error,
                now=self._now(),
            ):
                failed += 1
        return failed

    def _gbrain_configuration_error(self) -> str | None:
        missing = [
            name
            for name, value in (
                ("gbrain_endpoint", self.settings.gbrain_endpoint),
                ("gbrain_projection_api_key", self.settings.gbrain_projection_api_key),
                ("gbrain_managed_source_id", self.settings.gbrain_managed_source_id),
            )
            if not value
        ]
        if not missing:
            return None
        return f"GBrain projection configuration missing: {', '.join(missing)}"

    def _gbrain_terminal_result(self, jobs: Sequence[ProjectionJob]) -> WorkerRunResult:
        job_ids = [job.id for job in jobs]
        placeholders = ",".join("?" for _ in job_ids)
        with connect_app(self.settings) as conn:
            rows = conn.execute(
                f"SELECT id,status FROM knowledge_projection_jobs WHERE id IN ({placeholders})",
                job_ids,
            ).fetchall()
        statuses_by_id = {str(row["id"]): str(row["status"]) for row in rows}
        missing = [job_id for job_id in job_ids if job_id not in statuses_by_id]
        if missing:
            raise RuntimeError(
                f"GBrain terminal result missing claimed jobs: {', '.join(missing)}"
            )
        non_terminal = [
            f"{job_id}={statuses_by_id[job_id]}"
            for job_id in job_ids
            if statuses_by_id[job_id] not in TERMINAL_STATUSES
        ]
        if non_terminal:
            raise RuntimeError(
                "GBrain terminal result has non-terminal claimed jobs: "
                + ", ".join(non_terminal)
            )
        statuses = [statuses_by_id[job_id] for job_id in job_ids]
        return WorkerRunResult(
            target="gbrain",
            claimed=len(jobs),
            succeeded=statuses.count("succeeded"),
            failed=statuses.count("failed"),
            superseded=statuses.count("superseded"),
        )

    async def _with_lease_renewal(
        self,
        jobs: Sequence[ProjectionJob],
        operation: Awaitable[_T],
    ) -> _T:
        finished = asyncio.Event()
        operation_task = asyncio.create_task(operation)
        renewal_task = asyncio.create_task(self._renew_claims(jobs, finished))
        try:
            done, _ = await asyncio.wait(
                {operation_task, renewal_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if renewal_task in done:
                operation_task.cancel()
                await asyncio.gather(operation_task, return_exceptions=True)
                renewal_task.result()
                raise RuntimeError("projection lease renewal stopped unexpectedly")

            finished.set()
            await renewal_task
            return operation_task.result()
        finally:
            finished.set()
            for task in (operation_task, renewal_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation_task, renewal_task, return_exceptions=True)

    async def _renew_claims(
        self,
        jobs: Sequence[ProjectionJob],
        finished: asyncio.Event,
    ) -> None:
        while not finished.is_set():
            try:
                await asyncio.wait_for(
                    finished.wait(),
                    timeout=PROJECTION_LEASE_RENEW_SECONDS,
                )
            except TimeoutError:
                pass
            if finished.is_set():
                return
            lost = [
                job.id
                for job in jobs
                if not self.outbox.renew_lease(
                    job.id,
                    self.worker_id,
                    PROJECTION_LEASE_SECONDS,
                    now=self._now(),
                )
            ]
            if lost:
                raise _ClaimLeaseLost(lost)
