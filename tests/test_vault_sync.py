import asyncio
import hashlib
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.db import connect_app, connect_app_write, init_app_db
from app.vault_events import VaultEventStore
from app.vault_sync import VaultSyncService
from app.vault_watcher import VaultFsEvent
from app.wiki_markdown import FileObservationInput


@pytest.fixture
def settings(tmp_path):
    configured = get_settings().model_copy(
        deep=True,
        update={
            "database_backend": "sqlite",
            "database_path": tmp_path / "vault-sync.db",
            "vault_path": tmp_path / "vault",
            "vault_watch_enabled": False,
            "projection_worker_enabled": False,
            "vault_watch_stability_timeout_seconds": 0.5,
            "vault_watch_max_file_bytes": 1024 * 1024,
            "vault_watch_max_prefix_bytes": 64 * 1024,
            "vault_rename_grace_ms": 5000,
            "vault_watch_concurrency": 8,
        },
    )
    configured.vault_path.mkdir(parents=True)
    init_app_db(configured)
    return configured


def managed_bytes(page_id: str, *, body: str = "Managed body") -> bytes:
    return external_page_bytes(page_id=page_id, body=body)


def external_page_bytes(
    *,
    page_id: str | None = None,
    title: str = "External",
    body: str = "External body",
) -> bytes:
    identity = f"lgdo_page_id: {page_id}\n" if page_id is not None else ""
    return (
        "---\n"
        f"{identity}"
        f"title: {title}\n"
        "source_ids: []\n"
        "domain: product\n"
        "page_type: feature\n"
        "review_status: draft\n"
        "owner:\n"
        "---\n"
        f"# {title}\n{body}\n"
    ).encode("utf-8")


def seed_page(
    settings,
    *,
    page_id: str,
    page_path: str,
    content: bytes,
) -> None:
    timestamp = "2026-07-15T00:00:00+00:00"
    with connect_app_write(settings) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,page_id,domain,page_type,title,source_ids_json,review_status,
              created_at,updated_at,current_revision_id,revision_number,
              file_hash,semantic_hash,lifecycle_status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                page_path,
                page_id,
                "product",
                "feature",
                "Managed",
                "[]",
                "draft",
                timestamp,
                timestamp,
                f"wrev_{page_id}",
                1,
                hashlib.sha256(content).hexdigest(),
                hashlib.sha256(b"semantic:" + content).hexdigest(),
                "active",
            ),
        )


@dataclass(frozen=True)
class FakeResult:
    status: str
    page_id: str | None = "page_result"
    revision_id: str | None = "wrev_result"
    sync_issue_id: str | None = None


class FakeRevisionService:
    def __init__(self, *, failing_paths=(), projection_jobs=()):
        self.calls: list[tuple] = []
        self.failing_paths = set(failing_paths)
        self.projection_jobs = list(projection_jobs)

    def ingest_external_change(self, event_id, page_path, observation):
        self.calls.append(("ingest", event_id, page_path, observation.file_hash))
        if page_path in self.failing_paths:
            raise RuntimeError(f"bad add: {page_path}")
        return FakeResult("applied")

    def rename_page(self, event_id, old_path, new_path):
        self.calls.append(("rename", event_id, old_path, new_path))
        return FakeResult("renamed")

    def relocate_external_change(
        self,
        event_id,
        old_path,
        new_path,
        expected_page_id,
        observation,
    ):
        self.calls.append(
            (
                "relocate",
                event_id,
                old_path,
                new_path,
                expected_page_id,
                observation.file_hash,
            )
        )
        return FakeResult("applied", page_id=expected_page_id)

    def delete_page(self, event_id, page_path):
        self.calls.append(("delete", event_id, page_path))
        return FakeResult("deleted")

    def ensure_projection_jobs(self, page_id=None):
        self.calls.append(("ensure_projection_jobs", page_id))
        return self.projection_jobs


def write_page(settings, page_path: str, content: bytes) -> None:
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def remove_page(settings, page_path: str) -> None:
    target = settings.vault_path / page_path
    if target.exists():
        target.unlink()


def observation_for(content: bytes, *, mtime_ns: int = 1) -> FileObservationInput:
    return FileObservationInput(
        file_hash=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        mtime_ns=mtime_ns,
        content_bytes=content,
        content_prefix=None,
        content_truncated=False,
    )


async def handle(
    service: VaultSyncService,
    events: list[VaultFsEvent],
    *,
    detected_at: datetime,
) -> None:
    await service.handle_batch(
        events,
        detected_at=detected_at,
        stability_poll_interval=0.001,
    )


@pytest.mark.asyncio
async def test_exact_pending_rename_uses_rename_evidence(settings):
    page_id = "page_exact_rename"
    old_path = "wiki/product/old-exact.md"
    new_path = "wiki/product/new-exact.md"
    content = managed_bytes(page_id)
    seed_page(
        settings,
        page_id=page_id,
        page_path=old_path,
        content=content,
    )
    write_page(settings, old_path, content)
    remove_page(settings, old_path)
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)
    detected_at = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)

    await handle(
        service,
        [VaultFsEvent("delete", old_path)],
        detected_at=detected_at,
    )
    write_page(settings, new_path, content)
    await handle(
        service,
        [VaultFsEvent("add", new_path)],
        detected_at=detected_at + timedelta(milliseconds=100),
    )

    assert [call[0] for call in revisions.calls] == ["rename"]
    assert revisions.calls[0][2:] == (old_path, new_path)


