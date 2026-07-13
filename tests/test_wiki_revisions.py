import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.config import get_settings
from app.db import connect_app, connect_app_write, init_app_db
from app.ingest import scan_sources
from app.models import CompileRequest, ScanRequest
from app.wiki import compile_wiki
from app.wiki_markdown import MarkdownParseError
from app.wiki_revisions import (
    CompileCandidateCommand,
    ManualSaveCommand,
    MetadataUpdateCommand,
    PageReadResult,
    ResolveConflictCommand,
    RevisionConflict,
    StatusUpdateCommand,
    WikiRevisionService,
    canonical_state_json,
)
from app.vault_writer import IntentExecutor


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


@pytest.fixture
def compiled_source_fixture(tmp_path):
    settings = make_settings(tmp_path)
    settings.upload_path = tmp_path / "uploads"
    settings.gbrain_import_on_compile = False
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "demo.md").write_text(
        "# Demo\n\nGenerated source body.\n",
        encoding="utf-8",
    )
    scan_sources(
        settings,
        ScanRequest(
            root_path=str(samples),
            domain="product",
            owner="compiler-test",
        ),
    )
    with connect_app(settings) as conn:
        source_id = conn.execute(
            "SELECT id FROM sources WHERE title='demo'"
        ).fetchone()[0]
    compile_wiki(
        settings,
        CompileRequest(
            domain="product",
            source_ids=[source_id],
            compile_job_id="compile-initial",
        ),
    )
    with connect_app(settings) as conn:
        page_path = conn.execute(
            "SELECT path FROM wiki_pages WHERE title='demo'"
        ).fetchone()[0]
    return settings, source_id, page_path


@pytest.fixture
def page_with_generated(compiled_source_fixture):
    settings, _, page_path = compiled_source_fixture
    service = WikiRevisionService(settings)
    return service, service.get_page(page_path)


