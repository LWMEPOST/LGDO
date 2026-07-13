import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.config import get_settings
from app.db import connect_app, init_app_db
from app.wiki_markdown import MarkdownParseError
from app.wiki_revisions import (
    ManualSaveCommand,
    MetadataUpdateCommand,
    RevisionConflict,
    StatusUpdateCommand,
    WikiRevisionService,
    canonical_state_json,
)


def make_settings(tmp_path: Path):
    settings = get_settings().model_copy()
    settings.database_backend = "sqlite"
    settings.database_path = tmp_path / "wiki.db"
    settings.vault_path = tmp_path / "vault"
    (settings.vault_path / "wiki/product/faq").mkdir(parents=True)
    init_app_db(settings)
    return settings


def seed_legacy_page(settings, content: bytes) -> str:
    page_path = "wiki/product/faq/demo.md"
    (settings.vault_path / page_path).write_bytes(content)
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(path,domain,page_type,title,source_ids_json,review_status,owner,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (page_path,"product","faq","Demo",'["src_1"]',"reviewed","alice","t0","t0"),
        )
    return page_path


def test_first_read_creates_one_legacy_revision_without_generated_baseline(tmp_path):
    settings = make_settings(tmp_path)
    page_path = seed_legacy_page(
        settings,
        b"---\ntitle: Demo\nsource_ids: [src_1]\nreview_status: reviewed\n---\n# Human legacy\n",
    )
    service = WikiRevisionService(settings)

    first = service.get_page(page_path)
    replay = service.get_page(page_path)

    assert first.current_revision_id == replay.current_revision_id
    with connect_app(settings) as conn:
        page = conn.execute("SELECT * FROM wiki_pages WHERE path=?", (page_path,)).fetchone()
        revisions = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE page_id=?", (page["page_id"],)
        ).fetchall()
    assert len(revisions) == 1
    assert revisions[0]["origin"] == "legacy"
    assert page["current_revision_id"] == revisions[0]["id"]
    assert page["generated_revision_id"] is None
    assert page["file_hash"] == revisions[0]["file_hash"]
    assert first.content == replay.content


def test_revision_event_replay_reuses_only_same_idempotency_key(tmp_path):
    settings = make_settings(tmp_path)
    page_path = seed_legacy_page(
        settings,
        b"---\ntitle: Demo\nsource_ids: [src_1]\n---\n# A\n",
    )
    service = WikiRevisionService(settings)
    page = service.get_page(page_path)
    with service.coordinator.lock_page(page_path) as locked:
        one = service._create_revision_locked(
            locked.conn, locked.page, content=page.raw_bytes, origin="external",
            base_revision_id=page.current_revision_id, source_ids=["src_1"], actor="test",
            note=None, idempotency_key="event:one", metadata={},
        )
        replay = service._create_revision_locked(
            locked.conn, locked.page, content=page.raw_bytes, origin="external",
            base_revision_id=page.current_revision_id, source_ids=["src_1"], actor="test",
            note=None, idempotency_key="event:one", metadata={},
        )
        two = service._create_revision_locked(
            locked.conn, locked.page, content=page.raw_bytes, origin="external",
            base_revision_id=page.current_revision_id, source_ids=["src_1"], actor="test",
            note=None, idempotency_key="event:two", metadata={},
        )
    assert one.id == replay.id
    assert two.id != one.id
    assert two.revision_number == one.revision_number + 1


@pytest.fixture
def legacy_page_fixture(tmp_path):
    settings = make_settings(tmp_path)
    page_path = seed_legacy_page(
        settings,
        b"---\ntitle: Demo\nsource_ids: [src_1]\nreview_status: reviewed\n---\n# Demo\n",
    )
    service = WikiRevisionService(settings)
    page = service.get_page(page_path)
    with connect_app(settings) as conn:
        conn.execute(
            """
            UPDATE wiki_pages
            SET generated_revision_id=?,accepted_generated_revision_id=?
            WHERE page_id=?
            """,
            (page.current_revision_id, page.current_revision_id, page.page_id),
        )
    return service, service.get_page(page_path)


def test_manual_save_requires_current_revision_and_keeps_generated_pointer(
    legacy_page_fixture,
):
    service, page = legacy_page_fixture
    content = page.content.replace("# Demo", "# Demo\n\nHuman addition")

    result = service.prepare_manual_save(
        ManualSaveCommand(
            page_path=page.page_path,
            content=content,
            expected_revision_id=page.current_revision_id,
            request_id="save_1",
            actor="alice",
            owner="alice",
            note="human edit",
            review_status="reviewed",
        )
    )
    saved = service.get_page(page.page_path)

    assert result.status == "applied"
    assert saved.current_revision_id != page.current_revision_id
    assert saved.generated_revision_id == page.generated_revision_id
    assert saved.accepted_generated_revision_id == page.accepted_generated_revision_id
    assert "Human addition" in saved.content
    assert saved.metadata["review_status"] == "reviewed"
    assert len(result.projection_job_ids) == 2