@pytest.mark.asyncio
async def test_edited_pending_rename_uses_relocate_evidence(settings):
    page_id = "page_edited_rename"
    old_path = "wiki/product/old-edited.md"
    new_path = "wiki/product/new-edited.md"
    original = managed_bytes(page_id)
    edited = managed_bytes(page_id, body="Edited after move")
    seed_page(
        settings,
        page_id=page_id,
        page_path=old_path,
        content=original,
    )
    write_page(settings, old_path, original)
    remove_page(settings, old_path)
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)
    detected_at = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)

    await handle(
        service,
        [VaultFsEvent("delete", old_path)],
        detected_at=detected_at,
    )
    write_page(settings, new_path, edited)
    await handle(
        service,
        [VaultFsEvent("add", new_path)],
        detected_at=detected_at + timedelta(milliseconds=100),
    )

    assert [call[0] for call in revisions.calls] == ["relocate"]
    assert revisions.calls[0][2:5] == (old_path, new_path, page_id)


@pytest.mark.asyncio
async def test_pending_rename_uses_fresh_observation_under_page_lock(
    settings,
    monkeypatch,
):
    page_id = "page_fresh_rename"
    old_path = "wiki/product/fresh-old.md"
    new_path = "wiki/product/fresh-new.md"
    original = managed_bytes(page_id)
    edited = managed_bytes(page_id, body="Edited before page lock")
    seed_page(
        settings,
        page_id=page_id,
        page_path=old_path,
        content=original,
    )
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)
    detected_at = datetime(2026, 7, 15, 2, 30, tzinfo=timezone.utc)
    await handle(
        service,
        [VaultFsEvent("delete", old_path)],
        detected_at=detected_at,
    )
    write_page(settings, new_path, original)
    exact_observation = observation_for(original, mtime_ns=1)
    edited_observation = observation_for(edited, mtime_ns=2)
    observations = 0

    async def change_before_page_lock(path, *_args, **_kwargs):
        nonlocal observations
        observations += 1
        if observations == 1:
            path.write_bytes(edited)
            return exact_observation
        return edited_observation

    monkeypatch.setattr(
        "app.vault_sync.wait_for_stable_observation",
        change_before_page_lock,
    )

    await handle(
        service,
        [VaultFsEvent("add", new_path)],
        detected_at=detected_at + timedelta(milliseconds=100),
    )

    assert observations == 2
    assert [call[0] for call in revisions.calls] == ["relocate"]
    assert revisions.calls[0][-1] == edited_observation.file_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("edited", [False, True], ids=["exact", "edited"])
async def test_same_path_pending_rename_is_atomic_save(settings, edited):
    page_id = f"page_same_path_{edited}"
    page_path = f"wiki/product/same-path-{edited}.md"
    original = managed_bytes(page_id)
    arrived = (
        managed_bytes(page_id, body="Edited atomic save") if edited else original
    )
    seed_page(
        settings,
        page_id=page_id,
        page_path=page_path,
        content=original,
    )
    write_page(settings, page_path, arrived)
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)

    await handle(
        service,
        [VaultFsEvent("delete", page_path), VaultFsEvent("add", page_path)],
        detected_at=datetime(2026, 7, 15, 3, 0, tzinfo=timezone.utc),
    )

    assert [call[0] for call in revisions.calls] == ["ingest"]
    with connect_app(settings) as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM pending_vault_deletes WHERE status='pending'"
        ).fetchone()[0]
    assert pending == 0


@pytest.mark.asyncio
async def test_active_id_at_different_path_without_delete_evidence_ingests(settings):
    page_id = "page_active_elsewhere"
    active_path = "wiki/product/active.md"
    arriving_path = "wiki/product/unproven-copy.md"
    content = managed_bytes(page_id)
    seed_page(
        settings,
        page_id=page_id,
        page_path=active_path,
        content=content,
    )
    write_page(settings, active_path, content)
    write_page(settings, arriving_path, content)
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)

    await handle(
        service,
        [VaultFsEvent("add", arriving_path)],
        detected_at=datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc),
    )

    assert [call[0] for call in revisions.calls] == ["ingest"]


@pytest.mark.asyncio
async def test_invalid_id_does_not_match_pending_rename_evidence(settings):
    page_id = "page_invalid_identity"
    old_path = "wiki/product/invalid-id-old.md"
    new_path = "wiki/product/invalid-id-new.md"
    original = managed_bytes(page_id)
    conflicting = original.replace(
        f"lgdo_page_id: {page_id}\n".encode(),
        f"lgdo_page_id: {page_id}\nid: page_other\n".encode(),
    )
    seed_page(
        settings,
        page_id=page_id,
        page_path=old_path,
        content=original,
    )
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)
    detected_at = datetime(2026, 7, 15, 4, 30, tzinfo=timezone.utc)

    await handle(
        service,
        [VaultFsEvent("delete", old_path)],
        detected_at=detected_at,
    )
    write_page(settings, new_path, conflicting)
    await handle(
        service,
        [VaultFsEvent("add", new_path)],
        detected_at=detected_at + timedelta(milliseconds=100),
    )

    assert [call[0] for call in revisions.calls] == ["ingest"]


@pytest.mark.asyncio
async def test_event_failure_does_not_cancel_siblings_or_future_batches(settings):
    good_first = "wiki/product/good-first.md"
    bad = "wiki/product/bad.md"
    good_later = "wiki/product/good-later.md"
    for index, page_path in enumerate((good_first, bad, good_later)):
        write_page(settings, page_path, external_page_bytes(title=f"Page {index}"))
    revisions = FakeRevisionService(failing_paths={bad})
    service = VaultSyncService(settings, revisions=revisions)
    detected_at = datetime(2026, 7, 15, 5, 0, tzinfo=timezone.utc)

    await handle(
        service,
        [VaultFsEvent("add", good_first), VaultFsEvent("add", bad)],
        detected_at=detected_at,
    )
    await handle(
        service,
        [VaultFsEvent("add", good_later)],
        detected_at=detected_at + timedelta(seconds=1),
    )

    attempted_paths = [call[2] for call in revisions.calls]
    assert set(attempted_paths[:2]) == {good_first, bad}
    assert attempted_paths[2] == good_later
    with connect_app(settings) as conn:
        failed = conn.execute(
            "SELECT COUNT(*) FROM vault_watch_occurrences WHERE status='failed'"
        ).fetchone()[0]
    assert failed == 1