def test_recompile_after_manual_edit_preserves_human_file_and_creates_one_conflict(
    compiled_source_fixture,
):
    settings, source_id, page_path = compiled_source_fixture
    service = WikiRevisionService(settings)
    page = service.get_page(page_path)
    service.prepare_manual_save(
        ManualSaveCommand(
            page_path=page_path,
            content=page.content + "\nHuman text\n",
            expected_revision_id=page.current_revision_id,
            request_id="manual-before-compile",
            actor="alice",
            owner=None,
            note=None,
            review_status="reviewed",
        )
    )
    human_bytes = (settings.vault_path / page_path).read_bytes()

    first = compile_wiki(
        settings,
        CompileRequest(domain="product", source_ids=[source_id]),
    )
    second = compile_wiki(
        settings,
        CompileRequest(
            domain="product",
            source_ids=[source_id],
            compile_job_id=first.job_id,
        ),
    )

    assert (settings.vault_path / page_path).read_bytes() == human_bytes
    assert first.conflicted_pages == 1
    assert second.conflicted_pages == 1
    with connect_app(settings) as conn:
        pending = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_path=? AND issue_type='content_conflict' AND status='pending'
            """,
            (page_path,),
        ).fetchall()
        state = conn.execute(
            "SELECT * FROM wiki_pages WHERE path=?",
            (page_path,),
        ).fetchone()
    assert len(pending) == 1
    assert pending[0]["base_revision_id"] == state["current_revision_id"]
    assert pending[0]["candidate_revision_id"] == state["generated_revision_id"]
    assert json.loads(pending[0]["expected_state_json"]) == {
        "accepted_generated": state["accepted_generated_revision_id"],
        "current": state["current_revision_id"],
        "file_hash": state["file_hash"],
        "generated": state["generated_revision_id"],
        "lifecycle": state["lifecycle_status"],
        "path": state["path"],
        "pending_conflict": [pending[0]["id"]],
    }


def test_recompile_auto_advances_only_when_current_equals_previous_generated(
    compiled_source_fixture,
):
    settings, source_id, page_path = compiled_source_fixture
    service = WikiRevisionService(settings)
    before = service.get_page(page_path)
    assert before.current_revision_id == before.generated_revision_id
    with connect_app(settings) as conn:
        conn.execute(
            """
            UPDATE sources
            SET content_hash='source-v2', last_compiled_at=NULL
            WHERE id=?
            """,
            (source_id,),
        )

    result = compile_wiki(
        settings,
        CompileRequest(
            domain="product",
            source_ids=[source_id],
            compile_job_id="compile-v2",
        ),
    )
    after = service.get_page(page_path)

    assert result.updated_pages == 1
    assert result.conflicted_pages == 0
    assert after.current_revision_id == after.generated_revision_id
    assert after.current_revision_id != before.current_revision_id


def test_unchanged_generated_artifact_still_reconciles_new_manual_divergence(
    compiled_source_fixture,
):
    settings, source_id, page_path = compiled_source_fixture
    service = WikiRevisionService(settings)
    before = service.get_page(page_path)
    service.prepare_manual_save(
        ManualSaveCommand(
            page_path=page_path,
            content=before.content + "\nmanual\n",
            expected_revision_id=before.current_revision_id,
            request_id="manual-diverge",
            actor="alice",
            owner=None,
            note=None,
            review_status="reviewed",
        )
    )
    with connect_app(settings) as conn:
        generated_count = conn.execute(
            """
            SELECT COUNT(*) FROM wiki_page_revisions
            WHERE page_id=? AND origin='generated'
            """,
            (before.page_id,),
        ).fetchone()[0]

    result = compile_wiki(
        settings,
        CompileRequest(
            domain="product",
            source_ids=[source_id],
            compile_job_id="same-artifact",
        ),
    )
    with connect_app(settings) as conn:
        generated_after = conn.execute(
            """
            SELECT COUNT(*) FROM wiki_page_revisions
            WHERE page_id=? AND origin='generated'
            """,
            (before.page_id,),
        ).fetchone()[0]

    assert generated_after == generated_count
    assert result.conflicted_pages == 1


def test_generated_transition_reconciles_before_auto_write_finalize(
    page_with_generated,
    monkeypatch,
):
    service, before = page_with_generated
    command = CompileCandidateCommand(
        page_path=before.page_path,
        content=before.content + "\nGenerated transition before finalize.\n",
        domain=before.metadata["domain"],
        page_type=before.metadata["page_type"],
        title=before.metadata["title"],
        source_ids=before.metadata["source_ids"],
        owner=before.metadata.get("owner"),
        source_hash="generated-before-finalize-v2",
        compiler_version="wiki-revision-v1",
        compile_job_id="generated-before-finalize",
    )

    with monkeypatch.context() as patch:
        patch.setattr(IntentExecutor, "execute", lambda self, intent_id: None)
        with pytest.raises(RevisionConflict):
            service.apply_generated_candidate(command)

    with connect_app(service.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
        pending = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='content_conflict' AND status='pending'
            """,
            (before.page_id,),
        ).fetchall()
    assert page["current_revision_id"] == before.current_revision_id
    assert page["generated_revision_id"] != before.generated_revision_id
    assert page["pending_write_intent_id"] is not None
    assert len(pending) == 1
    assert pending[0]["base_revision_id"] == before.current_revision_id
    assert pending[0]["candidate_revision_id"] == page["generated_revision_id"]
    assert json.loads(pending[0]["expected_state_json"]) == {
        "accepted_generated": page["accepted_generated_revision_id"],
        "current": page["current_revision_id"],
        "file_hash": page["file_hash"],
        "generated": page["generated_revision_id"],
        "lifecycle": page["lifecycle_status"],
        "path": page["path"],
        "pending_conflict": [pending[0]["id"]],
    }

    recovered = IntentExecutor(service.settings).execute(
        page["pending_write_intent_id"]
    )
    assert recovered is not None
    assert recovered.intent_status == "applied"
    with connect_app(service.settings) as conn:
        finalized = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
        review = conn.execute(
            "SELECT status FROM review_items WHERE id=?",
            (pending[0]["id"],),
        ).fetchone()
    assert finalized["current_revision_id"] == finalized["generated_revision_id"]
    assert finalized["pending_write_intent_id"] is None
    assert review["status"] == "superseded"


