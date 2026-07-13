from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import get_settings
from app.db import connect_app, init_app_db
from app.vault_writer import (
    AtomicVaultWriter,
    IntentExecutor,
    TargetChanged,
    VaultWriteError,
)
from app.wiki_markdown import compute_file_hash
from app.wiki_revisions import ManualSaveCommand, WikiRevisionService


def test_writer_captures_old_inode_installs_without_replace_and_retains_backup(tmp_path: Path):
    vault = tmp_path / "vault"
    target = vault / "wiki/product/faq/demo.md"
    target.parent.mkdir(parents=True)
    old = b"old human bytes"
    new = b"new managed bytes"
    target.write_bytes(old)
    writer = AtomicVaultWriter(vault)

    captured = writer.capture(
        intent_id="wint_1", target_path="wiki/product/faq/demo.md",
        expected_file_hash=compute_file_hash(old),
    )
    installed = writer.install(
        intent_id="wint_1", target_path="wiki/product/faq/demo.md", content=new,
    )

    assert captured.backup_path.read_bytes() == old
    assert installed.target_hash == compute_file_hash(new)
    assert target.read_bytes() == new
    assert captured.backup_path.exists()


def test_writer_never_overwrites_target_created_after_capture(tmp_path: Path):
    vault = tmp_path / "vault"
    target = vault / "wiki/product/faq/demo.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    writer = AtomicVaultWriter(vault)
    writer.capture("wint_2", "wiki/product/faq/demo.md", compute_file_hash(b"old"))
    target.write_bytes(b"obsidian wins")

    with pytest.raises(TargetChanged):
        writer.install("wint_2", "wiki/product/faq/demo.md", b"managed")
    assert target.read_bytes() == b"obsidian wins"
    assert writer.backup_path("wint_2").read_bytes() == b"old"


def test_capture_hash_mismatch_keeps_unknown_bytes_in_backup(tmp_path: Path):
    vault = tmp_path / "vault"
    target = vault / "wiki/product/faq/demo.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"changed before capture")
    writer = AtomicVaultWriter(vault)

    with pytest.raises(TargetChanged) as exc_info:
        writer.capture("wint_3", "wiki/product/faq/demo.md", compute_file_hash(b"expected"))
    assert exc_info.value.observed_hash == compute_file_hash(b"changed before capture")
    assert writer.backup_path("wint_3").read_bytes() == b"changed before capture"
    assert not target.exists()


@pytest.fixture
def wiki_intent_fixture(tmp_path):
    settings = get_settings().model_copy()
    settings.database_backend = "sqlite"
    settings.database_path = tmp_path / "intent.db"
    settings.vault_path = tmp_path / "vault"
    page_path = "wiki/product/faq/intent.md"
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True)
    target.write_text(
        "---\ntitle: Intent\nsource_ids: [src_intent]\nreview_status: draft\n---\n# Intent\n",
        encoding="utf-8",
    )
    init_app_db(settings)
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,domain,page_type,title,source_ids_json,review_status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (page_path,"product","faq","Intent",'["src_intent"]',"draft","t0","t0"),
        )
    service = WikiRevisionService(settings)
    current = service.get_page(page_path)
    prepared = service.prepare_manual_save(
        ManualSaveCommand(
            page_path=page_path,
            content=current.content + "\nPrepared manual change.\n",
            expected_revision_id=current.current_revision_id,
            request_id="fixture-pending-intent",
            actor="test",
            owner=None,
            note=None,
            review_status="draft",
        ),
        execute_intent=False,
    )
    assert prepared.status == "prepared"
    assert prepared.write_intent_id is not None
    return settings, prepared.write_intent_id