@pytest.mark.asyncio
async def test_page_lock_registry_is_empty_after_unique_pages(settings):
    events = []
    for index in range(100):
        page_path = f"wiki/product/unique-{index}.md"
        write_page(settings, page_path, external_page_bytes(title=f"Page {index}"))
        events.append(VaultFsEvent("add", page_path))
    service = VaultSyncService(settings, revisions=FakeRevisionService())

    await handle(
        service,
        events,
        detected_at=datetime(2026, 7, 15, 6, 0, tzinfo=timezone.utc),
    )

    assert service._page_locks == {}


@pytest.mark.asyncio
async def test_page_lock_waiter_repeated_cancellation_does_not_leak(settings):
    service = VaultSyncService(settings, revisions=FakeRevisionService())
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()

    async def hold_page_lock():
        async with service._page_lock("page_cancelled_waiter"):
            holder_entered.set()
            await release_holder.wait()

    async def wait_for_page_lock():
        async with service._page_lock("page_cancelled_waiter"):
            pytest.fail("cancelled waiter acquired the page lock")

    holder = asyncio.create_task(hold_page_lock())
    await holder_entered.wait()
    waiter = asyncio.create_task(wait_for_page_lock())
    while service._page_locks["page_cancelled_waiter"].users != 2:
        await asyncio.sleep(0)

    await service._page_locks_guard.acquire()
    waiter.cancel()
    await asyncio.sleep(0)
    waiter.cancel()
    service._page_locks_guard.release()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release_holder.set()
    await holder

    assert service._page_locks == {}


@pytest.mark.asyncio
async def test_cancelled_revision_mutation_holds_page_lock_until_finalized(
    settings,
    monkeypatch,
):
    page_path = "wiki/product/cancelled-mutation.md"
    content = external_page_bytes(title="Cancelled mutation")
    observation = observation_for(content)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    async def stable_observation(*_args, **_kwargs):
        return observation

    monkeypatch.setattr(
        "app.vault_sync.wait_for_stable_observation",
        stable_observation,
    )

    class BlockingRevisionService(FakeRevisionService):
        def ingest_external_change(self, event_id, path, observed):
            self.calls.append(("ingest", event_id, path, observed.file_hash))
            if len(self.calls) == 1:
                first_entered.set()
                if not release_first.wait(timeout=5):
                    raise RuntimeError("blocked mutation was not released")
            else:
                second_entered.set()
            return FakeResult("applied")

    revisions = BlockingRevisionService()
    service = VaultSyncService(settings, revisions=revisions)
    first_at = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
    first = asyncio.create_task(
        handle(
            service,
            [VaultFsEvent("modify", page_path)],
            detected_at=first_at,
        )
    )
    while not first_entered.is_set():
        await asyncio.sleep(0)

    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    second = asyncio.create_task(
        handle(
            service,
            [VaultFsEvent("modify", page_path)],
            detected_at=first_at + timedelta(seconds=1),
        )
    )
    try:
        for _ in range(10):
            await asyncio.sleep(0)
        assert first.done() is False
        assert second_entered.is_set() is False
    finally:
        release_first.set()

    first_result, second_result = await asyncio.gather(
        first,
        second,
        return_exceptions=True,
    )
    assert isinstance(first_result, asyncio.CancelledError)
    assert second_result is None
    with connect_app(settings) as conn:
        statuses = [
            row["status"]
            for row in conn.execute(
                """
                SELECT status FROM vault_watch_occurrences
                WHERE page_path=? ORDER BY detected_at,id
                """,
                (page_path,),
            ).fetchall()
        ]
    assert statuses == ["applied", "applied"]
    assert service._page_locks == {}


@pytest.mark.asyncio
async def test_ambiguous_pending_rename_persists_issue_without_moving(settings):
    page_id = "page_ambiguous_rename"
    old_path = "wiki/product/ambiguous-old.md"
    other_old_path = "wiki/product/ambiguous-other-old.md"
    new_path = "wiki/product/ambiguous-new.md"
    content = managed_bytes(page_id)
    seed_page(
        settings,
        page_id=page_id,
        page_path=old_path,
        content=content,
    )
    detected_at = datetime(2026, 7, 15, 7, 0, tzinfo=timezone.utc)
    store = VaultEventStore(settings)
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions, events=store)

    await handle(
        service,
        [VaultFsEvent("delete", old_path)],
        detected_at=detected_at,
    )
    second_delete = store.begin_occurrence(
        "delete",
        other_old_path,
        detected_at=detected_at,
    )
    store.get_or_create_pending_delete(
        occurrence_id=second_delete.id,
        page_id=page_id,
        old_page_path=other_old_path,
        file_hash=hashlib.sha256(content).hexdigest(),
        semantic_hash=None,
        detected_at=detected_at,
        expires_at=detected_at + timedelta(seconds=5),
    )
    store.finish_occurrence(second_delete.id, "deferred", page_id=page_id)
    write_page(settings, new_path, content)

    await handle(
        service,
        [VaultFsEvent("add", new_path)],
        detected_at=detected_at + timedelta(milliseconds=100),
    )

    assert revisions.calls == []
    with connect_app(settings) as conn:
        occurrence = dict(
            conn.execute(
                """
                SELECT * FROM vault_watch_occurrences
                WHERE kind='add' AND page_path=? ORDER BY detected_at DESC,id DESC
                LIMIT 1
                """,
                (new_path,),
            ).fetchone()
        )
        issue = dict(
            conn.execute(
                "SELECT * FROM vault_sync_issues WHERE id=?",
                (occurrence["sync_issue_id"],),
            ).fetchone()
        )
    assert occurrence["status"] == "invalid"
    assert occurrence["result_page_id"] == page_id
    assert issue["issue_type"] == "ambiguous_rename"
    assert issue["page_path"] == new_path
    assert issue["file_hash"] == hashlib.sha256(content).hexdigest()
    assert issue["page_id"] == page_id
    assert issue["generation"] == 1

    await handle(
        service,
        [VaultFsEvent("add", new_path)],
        detected_at=detected_at + timedelta(seconds=1),
    )
    with connect_app(settings) as conn:
        replayed_issue = dict(
            conn.execute(
                "SELECT * FROM vault_sync_issues WHERE id=?",
                (issue["id"],),
            ).fetchone()
        )
    assert revisions.calls == []
    assert replayed_issue["generation"] == 2