def test_reused_generated_artifact_reconciles_in_transition_transaction(
    page_with_generated,
    monkeypatch,
):
    service, before = page_with_generated
    with connect_app(service.settings) as conn:
        generated = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (before.generated_revision_id,),
        ).fetchone()
    generated_metadata = json.loads(generated["metadata_json"])
    calls = []
    original_reconcile = service._reconcile_pending_reviews_locked

    def observe_reconcile(conn, page_id):
        page = conn.execute(
            "SELECT generated_revision_id FROM wiki_pages WHERE page_id=?",
            (page_id,),
        ).fetchone()
        calls.append((page_id, page["generated_revision_id"]))
        return original_reconcile(conn, page_id)

    monkeypatch.setattr(
        service,
        "_reconcile_pending_reviews_locked",
        observe_reconcile,
    )
    result = service.apply_generated_candidate(
        CompileCandidateCommand(
            page_path=before.page_path,
            content=generated["content"],
            domain=before.metadata["domain"],
            page_type=before.metadata["page_type"],
            title=before.metadata["title"],
            source_ids=before.metadata["source_ids"],
            owner=before.metadata.get("owner"),
            source_hash=generated_metadata["source_hash"],
            compiler_version=generated_metadata["compiler_version"],
            compile_job_id="reuse-with-reconcile-spy",
        )
    )

    assert result.candidate_revision_id == before.generated_revision_id
    assert calls == [(before.page_id, before.generated_revision_id)]


def test_applied_compile_job_replay_does_not_report_or_enqueue_an_update(
    compiled_source_fixture,
):
    settings, source_id, _ = compiled_source_fixture
    with connect_app(settings) as conn:
        revisions_before = conn.execute(
            """
            SELECT COUNT(*) FROM wiki_page_revisions
            WHERE origin='generated'
            """
        ).fetchone()[0]
        jobs_before = conn.execute(
            "SELECT COUNT(*) FROM knowledge_projection_jobs"
        ).fetchone()[0]

    result = compile_wiki(
        settings,
        CompileRequest(
            domain="product",
            source_ids=[source_id],
            compile_job_id="compile-initial",
        ),
    )

    with connect_app(settings) as conn:
        revisions_after = conn.execute(
            """
            SELECT COUNT(*) FROM wiki_page_revisions
            WHERE origin='generated'
            """
        ).fetchone()[0]
        jobs_after = conn.execute(
            "SELECT COUNT(*) FROM knowledge_projection_jobs"
        ).fetchone()[0]

    assert result.job_id == "compile-initial"
    assert result.created_pages == 0
    assert result.updated_pages == 0
    assert result.conflicted_pages == 0
    assert result.projection_jobs == 0
    assert revisions_after == revisions_before
    assert jobs_after == jobs_before


def test_compile_replay_fails_closed_while_matching_intent_cannot_be_claimed(
    tmp_path,
    monkeypatch,
):
    settings = make_settings(tmp_path)
    settings.upload_path = tmp_path / "uploads"
    settings.gbrain_import_on_compile = False
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "pending.md").write_text(
        "# Pending\n\nGenerated source body.\n",
        encoding="utf-8",
    )
    scan_sources(
        settings,
        ScanRequest(
            root_path=str(samples),
            domain="product",
            owner="compiler-test",
        ),
    )
    with connect_app(settings) as conn:
        source_id = conn.execute(
            "SELECT id FROM sources WHERE title='pending'"
        ).fetchone()[0]

    original_execute = IntentExecutor.execute
    monkeypatch.setattr(IntentExecutor, "execute", lambda self, intent_id: None)
    request = CompileRequest(
        domain="product",
        source_ids=[source_id],
        compile_job_id="compile-pending-owner",
    )

    with pytest.raises(RevisionConflict):
        compile_wiki(settings, request)
    with pytest.raises(RevisionConflict):
        compile_wiki(settings, request)

    with connect_app(settings) as conn:
        source = conn.execute(
            "SELECT last_compiled_at FROM sources WHERE id=?",
            (source_id,),
        ).fetchone()
        page = conn.execute(
            """
            SELECT current_revision_id,generated_revision_id,pending_write_intent_id
            FROM wiki_pages
            """
        ).fetchone()
        jobs = conn.execute(
            "SELECT COUNT(*) FROM knowledge_projection_jobs"
        ).fetchone()[0]
        generated = conn.execute(
            """
            SELECT COUNT(*) FROM wiki_page_revisions
            WHERE origin='generated'
            """
        ).fetchone()[0]

    assert source["last_compiled_at"] is None
    assert page["current_revision_id"] is None
    assert page["generated_revision_id"] is not None
    assert page["pending_write_intent_id"] is not None
    assert jobs == 0
    assert generated == 1

    def apply_elsewhere(self, intent_id):
        other = IntentExecutor(self.settings, owner="other-compile-executor")
        applied = original_execute(other, intent_id)
        assert applied is not None
        assert applied.intent_status == "applied"
        return None

    monkeypatch.setattr(IntentExecutor, "execute", apply_elsewhere)
    replay = compile_wiki(settings, request)
    with connect_app(settings) as conn:
        completed_source = conn.execute(
            "SELECT last_compiled_at FROM sources WHERE id=?",
            (source_id,),
        ).fetchone()
        completed_page = conn.execute(
            """
            SELECT current_revision_id,generated_revision_id,pending_write_intent_id
            FROM wiki_pages
            """
        ).fetchone()
        completed_jobs = conn.execute(
            "SELECT COUNT(*) FROM knowledge_projection_jobs"
        ).fetchone()[0]

    assert replay.created_pages == 0
    assert replay.updated_pages == 0
    assert replay.conflicted_pages == 0
    assert replay.projection_jobs == 0
    assert completed_source["last_compiled_at"] is not None
    assert completed_page["current_revision_id"] == completed_page["generated_revision_id"]
    assert completed_page["pending_write_intent_id"] is None
    assert completed_jobs == 2


