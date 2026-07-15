from __future__ import annotations

import asyncio
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
    VaultWatchOccurrence,
)
from app.vault_watcher import VaultFsEvent, wait_for_stable_observation
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
        self.running = False
        self.last_event_at: datetime | None = None
        self.last_error: str | None = None

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
            async with self._page_lock(same_path.page_id):
                observation = await wait_for_stable_observation(
                    absolute_path,
                    self.limits,
                    timeout_seconds=(
                        self.settings.vault_watch_stability_timeout_seconds
                    ),
                    poll_interval=stability_poll_interval,
                )
                pending = self._same_path_evidence(
                    same_path,
                    occurrence,
                    absolute_path,
                )
                arrived_before_deadline = occurrence.detected_at <= pending.expires_at
                if not self.events.cancel_delete(pending.id, occurrence.id):
                    raise RevisionConflict(
                        "same-path save evidence changed",
                        current_revision_id=None,
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
        while True:
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
                    lock_key = fresh_lock_key
                    continue

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
                pending = self._rename_evidence(
                    candidate,
                    occurrence,
                    absolute_path,
                )
                if observation.file_hash == pending.file_hash:
                    result, cancellation_requested = (
                        await self._await_revision_mutation(
                            self.revisions.rename_page,
                            occurrence.id,
                            pending.old_page_path,
                            occurrence.page_path,
                        )
                    )
                else:
                    result, cancellation_requested = (
                        await self._await_revision_mutation(
                            self.revisions.relocate_external_change,
                            occurrence.id,
                            pending.old_page_path,
                            occurrence.page_path,
                            pending.page_id,
                            observation,
                        )
                    )
                if result.status in {"renamed", "applied", "ignored"}:
                    self.events.cancel_delete(pending.id, occurrence.id)
                self._finish_result(occurrence, result)
                self._propagate_cancellation(cancellation_requested)
                return

    async def _handle_occurrence(
        self,
        occurrence: VaultWatchOccurrence,
        *,
        stability_poll_interval: float,
    ) -> None:
        try:
            if occurrence.kind == "delete":
                await self._queue_delete(occurrence, occurrence.detected_at)
            else:
                async with self._semaphore:
                    await self._process_add_or_modify(
                        occurrence,
                        stability_poll_interval=stability_poll_interval,
                    )
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
        snapshot["running"] = self.running
        return snapshot