@pytest.mark.asyncio
async def test_ambiguous_candidates_are_rechecked_under_page_lock(
    settings,
    monkeypatch,
):
    page_id = "page_ambiguous_race"
    old_path = "wiki/product/ambiguous-race-old.md"
    other_old_path = "wiki/product/ambiguous-race-other.md"
    new_path = "wiki/product/ambiguous-race-new.md"
    content = managed_bytes(page_id)
    seed_page(
        settings,
        page_id=page_id,
        page_path=old_path,
        content=content,
    )
    detected_at = datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc)
    store = VaultEventStore(settings)
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions, events=store)
    await handle(
        service,
        [VaultFsEvent("delete", old_path)],
        detected_at=detected_at,
    )
    second_delete = store.begin_occurrence(
        "delete",
        other_old_path,
        detected_at=detected_at,
    )
    second = store.get_or_create_pending_delete(
        occurrence_id=second_delete.id,
        page_id=page_id,
        old_page_path=other_old_path,
        file_hash=hashlib.sha256(content).hexdigest(),
        semantic_hash=None,
        detected_at=detected_at,
        expires_at=detected_at + timedelta(seconds=5),
    )
    store.finish_occurrence(second_delete.id, "deferred", page_id=page_id)
    write_page(settings, new_path, content)

    original_find = store.find_pending_deletes
    reads = 0

    def cancel_candidate_after_initial_read(*, page_id):
        nonlocal reads
        candidates = original_find(page_id=page_id)
        reads += 1
        if reads == 1:
            assert len(candidates) == 2
            assert store.cancel_delete(second.id, "vocc_racing_cancel") is True
        return candidates

    monkeypatch.setattr(
        store,
        "find_pending_deletes",
        cancel_candidate_after_initial_read,
    )

    await handle(
        service,
        [VaultFsEvent("add", new_path)],
        detected_at=detected_at + timedelta(milliseconds=100),
    )

    assert [call[0] for call in revisions.calls] == ["rename"]
    with connect_app(settings) as conn:
        occurrence = conn.execute(
            """
            SELECT status,sync_issue_id FROM vault_watch_occurrences
            WHERE kind='add' AND page_path=?
            """,
            (new_path,),
        ).fetchone()
        issue_count = conn.execute(
            "SELECT COUNT(*) FROM vault_sync_issues WHERE page_path=?",
            (new_path,),
        ).fetchone()[0]
    assert tuple(occurrence) == ("renamed", None)
    assert issue_count == 0


@pytest.mark.asyncio
async def test_delete_expiry_waits_for_predeadline_unclassified_add(settings):
    page_id = "page_delete_barrier"
    old_path = "wiki/product/delete-barrier-old.md"
    new_path = "wiki/product/delete-barrier-new.md"
    content = managed_bytes(page_id)
    seed_page(settings, page_id=page_id, page_path=old_path, content=content)
    revisions = FakeRevisionService()
    store = VaultEventStore(settings)
    service = VaultSyncService(settings, revisions=revisions, events=store)
    detected_at = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)

    await handle(
        service,
        [VaultFsEvent("delete", old_path)],
        detected_at=detected_at,
    )
    store.begin_occurrence(
        "add",
        new_path,
        detected_at=detected_at + timedelta(seconds=4),
    )

    expired = await service.expire_deletes(
        now=detected_at + timedelta(seconds=6)
    )

    assert expired == 0
    assert revisions.calls == []


@pytest.mark.asyncio
async def test_true_delete_rechecks_path_and_intent_then_applies(settings):
    page_id = "page_true_delete"
    old_path = "wiki/product/true-delete.md"
    content = managed_bytes(page_id)
    seed_page(settings, page_id=page_id, page_path=old_path, content=content)
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)
    detected_at = datetime(2026, 7, 15, 10, 30, tzinfo=timezone.utc)

    await handle(
        service,
        [VaultFsEvent("delete", old_path)],
        detected_at=detected_at,
    )

    expired = await service.expire_deletes(
        now=detected_at + timedelta(seconds=6)
    )

    assert expired == 1
    assert [call[0] for call in revisions.calls] == ["delete"]
    assert revisions.calls[0][2] == old_path


