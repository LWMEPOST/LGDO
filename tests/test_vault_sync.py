import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.db import connect_app, connect_app_write, init_app_db
from app.vault_events import VaultEventStore
from app.vault_sync import VaultSyncService
from app.vault_watcher import VaultFsEvent


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
    def __init__(self, *, failing_paths=()):
        self.calls: list[tuple] = []
        self.failing_paths = set(failing_paths)

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
        return []


def write_page(settings, page_path: str, content: bytes) -> None:
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


def remove_page(settings, page_path: str) -> None:
    target = settings.vault_path / page_path
    if target.exists():
        target.unlink()


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