def test_only_lease_and_os_lock_owner_can_advance_intent(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    now = datetime.now(timezone.utc)
    first = IntentExecutor(settings, owner="executor_a")
    second = IntentExecutor(settings, owner="executor_b")

    assert first.claim(intent_id, now=now, lease_seconds=30) is True
    assert second.claim(intent_id, now=now, lease_seconds=30) is False
    assert second.advance_phase(intent_id, expected_status="pending", status="captured") is False
    assert first.advance_phase(intent_id, expected_status="pending", status="captured") is True


def test_reconcile_finalizes_installed_intent_after_process_crash(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="crashed")
    assert executor.claim(intent_id, lease_seconds=1)
    executor.capture_and_install(intent_id, stop_after="installed")

    with connect_app(settings) as conn:
        before = conn.execute("SELECT current_revision_id,pending_write_intent_id FROM wiki_pages").fetchone()
    assert before[1] == intent_id

    recovered = IntentExecutor(settings, owner="recovery").reconcile_all(
        now=datetime.now(timezone.utc) + timedelta(seconds=2)
    )

    with connect_app(settings) as conn:
        page = conn.execute("SELECT current_revision_id,pending_write_intent_id FROM wiki_pages").fetchone()
        intent = conn.execute("SELECT status,backup_retention_status FROM vault_write_intents WHERE id=?", (intent_id,)).fetchone()
    assert recovered == [intent_id]
    assert page[0] is not None and page[1] is None
    assert tuple(intent) == ("applied", "retained")


def test_reconcile_finishes_install_crash_before_phase_was_recorded(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    crashed = IntentExecutor(settings, owner="crashed-before-installed-phase")
    assert crashed.claim(intent_id, lease_seconds=1)
    crashed.capture_and_install(intent_id, stop_after="captured")

    with connect_app(settings) as conn:
        intent = conn.execute(
            "SELECT target_path,revision_id,status FROM vault_write_intents WHERE id=?",
            (intent_id,),
        ).fetchone()
        revision = conn.execute(
            "SELECT content FROM wiki_page_revisions WHERE id=?",
            (intent["revision_id"],),
        ).fetchone()
    assert intent["status"] == "captured"
    crashed.writer.install(intent_id, intent["target_path"], revision["content"].encode("utf-8"))

    recovered = IntentExecutor(settings, owner="recovery").reconcile_all(
        now=datetime.now(timezone.utc) + timedelta(seconds=2)
    )

    with connect_app(settings) as conn:
        page = conn.execute(
            "SELECT current_revision_id,pending_write_intent_id FROM wiki_pages"
        ).fetchone()
        status = conn.execute(
            "SELECT status FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()[0]
    assert recovered == [intent_id]
    assert page[0] is not None and page[1] is None
    assert status == "applied"


def test_reconcile_keeps_unknown_installed_target_pending_for_recovery(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    crashed = IntentExecutor(settings, owner="crashed-with-external-edit")
    assert crashed.claim(intent_id, lease_seconds=1)
    crashed.capture_and_install(intent_id, stop_after="installed")

    with connect_app(settings) as conn:
        target_path = conn.execute(
            "SELECT target_path FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()[0]
    target = settings.vault_path / target_path
    target.write_bytes(b"unknown obsidian bytes")

    recovered = IntentExecutor(settings, owner="recovery").reconcile_all(
        now=datetime.now(timezone.utc) + timedelta(seconds=2)
    )

    with connect_app(settings) as conn:
        page = conn.execute("SELECT pending_write_intent_id FROM wiki_pages").fetchone()
        intent = conn.execute(
            "SELECT status,backup_retention_status FROM vault_write_intents WHERE id=?",
            (intent_id,),
        ).fetchone()
    assert recovered == []
    assert page[0] == intent_id
    assert tuple(intent) == ("recovery_required", "retained")
    assert crashed.writer.backup_path(intent_id).exists()


def test_finalize_hash_race_becomes_recovery_required(wiki_intent_fixture, monkeypatch):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="finalize-race")
    assert executor.claim(intent_id, lease_seconds=30)
    executor.capture_and_install(intent_id, stop_after="installed")
    with connect_app(settings) as conn:
        target_path = conn.execute(
            "SELECT target_path FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()[0]
    target = settings.vault_path / target_path
    original_finalize = executor.revisions.finalize_intent

    def edit_then_finalize(*args, **kwargs):
        target.write_bytes(b"obsidian raced finalize")
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(executor.revisions, "finalize_intent", edit_then_finalize)

    result = executor.capture_and_install(intent_id)

    with connect_app(settings) as conn:
        page = conn.execute("SELECT pending_write_intent_id FROM wiki_pages").fetchone()
        status = conn.execute(
            "SELECT status FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()[0]
    assert result.intent_status == "recovery_required"
    assert page[0] == intent_id
    assert status == "recovery_required"


def test_finalize_delete_race_becomes_recovery_required(wiki_intent_fixture, monkeypatch):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="finalize-delete-race")
    assert executor.claim(intent_id, lease_seconds=30)
    executor.capture_and_install(intent_id, stop_after="installed")
    with connect_app(settings) as conn:
        target_path = conn.execute(
            "SELECT target_path FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()[0]
    target = settings.vault_path / target_path
    original_finalize = executor.revisions.finalize_intent

    def delete_then_finalize(*args, **kwargs):
        target.unlink()
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(executor.revisions, "finalize_intent", delete_then_finalize)

    result = executor.capture_and_install(intent_id)

    with connect_app(settings) as conn:
        page = conn.execute("SELECT pending_write_intent_id FROM wiki_pages").fetchone()
        status = conn.execute(
            "SELECT status FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()[0]
    assert result.intent_status == "recovery_required"
    assert page[0] == intent_id
    assert status == "recovery_required"


def test_preflight_hash_delete_race_becomes_recovery_required(
    wiki_intent_fixture, monkeypatch
):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="preflight-delete-race")
    assert executor.claim(intent_id, lease_seconds=30)
    executor.capture_and_install(intent_id, stop_after="installed")
    with connect_app(settings) as conn:
        target_path = conn.execute(
            "SELECT target_path FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()[0]
    target = settings.vault_path / target_path
    original_stream_hash = executor.writer._stream_hash
    removed = False

    def delete_then_hash(path):
        nonlocal removed
        if path == target and not removed:
            removed = True
            target.unlink()
        return original_stream_hash(path)

    monkeypatch.setattr(executor.writer, "_stream_hash", delete_then_hash)

    result = executor.capture_and_install(intent_id)

    with connect_app(settings) as conn:
        page = conn.execute("SELECT pending_write_intent_id FROM wiki_pages").fetchone()
        status = conn.execute(
            "SELECT status FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()[0]
    assert result.intent_status == "recovery_required"
    assert page[0] == intent_id
    assert status == "recovery_required"


def test_failed_lease_renewal_stops_before_file_mutation(wiki_intent_fixture, monkeypatch):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="expired-owner")
    assert executor.claim(intent_id, lease_seconds=1)
    with connect_app(settings) as conn:
        target_path = conn.execute(
            "SELECT target_path FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()[0]
    target = settings.vault_path / target_path
    before = target.read_bytes()
    monkeypatch.setattr(executor, "renew_lease", lambda *args, **kwargs: False)

    with pytest.raises(VaultWriteError, match="lease ownership"):
        executor.capture_and_install(intent_id)

    assert target.read_bytes() == before
    assert not executor.writer.backup_path(intent_id).exists()


def test_get_page_reads_current_revision_during_capture_window(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="captured-reader")
    assert executor.claim(intent_id, lease_seconds=30)
    executor.capture_and_install(intent_id, stop_after="captured")
    with connect_app(settings) as conn:
        page = conn.execute(
            "SELECT path,current_revision_id FROM wiki_pages"
        ).fetchone()
        current_content = conn.execute(
            "SELECT content FROM wiki_page_revisions WHERE id=?",
            (page["current_revision_id"],),
        ).fetchone()[0]

    observed = WikiRevisionService(settings).get_page(page["path"])

    assert observed.current_revision_id == page["current_revision_id"]
    assert observed.content == current_content
    assert observed.write_in_progress is True
    assert observed.write_intent_id == intent_id


def test_terminal_clear_requires_matching_pending_intent(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="executor_a")
    assert executor.claim(intent_id, lease_seconds=30)
    with connect_app(settings) as conn:
        conn.execute("UPDATE wiki_pages SET pending_write_intent_id='wint_successor'")
    assert executor.clear_terminal(intent_id, "failed", "simulated") is False
    with connect_app(settings) as conn:
        page = conn.execute("SELECT pending_write_intent_id FROM wiki_pages").fetchone()
    assert page[0] == "wint_successor"


def test_finalize_hashes_installed_target_while_page_lock_is_held(
    wiki_intent_fixture, monkeypatch
):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="finalizer")
    assert executor.claim(intent_id, lease_seconds=30)
    executor.capture_and_install(intent_id, stop_after="installed")

    service = executor.revisions
    original_lock_page = service.coordinator.lock_page
    original_stream_hash = service.writer._stream_hash
    page_lock_held = False

    @contextmanager
    def tracked_lock_page(*args, **kwargs):
        nonlocal page_lock_held
        with original_lock_page(*args, **kwargs) as locked:
            page_lock_held = True
            try:
                yield locked
            finally:
                page_lock_held = False

    def checked_stream_hash(path):
        assert page_lock_held, "installed target hash was read outside the page lock"
        return original_stream_hash(path)

    monkeypatch.setattr(service.coordinator, "lock_page", tracked_lock_page)
    monkeypatch.setattr(service.writer, "_stream_hash", checked_stream_hash)

    result = service.finalize_intent(intent_id, executor.owner)

    assert result.status == "applied"


def test_terminal_clear_owner_mismatch_rolls_back_pending_pointer(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="stale-terminal-owner")
    assert executor.claim(intent_id, lease_seconds=30)
    with connect_app(settings) as conn:
        conn.execute(
            "UPDATE vault_write_intents SET executor_owner='successor' WHERE id=?",
            (intent_id,),
        )

    assert executor.clear_terminal(intent_id, "failed", "simulated") is False

    with connect_app(settings) as conn:
        page = conn.execute("SELECT pending_write_intent_id FROM wiki_pages").fetchone()
        intent = conn.execute(
            "SELECT status,executor_owner FROM vault_write_intents WHERE id=?",
            (intent_id,),
        ).fetchone()
    assert page[0] == intent_id
    assert tuple(intent) == ("pending", "successor")