@pytest.mark.asyncio
async def test_startup_unique_missing_path_is_explicit_offline_rename_evidence(
    settings,
):
    page_id = "page_startup_exact_move"
    old_path = "wiki/product/startup-exact-old.md"
    new_path = "wiki/product/startup-exact-new.md"
    content = managed_bytes(page_id)
    seed_page(settings, page_id=page_id, page_path=old_path, content=content)
    write_page(settings, new_path, content)
    revisions = FakeRevisionService(projection_jobs=["projection-1"])
    service = VaultSyncService(settings, revisions=revisions)

    result = await service.reconcile_startup(
        stability_poll_interval=0.001,
        reconcile_intents=False,
    )

    assert result == {
        "renamed": 1,
        "relocated": 0,
        "ingested": 0,
        "missing": 0,
        "replayed": 0,
        "duplicate_page_ids": 0,
        "failed": 0,
        "projection_jobs": 1,
    }
    assert [call[0] for call in revisions.calls] == [
        "rename",
        "ensure_projection_jobs",
    ]
    assert revisions.calls[0][2:] == (old_path, new_path)


@pytest.mark.asyncio
async def test_startup_edited_unique_missing_path_relocates_external_change(
    settings,
):
    page_id = "page_startup_edited_move"
    old_path = "wiki/product/startup-edited-old.md"
    new_path = "wiki/product/startup-edited-new.md"
    original = managed_bytes(page_id)
    edited = managed_bytes(page_id, body="Edited while the service was offline")
    seed_page(settings, page_id=page_id, page_path=old_path, content=original)
    write_page(settings, new_path, edited)
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)

    result = await service.reconcile_startup(
        stability_poll_interval=0.001,
        reconcile_intents=False,
    )

    assert result["relocated"] == 1
    assert result["renamed"] == 0
    assert [call[0] for call in revisions.calls] == [
        "relocate",
        "ensure_projection_jobs",
    ]
    assert revisions.calls[0][2:5] == (old_path, new_path, page_id)


@pytest.mark.asyncio
async def test_startup_present_original_and_copied_id_is_duplicate(settings):
    page_id = "page_startup_copy"
    old_path = "wiki/product/startup-copy-original.md"
    copied_path = "wiki/product/startup-copy-duplicate.md"
    content = managed_bytes(page_id)
    seed_page(settings, page_id=page_id, page_path=old_path, content=content)
    write_page(settings, old_path, content)
    write_page(settings, copied_path, content)
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)

    result = await service.reconcile_startup(
        stability_poll_interval=0.001,
        reconcile_intents=False,
    )

    assert result["duplicate_page_ids"] == 1
    assert result["renamed"] == 0
    assert result["relocated"] == 0
    assert [call[0] for call in revisions.calls] == ["ensure_projection_jobs"]


@pytest.mark.asyncio
async def test_startup_missing_page_delete_survives_restart_and_expires_once(
    settings,
):
    page_id = "page_startup_missing"
    old_path = "wiki/product/startup-missing.md"
    content = managed_bytes(page_id)
    seed_page(settings, page_id=page_id, page_path=old_path, content=content)
    first_revisions = FakeRevisionService()

    first_result = await VaultSyncService(
        settings,
        revisions=first_revisions,
    ).reconcile_startup(
        stability_poll_interval=0.001,
        reconcile_intents=False,
    )

    assert first_result["missing"] == 1
    assert [call[0] for call in first_revisions.calls] == [
        "ensure_projection_jobs"
    ]
    with connect_app(settings) as conn:
        pending = dict(
            conn.execute(
                "SELECT * FROM pending_vault_deletes WHERE status='pending'"
            ).fetchone()
        )
        occurrence_count = conn.execute(
            "SELECT COUNT(*) FROM vault_watch_occurrences WHERE kind='delete'"
        ).fetchone()[0]
    assert occurrence_count == 1

    restarted_revisions = FakeRevisionService()
    restarted = VaultSyncService(settings, revisions=restarted_revisions)
    expires_at = datetime.fromisoformat(pending["expires_at"])
    assert await restarted.expire_deletes(
        now=expires_at + timedelta(seconds=1)
    ) == 1
    assert await restarted.expire_deletes(
        now=expires_at + timedelta(seconds=2)
    ) == 0
    assert [call[0] for call in restarted_revisions.calls] == ["delete"]