@dataclass(frozen=True)
class ContentConflictScenario:
    service: WikiRevisionService
    conflict: dict
    current: PageReadResult
    generated_content: str

    def generated_job(
        self,
        content: str,
        source_hash: str,
        job_id: str,
    ) -> CompileCandidateCommand:
        metadata = self.current.metadata
        return CompileCandidateCommand(
            page_path=self.current.page_path,
            content=content,
            domain=metadata["domain"],
            page_type=metadata["page_type"],
            title=metadata["title"],
            source_ids=metadata["source_ids"],
            owner=metadata.get("owner"),
            source_hash=source_hash,
            compiler_version="wiki-revision-v1",
            compile_job_id=job_id,
        )

    def same_semantic_new_job(self) -> CompileCandidateCommand:
        return self.generated_job(
            self.generated_content,
            "same-semantic-v2",
            "compile-same-semantic",
        )

    def new_candidate_job(self) -> CompileCandidateCommand:
        return self.generated_job(
            self.generated_content + "\nNew generated fact.\n",
            "different-semantic-v3",
            "compile-different-semantic",
        )


@pytest.fixture
def content_conflict_fixture(page_with_generated):
    service, generated_page = page_with_generated
    service.prepare_manual_save(
        ManualSaveCommand(
            page_path=generated_page.page_path,
            content=generated_page.content + "\nHuman divergence.\n",
            expected_revision_id=generated_page.current_revision_id,
            request_id="fixture-manual-divergence",
            actor="alice",
            owner=None,
            note=None,
            review_status="reviewed",
        )
    )
    current = service.get_page(generated_page.page_path)
    service.apply_generated_candidate(
        CompileCandidateCommand(
            page_path=current.page_path,
            content=generated_page.content,
            domain=current.metadata["domain"],
            page_type=current.metadata["page_type"],
            title=current.metadata["title"],
            source_ids=current.metadata["source_ids"],
            owner=current.metadata.get("owner"),
            source_hash="fixture-generated-v1",
            compiler_version="wiki-revision-v1",
            compile_job_id="fixture-conflict",
        )
    )
    conflict = service.list_conflicts(current.page_path, status="pending")[0]
    assert conflict["issue_type"] == "content_conflict"
    assert isinstance(conflict["source_ids"], list)
    assert isinstance(conflict["expected_state"], dict)
    return ContentConflictScenario(
        service,
        conflict,
        current,
        generated_page.content,
    )


