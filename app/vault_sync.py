from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from app.config import Settings
from app.vault_events import (
    PendingVaultDelete,
    VaultEventStore,
    VaultOccurrenceConflict,
    VaultReconcileJob,
    VaultWatchOccurrence,
)
from app.vault_watcher import (
    VaultFsEvent,
    VaultWatchAdapter,
    canonical_wiki_path,
    iter_canonical_wiki_files,
    wait_for_stable_observation,
)
from app.vault_writer import IntentExecutor
from app.wiki_markdown import (
    FileObservationInput,
    FrontmatterLimits,
    MarkdownParseError,
    parse_wiki_bytes,
)
from app.wiki_revisions import MutationResult, RevisionConflict, WikiRevisionService


@dataclass
class PageLockEntry:
    lock: asyncio.Lock
    users: int = 0


StartupInventoryEntry = tuple[
    str,
    FileObservationInput | None,
    str | None,
]
StartupInventory = tuple[list[StartupInventoryEntry], set[str], int]


class ReconcileLeaseLost(RuntimeError):
    pass


class VaultSyncStopping(RuntimeError):
    pass


class VaultSyncService:
    def __init__(
        self,
        settings: Settings,
        *,
        revisions: WikiRevisionService | None = None,
        events: VaultEventStore | None = None,
        intent_executor: IntentExecutor | None = None,
    ):
        self.settings = settings
        self.revisions = revisions or WikiRevisionService(settings)
        self.events = events or VaultEventStore(settings)
        self.intent_executor = intent_executor or IntentExecutor(settings)
        self.limits = FrontmatterLimits(
            max_file_bytes=settings.vault_watch_max_file_bytes,
            max_prefix_bytes=settings.vault_watch_max_prefix_bytes,
        )
        self._semaphore = asyncio.Semaphore(settings.vault_watch_concurrency)
        self._page_locks: dict[str, PageLockEntry] = {}
        self._page_locks_guard = asyncio.Lock()
        self._reconcile_lock = asyncio.Lock()
        self._reconcile_owner = f"vault-reconcile-{uuid.uuid4().hex}"
        self._reconcile_tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_lock = asyncio.Lock()
        self._stopping = False
        self._accepting_reconciles = True
        self._delete_expiry_stop = asyncio.Event()
        self._delete_expiry_task: asyncio.Task[None] | None = None
        self._watcher = VaultWatchAdapter(
            settings,
            self.handle_batch,
            error_handler=self._record_watcher_error,
        )
        self.watcher_running = False
        self.running = False
        self.last_event_at: datetime | None = None
        self.last_error: str | None = None

    def _record_watcher_error(self, exc: Exception) -> None:
        self.last_error = (str(exc) or type(exc).__name__)[:500]

    async def _store_call(
        self,
        operation: Any,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[Any, bool]:
        worker = asyncio.create_task(
            asyncio.to_thread(operation, *args, **kwargs)
        )
        cancellation_requested = False
        while True:
            try:
                return await asyncio.shield(worker), cancellation_requested
            except asyncio.CancelledError:
                cancellation_requested = True
            except Exception:
                if cancellation_requested:
                    raise asyncio.CancelledError from None
                raise

    @staticmethod
    async def _cancel_and_await(task: asyncio.Task[Any] | None) -> bool:
        if task is None:
            return False
        owner_task = asyncio.current_task()
        cancelling_baseline = (
            owner_task.cancelling() if owner_task is not None else 0
        )
        task.cancel()
        cancellation_requested = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                current_cancelling = (
                    owner_task.cancelling() if owner_task is not None else 0
                )
                if current_cancelling > cancelling_baseline:
                    cancellation_requested = True
                    cancelling_baseline = current_cancelling
                if task.done() and task.cancelled():
                    break
                if owner_task is None:
                    cancellation_requested = True
            except Exception:
                break
        if task.done() and not task.cancelled():
            try:
                task.exception()
            except Exception:
                pass
        return cancellation_requested

    async def _reconcile_heartbeat(self, job_id: str) -> None:
        lease_seconds = self.settings.vault_reconcile_lease_seconds
        interval = lease_seconds / 3
        while True:
            await asyncio.sleep(interval)
            renewed, cancellation_requested = await self._store_call(
                self.events.renew_reconcile,
                job_id,
                self._reconcile_owner,
                now=datetime.now(timezone.utc),
                lease_seconds=lease_seconds,
            )
            self._propagate_cancellation(cancellation_requested)
            if not renewed:
                raise ReconcileLeaseLost(
                    f"vault reconcile lease lost: {job_id}"
                )

    async def _claim_reconcile_job(self, job_id: str) -> bool:
        lease_seconds = self.settings.vault_reconcile_lease_seconds
        claimed, cancellation_requested = await self._store_call(
            self.events.claim_reconcile,
            job_id,
            self._reconcile_owner,
            now=datetime.now(timezone.utc),
            lease_seconds=lease_seconds,
        )
        if cancellation_requested:
            if claimed:
                try:
                    await self._store_call(
                        self.events.requeue_reconcile,
                        job_id,
                        self._reconcile_owner,
                    )
                except BaseException:
                    pass
            raise asyncio.CancelledError
        return bool(claimed)

    async def _await_reconcile_claim(self, job_id: str) -> bool:
        poll_interval = max(
            0.01,
            min(0.25, self.settings.vault_reconcile_lease_seconds / 3),
        )
        while True:
            if await self._claim_reconcile_job(job_id):
                return True
            job, cancellation_requested = await self._store_call(
                self.events.get_reconcile,
                job_id,
            )
            self._propagate_cancellation(cancellation_requested)
            if job is None:
                raise RuntimeError(f"vault reconcile job disappeared: {job_id}")
            if job.status in {"succeeded", "failed"}:
                return False
            await asyncio.sleep(poll_interval)

    async def _run_reconcile_job(self, job_id: str) -> None:
        work: asyncio.Task[dict[str, int]] | None = None
        heartbeat: asyncio.Task[None] | None = None
        owns_lease = False
        async with self._reconcile_lock:
            try:
                owns_lease = await self._await_reconcile_claim(job_id)
                if not owns_lease:
                    return

                work = asyncio.create_task(
                    self.reconcile_startup(reconcile_intents=False),
                    name=f"vault-reconcile-inventory-{job_id}",
                )
                heartbeat = asyncio.create_task(
                    self._reconcile_heartbeat(job_id),
                    name=f"vault-reconcile-heartbeat-{job_id}",
                )
                done, _pending = await asyncio.wait(
                    {work, heartbeat},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if heartbeat in done:
                    self._propagate_cancellation(
                        await self._cancel_and_await(work)
                    )
                    if heartbeat.cancelled():
                        raise ReconcileLeaseLost(
                            f"vault reconcile heartbeat stopped: {job_id}"
                        )
                    try:
                        heartbeat.result()
                    except ReconcileLeaseLost:
                        raise
                    except Exception as exc:
                        raise ReconcileLeaseLost(
                            f"vault reconcile heartbeat failed: {job_id}"
                        ) from exc
                    raise ReconcileLeaseLost(
                        f"vault reconcile heartbeat stopped: {job_id}"
                    )

                result = work.result()
                self._propagate_cancellation(
                    await self._cancel_and_await(heartbeat)
                )
                finished, cancellation_requested = await self._store_call(
                    self.events.finish_reconcile,
                    job_id,
                    self._reconcile_owner,
                    result,
                )
                self._propagate_cancellation(cancellation_requested)
                if not finished:
                    raise ReconcileLeaseLost(
                        f"vault reconcile finish lost its lease: {job_id}"
                    )
            except asyncio.CancelledError:
                await self._cancel_and_await(work)
                await self._cancel_and_await(heartbeat)
                if owns_lease:
                    try:
                        await self._store_call(
                            self.events.requeue_reconcile,
                            job_id,
                            self._reconcile_owner,
                        )
                    except BaseException:
                        pass
                raise
            except ReconcileLeaseLost:
                cancellation_requested = await self._cancel_and_await(work)
                if cancellation_requested:
                    if owns_lease:
                        try:
                            await self._store_call(
                                self.events.requeue_reconcile,
                                job_id,
                                self._reconcile_owner,
                            )
                        except BaseException:
                            pass
                    raise asyncio.CancelledError from None
                raise
            except Exception as exc:
                cancellation_requested = await self._cancel_and_await(work)
                error = (str(exc) or type(exc).__name__)[:500]
                self.last_error = error
                failure_recorded = False
                failure_fence_returned = False
                if owns_lease:
                    try:
                        failure_recorded, cancelled_during_fence = (
                            await self._store_call(
                                self.events.fail_reconcile,
                                job_id,
                                self._reconcile_owner,
                                error,
                            )
                        )
                        failure_fence_returned = True
                        cancellation_requested = bool(
                            cancellation_requested or cancelled_during_fence
                        )
                    except asyncio.CancelledError:
                        cancellation_requested = True
                    except BaseException:
                        pass
                if cancellation_requested:
                    if owns_lease and not failure_recorded:
                        try:
                            await self._store_call(
                                self.events.requeue_reconcile,
                                job_id,
                                self._reconcile_owner,
                            )
                        except BaseException:
                            pass
                    raise asyncio.CancelledError from None
                if (
                    owns_lease
                    and failure_fence_returned
                    and not failure_recorded
                ):
                    raise ReconcileLeaseLost(
                        f"vault reconcile failure lost its lease: {job_id}"
                    ) from exc
                raise
            finally:
                active_error = sys.exc_info()[1]
                work_cancelled = await self._cancel_and_await(work)
                heartbeat_cancelled = await self._cancel_and_await(heartbeat)
                if active_error is None:
                    await asyncio.sleep(0)
                    if work_cancelled or heartbeat_cancelled:
                        raise asyncio.CancelledError

    def _consume_reconcile_task(
        self,
        job_id: str,
        task: asyncio.Task[None],
    ) -> None:
        if not task.cancelled():
            try:
                task.exception()
            except Exception:
                pass
        if self._reconcile_tasks.get(job_id) is task:
            self._reconcile_tasks.pop(job_id, None)

    def _ensure_reconcile_task(self, job_id: str) -> asyncio.Task[None]:
        task = self._reconcile_tasks.get(job_id)
        if task is not None and not task.done():
            return task
        if self._stopping or not self._accepting_reconciles:
            raise VaultSyncStopping("vault sync runtime stopping")
        task = asyncio.create_task(
            self._run_reconcile_job(job_id),
            name=f"vault-reconcile-{job_id}",
        )
        task.add_done_callback(
            lambda completed, requested_job_id=job_id: self._consume_reconcile_task(
                requested_job_id,
                completed,
            )
        )
        self._reconcile_tasks[job_id] = task
        return task

    def request_reconcile(self, requested_by: str) -> VaultReconcileJob:
        if self._stopping or not self._accepting_reconciles:
            raise VaultSyncStopping("vault sync runtime stopping")
        job = self.events.request_reconcile(requested_by)
        self._ensure_reconcile_task(job.id)
        return job

    async def reconcile_before_watcher_start(self) -> dict[str, int]:
        if self._stopping or not self._accepting_reconciles:
            raise VaultSyncStopping("vault sync runtime stopping")
        active, cancellation_requested = await self._store_call(
            self.events.active_reconcile
        )
        self._propagate_cancellation(cancellation_requested)
        if active is None:
            if self._stopping or not self._accepting_reconciles:
                raise VaultSyncStopping("vault sync runtime stopping")
            active, cancellation_requested = await self._store_call(
                self.events.request_reconcile,
                "startup",
            )
            self._propagate_cancellation(cancellation_requested)

        task = self._ensure_reconcile_task(active.id)
        try:
            await task
        except Exception as exc:
            terminal, cancellation_requested = await self._store_call(
                self.events.get_reconcile,
                active.id,
            )
            self._propagate_cancellation(cancellation_requested)
            if terminal is None or terminal.status != "succeeded":
                raise RuntimeError(
                    f"vault startup reconcile did not succeed: {active.id}"
                ) from exc

        terminal, cancellation_requested = await self._store_call(
            self.events.get_reconcile,
            active.id,
        )
        self._propagate_cancellation(cancellation_requested)
        if (
            terminal is None
            or terminal.status != "succeeded"
            or terminal.result is None
        ):
            error = terminal.error_summary if terminal is not None else None
            suffix = f": {error}" if error else ""
            raise RuntimeError(
                f"vault startup reconcile did not succeed: {active.id}{suffix}"
            )
        return terminal.result

    async def _delete_expiry_loop(self) -> None:
        poll_seconds = max(
            0.01,
            float(self.settings.vault_watch_debounce_ms) / 1000,
        )
        while not self._delete_expiry_stop.is_set():
            try:
                await self.expire_deletes()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = (str(exc) or type(exc).__name__)[:500]
            try:
                await asyncio.wait_for(
                    self._delete_expiry_stop.wait(),
                    timeout=poll_seconds,
                )
            except TimeoutError:
                pass

    async def start(self) -> None:
        async with self._stop_lock:
            if (
                not self.settings.vault_watch_enabled
                or self.watcher_running
                or self._stopping
            ):
                return
            await self._watcher.start()
            self.watcher_running = True
            self._delete_expiry_stop.clear()
            self._delete_expiry_task = asyncio.create_task(
                self._delete_expiry_loop(),
                name="vault-delete-expiry",
            )

    async def stop(self) -> None:
        self._stopping = True
        self._accepting_reconciles = False
        async with self._stop_lock:
            stop_error: BaseException | None = None
            expiry_task, self._delete_expiry_task = (
                self._delete_expiry_task,
                None,
            )
            self._delete_expiry_stop.set()
            if expiry_task is not None and not expiry_task.done():
                expiry_task.cancel()
            try:
                await self._watcher.stop()
            except BaseException as exc:
                stop_error = exc
            self.watcher_running = False

            if await self._cancel_and_await(expiry_task) and stop_error is None:
                stop_error = asyncio.CancelledError()

            while self._reconcile_tasks:
                tasks = list(self._reconcile_tasks.items())
                for _, task in tasks:
                    if not task.done():
                        task.cancel()
                waiter = asyncio.gather(
                    *(task for _, task in tasks),
                    return_exceptions=True,
                )
                while not waiter.done():
                    try:
                        await asyncio.shield(waiter)
                    except asyncio.CancelledError as exc:
                        if stop_error is None:
                            stop_error = exc
                for job_id, task in tasks:
                    if self._reconcile_tasks.get(job_id) is task:
                        self._reconcile_tasks.pop(job_id, None)
            if stop_error is not None:
                raise stop_error

    async def _drop_page_lock_user(
        self,
        key: str,
        entry: PageLockEntry,
    ) -> None:
        async with self._page_locks_guard:
            entry.users -= 1
            if entry.users == 0 and self._page_locks.get(key) is entry:
                self._page_locks.pop(key, None)

    @asynccontextmanager
    async def _page_lock(self, key: str) -> AsyncIterator[None]:
        async with self._page_locks_guard:
            entry = self._page_locks.get(key)
            if entry is None:
                entry = PageLockEntry(asyncio.Lock())
                self._page_locks[key] = entry
            entry.users += 1

        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            cleanup = asyncio.create_task(self._drop_page_lock_user(key, entry))
            cancelled_during_cleanup = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled_during_cleanup = True
            await cleanup
            if cancelled_during_cleanup:
                raise asyncio.CancelledError

    def _remember_event_time(self, detected_at: datetime) -> None:
        if self.last_event_at is None or detected_at > self.last_event_at:
            self.last_event_at = detected_at

    def _absolute_path(self, page_path: str) -> Path:
        return self.settings.vault_path / page_path

    async def _queue_delete(
        self,
        occurrence: VaultWatchOccurrence,
        detected_at: datetime,
    ) -> None:
        page = self.events.page_by_path(occurrence.page_path)
        if page is None or not page.get("page_id"):
            self.events.finish_occurrence(occurrence.id, "ignored")
            return

        page_id = str(page["page_id"])
        self.events.get_or_create_pending_delete(
            occurrence_id=occurrence.id,
            page_id=page_id,
            old_page_path=occurrence.page_path,
            file_hash=page.get("file_hash"),
            semantic_hash=page.get("semantic_hash"),
            detected_at=detected_at,
            expires_at=detected_at
            + timedelta(milliseconds=self.settings.vault_rename_grace_ms),
        )
        self.events.finish_occurrence(
            occurrence.id,
            "deferred",
            page_id=page_id,
        )

    async def _renew_delete_claim_until_stopped(
        self,
        delete_id: str,
        owner: str,
        stopped: asyncio.Event,
        *,
        allow_resolved_claim: bool = False,
    ) -> bool:
        lease_seconds = self.settings.vault_reconcile_lease_seconds
        interval = max(0.1, lease_seconds / 3)
        while True:
            try:
                await asyncio.wait_for(stopped.wait(), timeout=interval)
                return True
            except asyncio.TimeoutError:
                renewed = await asyncio.to_thread(
                    self.events.renew_delete_claim,
                    delete_id,
                    owner,
                    now=datetime.now(timezone.utc),
                )
                if not renewed:
                    return (
                        allow_resolved_claim
                        and self.events.get_pending_delete(delete_id) is None
                    )

    async def _await_claimed_mutation(
        self,
        pending: PendingVaultDelete,
        owner: str,
        mutation: Any,
        *args: Any,
        allow_resolved_claim: bool = False,
    ) -> tuple[Any, bool]:
        stopped = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._renew_delete_claim_until_stopped(
                pending.id,
                owner,
                stopped,
                allow_resolved_claim=allow_resolved_claim,
            )
        )
        cancellation_requested = False
        mutation_cancelled = False
        mutation_error: Exception | None = None
        result: Any = None
        try:
            result, cancellation_requested = await self._await_revision_mutation(
                mutation,
                *args,
            )
        except asyncio.CancelledError:
            mutation_cancelled = True
            cancellation_requested = True
        except Exception as exc:
            mutation_error = exc

        stopped.set()
        claim_retained = False
        heartbeat_error: Exception | None = None
        while not heartbeat.done():
            try:
                claim_retained = await asyncio.shield(heartbeat)
            except asyncio.CancelledError:
                cancellation_requested = True
            except Exception as exc:
                heartbeat_error = exc
        if heartbeat_error is None:
            try:
                claim_retained = heartbeat.result()
            except Exception as exc:
                heartbeat_error = exc

        if heartbeat_error is not None:
            if cancellation_requested:
                self._record_delete_expiry_failure(pending, heartbeat_error)
            else:
                raise heartbeat_error
        if mutation_error is not None:
            if cancellation_requested:
                raise asyncio.CancelledError from None
            raise mutation_error
        if heartbeat_error is not None:
            if mutation_cancelled:
                raise asyncio.CancelledError from None
            return result, cancellation_requested
        if not claim_retained:
            claim_error = RevisionConflict(
                "delete expiry lost its persistent claim",
                current_revision_id=None,
            )
            if cancellation_requested:
                self._record_delete_expiry_failure(pending, claim_error)
            else:
                raise claim_error
        if mutation_cancelled:
            raise asyncio.CancelledError from None
        return result, cancellation_requested

    def _record_delete_expiry_failure(
        self,
        pending: PendingVaultDelete,
        exc: Exception,
    ) -> None:
        error = (str(exc) or type(exc).__name__)[:500]
        self.last_error = error
        try:
            self.events.upsert_sync_issue(
                page_path=pending.old_page_path,
                file_hash=pending.file_hash or "",
                page_id=pending.page_id,
                issue_type="delete_expiry_failed",
                error_summary=error,
            )
        except Exception:
            pass

    async def expire_deletes(self, now: datetime | None = None) -> int:
        expire_at = now or datetime.now(timezone.utc)
        if expire_at.tzinfo is None:
            raise ValueError("now must be timezone-aware")

        completed = 0
        for candidate in self.events.list_due_deletes(expire_at):
            owner = f"vclaim_{uuid.uuid4().hex}"
            claimed = False
            claim_completed = False
            try:
                if (
                    self._absolute_path(candidate.old_page_path).exists()
                    or self.events.has_active_intent(candidate.page_id)
                    or self.events.has_unclassified_add_before(
                        candidate.expires_at
                    )
                ):
                    continue
                claimed = self.events.claim_delete(
                    candidate.id,
                    owner,
                    now=datetime.now(timezone.utc),
                    due_at=expire_at,
                )
                if not claimed:
                    continue
                async with self._page_lock(candidate.page_id):
                    pending = self.events.get_pending_delete(candidate.id)
                    if pending is None or pending.claim_owner != owner:
                        continue
                    if (
                        self._absolute_path(pending.old_page_path).exists()
                        or self.events.has_active_intent(pending.page_id)
                        or self.events.has_unclassified_add_before(
                            pending.expires_at
                        )
                    ):
                        continue

                    result, cancellation_requested = await (
                        self._await_claimed_mutation(
                            pending,
                            owner,
                            self.revisions.delete_page,
                            pending.occurrence_id,
                            pending.old_page_path,
                        )
                    )
                    if result.status in {"deleted", "ignored"}:
                        try:
                            claim_completed = self.events.complete_delete(
                                pending.id,
                                owner,
                                now=datetime.now(timezone.utc),
                            )
                        except Exception as exc:
                            if cancellation_requested:
                                self._record_delete_expiry_failure(pending, exc)
                                raise asyncio.CancelledError from None
                            raise
                        if claim_completed:
                            completed += 1
                    else:
                        self._record_delete_expiry_failure(
                            pending,
                            RuntimeError(
                                "delete expiry returned non-terminal status: "
                                f"{result.status}"
                            ),
                        )
                    self._propagate_cancellation(cancellation_requested)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_delete_expiry_failure(candidate, exc)
            finally:
                if claimed and not claim_completed:
                    try:
                        self.events.release_delete_claim(candidate.id, owner)
                    except Exception as exc:
                        self._record_delete_expiry_failure(candidate, exc)
        return completed

    @staticmethod
    def _startup_occurrence_id(prefix: str, *parts: str) -> str:
        payload = "\0".join((prefix, *parts)).encode("utf-8")
        return f"vocc_startup_{hashlib.sha256(payload).hexdigest()}"

    def _inventory_page_id(
        self,
        observation: FileObservationInput,
    ) -> str | None:
        if observation.content_truncated or observation.content_bytes is None:
            raise ValueError("startup inventory requires complete file bytes")
        document = parse_wiki_bytes(observation.content_bytes, self.limits)
        first_id = document.frontmatter.get("lgdo_page_id")
        second_id = document.frontmatter.get("id")
        if (
            first_id is not None
            and second_id is not None
            and first_id != second_id
        ):
            raise ValueError("wiki page declares conflicting managed IDs")
        value = first_id or second_id
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError("managed page ID must be a non-empty string")
        return value

    def _inventory_failure_path(self, path: Path) -> str:
        canonical = canonical_wiki_path(self.settings.vault_path, path)
        if canonical is not None:
            return canonical
        try:
            return path.relative_to(self.settings.vault_path).as_posix()
        except ValueError:
            return str(path)

    def _record_inventory_failure(
        self,
        page_path: str,
        exc: Exception,
    ) -> None:
        error = (str(exc) or type(exc).__name__)[:500]
        detected_at = datetime.now(timezone.utc)
        occurrence = self.events.begin_occurrence(
            "modify",
            page_path,
            detected_at=detected_at,
            occurrence_id=self._startup_occurrence_id(
                "inventory-failure",
                page_path,
                type(exc).__name__,
                error,
            ),
        )
        self._remember_event_time(occurrence.detected_at)
        self.events.finish_occurrence(
            occurrence.id,
            "failed",
            error_summary=error,
        )
        self.last_error = error

    def _record_startup_issue(
        self,
        *,
        page_path: str,
        file_hash: str,
        page_id: str | None,
        issue_type: str,
        error_summary: str,
    ) -> None:
        error = error_summary[:500]
        issue = self.events.upsert_sync_issue(
            page_path=page_path,
            file_hash=file_hash,
            page_id=page_id,
            issue_type=issue_type,
            error_summary=error,
        )
        detected_at = datetime.now(timezone.utc)
        occurrence = self.events.begin_occurrence(
            "modify",
            page_path,
            detected_at=detected_at,
            occurrence_id=self._startup_occurrence_id(
                "startup-issue",
                issue.id,
                issue_type,
                page_path,
                file_hash,
                page_id or "",
            ),
        )
        self._remember_event_time(occurrence.detected_at)
        self.events.finish_occurrence(
            occurrence.id,
            "invalid",
            page_id=page_id,
            sync_issue_id=issue.id,
            error_summary=error,
        )

    async def _replay_startup_move(
        self,
        occurrence: VaultWatchOccurrence,
        *,
        stability_poll_interval: float,
    ) -> None:
        if occurrence.old_page_path is None:
            raise RevisionConflict(
                "startup move occurrence has no old path",
                current_revision_id=None,
            )
        absolute_path = self._absolute_path(occurrence.page_path)
        if not absolute_path.exists():
            self.events.finish_occurrence(occurrence.id, "ignored")
            return
        observation = await wait_for_stable_observation(
            absolute_path,
            self.limits,
            timeout_seconds=self.settings.vault_watch_stability_timeout_seconds,
            poll_interval=stability_poll_interval,
        )
        page_id = self._inventory_page_id(observation)
        if page_id is None:
            raise RevisionConflict(
                "startup move replay has no managed identity",
                current_revision_id=None,
            )
        pending_deletes = self.events.find_pending_deletes(page_id=page_id)
        if len(pending_deletes) > 1 or any(
            pending.old_page_path != occurrence.old_page_path
            for pending in pending_deletes
        ):
            raise RevisionConflict(
                "startup move replay has ambiguous pending delete evidence",
                current_revision_id=None,
            )
        replay_owner = f"vclaim_replay_{uuid.uuid4().hex}"
        claimed_pending: PendingVaultDelete | None = None
        claim_resolved = False
        pending_completed = False
        cancellation_in_flight = False
        try:
            for pending in pending_deletes:
                claimed_pending = await self._claim_delete_after_wait(
                    pending,
                    replay_owner,
                    poll_interval=stability_poll_interval,
                )
                if claimed_pending is None:
                    pending_completed = True
            async with self._page_lock(page_id):
                observation = await wait_for_stable_observation(
                    absolute_path,
                    self.limits,
                    timeout_seconds=(
                        self.settings.vault_watch_stability_timeout_seconds
                    ),
                    poll_interval=stability_poll_interval,
                )
                if self._inventory_page_id(observation) != page_id:
                    raise RevisionConflict(
                        "startup move replay identity changed",
                        current_revision_id=None,
                    )
                if pending_completed:
                    mutation = self.revisions.ingest_external_change
                    args = (
                        f"{occurrence.id}_restore",
                        occurrence.page_path,
                        observation,
                    )
                elif occurrence.kind == "rename":
                    mutation = self.revisions.rename_page
                    args = (
                        occurrence.id,
                        occurrence.old_page_path,
                        occurrence.page_path,
                    )
                else:
                    mutation = self.revisions.relocate_external_change
                    args = (
                        occurrence.id,
                        occurrence.old_page_path,
                        occurrence.page_path,
                        page_id,
                        observation,
                    )
                if claimed_pending is not None and not pending_completed:
                    args = (*args, claimed_pending.id, replay_owner)
                if claimed_pending is None:
                    result, cancellation_requested = (
                        await self._await_revision_mutation(mutation, *args)
                    )
                else:
                    result, cancellation_requested = await self._await_claimed_mutation(
                        claimed_pending,
                        replay_owner,
                        mutation,
                        *args,
                        allow_resolved_claim=True,
                    )
                    remaining = self.events.get_pending_delete(
                        claimed_pending.id
                    )
                    if remaining is None:
                        claim_resolved = True
                    elif result.status in {"renamed", "applied", "ignored"}:
                        try:
                            claim_resolved = self.events.cancel_claimed_delete(
                                claimed_pending.id,
                                replay_owner,
                                occurrence.id,
                                now=datetime.now(timezone.utc),
                            )
                            if not claim_resolved:
                                raise RevisionConflict(
                                    "startup move replay lost its pending delete claim",
                                    current_revision_id=None,
                                )
                        except Exception as exc:
                            if cancellation_requested:
                                self._record_delete_expiry_failure(
                                    claimed_pending,
                                    exc,
                                )
                                raise asyncio.CancelledError from None
                            raise
                self._finish_result(occurrence, result)
                self._propagate_cancellation(cancellation_requested)
        except asyncio.CancelledError:
            cancellation_in_flight = True
            raise
        finally:
            if claimed_pending is not None and not claim_resolved:
                try:
                    self.events.release_delete_claim(
                        claimed_pending.id,
                        replay_owner,
                    )
                except Exception as exc:
                    self._record_delete_expiry_failure(claimed_pending, exc)
                    if not cancellation_in_flight:
                        raise

    async def _replay_pending_occurrences(
        self,
        stability_poll_interval: float,
    ) -> tuple[set[str], int, int]:
        pending = sorted(
            self.events.pending_occurrences(),
            key=lambda occurrence: (
                occurrence.kind != "delete",
                occurrence.detected_at,
                occurrence.id,
            ),
        )
        replayed_paths: set[str] = set()
        replayed = 0
        failed = 0
        for occurrence in pending:
            replayed += 1
            self._remember_event_time(occurrence.detected_at)
            if occurrence.kind != "delete":
                replayed_paths.add(occurrence.page_path)
            if (
                occurrence.kind in {"add", "modify", "rename", "relocate"}
                and not self._absolute_path(occurrence.page_path).exists()
            ):
                self.events.finish_occurrence(occurrence.id, "ignored")
                continue
            failed += int(
                await self._handle_occurrence(
                    occurrence,
                    stability_poll_interval=stability_poll_interval,
                )
            )
        return replayed_paths, replayed, failed

    async def _inventory(
        self,
        stability_poll_interval: float,
    ) -> StartupInventory:
        entries: list[StartupInventoryEntry] = []
        seen_paths: set[str] = set()
        failed = 0
        recorded_failures: set[tuple[str, str, str]] = set()

        def record_failure(path: Path, exc: Exception) -> None:
            nonlocal failed
            page_path = self._inventory_failure_path(path)
            seen_paths.add(page_path)
            identity = (page_path, type(exc).__name__, str(exc))
            if identity in recorded_failures:
                return
            recorded_failures.add(identity)
            failed += 1
            try:
                self._record_inventory_failure(page_path, exc)
            except Exception as record_exc:
                self.last_error = str(record_exc)[:500]

        for absolute_path in iter_canonical_wiki_files(
            self.settings.vault_path,
            error_handler=record_failure,
        ):
            page_path = canonical_wiki_path(
                self.settings.vault_path,
                absolute_path,
            )
            if page_path is None:
                continue
            seen_paths.add(page_path)
            try:
                observation = await wait_for_stable_observation(
                    absolute_path,
                    self.limits,
                    timeout_seconds=(
                        self.settings.vault_watch_stability_timeout_seconds
                    ),
                    poll_interval=stability_poll_interval,
                )
                page_id = self._inventory_page_id(observation)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                record_failure(absolute_path, exc)
                continue
            entries.append((page_path, observation, page_id))
        return entries, seen_paths, failed

    def _fail_startup_occurrence(
        self,
        occurrence: VaultWatchOccurrence | None,
        exc: Exception,
    ) -> None:
        error = (str(exc) or type(exc).__name__)[:500]
        self.last_error = error
        if occurrence is None or occurrence.status != "pending":
            return
        try:
            self.events.finish_occurrence(
                occurrence.id,
                "failed",
                error_summary=error,
            )
        except VaultOccurrenceConflict:
            pass

    async def _apply_startup_ingest(
        self,
        *,
        page_path: str,
        page_id: str | None,
        bound_page: dict[str, Any] | None,
        stability_poll_interval: float,
    ) -> bool:
        detected_at = datetime.now(timezone.utc)
        occurrence = self.events.begin_occurrence(
            "modify" if bound_page is not None else "add",
            page_path,
            detected_at=detected_at,
        )
        self._remember_event_time(occurrence.detected_at)
        if occurrence.status != "pending":
            return False

        pending = self.events.find_pending_delete_for_path(page_path)
        if pending is not None:
            if bound_page is None or str(
                bound_page.get("page_id")
            ) != pending.page_id:
                raise RevisionConflict(
                    "startup same-path delete evidence changed",
                    current_revision_id=None,
                )
            await self._cancel_delete_after_claim(
                pending,
                occurrence.id,
                poll_interval=stability_poll_interval,
            )

        lock_key = (
            str(bound_page["page_id"])
            if bound_page is not None and bound_page.get("page_id")
            else page_id or page_path
        )
        try:
            async with self._page_lock(lock_key):
                absolute_path = self._absolute_path(page_path)
                if not absolute_path.exists():
                    self.events.finish_occurrence(occurrence.id, "ignored")
                    return False
                if (
                    bound_page is not None
                    and bound_page.get("page_id")
                    and self.events.has_active_intent(
                        str(bound_page["page_id"])
                    )
                ):
                    raise RevisionConflict(
                        "startup ingest is blocked by an active intent",
                        current_revision_id=None,
                    )
                observation = await wait_for_stable_observation(
                    absolute_path,
                    self.limits,
                    timeout_seconds=(
                        self.settings.vault_watch_stability_timeout_seconds
                    ),
                    poll_interval=stability_poll_interval,
                )
                result, cancellation_requested = (
                    await self._await_revision_mutation(
                        self.revisions.ingest_external_change,
                        occurrence.id,
                        page_path,
                        observation,
                    )
                )
                self._finish_result(occurrence, result)
                self._propagate_cancellation(cancellation_requested)
                return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_startup_occurrence(occurrence, exc)
            raise

    async def _apply_startup_move(
        self,
        *,
        page: dict[str, Any],
        candidate_path: str,
        stability_poll_interval: float,
    ) -> str:
        page_id = str(page["page_id"])
        old_path = str(page["path"])
        absolute_candidate = self._absolute_path(candidate_path)
        occurrence: VaultWatchOccurrence | None = None
        move_owner = f"vclaim_move_{uuid.uuid4().hex}"
        claimed_pending: PendingVaultDelete | None = None
        claim_resolved = False
        cancellation_in_flight = False
        try:
            observation = await wait_for_stable_observation(
                absolute_candidate,
                self.limits,
                timeout_seconds=self.settings.vault_watch_stability_timeout_seconds,
                poll_interval=stability_poll_interval,
            )
            if self._inventory_page_id(observation) != page_id:
                raise RevisionConflict(
                    "startup move identity changed",
                    current_revision_id=None,
                )
            pending_deletes = self.events.find_pending_deletes(page_id=page_id)
            if len(pending_deletes) > 1 or any(
                pending.old_page_path != old_path
                for pending in pending_deletes
            ):
                raise RevisionConflict(
                    "startup move has ambiguous pending delete evidence",
                    current_revision_id=None,
                )
            exact = observation.file_hash == page.get("file_hash")
            action = "rename" if exact else "relocate"
            occurrence = self.events.begin_occurrence(
                action,
                candidate_path,
                old_page_path=old_path,
                detected_at=datetime.now(timezone.utc),
            )
            self._remember_event_time(occurrence.detected_at)
            if occurrence.status != "pending":
                return action
            pending_completed = False
            for pending in pending_deletes:
                claimed_pending = await self._claim_delete_after_wait(
                    pending,
                    move_owner,
                    poll_interval=stability_poll_interval,
                )
                if claimed_pending is None:
                    pending_completed = True

            async with self._page_lock(page_id):
                current = self.events.page_by_path(old_path)
                if (
                    current is None
                    or str(current.get("page_id")) != page_id
                    or self._absolute_path(old_path).exists()
                    or not absolute_candidate.exists()
                    or self.events.has_active_intent(page_id)
                ):
                    raise RevisionConflict(
                        "startup move evidence changed",
                        current_revision_id=None,
                    )
                observation = await wait_for_stable_observation(
                    absolute_candidate,
                    self.limits,
                    timeout_seconds=(
                        self.settings.vault_watch_stability_timeout_seconds
                    ),
                    poll_interval=stability_poll_interval,
                )
                if self._inventory_page_id(observation) != page_id:
                    raise RevisionConflict(
                        "startup move identity changed",
                        current_revision_id=None,
                    )
                if (observation.file_hash == page.get("file_hash")) != exact:
                    raise RevisionConflict(
                        "startup move content changed while waiting for delete claim",
                        current_revision_id=None,
                    )
                if pending_completed:
                    result, cancellation_requested = (
                        await self._await_revision_mutation(
                            self.revisions.ingest_external_change,
                            occurrence.id,
                            candidate_path,
                            observation,
                        )
                    )
                    self._finish_result(occurrence, result)
                    self._propagate_cancellation(cancellation_requested)
                    return "ingest"
                if exact:
                    args = (occurrence.id, old_path, candidate_path)
                    mutation = self.revisions.rename_page
                else:
                    args = (
                        occurrence.id,
                        old_path,
                        candidate_path,
                        page_id,
                        observation,
                    )
                    mutation = self.revisions.relocate_external_change
                if claimed_pending is not None:
                    args = (*args, claimed_pending.id, move_owner)
                if claimed_pending is None:
                    result, cancellation_requested = (
                        await self._await_revision_mutation(mutation, *args)
                    )
                else:
                    result, cancellation_requested = await self._await_claimed_mutation(
                        claimed_pending,
                        move_owner,
                        mutation,
                        *args,
                        allow_resolved_claim=True,
                    )
                    remaining = self.events.get_pending_delete(
                        claimed_pending.id
                    )
                    if remaining is None:
                        claim_resolved = True
                    elif result.status in {"renamed", "applied", "ignored"}:
                        try:
                            claim_resolved = self.events.cancel_claimed_delete(
                                claimed_pending.id,
                                move_owner,
                                occurrence.id,
                                now=datetime.now(timezone.utc),
                            )
                            if not claim_resolved:
                                raise RevisionConflict(
                                    "startup move lost its pending delete claim",
                                    current_revision_id=None,
                                )
                        except Exception as exc:
                            if cancellation_requested:
                                self._record_delete_expiry_failure(
                                    claimed_pending,
                                    exc,
                                )
                                raise asyncio.CancelledError from None
                            raise
                self._finish_result(occurrence, result)
                self._propagate_cancellation(cancellation_requested)
                return action
        except asyncio.CancelledError:
            cancellation_in_flight = True
            raise
        except Exception as exc:
            self._fail_startup_occurrence(occurrence, exc)
            raise
        finally:
            if claimed_pending is not None and not claim_resolved:
                try:
                    self.events.release_delete_claim(
                        claimed_pending.id,
                        move_owner,
                    )
                except Exception as exc:
                    self._record_delete_expiry_failure(claimed_pending, exc)
                    if not cancellation_in_flight:
                        raise

    async def _queue_startup_missing(
        self,
        page: dict[str, Any],
    ) -> bool:
        page_id = str(page["page_id"])
        page_path = str(page["path"])
        async with self._page_lock(page_id):
            current = self.events.page_by_path(page_path)
            if (
                current is None
                or str(current.get("page_id")) != page_id
                or self._absolute_path(page_path).exists()
                or self.events.has_active_intent(page_id)
            ):
                return False
            existing = self.events.find_pending_delete_for_path(page_path)
            if existing is not None and existing.page_id == page_id:
                return False
            occurrence = self.events.begin_occurrence(
                "delete",
                page_path,
                detected_at=datetime.now(timezone.utc),
            )
            self._remember_event_time(occurrence.detected_at)
            await self._queue_delete(occurrence, occurrence.detected_at)
            return True

    async def _reconcile_inventory(
        self,
        inventory: StartupInventory,
        replayed_paths: set[str],
        *,
        stability_poll_interval: float = 0.05,
    ) -> dict[str, int]:
        entries, seen_paths, inventory_failed = inventory
        counters = {
            "renamed": 0,
            "relocated": 0,
            "ingested": 0,
            "missing": 0,
            "duplicate_page_ids": 0,
            "failed": inventory_failed,
        }
        entries_by_path = {entry[0]: entry for entry in entries}
        entries_by_id: dict[str, list[StartupInventoryEntry]] = {}
        for entry in entries:
            if entry[2] is not None:
                entries_by_id.setdefault(entry[2], []).append(entry)

        pages = self.events.list_wiki_pages(
            lifecycle_statuses=("active", "invalid", "deleted")
        )
        pages_by_path = {str(page["path"]): page for page in pages}
        pages_by_id: dict[str, list[dict[str, Any]]] = {}
        for page in pages:
            if page.get("page_id"):
                pages_by_id.setdefault(str(page["page_id"]), []).append(page)

        isolated_paths: set[str] = set()
        processed_paths: set[str] = set()
        duplicate_ids = {
            page_id: candidates
            for page_id, candidates in entries_by_id.items()
            if len(candidates) > 1
        }
        for page_id, candidates in sorted(duplicate_ids.items()):
            counters["duplicate_page_ids"] += 1
            isolated_paths.update(candidate[0] for candidate in candidates)
            paths = ", ".join(candidate[0] for candidate in candidates)
            for page_path, observation, _ in candidates:
                try:
                    self._record_startup_issue(
                        page_path=page_path,
                        file_hash=(
                            observation.file_hash if observation is not None else ""
                        ),
                        page_id=page_id,
                        issue_type="duplicate_page_id",
                        error_summary=(
                            "startup inventory found duplicate managed page ID at: "
                            f"{paths}"
                        ),
                    )
                except Exception as exc:
                    self.last_error = str(exc)[:500]
                    counters["failed"] += 1

        movable_pages = [
            page
            for page in pages
            if page.get("page_id")
            and page.get("lifecycle_status") in {"active", "invalid"}
        ]
        for page in movable_pages:
            page_id = str(page["page_id"])
            old_path = str(page["path"])
            if old_path in seen_paths:
                continue
            candidates = entries_by_id.get(page_id, [])
            if candidates:
                if page_id in duplicate_ids or len(pages_by_id[page_id]) != 1:
                    continue
                candidate = candidates[0]
                candidate_path = candidate[0]
                if candidate_path in replayed_paths:
                    processed_paths.add(candidate_path)
                    continue
                occupied = pages_by_path.get(candidate_path)
                if (
                    occupied is not None
                    and str(occupied.get("page_id")) != page_id
                ):
                    isolated_paths.add(candidate_path)
                    counters["failed"] += 1
                    try:
                        self._record_startup_issue(
                            page_path=candidate_path,
                            file_hash=candidate[1].file_hash,
                            page_id=page_id,
                            issue_type="occupied_startup_move",
                            error_summary=(
                                "startup move target belongs to another database page"
                            ),
                        )
                    except Exception as exc:
                        self.last_error = str(exc)[:500]
                    continue
                if self.events.has_active_intent(page_id):
                    isolated_paths.add(candidate_path)
                    counters["failed"] += 1
                    try:
                        self._record_startup_issue(
                            page_path=candidate_path,
                            file_hash=candidate[1].file_hash,
                            page_id=page_id,
                            issue_type="startup_move_intent",
                            error_summary="startup move is blocked by an active intent",
                        )
                    except Exception as exc:
                        self.last_error = str(exc)[:500]
                    continue
                processed_paths.add(candidate_path)
                try:
                    action = await self._apply_startup_move(
                        page=page,
                        candidate_path=candidate_path,
                        stability_poll_interval=stability_poll_interval,
                    )
                    counter = {
                        "rename": "renamed",
                        "relocate": "relocated",
                        "ingest": "ingested",
                    }[action]
                    counters[counter] += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    counters["failed"] += 1
                continue

            if self.events.has_active_intent(page_id):
                counters["failed"] += 1
                try:
                    self._record_startup_issue(
                        page_path=old_path,
                        file_hash=str(page.get("file_hash") or ""),
                        page_id=page_id,
                        issue_type="startup_missing_intent",
                        error_summary="missing startup page has an active intent",
                    )
                except Exception as exc:
                    self.last_error = str(exc)[:500]
                continue
            counters["missing"] += 1
            try:
                await self._queue_startup_missing(page)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)[:500]
                counters["failed"] += 1

        for page_path, observation, page_id in entries:
            if (
                observation is None
                or page_path in replayed_paths
                or page_path in isolated_paths
                or page_path in processed_paths
            ):
                continue
            bound_page = pages_by_path.get(page_path)
            if bound_page is not None:
                bound_id = (
                    str(bound_page["page_id"])
                    if bound_page.get("page_id")
                    else None
                )
                if page_id is not None and bound_id != page_id:
                    counters["failed"] += 1
                    try:
                        self._record_startup_issue(
                            page_path=page_path,
                            file_hash=observation.file_hash,
                            page_id=page_id,
                            issue_type="startup_identity_mismatch",
                            error_summary=(
                                "startup file identity does not match its database path"
                            ),
                        )
                    except Exception as exc:
                        self.last_error = str(exc)[:500]
                    continue
                if (
                    bound_page.get("lifecycle_status") == "active"
                    and bound_page.get("file_hash") == observation.file_hash
                    and self.events.find_pending_delete_for_path(page_path)
                    is None
                ):
                    continue
                if bound_id is not None and self.events.has_active_intent(bound_id):
                    counters["failed"] += 1
                    try:
                        self._record_startup_issue(
                            page_path=page_path,
                            file_hash=observation.file_hash,
                            page_id=bound_id,
                            issue_type="startup_ingest_intent",
                            error_summary=(
                                "startup external change is blocked by an active intent"
                            ),
                        )
                    except Exception as exc:
                        self.last_error = str(exc)[:500]
                    continue
            elif page_id is not None and page_id in pages_by_id:
                identity_pages = pages_by_id[page_id]
                restorable_deleted_page = (
                    len(identity_pages) == 1
                    and identity_pages[0].get("lifecycle_status") == "deleted"
                    and str(identity_pages[0].get("path")) not in seen_paths
                    and not self.events.has_active_intent(page_id)
                )
                if not restorable_deleted_page:
                    counters["failed"] += 1
                    try:
                        self._record_startup_issue(
                            page_path=page_path,
                            file_hash=observation.file_hash,
                            page_id=page_id,
                            issue_type="unproven_startup_move",
                            error_summary=(
                                "managed identity is already assigned without unique "
                                "startup move evidence"
                            ),
                        )
                    except Exception as exc:
                        self.last_error = str(exc)[:500]
                    continue

            try:
                applied = await self._apply_startup_ingest(
                    page_path=page_path,
                    page_id=page_id,
                    bound_page=bound_page,
                    stability_poll_interval=stability_poll_interval,
                )
                counters["ingested"] += int(applied)
            except asyncio.CancelledError:
                raise
            except Exception:
                counters["failed"] += 1
        return counters

    async def reconcile_startup(
        self,
        *,
        stability_poll_interval: float = 0.05,
        reconcile_intents: bool = True,
    ) -> dict[str, int]:
        if reconcile_intents:
            _, cancellation_requested = await self._await_revision_mutation(
                self.intent_executor.reconcile_all
            )
            self._propagate_cancellation(cancellation_requested)

        replayed_paths, replayed, replay_failed = (
            await self._replay_pending_occurrences(stability_poll_interval)
        )
        inventory = await self._inventory(stability_poll_interval)
        result = await self._reconcile_inventory(
            inventory,
            replayed_paths,
            stability_poll_interval=stability_poll_interval,
        )
        result["replayed"] = replayed
        result["failed"] += replay_failed
        result["projection_jobs"] = 0
        projection_jobs, cancellation_requested = (
            await self._await_revision_mutation(
                self.revisions.ensure_projection_jobs,
                None,
            )
        )
        result["projection_jobs"] = len(projection_jobs)
        self._propagate_cancellation(cancellation_requested)
        return result

    def _managed_page_id(self, observation: FileObservationInput) -> str | None:
        if observation.content_truncated or observation.content_bytes is None:
            return None
        try:
            document = parse_wiki_bytes(observation.content_bytes, self.limits)
        except (MarkdownParseError, ValueError):
            return None
        first_id = document.frontmatter.get("lgdo_page_id")
        second_id = document.frontmatter.get("id")
        if (
            first_id is not None
            and second_id is not None
            and first_id != second_id
        ):
            return None
        value = first_id or second_id
        return str(value) if isinstance(value, str) and value else None

    def _finish_result(
        self,
        occurrence: VaultWatchOccurrence,
        result: MutationResult | Any,
        *,
        error_summary: str | None = None,
    ) -> None:
        self.events.finish_occurrence(
            occurrence.id,
            str(result.status),
            page_id=result.page_id,
            revision_id=result.revision_id,
            sync_issue_id=result.sync_issue_id,
            error_summary=error_summary,
        )

    async def _await_revision_mutation(
        self,
        mutation: Any,
        *args: Any,
    ) -> tuple[Any, bool]:
        worker = asyncio.create_task(asyncio.to_thread(mutation, *args))
        cancellation_requested = False
        while True:
            try:
                result = await asyncio.shield(worker)
                return result, cancellation_requested
            except asyncio.CancelledError:
                cancellation_requested = True
            except Exception:
                if cancellation_requested:
                    raise asyncio.CancelledError from None
                raise

    @staticmethod
    def _propagate_cancellation(cancellation_requested: bool) -> None:
        if cancellation_requested:
            raise asyncio.CancelledError

    async def _cancel_delete_after_claim(
        self,
        candidate: PendingVaultDelete,
        occurrence_id: str,
        *,
        poll_interval: float,
    ) -> PendingVaultDelete | None:
        interval = max(0.01, poll_interval)
        while True:
            pending = self.events.get_pending_delete(candidate.id)
            if pending is None:
                return None
            if self.events.cancel_delete(pending.id, occurrence_id):
                return pending
            await asyncio.sleep(interval)

    async def _claim_delete_after_wait(
        self,
        candidate: PendingVaultDelete,
        owner: str,
        *,
        poll_interval: float,
    ) -> PendingVaultDelete | None:
        interval = max(0.01, poll_interval)
        while True:
            pending = self.events.get_pending_delete(candidate.id)
            if pending is None:
                return None
            if self.events.claim_delete(
                pending.id,
                owner,
                now=datetime.now(timezone.utc),
                due_at=pending.expires_at,
            ):
                claimed = self.events.get_pending_delete(pending.id)
                if claimed is not None and claimed.claim_owner == owner:
                    return claimed
            await asyncio.sleep(interval)

    def _same_path_evidence(
        self,
        candidate: PendingVaultDelete,
        occurrence: VaultWatchOccurrence,
        absolute_path: Path,
    ) -> PendingVaultDelete:
        pending = self.events.find_pending_delete_for_path(occurrence.page_path)
        if (
            pending is None
            or pending.id != candidate.id
            or not absolute_path.exists()
            or self.events.has_active_intent(candidate.page_id)
        ):
            raise RevisionConflict(
                "same-path save evidence changed",
                current_revision_id=None,
            )
        return pending

    def _rename_evidence(
        self,
        candidate: PendingVaultDelete,
        occurrence: VaultWatchOccurrence,
        absolute_path: Path,
    ) -> PendingVaultDelete:
        pending = self.events.find_pending_deletes(page_id=candidate.page_id)
        page = self.events.page_by_path(candidate.old_page_path)
        if (
            len(pending) != 1
            or pending[0].id != candidate.id
            or page is None
            or str(page.get("page_id")) != candidate.page_id
            or self._absolute_path(candidate.old_page_path).exists()
            or not absolute_path.exists()
            or self.events.has_active_intent(candidate.page_id)
        ):
            raise RevisionConflict(
                "rename evidence changed",
                current_revision_id=None,
            )
        return pending[0]

    async def _ingest(
        self,
        occurrence: VaultWatchOccurrence,
        observation: FileObservationInput,
    ) -> None:
        result, cancellation_requested = await self._await_revision_mutation(
            self.revisions.ingest_external_change,
            occurrence.id,
            occurrence.page_path,
            observation,
        )
        self._finish_result(occurrence, result)
        self._propagate_cancellation(cancellation_requested)

    async def _apply_pending_move_arrival(
        self,
        occurrence: VaultWatchOccurrence,
        pending: PendingVaultDelete,
        observation: FileObservationInput,
        absolute_path: Path,
        claim_owner: str,
    ) -> None:
        claim_resolved = False
        cancellation_in_flight = False
        try:
            current_pending = self.events.get_pending_delete(pending.id)
            page = self.events.page_by_path(pending.old_page_path)
            if (
                current_pending is None
                or current_pending.claim_owner != claim_owner
                or page is None
                or str(page.get("page_id")) != pending.page_id
                or self._absolute_path(pending.old_page_path).exists()
                or not absolute_path.exists()
                or self.events.has_active_intent(pending.page_id)
                or self._managed_page_id(observation) != pending.page_id
            ):
                raise RevisionConflict(
                    "rename evidence changed",
                    current_revision_id=None,
                )
            if observation.file_hash == pending.file_hash:
                mutation = self.revisions.rename_page
                args = (
                    occurrence.id,
                    pending.old_page_path,
                    occurrence.page_path,
                    pending.id,
                    claim_owner,
                )
            else:
                mutation = self.revisions.relocate_external_change
                args = (
                    occurrence.id,
                    pending.old_page_path,
                    occurrence.page_path,
                    pending.page_id,
                    observation,
                    pending.id,
                    claim_owner,
                )
            result, cancellation_requested = await self._await_claimed_mutation(
                current_pending,
                claim_owner,
                mutation,
                *args,
                allow_resolved_claim=True,
            )
            remaining = self.events.get_pending_delete(pending.id)
            if remaining is None:
                claim_resolved = True
            elif (
                remaining.claim_owner == claim_owner
                and result.status in {"renamed", "applied", "ignored"}
            ):
                claim_resolved = self.events.cancel_claimed_delete(
                    pending.id,
                    claim_owner,
                    occurrence.id,
                    now=datetime.now(timezone.utc),
                )
            if not claim_resolved:
                raise RevisionConflict(
                    "live move lost its pending delete claim",
                    current_revision_id=None,
                )
            self._finish_result(occurrence, result)
            self._propagate_cancellation(cancellation_requested)
        except asyncio.CancelledError:
            cancellation_in_flight = True
            raise
        finally:
            if not claim_resolved:
                try:
                    self.events.release_delete_claim(
                        pending.id,
                        claim_owner,
                    )
                except Exception as exc:
                    self._record_delete_expiry_failure(pending, exc)
                    if not cancellation_in_flight:
                        raise

    async def _process_add_or_modify(
        self,
        occurrence: VaultWatchOccurrence,
        *,
        stability_poll_interval: float,
    ) -> None:
        absolute_path = self._absolute_path(occurrence.page_path)
        observation = await wait_for_stable_observation(
            absolute_path,
            self.limits,
            timeout_seconds=self.settings.vault_watch_stability_timeout_seconds,
            poll_interval=stability_poll_interval,
        )

        same_path = self.events.find_pending_delete_for_path(occurrence.page_path)
        if same_path is not None:
            evidence = self._same_path_evidence(
                same_path,
                occurrence,
                absolute_path,
            )
            pending = await self._cancel_delete_after_claim(
                evidence,
                occurrence.id,
                poll_interval=stability_poll_interval,
            )
            async with self._page_lock(same_path.page_id):
                if (
                    not absolute_path.exists()
                    or self.events.has_active_intent(same_path.page_id)
                ):
                    raise RevisionConflict(
                        "same-path save evidence changed",
                        current_revision_id=None,
                    )
                observation = await wait_for_stable_observation(
                    absolute_path,
                    self.limits,
                    timeout_seconds=(
                        self.settings.vault_watch_stability_timeout_seconds
                    ),
                    poll_interval=stability_poll_interval,
                )
                arrived_before_deadline = (
                    occurrence.detected_at
                    <= (pending or evidence).expires_at
                )
                result, cancellation_requested = await self._await_revision_mutation(
                    self.revisions.ingest_external_change,
                    occurrence.id,
                    occurrence.page_path,
                    observation,
                )
                self._finish_result(
                    occurrence,
                    result,
                    error_summary=(
                        None
                        if arrived_before_deadline
                        else "reappeared after delete grace"
                    ),
                )
                self._propagate_cancellation(cancellation_requested)
            return

        page_id = self._managed_page_id(observation)
        if page_id is not None:
            self.events.find_pending_deletes(page_id=page_id)
        lock_key = page_id or occurrence.page_path
        resolution_ready = False
        resolved_pending: PendingVaultDelete | None = None
        resolved_owner: str | None = None
        while True:
            wait_candidate: PendingVaultDelete | None = None
            async with self._page_lock(lock_key):
                observation = await wait_for_stable_observation(
                    absolute_path,
                    self.limits,
                    timeout_seconds=(
                        self.settings.vault_watch_stability_timeout_seconds
                    ),
                    poll_interval=stability_poll_interval,
                )
                page_id = self._managed_page_id(observation)
                fresh_lock_key = page_id or occurrence.page_path
                if fresh_lock_key != lock_key:
                    if resolution_ready:
                        raise RevisionConflict(
                            "managed identity changed while waiting for delete claim",
                            current_revision_id=None,
                        )
                    lock_key = fresh_lock_key
                    continue

                if resolution_ready:
                    if resolved_pending is None:
                        await self._ingest(occurrence, observation)
                    else:
                        await self._apply_pending_move_arrival(
                            occurrence,
                            resolved_pending,
                            observation,
                            absolute_path,
                            resolved_owner or "",
                        )
                    return

                candidates = (
                    self.events.find_pending_deletes(page_id=page_id)
                    if page_id is not None
                    else []
                )
                if not candidates:
                    await self._ingest(occurrence, observation)
                    return
                if len(candidates) > 1:
                    candidate_paths = ", ".join(
                        candidate.old_page_path for candidate in candidates
                    )
                    issue = self.events.upsert_sync_issue(
                        page_path=occurrence.page_path,
                        file_hash=observation.file_hash,
                        page_id=page_id,
                        issue_type="ambiguous_rename",
                        error_summary=(
                            "multiple pending deletes match managed page identity: "
                            f"{candidate_paths}"
                        ),
                    )
                    self.events.finish_occurrence(
                        occurrence.id,
                        "invalid",
                        page_id=page_id,
                        sync_issue_id=issue.id,
                    )
                    return

                candidate = candidates[0]
                evidence = self._rename_evidence(
                    candidate,
                    occurrence,
                    absolute_path,
                )
                wait_candidate = evidence
            if wait_candidate is not None:
                resolved_owner = f"vclaim_move_{uuid.uuid4().hex}"
                resolved_pending = await self._claim_delete_after_wait(
                    wait_candidate,
                    resolved_owner,
                    poll_interval=stability_poll_interval,
                )
                resolution_ready = True

    async def _handle_occurrence(
        self,
        occurrence: VaultWatchOccurrence,
        *,
        stability_poll_interval: float,
    ) -> bool:
        try:
            if occurrence.kind == "delete":
                await self._queue_delete(occurrence, occurrence.detected_at)
            elif occurrence.kind in {"rename", "relocate"}:
                await self._replay_startup_move(
                    occurrence,
                    stability_poll_interval=stability_poll_interval,
                )
            else:
                async with self._semaphore:
                    await self._process_add_or_modify(
                        occurrence,
                        stability_poll_interval=stability_poll_interval,
                    )
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = str(exc)[:500]
            self.last_error = error
            try:
                self.events.finish_occurrence(
                    occurrence.id,
                    "failed",
                    error_summary=error,
                )
            except VaultOccurrenceConflict:
                pass
            return True

    async def handle_batch(
        self,
        events: list[VaultFsEvent],
        detected_at: datetime | None = None,
        stability_poll_interval: float = 0.05,
    ) -> None:
        detected_at = detected_at or datetime.now(timezone.utc)
        if detected_at.tzinfo is None:
            raise ValueError("detected_at must be timezone-aware")

        self.running = True
        try:
            occurrences = [
                self.events.begin_occurrence(
                    event.kind,
                    event.page_path,
                    detected_at=detected_at,
                )
                for event in events
            ]
            for occurrence in occurrences:
                self._remember_event_time(occurrence.detected_at)

            for occurrence in occurrences:
                if occurrence.kind == "delete":
                    await self._handle_occurrence(
                        occurrence,
                        stability_poll_interval=stability_poll_interval,
                    )

            await asyncio.gather(
                *(
                    self._handle_occurrence(
                        occurrence,
                        stability_poll_interval=stability_poll_interval,
                    )
                    for occurrence in occurrences
                    if occurrence.kind in {"add", "modify"}
                )
            )
        finally:
            self.running = False

    def snapshot(self) -> dict[str, int | str | bool | None]:
        snapshot: dict[str, int | str | bool | None] = dict(
            self.events.status_snapshot()
        )
        persistent_event_at = snapshot.get("last_event_at")
        memory_event_at = self.last_event_at
        if memory_event_at is not None:
            if persistent_event_at is None:
                snapshot["last_event_at"] = memory_event_at.isoformat()
            else:
                parsed = datetime.fromisoformat(str(persistent_event_at))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if memory_event_at > parsed:
                    snapshot["last_event_at"] = memory_event_at.isoformat()
        if snapshot.get("last_error") is None and self.last_error is not None:
            snapshot["last_error"] = self.last_error
        snapshot["running"] = self.watcher_running
        return snapshot