@pytest.mark.asyncio
async def test_startup_restored_same_path_cancels_persistent_delete(
    settings,
):
    page_id = "page_startup_restored_same_path"
    page_path = "wiki/product/startup-restored-same-path.md"
    content = managed_bytes(page_id)
    seed_page(settings, page_id=page_id, page_path=page_path, content=content)
    first = VaultSyncService(settings, revisions=FakeRevisionService())

    first_result = await first.reconcile_startup(
        stability_poll_interval=0.001,
        reconcile_intents=False,
    )

    assert first_result["missing"] == 1
    assert first.snapshot()["pending_deletes"] == 1
    with connect_app(settings) as conn:
        expires_at = datetime.fromisoformat(
            conn.execute(
                "SELECT expires_at FROM pending_vault_deletes WHERE status='pending'"
            ).fetchone()["expires_at"]
        )

    write_page(settings, page_path, content)
    revisions = FakeRevisionService()
    restarted = VaultSyncService(settings, revisions=revisions)

    result = await restarted.reconcile_startup(
        stability_poll_interval=0.001,
        reconcile_intents=False,
    )

    assert restarted.snapshot()["pending_deletes"] == 0
    assert result["ingested"] == 1
    assert [call[0] for call in revisions.calls] == [
        "ingest",
        "ensure_projection_jobs",
    ]
    assert await restarted.expire_deletes(
        now=expires_at + timedelta(seconds=1)
    ) == 0
    assert all(call[0] != "delete" for call in revisions.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("edited", [False, True], ids=["exact", "edited"])
async def test_startup_offline_move_cancels_persistent_delete(
    settings,
    edited,
):
    page_id = f"page_startup_pending_move_{edited}"
    old_path = f"wiki/product/startup-pending-old-{edited}.md"
    new_path = f"wiki/product/startup-pending-new-{edited}.md"
    original = managed_bytes(page_id)
    moved = (
        managed_bytes(page_id, body="Edited while offline")
        if edited
        else original
    )
    seed_page(
        settings,
        page_id=page_id,
        page_path=old_path,
        content=original,
    )
    first = VaultSyncService(settings, revisions=FakeRevisionService())

    first_result = await first.reconcile_startup(
        stability_poll_interval=0.001,
        reconcile_intents=False,
    )

    assert first_result["missing"] == 1
    assert first.snapshot()["pending_deletes"] == 1
    with connect_app(settings) as conn:
        expires_at = datetime.fromisoformat(
            conn.execute(
                "SELECT expires_at FROM pending_vault_deletes WHERE status='pending'"
            ).fetchone()["expires_at"]
        )

    write_page(settings, new_path, moved)
    revisions = FakeRevisionService()
    restarted = VaultSyncService(settings, revisions=revisions)

    result = await restarted.reconcile_startup(
        stability_poll_interval=0.001,
        reconcile_intents=False,
    )

    expected_action = "relocate" if edited else "rename"
    expected_counter = "relocated" if edited else "renamed"
    assert restarted.snapshot()["pending_deletes"] == 0
    assert result[expected_counter] == 1
    assert [call[0] for call in revisions.calls] == [
        expected_action,
        "ensure_projection_jobs",
    ]
    assert await restarted.expire_deletes(
        now=expires_at + timedelta(seconds=1)
    ) == 0
    assert all(call[0] != "delete" for call in revisions.calls)


@pytest.mark.asyncio
async def test_startup_inventory_is_canonical_and_continues_after_failure(
    settings,
    monkeypatch,
):
    good_path = "wiki/product/startup-good.md"
    bad_path = "wiki/product/startup-bad.md"
    hidden_path = "wiki/product/.startup-hidden.md"
    good = external_page_bytes(title="Startup good")
    bad = external_page_bytes(title="Startup bad")
    hidden = external_page_bytes(title="Startup hidden")
    write_page(settings, good_path, good)
    write_page(settings, bad_path, bad)
    write_page(settings, hidden_path, hidden)

    async def observe(path, *_args, **_kwargs):
        if path.name == "startup-bad.md":
            raise RuntimeError("unreadable startup page")
        return observation_for(path.read_bytes())

    monkeypatch.setattr("app.vault_sync.wait_for_stable_observation", observe)
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)

    result = await service.reconcile_startup(
        stability_poll_interval=0.001,
        reconcile_intents=False,
    )

    assert result["ingested"] == 1
    assert result["failed"] == 1
    assert [call[2] for call in revisions.calls if call[0] == "ingest"] == [
        good_path
    ]
    with connect_app(settings) as conn:
        failures = conn.execute(
            "SELECT COUNT(*) FROM vault_watch_occurrences WHERE status='failed'"
        ).fetchone()[0]
    assert failures == 1


@pytest.mark.asyncio
async def test_startup_replays_pending_occurrences_once_and_ignores_missing(
    settings,
):
    existing_path = "wiki/product/startup-replay-add.md"
    missing_path = "wiki/product/startup-replay-missing.md"
    write_page(settings, existing_path, external_page_bytes(title="Replay"))
    store = VaultEventStore(settings)
    detected_at = datetime(2026, 7, 15, 11, 0, tzinfo=timezone.utc)
    store.begin_occurrence("add", existing_path, detected_at=detected_at)
    store.begin_occurrence(
        "modify",
        missing_path,
        detected_at=detected_at + timedelta(seconds=1),
    )
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions, events=store)

    result = await service.reconcile_startup(
        stability_poll_interval=0.001,
        reconcile_intents=False,
    )

    assert result["replayed"] == 2
    assert [call[0] for call in revisions.calls] == [
        "ingest",
        "ensure_projection_jobs",
    ]
    with connect_app(settings) as conn:
        statuses = {
            row["page_path"]: row["status"]
            for row in conn.execute(
                "SELECT page_path,status FROM vault_watch_occurrences"
            ).fetchall()
        }
    assert statuses == {existing_path: "applied", missing_path: "ignored"}
    assert store.pending_occurrences() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_state", ["path", "intent"])
async def test_delete_expiry_rechecks_path_and_intent_under_lock(
    settings,
    monkeypatch,
    changed_state,
):
    page_id = f"page_delete_recheck_{changed_state}"
    old_path = f"wiki/product/delete-recheck-{changed_state}.md"
    content = managed_bytes(page_id)
    seed_page(settings, page_id=page_id, page_path=old_path, content=content)
    store = VaultEventStore(settings)
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions, events=store)
    detected_at = datetime(2026, 7, 15, 11, 30, tzinfo=timezone.utc)
    await handle(
        service,
        [VaultFsEvent("delete", old_path)],
        detected_at=detected_at,
    )
    original_page_lock = service._page_lock
    intent_active = False

    @asynccontextmanager
    async def change_after_lock(key):
        nonlocal intent_active
        async with original_page_lock(key):
            if changed_state == "path":
                write_page(settings, old_path, content)
            else:
                intent_active = True
            yield

    monkeypatch.setattr(service, "_page_lock", change_after_lock)
    monkeypatch.setattr(store, "has_active_intent", lambda _page_id: intent_active)

    assert await service.expire_deletes(
        now=detected_at + timedelta(seconds=6)
    ) == 0
    assert revisions.calls == []