@pytest.fixture
def concurrent_conflict_fixture(page_with_generated):
    service, before = page_with_generated
    prepared = service.prepare_manual_save(
        ManualSaveCommand(
            page_path=before.page_path,
            content=before.content + "\nConcurrent manual candidate.\n",
            expected_revision_id=before.current_revision_id,
            request_id="fixture-concurrent-candidate",
            actor="bob",
            owner=None,
            note=None,
            review_status="reviewed",
        ),
        execute_intent=False,
    )
    review_id = f"review_{uuid.uuid4().hex}"
    with connect_app_write(service.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
        timestamp = "2026-07-14T00:00:00+00:00"
        conn.execute(
            """
            UPDATE vault_write_intents
            SET status='superseded',updated_at=?
            WHERE id=? AND status='pending'
            """,
            (timestamp, prepared.write_intent_id),
        )
        cleared = conn.execute(
            """
            UPDATE wiki_pages SET pending_write_intent_id=NULL
            WHERE page_id=? AND pending_write_intent_id=?
            """,
            (before.page_id, prepared.write_intent_id),
        )
        assert cleared.rowcount == 1
        expected_state = {
            "accepted_generated": page["accepted_generated_revision_id"],
            "current": page["current_revision_id"],
            "file_hash": page["file_hash"],
            "generated": page["generated_revision_id"],
            "lifecycle": page["lifecycle_status"],
            "path": page["path"],
            "pending_conflict": [review_id],
        }
        conn.execute(
            """
            INSERT INTO review_items(
              id,page_path,issue_type,status,source_ids_json,created_at,updated_at,
              page_id,base_revision_id,candidate_revision_id,expected_state_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                review_id,
                before.page_path,
                "concurrent_write_conflict",
                "pending",
                page["source_ids_json"],
                timestamp,
                timestamp,
                before.page_id,
                before.current_revision_id,
                prepared.revision_id,
                canonical_state_json(expected_state),
            ),
        )
    conflict = service.list_conflicts(before.page_path, status="pending")[0]
    assert conflict["issue_type"] == "concurrent_write_conflict"
    return service, conflict, before


@pytest.mark.parametrize(
    ("resolution", "merged_suffix", "revision_delta", "intent_delta"),
    [
        ("keep_current", None, 0, 0),
        ("accept_candidate", None, 0, 1),
        ("merged_content", "\nMerged human rule\n", 1, 1),
    ],
)
def test_content_conflict_resolution_records_locked_branch_contract(
    content_conflict_fixture,
    resolution,
    merged_suffix,
    revision_delta,
    intent_delta,
):
    scenario = content_conflict_fixture
    service, conflict, current = scenario.service, scenario.conflict, scenario.current
    merged = current.content + merged_suffix if merged_suffix else None
    with connect_app(service.settings) as conn:
        revisions_before = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
            (current.page_id,),
        ).fetchone()[0]
        intents_before = conn.execute(
            "SELECT COUNT(*) FROM vault_write_intents WHERE page_id=?",
            (current.page_id,),
        ).fetchone()[0]

    result = service.resolve_conflict(
        ResolveConflictCommand(
            review_id=conflict["id"],
            resolution=resolution,
            merged_content=merged,
            expected_current_revision_id=conflict["base_revision_id"],
            expected_generated_revision_id=conflict["candidate_revision_id"],
            request_id=f"resolve-{resolution}",
            actor="alice",
            note="reviewed",
        )
    )
    page = service.get_page(current.page_path)
    with connect_app(service.settings) as conn:
        review = conn.execute(
            "SELECT * FROM review_items WHERE id=?",
            (conflict["id"],),
        ).fetchone()
        revisions_after = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
            (current.page_id,),
        ).fetchone()[0]
        intents_after = conn.execute(
            "SELECT COUNT(*) FROM vault_write_intents WHERE page_id=?",
            (current.page_id,),
        ).fetchone()[0]
        revision = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (page.current_revision_id,),
        ).fetchone()
        audit_payload = json.loads(
            conn.execute(
                """
                SELECT payload_json FROM audit_logs
                WHERE event_type='wiki_conflict_resolved'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()[0]
        )

    assert result.status == "resolved"
    assert page.accepted_generated_revision_id == conflict["candidate_revision_id"]
    assert revisions_after == revisions_before + revision_delta
    assert intents_after == intents_before + intent_delta
    assert review["status"] == "resolved"
    assert review["resolved_at"] is not None
    assert review["resolution_revision_id"] == page.current_revision_id
    assert audit_payload["resolution"] == resolution
    assert audit_payload["actor"] == "alice"
    assert audit_payload["note"] == "reviewed"

    if resolution == "keep_current":
        assert result.revision_id == current.current_revision_id
        assert page.current_revision_id == current.current_revision_id
        assert revision["origin"] == "manual"
    elif resolution == "accept_candidate":
        assert result.revision_id == conflict["candidate_revision_id"]
        assert page.current_revision_id == page.generated_revision_id
        assert revision["origin"] == "generated"
    else:
        assert result.revision_id == page.current_revision_id
        assert page.current_revision_id != page.generated_revision_id
        assert revision["origin"] == "merge"
        assert revision["base_revision_id"] == current.current_revision_id


def test_same_semantic_candidate_after_keep_does_not_reopen_content_conflict(
    content_conflict_fixture,
):
    scenario = content_conflict_fixture
    service, conflict, current = scenario.service, scenario.conflict, scenario.current
    service.resolve_conflict(
        ResolveConflictCommand(
            conflict["id"],
            "keep_current",
            None,
            conflict["base_revision_id"],
            conflict["candidate_revision_id"],
            "keep-1",
            "alice",
            None,
        )
    )

    same_semantic = service.apply_generated_candidate(
        scenario.same_semantic_new_job()
    )

    assert same_semantic.candidate_revision_id != conflict["candidate_revision_id"]
    assert service.list_conflicts(current.page_path, status="pending") == []

    changed = service.apply_generated_candidate(scenario.new_candidate_job())
    pending = service.list_conflicts(current.page_path, status="pending")
    assert len(pending) == 1
    assert pending[0]["candidate_revision_id"] == changed.candidate_revision_id


def test_new_candidate_supersedes_old_and_stale_resolve_cannot_roll_back_generated(
    content_conflict_fixture,
):
    scenario = content_conflict_fixture
    service, old_conflict, current = scenario.service, scenario.conflict, scenario.current
    newer = service.apply_generated_candidate(scenario.new_candidate_job())
    pending = service.list_conflicts(current.page_path, status="pending")
    assert len(pending) == 1
    assert pending[0]["id"] != old_conflict["id"]
    assert pending[0]["candidate_revision_id"] == newer.candidate_revision_id
    with connect_app(service.settings) as conn:
        old_status = conn.execute(
            "SELECT status FROM review_items WHERE id=?",
            (old_conflict["id"],),
        ).fetchone()[0]
    assert old_status == "superseded"
    before_file = (service.settings.vault_path / current.page_path).read_bytes()

    with pytest.raises(RevisionConflict):
        service.resolve_conflict(
            ResolveConflictCommand(
                old_conflict["id"],
                "accept_candidate",
                None,
                old_conflict["base_revision_id"],
                old_conflict["candidate_revision_id"],
                "stale-resolve",
                "alice",
                None,
            )
        )

    page = service.get_page(current.page_path)
    assert page.generated_revision_id == newer.candidate_revision_id
    assert (service.settings.vault_path / current.page_path).read_bytes() == before_file


def test_path_transition_reconciles_stale_content_conflict_before_intent(
    content_conflict_fixture,
):
    scenario = content_conflict_fixture
    service, old_conflict, current = scenario.service, scenario.conflict, scenario.current
    target_path = "wiki/product/faq/demo-renamed.md"

    service.update_metadata(
        MetadataUpdateCommand(
            page_path=current.page_path,
            changes={},
            expected_revision_id=current.current_revision_id,
            request_id="rename-with-conflict",
            actor="alice",
        ),
        target_page_path=target_path,
        execute_intent=False,
    )

    pending = service.list_conflicts(target_path, status="pending")
    assert len(pending) == 1
    assert pending[0]["id"] != old_conflict["id"]
    assert pending[0]["expected_state"]["path"] == target_path
    with connect_app(service.settings) as conn:
        old_status = conn.execute(
            "SELECT status FROM review_items WHERE id=?",
            (old_conflict["id"],),
        ).fetchone()[0]
    assert old_status == "superseded"


@pytest.mark.parametrize(
    ("resolution", "revision_delta", "intent_delta"),
    [
        ("keep_current", 0, 0),
        ("accept_candidate", 0, 1),
        ("merged_content", 1, 1),
    ],
)
def test_concurrent_write_resolution_never_changes_generated_pointers(
    concurrent_conflict_fixture,
    resolution,
    revision_delta,
    intent_delta,
):
    service, conflict, before = concurrent_conflict_fixture
    with connect_app(service.settings) as conn:
        revisions_before = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
            (before.page_id,),
        ).fetchone()[0]
        intents_before = conn.execute(
            "SELECT COUNT(*) FROM vault_write_intents WHERE page_id=?",
            (before.page_id,),
        ).fetchone()[0]

    result = service.resolve_conflict(
        ResolveConflictCommand(
            conflict["id"],
            resolution,
            before.content + "\nmerged\n" if resolution == "merged_content" else None,
            conflict["base_revision_id"],
            before.generated_revision_id,
            f"concurrent-{resolution}",
            "alice",
            None,
        )
    )
    after = service.get_page(before.page_path)
    with connect_app(service.settings) as conn:
        review = conn.execute(
            "SELECT * FROM review_items WHERE id=?",
            (conflict["id"],),
        ).fetchone()
        revision = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (after.current_revision_id,),
        ).fetchone()
        revisions_after = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
            (before.page_id,),
        ).fetchone()[0]
        intents_after = conn.execute(
            "SELECT COUNT(*) FROM vault_write_intents WHERE page_id=?",
            (before.page_id,),
        ).fetchone()[0]

    assert result.status == "resolved"
    assert after.generated_revision_id == before.generated_revision_id
    assert after.accepted_generated_revision_id == before.accepted_generated_revision_id
    assert revisions_after == revisions_before + revision_delta
    assert intents_after == intents_before + intent_delta
    assert review["status"] == "resolved"
    assert review["resolution_revision_id"] == after.current_revision_id

    if resolution == "keep_current":
        assert result.revision_id == before.current_revision_id
        assert after.current_revision_id == before.current_revision_id
    elif resolution == "accept_candidate":
        assert result.revision_id == conflict["candidate_revision_id"]
        assert after.current_revision_id == conflict["candidate_revision_id"]
        assert revision["origin"] == "manual"
    else:
        assert result.revision_id == after.current_revision_id
        assert after.current_revision_id != conflict["candidate_revision_id"]
        assert revision["origin"] == "merge"
        assert revision["base_revision_id"] == before.current_revision_id