def test_manual_request_replays_same_state_and_can_transition_new_expected_state(
    legacy_page_fixture,
):
    service, page = legacy_page_fixture
    command = ManualSaveCommand(
        page_path=page.page_path,
        content=page.content + "\nfirst",
        expected_revision_id=page.current_revision_id,
        request_id="save_replay",
        actor="alice",
        owner=None,
        note=None,
        review_status="draft",
    )

    prepared = service.prepare_manual_save(command, execute_intent=False)
    replayed = service.prepare_manual_save(command, execute_intent=False)
    applied = service.prepare_manual_save(command)
    next_result = service.prepare_manual_save(
        ManualSaveCommand(
            page_path=page.page_path,
            content=service.get_page(page.page_path).content + "\nsecond",
            expected_revision_id=applied.current_revision_id,
            request_id="save_replay",
            actor="alice",
            owner=None,
            note=None,
            review_status="draft",
        )
    )

    assert replayed.replayed is True
    assert replayed.revision_id == prepared.revision_id
    assert replayed.write_intent_id == prepared.write_intent_id
    assert applied.revision_id == prepared.revision_id
    assert next_result.revision_id != prepared.revision_id


def test_manual_replay_requires_the_same_canonical_state(legacy_page_fixture):
    service, page = legacy_page_fixture
    command = ManualSaveCommand(
        page_path=page.page_path,
        content=page.content + "\nfirst",
        expected_revision_id=page.current_revision_id,
        request_id="save_state",
        actor="alice",
        owner=None,
        note=None,
        review_status="draft",
    )
    service.prepare_manual_save(command, execute_intent=False)
    with connect_app(service.settings) as conn:
        conn.execute(
            """
            UPDATE wiki_pages SET accepted_generated_revision_id=NULL
            WHERE page_id=?
            """,
            (page.page_id,),
        )

    with pytest.raises(RevisionConflict):
        service.prepare_manual_save(command, execute_intent=False)


def test_manual_replay_requires_matching_expected_revision(legacy_page_fixture):
    service, page = legacy_page_fixture
    service.prepare_manual_save(
        ManualSaveCommand(
            page_path=page.page_path,
            content=page.content + "\nfirst",
            expected_revision_id=page.current_revision_id,
            request_id="save_expected",
            actor="alice",
            owner=None,
            note=None,
            review_status="draft",
        ),
        execute_intent=False,
    )

    with pytest.raises(RevisionConflict) as exc_info:
        service.prepare_manual_save(
            ManualSaveCommand(
                page_path=page.page_path,
                content=page.content + "\nstale",
                expected_revision_id="wrev_stale",
                request_id="save_expected",
                actor="alice",
                owner=None,
                note=None,
                review_status="draft",
            ),
            execute_intent=False,
        )

    assert exc_info.value.current_revision_id == page.current_revision_id


@pytest.mark.parametrize("source_ids", ["''", "null", "{}", "0"])
def test_manual_save_rejects_falsy_non_list_source_ids(
    legacy_page_fixture,
    source_ids,
):
    service, page = legacy_page_fixture
    content = page.content.replace(
        "source_ids: [src_1]",
        f"source_ids: {source_ids}",
    )

    with pytest.raises(MarkdownParseError) as exc_info:
        service.prepare_manual_save(
            ManualSaveCommand(
                page_path=page.page_path,
                content=content,
                expected_revision_id=page.current_revision_id,
                request_id=f"invalid_source_ids_{source_ids}",
                actor="alice",
                owner=None,
                note=None,
                review_status="draft",
            ),
            execute_intent=False,
        )

    assert exc_info.value.code == "invalid_source_ids"


def test_status_update_is_a_manual_revision_and_changes_frontmatter(
    legacy_page_fixture,
):
    service, page = legacy_page_fixture

    result = service.update_status(
        StatusUpdateCommand(
            page_path=page.page_path,
            review_status="stale",
            expected_revision_id=page.current_revision_id,
            request_id="status_1",
            actor="alice",
            owner=None,
            note="expired",
        )
    )
    changed = service.get_page(page.page_path)
    with connect_app(service.settings) as conn:
        revision = conn.execute(
            "SELECT origin FROM wiki_page_revisions WHERE id=?",
            (result.revision_id,),
        ).fetchone()

    assert result.current_revision_id == changed.current_revision_id
    assert changed.generated_revision_id == page.generated_revision_id
    assert changed.accepted_generated_revision_id == page.accepted_generated_revision_id
    assert revision["origin"] == "manual"
    assert "review_status: stale" in changed.content