@pytest.mark.asyncio
async def test_delete_expiry_cancellation_drains_before_propagating(settings):
    page_id = "page_delete_cancelled"
    old_path = "wiki/product/delete-cancelled.md"
    content = managed_bytes(page_id)
    seed_page(settings, page_id=page_id, page_path=old_path, content=content)
    mutation_entered = threading.Event()
    release_mutation = threading.Event()

    class BlockingDeleteRevisions(FakeRevisionService):
        def delete_page(self, event_id, page_path):
            self.calls.append(("delete", event_id, page_path))
            mutation_entered.set()
            if not release_mutation.wait(timeout=5):
                raise RuntimeError("blocked delete was not released")
            return FakeResult("deleted")

    revisions = BlockingDeleteRevisions()
    service = VaultSyncService(settings, revisions=revisions)
    detected_at = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    await handle(
        service,
        [VaultFsEvent("delete", old_path)],
        detected_at=detected_at,
    )
    expiry = asyncio.create_task(
        service.expire_deletes(now=detected_at + timedelta(seconds=6))
    )
    while not mutation_entered.is_set():
        await asyncio.sleep(0)

    expiry.cancel()
    await asyncio.sleep(0)
    expiry.cancel()
    assert expiry.done() is False
    release_mutation.set()
    with pytest.raises(asyncio.CancelledError):
        await expiry

    with connect_app(settings) as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM pending_vault_deletes WHERE status='pending'"
        ).fetchone()[0]
    assert pending == 0
    assert revisions.calls == [("delete", revisions.calls[0][1], old_path)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "same_instance",
    [False, True],
    ids=["cross-instance", "same-instance"],
)
async def test_cross_instance_add_waits_for_claimed_delete_then_restores(
    settings,
    same_instance,
):
    page_id = "page_cross_instance_claim"
    page_path = "wiki/product/cross-instance-claim.md"
    content = managed_bytes(page_id)
    seed_page(settings, page_id=page_id, page_path=page_path, content=content)
    detected_at = datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc)
    delete_entered = threading.Event()
    release_delete = threading.Event()
    ingest_entered = threading.Event()
    cancel_attempted = threading.Event()
    cancel_results: list[bool] = []
    order: list[str] = []

    class OrderedRevisionService(FakeRevisionService):
        def delete_page(self, event_id, path):
            self.calls.append(("delete", event_id, path))
            order.append("delete_enter")
            delete_entered.set()
            if not release_delete.wait(timeout=5):
                raise RuntimeError("blocked cross-instance delete was not released")
            order.append("delete_commit")
            return FakeResult("deleted", page_id=page_id)

        def ingest_external_change(self, event_id, path, observation):
            self.calls.append(("ingest", event_id, path, observation.file_hash))
            order.append("ingest_commit")
            ingest_entered.set()
            return FakeResult("applied", page_id=page_id)

    class ObservedEventStore(VaultEventStore):
        def cancel_delete(self, delete_id, occurrence_id):
            result = super().cancel_delete(delete_id, occurrence_id)
            cancel_results.append(result)
            cancel_attempted.set()
            return result

    revisions = OrderedRevisionService()
    observed_store = ObservedEventStore(settings)
    expirer = VaultSyncService(
        settings,
        revisions=revisions,
        events=observed_store if same_instance else None,
    )
    observer = (
        expirer
        if same_instance
        else VaultSyncService(
            settings,
            revisions=revisions,
            events=observed_store,
        )
    )
    await handle(
        expirer,
        [VaultFsEvent("delete", page_path)],
        detected_at=detected_at,
    )
    expiry = asyncio.create_task(
        expirer.expire_deletes(now=detected_at + timedelta(seconds=6))
    )
    while not delete_entered.is_set():
        await asyncio.sleep(0)

    write_page(settings, page_path, content)
    add = asyncio.create_task(
        handle(
            observer,
            [VaultFsEvent("add", page_path)],
            detected_at=detected_at + timedelta(seconds=6),
        )
    )
    while not cancel_attempted.is_set():
        await asyncio.sleep(0)
    add_was_blocked = (
        cancel_results == [False]
        and not ingest_entered.is_set()
        and not add.done()
    )

    release_delete.set()
    expired, add_result = await asyncio.gather(expiry, add)

    assert add_was_blocked is True
    assert expired == 1
    assert add_result is None
    assert order == ["delete_enter", "delete_commit", "ingest_commit"]
    assert observer.snapshot()["pending_deletes"] == 0
    assert observer.events.pending_occurrences() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("edited", [False, True], ids=["rename", "relocate"])
async def test_startup_move_replay_cleans_predecessor_pending_delete(
    settings,
    edited,
):
    page_id = f"page_replayed_startup_move_{edited}"
    old_path = f"wiki/product/replayed-startup-old-{edited}.md"
    new_path = f"wiki/product/replayed-startup-new-{edited}.md"
    original = managed_bytes(page_id)
    moved = managed_bytes(page_id, body="Edited replay") if edited else original
    seed_page(settings, page_id=page_id, page_path=old_path, content=original)
    detected_at = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    store = VaultEventStore(settings)
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions, events=store)
    await handle(
        service,
        [VaultFsEvent("delete", old_path)],
        detected_at=detected_at,
    )
    write_page(settings, new_path, moved)
    kind = "relocate" if edited else "rename"
    move = store.begin_occurrence(
        kind,
        new_path,
        old_page_path=old_path,
        detected_at=detected_at + timedelta(seconds=1),
    )

    result = await service.reconcile_startup(
        stability_poll_interval=0.001,
        reconcile_intents=False,
    )

    with connect_app(settings) as conn:
        move_status = conn.execute(
            "SELECT status FROM vault_watch_occurrences WHERE id=?",
            (move.id,),
        ).fetchone()["status"]
    assert result["replayed"] == 1
    assert move_status == ("applied" if edited else "renamed")
    assert service.snapshot()["pending_deletes"] == 0
    assert [call[0] for call in revisions.calls] == [
        kind,
        "ensure_projection_jobs",
    ]