def test_merge_origin_concurrent_candidate_is_rejected_and_superseded(
    concurrent_conflict_fixture,
):
    service, conflict, before = concurrent_conflict_fixture
    target = service.settings.vault_path / before.page_path
    file_before = target.read_bytes()
    with connect_app_write(service.settings) as conn:
        changed = conn.execute(
            "UPDATE wiki_page_revisions SET origin='merge' WHERE id=?",
            (conflict["candidate_revision_id"],),
        )
        assert changed.rowcount == 1
        page_before = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()

    with pytest.raises(RevisionConflict):
        service.resolve_conflict(
            ResolveConflictCommand(
                review_id=conflict["id"],
                resolution="keep_current",
                merged_content=None,
                expected_current_revision_id=conflict["base_revision_id"],
                expected_generated_revision_id=before.generated_revision_id,
                request_id="reject-merge-origin-candidate",
                actor="alice",
                note=None,
            )
        )

    with service.coordinator.lock_page(page_id=before.page_id) as locked:
        service._reconcile_pending_reviews_locked(locked.conn, before.page_id)

    with connect_app(service.settings) as conn:
        page_after = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
        review = conn.execute(
            "SELECT * FROM review_items WHERE id=?",
            (conflict["id"],),
        ).fetchone()
        pending = conn.execute(
            """
            SELECT COUNT(*) FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            """,
            (before.page_id,),
        ).fetchone()[0]
    assert review["status"] == "superseded"
    assert review["resolved_at"] is not None
    assert pending == 0
    assert page_after["current_revision_id"] == page_before["current_revision_id"]
    assert page_after["generated_revision_id"] == page_before["generated_revision_id"]
    assert (
        page_after["accepted_generated_revision_id"]
        == page_before["accepted_generated_revision_id"]
    )
    assert target.read_bytes() == file_before