def test_metadata_update_rejects_invalid_review_status(legacy_page_fixture):
    service, page = legacy_page_fixture

    with pytest.raises(MarkdownParseError) as exc_info:
        service.update_metadata(
            MetadataUpdateCommand(
                page_path=page.page_path,
                changes={"review_status": None},
                expected_revision_id=page.current_revision_id,
                request_id="metadata_invalid_status",
                actor="alice",
            ),
            execute_intent=False,
        )

    assert exc_info.value.code == "invalid_review_status"
    with connect_app(service.settings) as conn:
        intents = conn.execute(
            """
            SELECT COUNT(*) FROM vault_write_intents
            WHERE page_id=? AND expected_revision_id=?
            """,
            (page.page_id, page.current_revision_id),
        ).fetchone()[0]
    assert intents == 0


def test_metadata_transition_digest_uses_locked_state_and_replays_new_target(
    legacy_page_fixture,
):
    service, page = legacy_page_fixture
    with connect_app(service.settings) as conn:
        locked_state = dict(
            conn.execute(
                "SELECT * FROM wiki_pages WHERE page_id=?",
                (page.page_id,),
            ).fetchone()
        )
    state = {
        "current": locked_state["current_revision_id"],
        "generated": locked_state["generated_revision_id"],
        "accepted_generated": locked_state["accepted_generated_revision_id"],
        "lifecycle": locked_state["lifecycle_status"],
        "path": locked_state["path"],
        "file_hash": locked_state["file_hash"],
        "pending_conflict": [],
    }
    state_digest = hashlib.sha256(
        canonical_state_json(state).encode("utf-8")
    ).hexdigest()
    request_id = "metadata_target_replay"
    first_target = "wiki/product/faq/first.md"
    prepared = service.update_metadata(
        MetadataUpdateCommand(
            page_path=page.page_path,
            changes={"domain": "administration"},
            expected_revision_id=page.current_revision_id,
            request_id=request_id,
            actor="domain-reclassifier",
        ),
        execute_intent=False,
        target_page_path=first_target,
        page_domain="administration",
    )
    with connect_app(service.settings) as conn:
        revision = conn.execute(
            "SELECT idempotency_key FROM wiki_page_revisions WHERE id=?",
            (prepared.revision_id,),
        ).fetchone()

    replayed = service.update_metadata(
        MetadataUpdateCommand(
            page_path=first_target,
            changes={"domain": "administration"},
            expected_revision_id=page.current_revision_id,
            request_id=request_id,
            actor="domain-reclassifier",
        ),
        execute_intent=False,
        target_page_path="wiki/product/faq/second.md",
        page_domain="administration",
    )

    assert revision["idempotency_key"] == (
        f"metadata:{request_id}:{state_digest}"
    )
    assert replayed.replayed is True
    assert replayed.revision_id == prepared.revision_id
    assert replayed.write_intent_id == prepared.write_intent_id


def test_stale_manual_save_fails_before_file_change(legacy_page_fixture):
    service, page = legacy_page_fixture
    path = service.settings.vault_path / page.page_path
    before = path.read_bytes()

    with pytest.raises(RevisionConflict) as exc_info:
        service.prepare_manual_save(
            ManualSaveCommand(
                page_path=page.page_path,
                content=page.content + "\nstale",
                expected_revision_id="wrev_stale",
                request_id="save_stale",
                actor="bob",
                owner=None,
                note=None,
                review_status="draft",
            )
        )

    assert exc_info.value.current_revision_id == page.current_revision_id
    assert path.read_bytes() == before


def test_two_sqlite_writers_prepare_only_one_intent(legacy_page_fixture):
    service, page = legacy_page_fixture

    def save(request_id: str):
        try:
            return service.prepare_manual_save(
                ManualSaveCommand(
                    page_path=page.page_path,
                    content=page.content + request_id,
                    expected_revision_id=page.current_revision_id,
                    request_id=request_id,
                    actor=request_id,
                    owner=None,
                    note=None,
                    review_status="draft",
                ),
                execute_intent=False,
            )
        except RevisionConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save, ["a", "b"]))

    assert sum(isinstance(item, RevisionConflict) for item in outcomes) == 1
    assert sum(getattr(item, "write_intent_id", None) is not None for item in outcomes) == 1
    with connect_app(service.settings) as conn:
        prepared = conn.execute(
            """
            SELECT COUNT(*) FROM vault_write_intents
            WHERE page_id=? AND expected_revision_id=?
            """,
            (page.page_id, page.current_revision_id),
        ).fetchone()[0]
    assert prepared == 1