@pytest.mark.asyncio
async def test_startup_repeated_missing_cycle_uses_new_delete_occurrence(settings):
    page_id = "page_repeated_missing_cycle"
    page_path = "wiki/product/repeated-missing-cycle.md"
    content = managed_bytes(page_id)
    seed_page(settings, page_id=page_id, page_path=page_path, content=content)

    first = VaultSyncService(settings, revisions=FakeRevisionService())
    await first.reconcile_startup(
        stability_poll_interval=0.001,
        reconcile_intents=False,
    )
    with connect_app(settings) as conn:
        first_pending = dict(
            conn.execute(
                "SELECT * FROM pending_vault_deletes WHERE status='pending'"
            ).fetchone()
        )

    write_page(settings, page_path, content)
    restored = VaultSyncService(settings, revisions=FakeRevisionService())
    await restored.reconcile_startup(
        stability_poll_interval=0.001,
        reconcile_intents=False,
    )
    assert restored.snapshot()["pending_deletes"] == 0
    remove_page(settings, page_path)

    revisions = FakeRevisionService()
    missing_again = VaultSyncService(settings, revisions=revisions)
    result = await missing_again.reconcile_startup(
        stability_poll_interval=0.001,
        reconcile_intents=False,
    )
    with connect_app(settings) as conn:
        pending_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM pending_vault_deletes WHERE status='pending'"
            ).fetchall()
        ]

    assert result["missing"] == 1
    assert len(pending_rows) == 1
    second_pending = pending_rows[0]
    assert second_pending["occurrence_id"] != first_pending["occurrence_id"]
    expires_at = datetime.fromisoformat(second_pending["expires_at"])
    assert await missing_again.expire_deletes(
        now=expires_at + timedelta(seconds=1)
    ) == 1
    assert await missing_again.expire_deletes(
        now=expires_at + timedelta(seconds=2)
    ) == 0
    assert [call[0] for call in revisions.calls].count("delete") == 1


@pytest.mark.asyncio
async def test_delete_expiry_prechecks_existing_path_before_claim(settings):
    page_id = "page_preclaim_path"
    page_path = "wiki/product/preclaim-path.md"
    content = managed_bytes(page_id)
    seed_page(settings, page_id=page_id, page_path=page_path, content=content)
    claim_calls = 0

    class ClaimObservedStore(VaultEventStore):
        def claim_delete(self, *args, **kwargs):
            nonlocal claim_calls
            claim_calls += 1
            return super().claim_delete(*args, **kwargs)

    store = ClaimObservedStore(settings)
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions, events=store)
    detected_at = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)
    await handle(
        service,
        [VaultFsEvent("delete", page_path)],
        detected_at=detected_at,
    )
    write_page(settings, page_path, content)

    assert await service.expire_deletes(
        now=detected_at + timedelta(seconds=6)
    ) == 0
    assert claim_calls == 0
    assert revisions.calls == []


@pytest.mark.asyncio
async def test_delete_expiry_claim_failure_does_not_abort_later_candidates(
    settings,
):
    detected_at = datetime(2026, 7, 15, 15, 30, tzinfo=timezone.utc)
    paths = [
        "wiki/product/claim-failure-first.md",
        "wiki/product/claim-failure-second.md",
    ]
    for index, page_path in enumerate(paths):
        content = managed_bytes(f"page_claim_failure_{index}")
        seed_page(
            settings,
            page_id=f"page_claim_failure_{index}",
            page_path=page_path,
            content=content,
        )

    class FailFirstClaimStore(VaultEventStore):
        def __init__(self, configured_settings):
            super().__init__(configured_settings)
            self.claim_calls = 0

        def claim_delete(self, *args, **kwargs):
            self.claim_calls += 1
            if self.claim_calls == 1:
                raise RuntimeError("claim database unavailable")
            return super().claim_delete(*args, **kwargs)

    store = FailFirstClaimStore(settings)
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions, events=store)
    await handle(
        service,
        [VaultFsEvent("delete", page_path) for page_path in paths],
        detected_at=detected_at,
    )

    expired = await service.expire_deletes(
        now=detected_at + timedelta(seconds=6)
    )

    assert expired == 1
    assert store.claim_calls == 2
    assert [call[0] for call in revisions.calls] == ["delete"]
    assert service.snapshot()["pending_deletes"] == 1


@pytest.mark.asyncio
async def test_delete_expiry_release_failure_preserves_cancellation(settings):
    page_id = "page_release_failure_cancel"
    page_path = "wiki/product/release-failure-cancel.md"
    content = managed_bytes(page_id)
    seed_page(settings, page_id=page_id, page_path=page_path, content=content)
    mutation_entered = threading.Event()
    release_mutation = threading.Event()

    class ReleaseFailingStore(VaultEventStore):
        def release_delete_claim(self, *args, **kwargs):
            raise RuntimeError("claim release database unavailable")

    class FailingDeleteRevisions(FakeRevisionService):
        def delete_page(self, event_id, path):
            self.calls.append(("delete", event_id, path))
            mutation_entered.set()
            if not release_mutation.wait(timeout=5):
                raise RuntimeError("blocked cancellation delete was not released")
            raise RuntimeError("delete failed after cancellation")

    revisions = FailingDeleteRevisions()
    service = VaultSyncService(
        settings,
        revisions=revisions,
        events=ReleaseFailingStore(settings),
    )
    detected_at = datetime(2026, 7, 15, 16, 0, tzinfo=timezone.utc)
    await handle(
        service,
        [VaultFsEvent("delete", page_path)],
        detected_at=detected_at,
    )
    expiry = asyncio.create_task(
        service.expire_deletes(now=detected_at + timedelta(seconds=6))
    )
    while not mutation_entered.is_set():
        await asyncio.sleep(0)

    expiry.cancel()
    release_mutation.set()

    with pytest.raises(asyncio.CancelledError):
        await expiry
