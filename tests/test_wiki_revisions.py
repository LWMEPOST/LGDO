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
from app.wiki_markdown import (
    MarkdownParseError,
    capture_file_observation,
    parse_wiki_bytes,
)
from app.wiki_revisions import (
    SYNC_ISSUE_REFS_METADATA_KEY,
    CompileCandidateCommand,
    ManualSaveCommand,
    MetadataUpdateCommand,
    MutationResult,
    PageReadResult,
    ResolveConflictCommand,
    RevisionConflict,
    StatusUpdateCommand,
    WikiRevisionError,
    WikiRevisionService,
    _decode_sync_issue_refs,
    _upsert_sync_issue_locked,
    canonical_state_json,
)
from app.vault_writer import IntentExecutor


def external_page_bytes(
    *,
    title="External",
    source_ids=(),
    domain="product",
    body="Body",
) -> bytes:
    rendered_sources = ", ".join(source_ids)
    return (
        "---\n"
        f"title: {title}\n"
        f"source_ids: [{rendered_sources}]\n"
        f"domain: {domain}\n"
        "page_type: feature\n"
        "review_status: draft\n"
        "owner:\n"
        "---\n"
        f"# {title}\n{body}\n"
    ).encode("utf-8")


def seed_source(settings, source_id: str, *, domain="product", status="active") -> None:
    timestamp = "2026-07-15T00:00:00+00:00"
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO sources(
              id,domain,title,source_type,original_path,raw_path,content_hash,
              size_bytes,status,metadata_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source_id,
                domain,
                source_id,
                "markdown",
                source_id,
                f"raw/{domain}/{source_id}.md",
                "a" * 64,
                1,
                status,
                "{}",
                timestamp,
                timestamp,
            ),
        )


def test_mutation_result_round_trips_nullable_page_and_sync_issue():
    result = MutationResult(
        page_id=None,
        page_path="wiki/product/broken.md",
        status="invalid",
        sync_issue_id="visi_broken",
    )
    assert MutationResult.from_event_payload(result.to_event_payload()) == result


def test_external_new_page_accepts_empty_sources_and_creates_managed_revision(tmp_path):
    settings = make_settings(tmp_path)
    init_app_db(settings)
    page_path = "wiki/product/new-empty.md"
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(external_page_bytes(source_ids=()))
    service = WikiRevisionService(settings)

    applied = service.ingest_external_change(
        "external-new-empty",
        page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )

    with connect_app(settings) as conn:
        page = dict(
            conn.execute(
                "SELECT * FROM wiki_pages WHERE path=?", (page_path,)
            ).fetchone()
        )
        revision = dict(
            conn.execute(
                "SELECT * FROM wiki_page_revisions WHERE id=?",
                (page["current_revision_id"],),
            ).fetchone()
        )
    assert applied.status == "applied"
    assert applied.page_id == page["page_id"]
    assert json.loads(page["source_ids_json"]) == []
    assert revision["origin"] == "external"


@pytest.mark.parametrize(
    ("source_id", "status", "expected_code"),
    [
        ("src_missing", None, "unknown_source"),
        ("src_inactive", "inactive", "inactive_source"),
    ],
)
def test_external_sources_must_exist_and_be_active(
    tmp_path, source_id, status, expected_code
):
    settings = make_settings(tmp_path)
    init_app_db(settings)
    if status is not None:
        seed_source(settings, source_id, status=status)
    page_path = "wiki/product/source-check.md"
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(external_page_bytes(source_ids=(source_id,)))

    service = WikiRevisionService(settings)
    event_id = f"external-{expected_code}"
    observation = capture_file_observation(
        target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
    )
    result = service.ingest_external_change(event_id, page_path, observation)
    replay = service.ingest_external_change(event_id, page_path, observation)

    assert result.status == "invalid"
    assert result.page_id is None
    assert result.sync_issue_id
    assert replay.replayed is True
    assert replay.to_event_payload() | {"replayed": False} == result.to_event_payload()
    with connect_app(settings) as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "wiki_pages",
                "wiki_page_revisions",
                "vault_write_intents",
                "review_items",
                "knowledge_projection_jobs",
            )
        }
        assert counts == {table: 0 for table in counts}
        assert conn.execute(
            "SELECT COUNT(*) FROM vault_sync_issues WHERE page_path=?",
            (page_path,),
        ).fetchone()[0] == 1


def test_active_source_from_another_domain_is_valid(tmp_path):
    settings = make_settings(tmp_path)
    init_app_db(settings)
    seed_source(settings, "src_support", domain="support", status="active")
    page_path = "wiki/product/cross-domain.md"
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        external_page_bytes(source_ids=("src_support",), domain="product")
    )
    result = WikiRevisionService(settings).ingest_external_change(
        "external-cross-domain",
        page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert result.status == "applied"
    assert result.page_id is not None


def test_unknown_invalid_external_file_has_no_fake_domain_rows(tmp_path):
    settings = make_settings(tmp_path)
    init_app_db(settings)
    page_path = "wiki/product/unknown-invalid.md"
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"---\ntitle: [broken\n---\n# Broken\n")
    result = WikiRevisionService(settings).ingest_external_change(
        "external-unknown-invalid",
        page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    with connect_app(settings) as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "wiki_pages",
                "wiki_page_revisions",
                "vault_write_intents",
                "review_items",
                "knowledge_projection_jobs",
            )
        }
        observation_count = conn.execute(
            "SELECT COUNT(*) FROM wiki_file_observations WHERE page_path=?",
            (page_path,),
        ).fetchone()[0]
        event = conn.execute(
            "SELECT status,result_payload_json FROM vault_change_events WHERE id=?",
            ("external-unknown-invalid",),
        ).fetchone()
        issue_count = conn.execute(
            """
            SELECT COUNT(*) FROM vault_sync_issues
            WHERE page_path=? AND status='open'
            """,
            (page_path,),
        ).fetchone()[0]
    assert result.page_id is None
    assert result.sync_issue_id
    assert counts == {table: 0 for table in counts}
    assert observation_count == 1
    assert issue_count == 1
    assert event["status"] == "invalid"
    assert json.loads(event["result_payload_json"])["sync_issue_id"] == result.sync_issue_id
    replay = WikiRevisionService(settings).ingest_external_change(
        "external-unknown-invalid",
        page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert replay.replayed is True
    assert replay.sync_issue_id == result.sync_issue_id


OPTIONAL_MUTATION_RESULT_ID_FIELDS = (
    "page_id",
    "revision_id",
    "current_revision_id",
    "generated_revision_id",
    "candidate_revision_id",
    "write_intent_id",
    "conflict_review_id",
    "observation_id",
    "audit_revision_id",
    "sync_issue_id",
)


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        *[(field_name, 0) for field_name in OPTIONAL_MUTATION_RESULT_ID_FIELDS],
        pytest.param("projection_job_ids", 0, id="jobs-int"),
        pytest.param("projection_job_ids", False, id="jobs-bool"),
        pytest.param("projection_job_ids", "job_1", id="jobs-string"),
        pytest.param("projection_job_ids", ["job_1", 2], id="jobs-mixed"),
        pytest.param("replayed", "false", id="stored-replayed-string"),
        pytest.param("replayed", 0, id="stored-replayed-zero"),
        pytest.param("replayed", 1, id="stored-replayed-one"),
        pytest.param("replayed", None, id="stored-replayed-null"),
        pytest.param("page_path", 0, id="page-path-int"),
        pytest.param("page_path", False, id="page-path-bool"),
        pytest.param("page_path", None, id="page-path-null"),
    ],
)
def test_corrupt_terminal_result_payload_fails_as_revision_conflict(
    legacy_page_fixture, field_name, bad_value
):
    service, before = legacy_page_fixture
    (service.settings.vault_path / before.page_path).unlink()
    result = service.delete_page("delete-corrupt-result", before.page_path)
    assert result.status == "deleted"
    with connect_app(service.settings) as conn:
        row = conn.execute(
            "SELECT result_payload_json FROM vault_change_events WHERE id=?",
            ("delete-corrupt-result",),
        ).fetchone()
        payload = json.loads(row["result_payload_json"])
        payload[field_name] = bad_value
        conn.execute(
            "UPDATE vault_change_events SET result_payload_json=? WHERE id=?",
            (
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                "delete-corrupt-result",
            ),
        )
    with pytest.raises(RevisionConflict, match="vault event result payload is invalid"):
        service.delete_page("delete-corrupt-result", before.page_path)


@pytest.mark.parametrize("bad_replayed", ["false", 0, 1, None])
def test_mutation_result_codec_rejects_non_boolean_replay_argument(bad_replayed):
    payload = MutationResult(
        page_id=None,
        page_path="wiki/product/invalid.md",
        status="invalid",
    ).to_event_payload()
    with pytest.raises(RevisionConflict, match="vault event result payload is invalid"):
        MutationResult.from_event_payload(payload, replayed=bad_replayed)


def test_mutation_result_codec_rejects_unhashable_status():
    payload = MutationResult(
        page_id=None,
        page_path="wiki/product/invalid.md",
        status="invalid",
    ).to_event_payload()
    payload["status"] = []
    with pytest.raises(RevisionConflict, match="vault event result payload is invalid"):
        MutationResult.from_event_payload(payload)


def test_legacy_pending_external_intent_recovers_without_fabricated_event_result(
    legacy_page_fixture, monkeypatch
):
    service, before = legacy_page_fixture
    target = service.settings.vault_path / before.page_path
    target.write_bytes(before.raw_bytes + b"\nLegacy pending recovery.\n")

    def interrupt_execute(self, intent_id):
        raise RuntimeError("stop after external preparation")

    with monkeypatch.context() as scoped:
        scoped.setattr(IntentExecutor, "execute", interrupt_execute)
        with pytest.raises(RuntimeError, match="stop after external preparation"):
            service.ingest_external_change(
                "legacy-pending-intent",
                before.page_path,
                capture_file_observation(
                    target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
                ),
            )
    with connect_app(service.settings) as conn:
        prepared_intent = conn.execute(
            """
            SELECT i.id,i.status,i.revision_id
            FROM wiki_pages AS p
            JOIN vault_write_intents AS i ON i.id=p.pending_write_intent_id
            WHERE p.page_id=?
            """,
            (before.page_id,),
        ).fetchone()
        assert prepared_intent["status"] == "pending"
        prepared_intent_id = prepared_intent["id"]
        prepared_revision_id = prepared_intent["revision_id"]
        conn.execute(
            """
            UPDATE vault_change_events
            SET payload_digest='',result_revision_id=?,result_payload_json=NULL
            WHERE id='legacy-pending-intent'
            """,
            (prepared_revision_id,),
        )
    IntentExecutor(service.settings).reconcile_all()
    with connect_app(service.settings) as conn:
        event = conn.execute(
            """
            SELECT status,payload_digest,result_revision_id,result_payload_json
            FROM vault_change_events WHERE id='legacy-pending-intent'
            """
        ).fetchone()
        intent = conn.execute(
            """
            SELECT status FROM vault_write_intents WHERE id=?
            """,
            (prepared_intent_id,),
        ).fetchone()
    assert intent["status"] == "applied"
    assert event["status"] == "legacy_applied_unreplayable"
    assert event["payload_digest"] == ""
    assert event["result_revision_id"] == prepared_revision_id
    assert event["result_payload_json"] is None
    with pytest.raises(
        RevisionConflict,
        match="legacy vault event has no authoritative result payload",
    ):
        service.ingest_external_change(
            "legacy-pending-intent",
            before.page_path,
            capture_file_observation(
                target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
            ),
        )


def test_legacy_pending_external_intent_rejects_different_result_revision(
    legacy_page_fixture, monkeypatch
):
    service, before = legacy_page_fixture
    target = service.settings.vault_path / before.page_path
    target.write_bytes(before.raw_bytes + b"\nLegacy mismatched result.\n")

    def interrupt_execute(self, intent_id):
        raise RuntimeError("stop after external preparation")

    with monkeypatch.context() as scoped:
        scoped.setattr(IntentExecutor, "execute", interrupt_execute)
        with pytest.raises(RuntimeError, match="stop after external preparation"):
            service.ingest_external_change(
                "legacy-mismatched-result",
                before.page_path,
                capture_file_observation(
                    target,
                    max_content_bytes=1_000_000,
                    prefix_bytes=64 * 1024,
                ),
            )
    with connect_app(service.settings) as conn:
        page_before = conn.execute(
            """
            SELECT current_revision_id,pending_write_intent_id,projection_epoch
            FROM wiki_pages WHERE page_id=?
            """,
            (before.page_id,),
        ).fetchone()
        prepared_intent = conn.execute(
            "SELECT revision_id FROM vault_write_intents WHERE id=?",
            (page_before["pending_write_intent_id"],),
        ).fetchone()
        assert prepared_intent["revision_id"] != before.current_revision_id
        conn.execute(
            """
            UPDATE vault_change_events
            SET payload_digest='',result_revision_id=?,result_payload_json=NULL
            WHERE id='legacy-mismatched-result'
            """,
            (before.current_revision_id,),
        )

    IntentExecutor(service.settings).reconcile_all()

    with connect_app(service.settings) as conn:
        page_after = conn.execute(
            """
            SELECT current_revision_id,pending_write_intent_id,projection_epoch
            FROM wiki_pages WHERE page_id=?
            """,
            (before.page_id,),
        ).fetchone()
        intent_after = conn.execute(
            "SELECT status FROM vault_write_intents WHERE id=?",
            (page_before["pending_write_intent_id"],),
        ).fetchone()
        event_after = conn.execute(
            """
            SELECT status,result_revision_id,result_payload_json
            FROM vault_change_events WHERE id='legacy-mismatched-result'
            """
        ).fetchone()
    assert tuple(page_after) == tuple(page_before)
    assert intent_after["status"] == "recovery_required"
    assert tuple(event_after) == ("prepared", before.current_revision_id, None)


def test_recovery_candidate_result_is_written_only_at_terminal_finalize(
    legacy_page_fixture,
):
    service, before = legacy_page_fixture
    event_id = "recovery-candidate-terminal-result"
    target = service.settings.vault_path / before.page_path
    target.write_bytes(before.raw_bytes + b"\nRecovered external bytes.\n")
    captured = capture_file_observation(
        target,
        max_content_bytes=1_000_000,
        prefix_bytes=64 * 1024,
    )
    target.write_bytes(before.raw_bytes)

    def prepare_candidate():
        with service.coordinator.lock_page(before.page_path) as locked:
            observation = service._persist_observation_locked(
                locked.conn,
                locked.page,
                captured,
            )
            expected_state_json = canonical_state_json(
                service._canonical_state_locked(locked.conn, locked.page)
            )
            return service._create_external_candidate_locked(
                locked.conn,
                locked.page,
                observation=observation,
                event_id=event_id,
                expected_state_json=expected_state_json,
                actor="vault-recovery",
                metadata_extra={"recovery_source": "target"},
            )

    def stored_event():
        with connect_app(service.settings) as conn:
            return dict(
                conn.execute(
                    "SELECT * FROM vault_change_events WHERE id=?",
                    (event_id,),
                ).fetchone()
            )

    first_candidate = prepare_candidate()
    with service.coordinator.lock_page(before.page_path) as locked:
        review_id = service._seed_concurrent_review_locked(
            locked.conn,
            locked.page,
            candidate=first_candidate,
            event_id=event_id,
        )
    first_event = stored_event()

    replayed_candidate = prepare_candidate()
    with service.coordinator.lock_page(before.page_path) as locked:
        replayed_review_id = service._seed_concurrent_review_locked(
            locked.conn,
            locked.page,
            candidate=replayed_candidate,
            event_id=event_id,
        )
    replayed_event = stored_event()

    assert first_candidate.id == replayed_candidate.id
    assert review_id == replayed_review_id
    for event in (first_event, replayed_event):
        assert event["status"] == "prepared"
        assert event["result_revision_id"] is None
        assert event["result_payload_json"] is None

    resolved = service.resolve_conflict(
        ResolveConflictCommand(
            review_id=review_id,
            resolution="accept_candidate",
            merged_content=None,
            expected_current_revision_id=before.current_revision_id,
            expected_generated_revision_id=before.generated_revision_id,
            request_id="accept-recovery-candidate",
            actor="recovery-admin",
            note=None,
        )
    )
    terminal_event = stored_event()
    terminal_payload = json.loads(terminal_event["result_payload_json"])

    assert resolved.status == "resolved"
    assert terminal_event["status"] == "applied"
    assert terminal_event["result_revision_id"] == first_candidate.id
    assert terminal_payload["revision_id"] == first_candidate.id
    assert terminal_payload["current_revision_id"] == first_candidate.id
    assert terminal_event["result_payload_json"] == json.dumps(
        terminal_payload,
        sort_keys=True,
        separators=(",", ":"),
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
    seed_source(settings, "src_1")
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(path,domain,page_type,title,source_ids_json,review_status,owner,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (page_path,"product","faq","Demo",'["src_1"]',"reviewed","alice","t0","t0"),
        )
    return page_path


def applied_audit_payload(settings, revision_id: str) -> dict:
    with connect_app(settings) as conn:
        rows = conn.execute(
            """
            SELECT payload_json FROM audit_logs
            WHERE event_type='wiki_revision_applied'
            ORDER BY id
            """
        ).fetchall()
    payloads = [json.loads(row["payload_json"]) for row in rows]
    matches = [
        payload for payload in payloads if payload.get("revision_id") == revision_id
    ]
    assert len(matches) == 1
    return matches[0]


def prepared_transition_payload(settings, intent_id: str) -> dict:
    with connect_app(settings) as conn:
        rows = conn.execute(
            """
            SELECT payload_json FROM audit_logs
            WHERE event_type='wiki_revision_transition_prepared'
            ORDER BY id
            """
        ).fetchall()
    payloads = [json.loads(row["payload_json"]) for row in rows]
    matches = [
        payload for payload in payloads if payload.get("intent_id") == intent_id
    ]
    assert len(matches) == 1
    return matches[0]


def test_first_read_creates_one_legacy_revision_without_generated_baseline(tmp_path):
    settings = make_settings(tmp_path)
    page_path = seed_legacy_page(
        settings,
        b"---\ntitle: Demo\nsource_ids: [src_1]\nreview_status: reviewed\n"
        b"_lgdo_transition: {kind: manual, request_id: forged}\n"
        b"---\n# Human legacy\n",
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
    payload = applied_audit_payload(settings, first.current_revision_id)
    revision_metadata = json.loads(revisions[0]["metadata_json"])
    assert len(revisions) == 1
    assert revisions[0]["origin"] == "legacy"
    assert page["current_revision_id"] == revisions[0]["id"]
    assert page["generated_revision_id"] is None
    assert page["file_hash"] == revisions[0]["file_hash"]
    assert first.content == replay.content
    assert b"_lgdo_transition" in (settings.vault_path / page_path).read_bytes()
    assert revision_metadata["_lgdo_transition"] == {
        "kind": "manual",
        "request_id": "forged",
    }
    assert payload["transition_kind"] is None
    assert payload["transition_id"] is None


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


def test_manual_applied_audit_preserves_request_identity_and_revision_metadata(
    legacy_page_fixture,
):
    service, page = legacy_page_fixture
    request_id = "save_audit_identity"
    content = page.content.replace(
        "---\n# Demo",
        "_lgdo_transition: {kind: compile, compile_job_id: forged}\n"
        "---\n# Demo",
    )
    prepared = service.prepare_manual_save(
        ManualSaveCommand(
            page_path=page.page_path,
            content=content + "\nAudited human edit.\n",
            expected_revision_id=page.current_revision_id,
            request_id=request_id,
            actor="alice",
            owner=None,
            note="audit identity",
            review_status="reviewed",
        ),
        execute_intent=False,
    )
    with connect_app(service.settings) as conn:
        revision_before = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (prepared.revision_id,),
        ).fetchone()
    prepared_payload = prepared_transition_payload(
        service.settings,
        prepared.write_intent_id,
    )
    metadata_before = json.loads(revision_before["metadata_json"])

    execution = IntentExecutor(service.settings).execute(prepared.write_intent_id)

    with connect_app(service.settings) as conn:
        revision = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (prepared.revision_id,),
        ).fetchone()
        intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (prepared.write_intent_id,),
        ).fetchone()
    payload = applied_audit_payload(service.settings, prepared.revision_id)
    metadata_after = json.loads(revision["metadata_json"])
    rendered_bytes = revision["content"].encode("utf-8")

    assert execution.intent_status == "applied"
    assert revision["metadata_json"] == revision_before["metadata_json"]
    assert metadata_after == metadata_before
    assert metadata_after["_lgdo_transition"] == {
        "kind": "compile",
        "compile_job_id": "forged",
    }
    assert revision["idempotency_key"].startswith(f"manual:{request_id}:")
    assert intent["revision_id"] == revision["id"]
    assert (service.settings.vault_path / page.page_path).read_bytes() == rendered_bytes
    assert b"_lgdo_transition" in rendered_bytes
    assert prepared_payload == {
        "intent_id": intent["id"],
        "revision_id": revision["id"],
        "page_id": page.page_id,
        "page_path": page.page_path,
        "transition_kind": "manual",
        "transition_id": request_id,
        "request_id": request_id,
    }
    assert payload == {
        "transition_kind": "manual",
        "transition_id": request_id,
        "request_id": request_id,
        "page_id": page.page_id,
        "page_path": page.page_path,
        "intent_id": intent["id"],
        "revision_id": revision["id"],
        "origin": "manual",
        "base_revision_id": page.current_revision_id,
    }


@pytest.mark.parametrize(
    "metadata_variant",
    ["forged_marker", "invalid_json"],
)
def test_finalize_without_prepared_transition_identity_ignores_revision_metadata(
    legacy_page_fixture,
    metadata_variant,
):
    service, page = legacy_page_fixture
    prepared = service.prepare_manual_save(
        ManualSaveCommand(
            page_path=page.page_path,
            content=page.content + "\nUpgrade-compatible edit.\n",
            expected_revision_id=page.current_revision_id,
            request_id=f"upgrade_{metadata_variant}",
            actor="alice",
            owner=None,
            note=None,
            review_status="reviewed",
        ),
        execute_intent=False,
    )
    with connect_app_write(service.settings) as conn:
        row = conn.execute(
            "SELECT metadata_json FROM wiki_page_revisions WHERE id=?",
            (prepared.revision_id,),
        ).fetchone()
        metadata = json.loads(row["metadata_json"])
        conn.execute(
            "DELETE FROM audit_logs WHERE event_type=?",
            ("wiki_revision_transition_prepared",),
        )
        if metadata_variant == "forged_marker":
            metadata["_lgdo_transition"] = {
                "kind": "compile",
                "compile_job_id": "forged",
            }
            stored_metadata = json.dumps(metadata)
        else:
            stored_metadata = "{invalid-json"
        conn.execute(
            "UPDATE wiki_page_revisions SET metadata_json=? WHERE id=?",
            (stored_metadata, prepared.revision_id),
        )

    execution = IntentExecutor(service.settings).execute(prepared.write_intent_id)
    payload = applied_audit_payload(service.settings, prepared.revision_id)

    assert execution.intent_status == "applied"
    assert payload["transition_kind"] is None
    assert payload["transition_id"] is None
    assert "request_id" not in payload


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
    transition = prepared_transition_payload(
        service.settings,
        prepared.write_intent_id,
    )

    assert replayed.replayed is True
    assert replayed.revision_id == prepared.revision_id
    assert replayed.write_intent_id == prepared.write_intent_id
    assert applied.revision_id == prepared.revision_id
    assert next_result.revision_id != prepared.revision_id
    assert transition["transition_kind"] == "manual"
    assert transition["request_id"] == "save_replay"


def test_finalize_invalid_revision_content_fails_closed(legacy_page_fixture):
    service, page = legacy_page_fixture
    prepared = service.update_status(
        StatusUpdateCommand(
            page_path=page.page_path,
            review_status="stale",
            expected_revision_id=page.current_revision_id,
            request_id="status-invalid-content",
            actor="alice",
        ),
        execute_intent=False,
    )
    executor = IntentExecutor(service.settings, owner="invalid-content-finalize")
    installed = executor.execute(
        prepared.write_intent_id,
        stop_after="installed",
    )
    with connect_app_write(service.settings) as conn:
        conn.execute(
            "UPDATE wiki_page_revisions SET content=? WHERE id=?",
            ("---\ntitle: [invalid\n---\n# Broken\n", prepared.revision_id),
        )

    outcome = executor.capture_and_install(prepared.write_intent_id)

    with connect_app(service.settings) as conn:
        page_row = conn.execute(
            "SELECT current_revision_id,pending_write_intent_id FROM wiki_pages WHERE page_id=?",
            (page.page_id,),
        ).fetchone()
        intent = conn.execute(
            "SELECT status FROM vault_write_intents WHERE id=?",
            (prepared.write_intent_id,),
        ).fetchone()
        applied_rows = conn.execute(
            """
            SELECT payload_json FROM audit_logs
            WHERE event_type='wiki_revision_applied'
            """,
        ).fetchall()
    applied_count = sum(
        json.loads(row["payload_json"]).get("revision_id") == prepared.revision_id
        for row in applied_rows
    )

    assert installed.intent_status == "installed"
    assert outcome.intent_status == "recovery_required"
    assert page_row["current_revision_id"] == page.current_revision_id
    assert page_row["pending_write_intent_id"] == prepared.write_intent_id
    assert intent["status"] == "recovery_required"
    assert applied_count == 0


def test_finalize_valid_revision_content_hash_mismatch_fails_closed(
    legacy_page_fixture,
):
    service, page = legacy_page_fixture
    prepared = service.update_status(
        StatusUpdateCommand(
            page_path=page.page_path,
            review_status="stale",
            expected_revision_id=page.current_revision_id,
            request_id="status-valid-content-hash-mismatch",
            actor="alice",
            owner="status-owner",
        ),
        execute_intent=False,
    )
    executor = IntentExecutor(service.settings, owner="content-hash-finalize")
    installed = executor.execute(
        prepared.write_intent_id,
        stop_after="installed",
    )
    with connect_app_write(service.settings) as conn:
        revision = conn.execute(
            "SELECT content FROM wiki_page_revisions WHERE id=?",
            (prepared.revision_id,),
        ).fetchone()
        tampered_content = revision["content"].replace(
            "review_status: stale",
            "review_status: rejected",
        )
        assert tampered_content != revision["content"]
        conn.execute(
            "UPDATE wiki_page_revisions SET content=? WHERE id=?",
            (tampered_content, prepared.revision_id),
        )

    outcome = executor.capture_and_install(prepared.write_intent_id)

    with connect_app(service.settings) as conn:
        page_row = conn.execute(
            """
            SELECT current_revision_id,pending_write_intent_id,projection_epoch,
                   review_status,owner
            FROM wiki_pages WHERE page_id=?
            """,
            (page.page_id,),
        ).fetchone()
        intent = conn.execute(
            "SELECT status FROM vault_write_intents WHERE id=?",
            (prepared.write_intent_id,),
        ).fetchone()
        applied_count = conn.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE event_type='wiki_revision_applied'
              AND payload_json LIKE ?
            """,
            (f'%"revision_id":"{prepared.revision_id}"%',),
        ).fetchone()[0]
        projection_count = conn.execute(
            """
            SELECT COUNT(*) FROM knowledge_projection_jobs
            WHERE revision_id=?
            """,
            (prepared.revision_id,),
        ).fetchone()[0]

    assert installed.intent_status == "installed"
    assert outcome.intent_status == "recovery_required"
    assert page_row["current_revision_id"] == page.current_revision_id
    assert page_row["pending_write_intent_id"] == prepared.write_intent_id
    assert page_row["projection_epoch"] == page.projection_epoch
    assert page_row["review_status"] == "reviewed"
    assert page_row["owner"] == "alice"
    assert intent["status"] == "recovery_required"
    assert applied_count == 0
    assert projection_count == 0


@pytest.mark.parametrize(
    "tampered_field",
    ["revision_id", "transition_kind", "transition_id"],
)
def test_finalize_rejects_invalid_prepared_transition_for_same_intent(
    legacy_page_fixture,
    tampered_field,
):
    service, page = legacy_page_fixture
    prepared = service.prepare_manual_save(
        ManualSaveCommand(
            page_path=page.page_path,
            content=page.content + "\nPrepared audit integrity.\n",
            expected_revision_id=page.current_revision_id,
            request_id="prepared-audit-integrity",
            actor="alice",
            owner=None,
            note=None,
            review_status="reviewed",
        ),
        execute_intent=False,
    )
    with connect_app_write(service.settings) as conn:
        row = conn.execute(
            """
            SELECT id,payload_json FROM audit_logs
            WHERE event_type='wiki_revision_transition_prepared'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        payload = json.loads(row["payload_json"])
        assert payload["intent_id"] == prepared.write_intent_id
        if tampered_field == "revision_id":
            payload["revision_id"] = "wrev_wrong"
        elif tampered_field == "transition_kind":
            payload["transition_kind"] = "status"
        else:
            payload["transition_id"] = "forged-request"
            payload["request_id"] = "forged-request"
        conn.execute(
            "UPDATE audit_logs SET payload_json=? WHERE id=?",
            (json.dumps(payload), row["id"]),
        )

    outcome = IntentExecutor(service.settings).execute(prepared.write_intent_id)

    with connect_app(service.settings) as conn:
        page_row = conn.execute(
            """
            SELECT current_revision_id,pending_write_intent_id,projection_epoch
            FROM wiki_pages WHERE page_id=?
            """,
            (page.page_id,),
        ).fetchone()
        intent = conn.execute(
            "SELECT status FROM vault_write_intents WHERE id=?",
            (prepared.write_intent_id,),
        ).fetchone()
        applied_count = conn.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE event_type='wiki_revision_applied'
              AND payload_json LIKE ?
            """,
            (f'%"revision_id":"{prepared.revision_id}"%',),
        ).fetchone()[0]
        projection_count = conn.execute(
            """
            SELECT COUNT(*) FROM knowledge_projection_jobs
            WHERE revision_id=?
            """,
            (prepared.revision_id,),
        ).fetchone()[0]

    assert outcome.intent_status == "recovery_required"
    assert page_row["current_revision_id"] == page.current_revision_id
    assert page_row["pending_write_intent_id"] == prepared.write_intent_id
    assert page_row["projection_epoch"] == page.projection_epoch
    assert intent["status"] == "recovery_required"
    assert applied_count == 0
    assert projection_count == 0


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


def test_status_applied_audit_preserves_status_request_identity(
    legacy_page_fixture,
):
    service, page = legacy_page_fixture
    request_id = "status_audit_identity"

    prepared = service.update_status(
        StatusUpdateCommand(
            page_path=page.page_path,
            review_status="stale",
            expected_revision_id=page.current_revision_id,
            request_id=request_id,
            actor="alice",
            owner="status-owner",
            note="status audit identity",
        ),
        execute_intent=False,
    )
    with connect_app_write(service.settings) as conn:
        revision = conn.execute(
            "SELECT metadata_json FROM wiki_page_revisions WHERE id=?",
            (prepared.revision_id,),
        ).fetchone()
        original_metadata = json.loads(revision["metadata_json"])
        conn.execute(
            "UPDATE wiki_page_revisions SET metadata_json=? WHERE id=?",
            ("{invalid-json", prepared.revision_id),
        )

    execution = IntentExecutor(service.settings).execute(prepared.write_intent_id)
    changed = service.get_page(page.page_path)
    with connect_app(service.settings) as conn:
        page_row = conn.execute(
            "SELECT review_status,owner FROM wiki_pages WHERE page_id=?",
            (page.page_id,),
        ).fetchone()
    payload = applied_audit_payload(service.settings, prepared.revision_id)

    assert execution.intent_status == "applied"
    assert changed.metadata["review_status"] == "stale"
    assert changed.metadata["owner"] == "status-owner"
    assert page_row["review_status"] == "stale"
    assert page_row["owner"] == "status-owner"
    prepared_payload = prepared_transition_payload(
        service.settings,
        prepared.write_intent_id,
    )
    assert "_lgdo_transition" not in original_metadata
    assert prepared_payload["transition_kind"] == "status"
    assert prepared_payload["transition_id"] == request_id
    assert prepared_payload["request_id"] == request_id
    assert prepared_payload["intent_id"] == prepared.write_intent_id
    assert prepared_payload["revision_id"] == prepared.revision_id
    assert payload["transition_kind"] == "status"
    assert payload["transition_id"] == request_id
    assert payload["request_id"] == request_id
    assert payload["page_id"] == page.page_id
    assert payload["page_path"] == page.page_path
    assert payload["intent_id"] == prepared.write_intent_id
    assert payload["revision_id"] == prepared.revision_id
    assert payload["origin"] == "manual"
    assert payload["base_revision_id"] == page.current_revision_id


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


def test_first_generated_auto_apply_audit_preserves_compile_job_identity(
    page_with_generated,
):
    service, page = page_with_generated
    with connect_app(service.settings) as conn:
        revision = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (page.current_revision_id,),
        ).fetchone()
        revision_metadata = json.loads(revision["metadata_json"])
        intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE revision_id=?",
            (page.current_revision_id,),
        ).fetchone()
    prepared_payload = prepared_transition_payload(service.settings, intent["id"])
    payload = applied_audit_payload(service.settings, page.current_revision_id)

    assert revision["origin"] == "generated"
    assert intent["status"] == "applied"
    assert "_lgdo_transition" not in revision_metadata
    assert prepared_payload == {
        "intent_id": intent["id"],
        "revision_id": revision["id"],
        "page_id": page.page_id,
        "page_path": page.page_path,
        "transition_kind": "compile",
        "transition_id": "compile-initial",
        "compile_job_id": "compile-initial",
    }
    assert payload["transition_kind"] == "compile"
    assert payload["transition_id"] == "compile-initial"
    assert payload["compile_job_id"] == "compile-initial"
    assert payload["page_id"] == page.page_id
    assert payload["page_path"] == page.page_path
    assert payload["intent_id"] == intent["id"]
    assert payload["revision_id"] == revision["id"]
    assert payload["origin"] == "generated"
    assert payload["base_revision_id"] is None


def test_valid_external_change_applied_audit_preserves_event_identity(
    page_with_generated,
):
    service, before = page_with_generated
    target = service.settings.vault_path / before.page_path
    target.write_bytes(before.raw_bytes + b"\nAudited Obsidian edit.\n")
    observation = capture_file_observation(
        target,
        max_content_bytes=1024 * 1024,
    )
    event_id = "external-audit-identity"

    result = service.ingest_external_change(
        event_id,
        before.page_path,
        observation,
    )
    with connect_app(service.settings) as conn:
        revision = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (result.revision_id,),
        ).fetchone()
        event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
    payload = applied_audit_payload(service.settings, result.revision_id)
    metadata = json.loads(revision["metadata_json"])

    assert metadata["vault_change_event_id"] == event_id == event["id"]
    assert payload["transition_kind"] == "external"
    assert payload["transition_id"] == event_id
    assert payload["event_id"] == event_id
    assert payload["page_id"] == before.page_id
    assert payload["page_path"] == before.page_path
    assert payload["intent_id"] == result.write_intent_id
    assert payload["revision_id"] == revision["id"]
    assert payload["origin"] == "external"
    assert payload["base_revision_id"] == before.current_revision_id


def test_active_exact_current_observation_is_terminal_ignored_noop(
    page_with_generated,
):
    service, before = page_with_generated
    target = service.settings.vault_path / before.page_path
    target.write_bytes(before.raw_bytes)
    with connect_app(service.settings) as conn:
        conn.execute(
            """
            UPDATE wiki_pages
            SET rag_visible_revision_id=current_revision_id,
                rag_visible_epoch=projection_epoch
            WHERE page_id=?
            """,
            (before.page_id,),
        )
        revision_count_before = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
            (before.page_id,),
        ).fetchone()[0]
        job_count_before = conn.execute(
            "SELECT COUNT(*) FROM knowledge_projection_jobs WHERE page_id=?",
            (before.page_id,),
        ).fetchone()[0]

    observation = capture_file_observation(
        target,
        max_content_bytes=1024 * 1024,
    )
    result = service.ingest_external_change(
        "external-exact-current-ignored",
        before.page_path,
        observation,
    )

    with connect_app(service.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
        revision_count_after = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
            (before.page_id,),
        ).fetchone()[0]
        job_count_after = conn.execute(
            "SELECT COUNT(*) FROM knowledge_projection_jobs WHERE page_id=?",
            (before.page_id,),
        ).fetchone()[0]
        event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            ("external-exact-current-ignored",),
        ).fetchone()
        stored_observation = conn.execute(
            "SELECT * FROM wiki_file_observations WHERE id=?",
            (result.observation_id,),
        ).fetchone()
        audits = conn.execute(
            """
            SELECT payload_json FROM audit_logs
            WHERE event_type='wiki_external_change_ignored'
            """
        ).fetchall()

    assert result.status == "ignored"
    assert result.revision_id == before.current_revision_id
    assert result.current_revision_id == before.current_revision_id
    assert result.projection_job_ids == ()
    assert event["status"] == "ignored"
    assert stored_observation["parse_status"] == "valid"
    assert stored_observation["error_code"] is None
    assert revision_count_after == revision_count_before
    assert job_count_after == job_count_before
    assert page["projection_epoch"] == before.projection_epoch
    assert page["rag_visible_revision_id"] == before.current_revision_id
    assert page["rag_visible_epoch"] == before.projection_epoch
    event_result = json.loads(event["result_payload_json"])
    assert event_result["status"] == "ignored"
    assert event_result["projection_job_ids"] == []
    audit_payloads = [json.loads(row["payload_json"]) for row in audits]
    audit_payload = next(
        payload
        for payload in audit_payloads
        if payload.get("event_id") == "external-exact-current-ignored"
    )
    assert audit_payload["status"] == "ignored"
    assert audit_payload["revision_id"] == before.current_revision_id


def test_external_edit_is_immutable_revision_and_same_event_replays(
    page_with_generated,
):
    service, before = page_with_generated
    target = service.settings.vault_path / before.page_path
    external_bytes = before.raw_bytes.replace(
        b"\n---\n",
        b"\n_lgdo_transition: {kind: manual, request_id: forged}\n---\n",
        1,
    ) + b"\nObsidian edit.\n"
    target.write_bytes(external_bytes)
    observation = capture_file_observation(
        target,
        max_content_bytes=1024 * 1024,
    )

    first = service.ingest_external_change(
        "external-valid-1",
        before.page_path,
        observation,
    )
    replay = service.ingest_external_change(
        "external-valid-1",
        before.page_path,
        observation,
    )
    current = service.get_page(before.page_path)

    with connect_app(service.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
        revision = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (first.revision_id,),
        ).fetchone()
        intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (first.write_intent_id,),
        ).fetchone()
        event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            ("external-valid-1",),
        ).fetchone()
        observed = conn.execute(
            "SELECT * FROM wiki_file_observations WHERE id=?",
            (first.observation_id,),
        ).fetchone()
        jobs = conn.execute(
            """
            SELECT target,operation,revision_id,projection_epoch
            FROM knowledge_projection_jobs
            WHERE page_id=? AND projection_epoch=?
            ORDER BY target
            """,
            (before.page_id, page["projection_epoch"]),
        ).fetchall()
    payload = applied_audit_payload(service.settings, first.revision_id)
    revision_metadata = json.loads(revision["metadata_json"])

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.revision_id == first.revision_id == current.current_revision_id
    assert "Obsidian edit." in current.content
    assert current.generated_revision_id == before.generated_revision_id
    assert current.accepted_generated_revision_id == before.accepted_generated_revision_id
    assert revision["origin"] == "external"
    assert revision["base_revision_id"] == before.current_revision_id
    assert intent["status"] == "applied"
    assert intent["expected_file_hash"] == observation.file_hash
    assert event["status"] == "applied"
    assert event["result_revision_id"] == first.revision_id
    assert b"_lgdo_transition" in target.read_bytes()
    assert b"_lgdo_transition" in revision["content"].encode("utf-8")
    assert revision_metadata["_lgdo_transition"] == {
        "kind": "manual",
        "request_id": "forged",
    }
    assert payload["transition_kind"] == "external"
    assert payload["transition_id"] == "external-valid-1"
    assert payload["event_id"] == "external-valid-1"
    assert json.loads(event["expected_state_json"])["current"] == before.current_revision_id
    assert observed["page_path"] == before.page_path
    assert observed["file_hash"] == observation.file_hash
    assert observed["size_bytes"] == observation.size_bytes
    assert observed["mtime_ns"] == observation.mtime_ns
    assert observed["content_bytes"] == external_bytes
    assert observed["content_prefix"] is None
    assert observed["content_truncated"] == 0
    assert observed["parse_status"] == "valid"
    assert observed["error_code"] is None
    assert page["projection_epoch"] == before.projection_epoch + 1
    assert [(row["target"], row["operation"]) for row in jobs] == [
        ("gbrain", "upsert"),
        ("rag", "upsert"),
    ]
    assert all(row["revision_id"] == first.revision_id for row in jobs)


@pytest.mark.parametrize(
    ("error_code", "invalid_bytes"),
    [
        ("invalid_utf8", b"---\ntitle: Demo\nsource_ids: [src_1]\n---\n\xff"),
        ("invalid_yaml", b"---\ntitle: [unterminated\n---\n# Invalid\n"),
        (
            "page_identity_conflict",
            b"---\ntitle: Demo\nsource_ids: [src_1]\nlgdo_page_id: page_elsewhere\n---\n# Invalid\n",
        ),
    ],
)
def test_invalid_external_bytes_fail_closed_without_changing_vault_or_current(
    page_with_generated,
    error_code,
    invalid_bytes,
):
    service, before = page_with_generated
    target = service.settings.vault_path / before.page_path
    with connect_app(service.settings) as conn:
        revision_count_before = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
            (before.page_id,),
        ).fetchone()[0]
    target.write_bytes(invalid_bytes)
    observation = capture_file_observation(
        target,
        max_content_bytes=1024 * 1024,
    )
    event_id = f"external-{error_code}"

    first = service.ingest_external_change(event_id, before.page_path, observation)
    replay = service.ingest_external_change(event_id, before.page_path, observation)
    historical = service.get_page(before.page_path)

    with connect_app(service.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
        revisions = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE page_id=? ORDER BY revision_number",
            (before.page_id,),
        ).fetchall()
        observed = conn.execute(
            "SELECT * FROM wiki_file_observations WHERE id=?",
            (first.observation_id,),
        ).fetchone()
        event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
        reviews = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='invalid_frontmatter' AND status='pending'
            """,
            (before.page_id,),
        ).fetchall()
        jobs = conn.execute(
            """
            SELECT target,operation,revision_id,projection_epoch
            FROM knowledge_projection_jobs
            WHERE page_id=? AND projection_epoch=?
            ORDER BY target
            """,
            (before.page_id, page["projection_epoch"]),
        ).fetchall()
        stale_jobs = conn.execute(
            """
            SELECT status FROM knowledge_projection_jobs
            WHERE page_id=? AND projection_epoch<?
            ORDER BY id
            """,
            (before.page_id, page["projection_epoch"]),
        ).fetchall()

    assert first.status == "invalid"
    assert first.revision_id == before.current_revision_id
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.revision_id == before.current_revision_id
    assert first.write_intent_id is None
    assert replay.write_intent_id == first.write_intent_id
    assert target.read_bytes() == invalid_bytes
    assert historical.current_revision_id == before.current_revision_id
    assert historical.raw_bytes == before.raw_bytes
    assert historical.content == before.content
    assert len(revisions) == revision_count_before
    assert page["current_revision_id"] == before.current_revision_id
    assert page["generated_revision_id"] == before.generated_revision_id
    assert page["accepted_generated_revision_id"] == before.accepted_generated_revision_id
    assert page["lifecycle_status"] == "invalid"
    assert page["sync_error"] == error_code
    assert page["observed_file_hash"] == observation.file_hash
    assert page["projection_epoch"] == before.projection_epoch + 1
    assert page["rag_visible_revision_id"] is None
    assert page["rag_visible_epoch"] is None
    assert observed["content_bytes"] == invalid_bytes
    assert observed["content_prefix"] is None
    assert observed["content_truncated"] == 0
    assert observed["parse_status"] == "invalid"
    assert observed["error_code"] == error_code
    assert event["status"] == "invalid"
    assert event["result_revision_id"] == before.current_revision_id
    assert len(reviews) == 1
    assert first.conflict_review_id == reviews[0]["id"]
    assert [(row["target"], row["operation"]) for row in jobs] == [
        ("gbrain", "delete"),
        ("rag", "delete"),
    ]
    assert all(row["revision_id"] is None for row in jobs)
    assert stale_jobs
    assert {row["status"] for row in stale_jobs} == {"superseded"}


def test_valid_external_change_repairs_invalid_page_and_closes_review(
    page_with_generated,
):
    service, before = page_with_generated
    target = service.settings.vault_path / before.page_path
    invalid_bytes = b"---\ntitle: [invalid\n---\n# Broken\n"
    target.write_bytes(invalid_bytes)
    invalid_observation = capture_file_observation(
        target,
        max_content_bytes=1024 * 1024,
    )
    invalid = service.ingest_external_change(
        "external-invalid-before-repair",
        before.page_path,
        invalid_observation,
    )
    valid_bytes = before.raw_bytes + b"\nRepaired in Obsidian.\n"
    target.write_bytes(valid_bytes)
    valid_observation = capture_file_observation(
        target,
        max_content_bytes=1024 * 1024,
    )

    repaired = service.ingest_external_change(
        "external-valid-repair",
        before.page_path,
        valid_observation,
    )
    current = service.get_page(before.page_path)

    with connect_app(service.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
        revision = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (repaired.revision_id,),
        ).fetchone()
        reviews = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='invalid_frontmatter'
            """,
            (before.page_id,),
        ).fetchall()
        jobs = conn.execute(
            """
            SELECT target,operation,revision_id
            FROM knowledge_projection_jobs
            WHERE page_id=? AND projection_epoch=?
            ORDER BY target
            """,
            (before.page_id, page["projection_epoch"]),
        ).fetchall()

    assert invalid.status == "invalid"
    assert repaired.status == "applied"
    assert current.current_revision_id == repaired.revision_id
    assert "Repaired in Obsidian." in current.content
    assert current.lifecycle_status == "active"
    assert current.sync_error is None
    assert page["observed_file_hash"] == valid_observation.file_hash
    assert page["projection_epoch"] == before.projection_epoch + 2
    assert page["generated_revision_id"] == before.generated_revision_id
    assert page["accepted_generated_revision_id"] == before.accepted_generated_revision_id
    assert revision["origin"] == "external"
    assert revision["base_revision_id"] == before.current_revision_id
    assert len(reviews) == 1
    assert reviews[0]["id"] == invalid.conflict_review_id
    assert reviews[0]["status"] == "superseded"
    assert reviews[0]["resolved_at"] is not None
    assert [(row["target"], row["operation"]) for row in jobs] == [
        ("gbrain", "upsert"),
        ("rag", "upsert"),
    ]
    assert all(row["revision_id"] == repaired.revision_id for row in jobs)


def test_truncated_large_external_observation_never_reloads_or_writes_vault(
    page_with_generated,
    monkeypatch,
):
    service, before = page_with_generated
    target = service.settings.vault_path / before.page_path
    with connect_app(service.settings) as conn:
        revision_count_before = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
            (before.page_id,),
        ).fetchone()[0]
    large_bytes = before.raw_bytes + (b"x" * 4096)
    target.write_bytes(large_bytes)
    observation = capture_file_observation(
        target,
        max_content_bytes=1024,
        prefix_bytes=64,
    )

    def fail_read_bytes(path):
        raise AssertionError(f"unexpected full-file read: {path}")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", fail_read_bytes)
        first = service.ingest_external_change(
            "external-truncated-large",
            before.page_path,
            observation,
        )
        replay = service.ingest_external_change(
            "external-truncated-large",
            before.page_path,
            observation,
        )

    with connect_app(service.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
        observed = conn.execute(
            "SELECT * FROM wiki_file_observations WHERE id=?",
            (first.observation_id,),
        ).fetchone()
        revisions = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
            (before.page_id,),
        ).fetchone()[0]
        jobs = conn.execute(
            """
            SELECT target,operation FROM knowledge_projection_jobs
            WHERE page_id=? AND projection_epoch=?
            ORDER BY target
            """,
            (before.page_id, page["projection_epoch"]),
        ).fetchall()

    assert observation.content_bytes is None
    assert observation.content_prefix == large_bytes[:64]
    assert observation.content_truncated is True
    assert first.status == "invalid"
    assert replay.replayed is True
    assert target.read_bytes() == large_bytes
    assert revisions == revision_count_before
    assert page["current_revision_id"] == before.current_revision_id
    assert page["lifecycle_status"] == "invalid"
    assert page["sync_error"] == "file_too_large"
    assert page["observed_file_hash"] == observation.file_hash
    assert page["projection_epoch"] == before.projection_epoch + 1
    assert observed["size_bytes"] == len(large_bytes)
    assert observed["mtime_ns"] == observation.mtime_ns
    assert observed["file_hash"] == observation.file_hash
    assert observed["content_bytes"] is None
    assert observed["content_prefix"] == large_bytes[:64]
    assert observed["content_truncated"] == 1
    assert observed["parse_status"] == "invalid"
    assert observed["error_code"] == "file_too_large"
    assert [(row["target"], row["operation"]) for row in jobs] == [
        ("gbrain", "delete"),
        ("rag", "delete"),
    ]


def test_pending_external_event_retries_after_transition_transaction_rollback(
    page_with_generated,
    monkeypatch,
):
    service, before = page_with_generated
    target = service.settings.vault_path / before.page_path
    external_bytes = before.raw_bytes + b"\nRetry after transition rollback.\n"
    target.write_bytes(external_bytes)
    observation = capture_file_observation(
        target,
        max_content_bytes=1024 * 1024,
    )
    event_id = "external-retry-after-transition-rollback"
    original_canonical_state = service._canonical_state_locked
    calls = 0

    def fail_first_canonical_state(conn, page):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated transition crash")
        return original_canonical_state(conn, page)

    monkeypatch.setattr(
        service,
        "_canonical_state_locked",
        fail_first_canonical_state,
    )

    with pytest.raises(RuntimeError, match="simulated transition crash"):
        service.ingest_external_change(event_id, before.page_path, observation)

    with connect_app(service.settings) as conn:
        pending_event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
        observation_count = conn.execute(
            """
            SELECT COUNT(*) FROM wiki_file_observations
            WHERE page_path=? AND file_hash=?
            """,
            (before.page_path, observation.file_hash),
        ).fetchone()[0]
        external_revisions = conn.execute(
            """
            SELECT COUNT(*) FROM wiki_page_revisions
            WHERE page_id=? AND origin='external'
            """,
            (before.page_id,),
        ).fetchone()[0]
        external_intents = conn.execute(
            """
            SELECT COUNT(*) FROM vault_write_intents AS intent
            JOIN wiki_page_revisions AS revision ON revision.id=intent.revision_id
            WHERE revision.page_id=? AND revision.origin='external'
            """,
            (before.page_id,),
        ).fetchone()[0]

    assert pending_event["status"] == "pending"
    pending_expected_state = json.loads(pending_event["expected_state_json"])
    assert pending_expected_state["current"] == before.current_revision_id
    assert pending_expected_state["file_hash"] == hashlib.sha256(
        before.raw_bytes
    ).hexdigest()
    assert observation_count == 1
    assert external_revisions == 0
    assert external_intents == 0

    applied = service.ingest_external_change(
        event_id,
        before.page_path,
        observation,
    )

    with connect_app(service.settings) as conn:
        event_count = conn.execute(
            "SELECT COUNT(*) FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()[0]
        final_event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
        observation_count = conn.execute(
            """
            SELECT COUNT(*) FROM wiki_file_observations
            WHERE page_path=? AND file_hash=?
            """,
            (before.page_path, observation.file_hash),
        ).fetchone()[0]
        revisions = conn.execute(
            """
            SELECT * FROM wiki_page_revisions
            WHERE page_id=? AND origin='external'
            """,
            (before.page_id,),
        ).fetchall()
        jobs = conn.execute(
            """
            SELECT * FROM knowledge_projection_jobs
            WHERE page_id=? AND revision_id=? AND operation='upsert'
            """,
            (before.page_id, applied.revision_id),
        ).fetchall()

    assert applied.status == "applied"
    assert applied.replayed is False
    assert event_count == 1
    assert final_event["status"] == "applied"
    assert final_event["result_revision_id"] == applied.revision_id
    assert observation_count == 1
    assert [row["id"] for row in revisions] == [applied.revision_id]
    assert len(jobs) == 2


def test_corrupt_pending_external_result_rolls_back_preparation(
    page_with_generated,
    monkeypatch,
):
    service, before = page_with_generated
    target = service.settings.vault_path / before.page_path
    external_bytes = before.raw_bytes + b"\nCorrupt pending event result.\n"
    target.write_bytes(external_bytes)
    observation = capture_file_observation(
        target,
        max_content_bytes=1024 * 1024,
    )
    event_id = "external-corrupt-pending-result"
    original_canonical_state = service._canonical_state_locked
    calls = 0

    def fail_first_canonical_state(conn, page):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated transition crash")
        return original_canonical_state(conn, page)

    monkeypatch.setattr(
        service,
        "_canonical_state_locked",
        fail_first_canonical_state,
    )
    with pytest.raises(RuntimeError, match="simulated transition crash"):
        service.ingest_external_change(event_id, before.page_path, observation)
    monkeypatch.setattr(
        service,
        "_canonical_state_locked",
        original_canonical_state,
    )

    def snapshot():
        with connect_app(service.settings) as conn:
            return {
                "event": dict(
                    conn.execute(
                        "SELECT * FROM vault_change_events WHERE id=?",
                        (event_id,),
                    ).fetchone()
                ),
                "page": tuple(
                    conn.execute(
                        """
                        SELECT current_revision_id,pending_write_intent_id,
                               revision_number,projection_epoch,file_hash
                        FROM wiki_pages WHERE page_id=?
                        """,
                        (before.page_id,),
                    ).fetchone()
                ),
                "external_revisions": conn.execute(
                    """
                    SELECT COUNT(*) FROM wiki_page_revisions
                    WHERE page_id=? AND origin='external'
                    """,
                    (before.page_id,),
                ).fetchone()[0],
                "external_intents": conn.execute(
                    """
                    SELECT COUNT(*) FROM vault_write_intents AS intent
                    JOIN wiki_page_revisions AS revision
                      ON revision.id=intent.revision_id
                    WHERE revision.page_id=? AND revision.origin='external'
                    """,
                    (before.page_id,),
                ).fetchone()[0],
            }

    with connect_app(service.settings) as conn:
        corrupt_revision_id = "wrev_inconsistent_pending_result"
        conn.execute(
            """
            UPDATE vault_change_events SET result_revision_id=?
            WHERE id=? AND status='pending' AND payload_digest<>''
              AND result_payload_json IS NULL
            """,
            (corrupt_revision_id, event_id),
        )
    before_retry = snapshot()
    assert before_retry["event"]["payload_digest"]
    assert before_retry["event"]["result_revision_id"] == corrupt_revision_id

    with pytest.raises(RevisionConflict):
        service.ingest_external_change(event_id, before.page_path, observation)

    after_retry = snapshot()
    assert after_retry == before_retry
    assert target.read_bytes() == external_bytes


def test_pending_invalid_event_keeps_original_expected_state_after_crash(
    page_with_generated,
    monkeypatch,
):
    service, before = page_with_generated
    target = service.settings.vault_path / before.page_path
    invalid_bytes = b"---\ntitle: [crashed-invalid\n---\n# Broken\n"
    target.write_bytes(invalid_bytes)
    invalid_observation = capture_file_observation(
        target,
        max_content_bytes=1024 * 1024,
    )
    invalid_event_id = "external-invalid-crash-before-transition"
    original_invalid_transition = service._invalid_external_change_locked
    calls = 0

    def fail_first_invalid_transition(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated invalid transition crash")
        return original_invalid_transition(*args, **kwargs)

    monkeypatch.setattr(
        service,
        "_invalid_external_change_locked",
        fail_first_invalid_transition,
    )

    with pytest.raises(RuntimeError, match="simulated invalid transition crash"):
        service.ingest_external_change(
            invalid_event_id,
            before.page_path,
            invalid_observation,
        )

    with connect_app(service.settings) as conn:
        pending_invalid = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (invalid_event_id,),
        ).fetchone()
    original_expected_state = json.loads(pending_invalid["expected_state_json"])
    assert pending_invalid["status"] == "pending"
    assert original_expected_state["current"] == before.current_revision_id
    assert original_expected_state["file_hash"] == hashlib.sha256(
        before.raw_bytes
    ).hexdigest()

    valid_bytes = before.raw_bytes + b"\nNewer valid event B.\n"
    target.write_bytes(valid_bytes)
    valid_observation = capture_file_observation(
        target,
        max_content_bytes=1024 * 1024,
    )
    applied_b = service.ingest_external_change(
        "external-valid-after-invalid-crash",
        before.page_path,
        valid_observation,
    )
    with connect_app(service.settings) as conn:
        page_before_retry = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
        jobs_before_retry = conn.execute(
            "SELECT COUNT(*) FROM knowledge_projection_jobs WHERE page_id=?",
            (before.page_id,),
        ).fetchone()[0]

    with pytest.raises(RevisionConflict, match="expected state changed"):
        service.ingest_external_change(
            invalid_event_id,
            before.page_path,
            invalid_observation,
        )

    with connect_app(service.settings) as conn:
        final_invalid_event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (invalid_event_id,),
        ).fetchone()
        page_after_retry = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
        jobs_after_retry = conn.execute(
            "SELECT COUNT(*) FROM knowledge_projection_jobs WHERE page_id=?",
            (before.page_id,),
        ).fetchone()[0]
        invalid_revisions = conn.execute(
            """
            SELECT COUNT(*) FROM wiki_page_revisions
            WHERE page_id=? AND idempotency_key LIKE ?
            """,
            (before.page_id, f"external:{invalid_event_id}:%"),
        ).fetchone()[0]

    assert final_invalid_event["status"] == "superseded"
    assert json.loads(final_invalid_event["expected_state_json"]) == (
        original_expected_state
    )
    assert page_after_retry["lifecycle_status"] == "active"
    assert page_after_retry["current_revision_id"] == applied_b.revision_id
    assert page_after_retry["current_revision_id"] == page_before_retry["current_revision_id"]
    assert page_after_retry["projection_epoch"] == page_before_retry["projection_epoch"]
    assert jobs_after_retry == jobs_before_retry
    assert invalid_revisions == 0


def test_pending_external_event_resumes_matching_prepared_intent(
    page_with_generated,
    monkeypatch,
):
    service, before = page_with_generated
    target = service.settings.vault_path / before.page_path
    external_bytes = before.raw_bytes + b"\nResume prepared external intent.\n"
    target.write_bytes(external_bytes)
    observation = capture_file_observation(
        target,
        max_content_bytes=1024 * 1024,
    )
    event_id = "external-resume-prepared-intent"

    with monkeypatch.context() as patch:
        patch.setattr(IntentExecutor, "execute", lambda self, intent_id: None)
        with pytest.raises(RevisionConflict):
            service.ingest_external_change(event_id, before.page_path, observation)

    with connect_app(service.settings) as conn:
        pending_event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
        intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (page["pending_write_intent_id"],),
        ).fetchone()
        revision = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (intent["revision_id"],),
        ).fetchone()
        jobs_before = conn.execute(
            """
            SELECT COUNT(*) FROM knowledge_projection_jobs
            WHERE page_id=? AND revision_id=?
            """,
            (before.page_id, revision["id"]),
        ).fetchone()[0]
    revision_metadata = json.loads(revision["metadata_json"])

    assert pending_event["status"] == "prepared"
    assert pending_event["result_revision_id"] is None
    assert pending_event["observation_id"] == revision_metadata["observation_id"]
    assert revision_metadata["vault_change_event_id"] == event_id
    assert revision["origin"] == "external"
    assert revision["base_revision_id"] == before.current_revision_id
    assert intent["revision_id"] == revision["id"]
    assert intent["expected_revision_id"] == before.current_revision_id
    assert intent["expected_file_hash"] == observation.file_hash
    assert intent["target_path"] == before.page_path
    assert jobs_before == 0

    resumed = service.ingest_external_change(
        event_id,
        before.page_path,
        observation,
    )

    with connect_app(service.settings) as conn:
        final_event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
        external_revisions = conn.execute(
            """
            SELECT * FROM wiki_page_revisions
            WHERE page_id=? AND origin='external'
            """,
            (before.page_id,),
        ).fetchall()
        external_intents = conn.execute(
            """
            SELECT intent.* FROM vault_write_intents AS intent
            JOIN wiki_page_revisions AS revision ON revision.id=intent.revision_id
            WHERE revision.page_id=? AND revision.origin='external'
            """,
            (before.page_id,),
        ).fetchall()
        jobs = conn.execute(
            """
            SELECT * FROM knowledge_projection_jobs
            WHERE page_id=? AND revision_id=? AND operation='upsert'
            """,
            (before.page_id, resumed.revision_id),
        ).fetchall()

    assert resumed.status == "applied"
    assert resumed.replayed is True
    assert resumed.revision_id == revision["id"]
    assert final_event["status"] == "applied"
    assert len(external_revisions) == 1
    assert len(external_intents) == 1
    assert external_intents[0]["id"] == intent["id"]
    assert len(jobs) == 2


def test_pending_external_event_resumes_after_unrelated_intent_is_cleared(
    page_with_generated,
):
    service, before = page_with_generated
    unrelated = service.prepare_manual_save(
        ManualSaveCommand(
            page_path=before.page_path,
            content=before.content + "\nUnrelated pending manual edit.\n",
            expected_revision_id=before.current_revision_id,
            request_id="unrelated-pending-before-external",
            actor="alice",
            owner=None,
            note=None,
            review_status="reviewed",
        ),
        execute_intent=False,
    )
    target = service.settings.vault_path / before.page_path
    external_bytes = before.raw_bytes + b"\nExternal after unrelated pending.\n"
    target.write_bytes(external_bytes)
    observation = capture_file_observation(
        target,
        max_content_bytes=1024 * 1024,
    )
    event_id = "external-after-unrelated-pending"

    with pytest.raises(RevisionConflict) as exc_info:
        service.ingest_external_change(event_id, before.page_path, observation)

    with connect_app(service.settings) as conn:
        pending_event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
    assert exc_info.value.pending_intent_id == unrelated.write_intent_id
    assert pending_event["status"] == "pending"
    assert json.loads(pending_event["expected_state_json"])["current"] == (
        before.current_revision_id
    )

    with connect_app_write(service.settings) as conn:
        conn.execute(
            """
            UPDATE vault_write_intents SET status='superseded',updated_at=?
            WHERE id=? AND status='pending'
            """,
            ("t-clear", unrelated.write_intent_id),
        )
        cleared = conn.execute(
            """
            UPDATE wiki_pages SET pending_write_intent_id=NULL
            WHERE page_id=? AND pending_write_intent_id=?
            """,
            (before.page_id, unrelated.write_intent_id),
        )
        assert cleared.rowcount == 1

    resumed = service.ingest_external_change(
        event_id,
        before.page_path,
        observation,
    )

    with connect_app(service.settings) as conn:
        events = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchall()
        observations = conn.execute(
            """
            SELECT * FROM wiki_file_observations
            WHERE page_path=? AND file_hash=?
            """,
            (before.page_path, observation.file_hash),
        ).fetchall()
        external_revisions = conn.execute(
            """
            SELECT * FROM wiki_page_revisions
            WHERE page_id=? AND origin='external'
            """,
            (before.page_id,),
        ).fetchall()

    assert resumed.status == "applied"
    assert resumed.replayed is False
    assert len(events) == 1
    assert events[0]["status"] == "applied"
    assert len(observations) == 1
    assert [row["id"] for row in external_revisions] == [resumed.revision_id]


def test_external_event_id_rejects_a_different_observation_payload(
    page_with_generated,
):
    service, before = page_with_generated
    target = service.settings.vault_path / before.page_path
    first_bytes = b"---\ntitle: [first-invalid\n---\n# Broken\n"
    target.write_bytes(first_bytes)
    first_observation = capture_file_observation(
        target,
        max_content_bytes=1024 * 1024,
    )
    event_id = "external-payload-identity"
    first = service.ingest_external_change(
        event_id,
        before.page_path,
        first_observation,
    )

    second_bytes = b"---\ntitle: [different-invalid\n---\n# Broken\n"
    target.write_bytes(second_bytes)
    second_observation = capture_file_observation(
        target,
        max_content_bytes=1024 * 1024,
    )

    with pytest.raises(RevisionConflict, match="payload"):
        service.ingest_external_change(
            event_id,
            before.page_path,
            second_observation,
        )

    with connect_app(service.settings) as conn:
        event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
        observations = conn.execute(
            "SELECT * FROM wiki_file_observations WHERE page_path=?",
            (before.page_path,),
        ).fetchall()

    assert event["observation_id"] == first.observation_id
    assert len(observations) == 1
    assert observations[0]["file_hash"] == first_observation.file_hash


def test_external_event_rechecks_terminal_status_after_occurrence_lookup(
    page_with_generated,
    monkeypatch,
):
    service, before = page_with_generated
    target = service.settings.vault_path / before.page_path
    external_bytes = before.raw_bytes + b"\nTerminal replay race.\n"
    target.write_bytes(external_bytes)
    observation = capture_file_observation(
        target,
        max_content_bytes=1024 * 1024,
    )
    event_id = "external-terminal-reread"
    applied = service.ingest_external_change(
        event_id,
        before.page_path,
        observation,
    )
    original_persist = service._persist_external_occurrence

    def stale_pending_occurrence(*args, **kwargs):
        event, created = original_persist(*args, **kwargs)
        assert event["status"] == "applied"
        assert created is False
        return {**event, "status": "pending"}, False

    monkeypatch.setattr(
        service,
        "_persist_external_occurrence",
        stale_pending_occurrence,
    )

    replay = service.ingest_external_change(
        event_id,
        before.page_path,
        observation,
    )

    assert replay.replayed is True
    assert replay.status == "applied"
    assert replay.revision_id == applied.revision_id


def move_page_and_reuse_old_path(
    service: WikiRevisionService,
    *,
    page_id: str,
    old_path: str,
    new_path: str,
) -> str:
    old_target = service.settings.vault_path / old_path
    new_target = service.settings.vault_path / new_path
    new_target.parent.mkdir(parents=True, exist_ok=True)
    old_target.replace(new_target)
    reused_page_id = f"page_reused_{uuid.uuid4().hex}"
    reused_suffix = uuid.uuid4().hex
    with connect_app_write(service.settings) as conn:
        moved = conn.execute(
            "UPDATE wiki_pages SET path=?,updated_at=? WHERE page_id=? AND path=?",
            (new_path, "t-moved", page_id, old_path),
        )
        assert moved.rowcount == 1
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,page_id,domain,page_type,title,source_ids_json,review_status,
              owner,created_at,updated_at,current_revision_id,generated_revision_id,
              revision_number,file_hash,semantic_hash,lifecycle_status,projection_epoch
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                old_path,
                reused_page_id,
                "product",
                "faq",
                "Reused old path",
                "[]",
                "draft",
                None,
                "t-reused",
                "t-reused",
                f"wrev_reused_current_{reused_suffix}",
                f"wrev_reused_generated_{reused_suffix}",
                1,
                f"file_reused_{reused_suffix}",
                f"semantic_reused_{reused_suffix}",
                "active",
                99,
            ),
        )
    return reused_page_id


def test_applied_external_event_replay_keeps_identity_after_path_reuse(
    page_with_generated,
):
    service, before = page_with_generated
    old_path = before.page_path
    target = service.settings.vault_path / old_path
    external_bytes = before.raw_bytes + b"\nApplied event before rename.\n"
    target.write_bytes(external_bytes)
    observation = capture_file_observation(
        target,
        max_content_bytes=1024 * 1024,
    )
    event_id = "external-applied-before-path-reuse"
    first = service.ingest_external_change(event_id, old_path, observation)
    current = service.get_page(old_path)
    newer = service.prepare_manual_save(
        ManualSaveCommand(
            page_path=old_path,
            content=current.content + "\nNewer current before rename.\n",
            expected_revision_id=current.current_revision_id,
            request_id="manual-before-path-reuse",
            actor="alice",
            owner=None,
            note=None,
            review_status="reviewed",
        )
    )
    new_path = old_path.removesuffix(".md") + "-renamed.md"
    reused_page_id = move_page_and_reuse_old_path(
        service,
        page_id=before.page_id,
        old_path=old_path,
        new_path=new_path,
    )

    replay = service.ingest_external_change(
        event_id,
        old_path,
        observation,
    )

    assert newer.current_revision_id != first.current_revision_id
    assert replay.replayed is True
    assert replay.status == first.status == "applied"
    assert replay.page_id == first.page_id == before.page_id
    assert replay.page_id != reused_page_id
    assert replay.page_path == first.page_path == old_path
    assert replay.revision_id == first.revision_id
    assert replay.current_revision_id == first.current_revision_id
    assert replay.generated_revision_id == first.generated_revision_id
    assert replay.write_intent_id == first.write_intent_id


def test_invalid_external_event_replay_keeps_identity_after_path_reuse(
    page_with_generated,
):
    service, before = page_with_generated
    old_path = before.page_path
    target = service.settings.vault_path / old_path
    invalid_bytes = b"---\ntitle: [invalid-before-rename\n---\n# Broken\n"
    target.write_bytes(invalid_bytes)
    observation = capture_file_observation(
        target,
        max_content_bytes=1024 * 1024,
    )
    event_id = "external-invalid-before-path-reuse"
    first = service.ingest_external_change(event_id, old_path, observation)
    new_path = old_path.removesuffix(".md") + "-invalid-renamed.md"
    reused_page_id = move_page_and_reuse_old_path(
        service,
        page_id=before.page_id,
        old_path=old_path,
        new_path=new_path,
    )

    replay = service.ingest_external_change(
        event_id,
        old_path,
        observation,
    )

    assert replay.replayed is True
    assert replay.status == first.status == "invalid"
    assert replay.page_id == first.page_id == before.page_id
    assert replay.page_id != reused_page_id
    assert replay.page_path == first.page_path == old_path
    assert replay.revision_id == first.revision_id
    assert replay.current_revision_id == first.current_revision_id
    assert replay.generated_revision_id == first.generated_revision_id
    assert replay.write_intent_id == first.write_intent_id is None


def test_pending_external_event_uses_stable_page_identity_after_rename(
    page_with_generated,
    monkeypatch,
):
    service, before = page_with_generated
    old_path = before.page_path
    old_target = service.settings.vault_path / old_path
    external_bytes = before.raw_bytes + b"\nPending event before rename.\n"
    old_target.write_bytes(external_bytes)
    observation = capture_file_observation(
        old_target,
        max_content_bytes=1024 * 1024,
    )
    event_id = "external-pending-before-rename"
    original_canonical_state = service._canonical_state_locked
    calls = 0

    def fail_transition_canonical_state(conn, page):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated transition crash before rename")
        return original_canonical_state(conn, page)

    monkeypatch.setattr(
        service,
        "_canonical_state_locked",
        fail_transition_canonical_state,
    )
    with pytest.raises(RuntimeError, match="transition crash before rename"):
        service.ingest_external_change(event_id, old_path, observation)

    new_path = old_path.removesuffix(".md") + "-pending-renamed.md"
    new_target = service.settings.vault_path / new_path
    new_target.parent.mkdir(parents=True, exist_ok=True)
    old_target.replace(new_target)
    with connect_app_write(service.settings) as conn:
        moved = conn.execute(
            "UPDATE wiki_pages SET path=?,updated_at=? WHERE page_id=? AND path=?",
            (new_path, "t-pending-moved", before.page_id, old_path),
        )
        assert moved.rowcount == 1
        page_before_retry = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
        jobs_before_retry = conn.execute(
            "SELECT COUNT(*) FROM knowledge_projection_jobs WHERE page_id=?",
            (before.page_id,),
        ).fetchone()[0]
        pending_event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()

    assert pending_event["status"] == "pending"
    assert json.loads(pending_event["expected_state_json"])["path"] == old_path

    with pytest.raises(RevisionConflict, match="expected state changed"):
        service.ingest_external_change(event_id, old_path, observation)

    with connect_app(service.settings) as conn:
        final_event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
        page_after_retry = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
        jobs_after_retry = conn.execute(
            "SELECT COUNT(*) FROM knowledge_projection_jobs WHERE page_id=?",
            (before.page_id,),
        ).fetchone()[0]
        external_revisions = conn.execute(
            """
            SELECT COUNT(*) FROM wiki_page_revisions
            WHERE page_id=? AND idempotency_key LIKE ?
            """,
            (before.page_id, f"external:{event_id}:%"),
        ).fetchone()[0]

    assert final_event["status"] == "superseded"
    assert page_after_retry["path"] == new_path
    assert page_after_retry["current_revision_id"] == page_before_retry["current_revision_id"]
    assert page_after_retry["lifecycle_status"] == page_before_retry["lifecycle_status"]
    assert page_after_retry["projection_epoch"] == page_before_retry["projection_epoch"]
    assert jobs_after_retry == jobs_before_retry
    assert external_revisions == 0


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
            "SELECT id,target FROM knowledge_projection_jobs ORDER BY target"
        ).fetchall()

    assert replay.created_pages == 0
    assert replay.updated_pages == 0
    assert replay.conflicted_pages == 0
    assert replay.projection_jobs == 0
    assert replay.projection_job_ids == [completed_jobs[0]["id"]]
    assert completed_source["last_compiled_at"] is not None
    assert completed_page["current_revision_id"] == completed_page["generated_revision_id"]
    assert completed_page["pending_write_intent_id"] is None
    assert [row["target"] for row in completed_jobs] == ["rag"]


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
    merged = (
        current.content.replace(
            "\n---\n",
            "\n_lgdo_transition: {kind: manual, request_id: forged}\n---\n",
            1,
        )
        + merged_suffix
        if merged_suffix
        else None
    )
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

    if resolution != "keep_current":
        applied_payload = applied_audit_payload(
            service.settings,
            page.current_revision_id,
        )
        assert applied_payload["transition_kind"] == "conflict_resolution"
        assert applied_payload["transition_id"] == f"resolve-{resolution}"
        assert applied_payload["request_id"] == f"resolve-{resolution}"

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
        assert b"_lgdo_transition" in page.raw_bytes
        assert json.loads(revision["metadata_json"])["_lgdo_transition"] == {
            "kind": "manual",
            "request_id": "forged",
        }


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


@pytest.fixture
def page_with_two_conflict_types(page_with_generated):
    service, before = page_with_generated
    content_review_id = f"review_{uuid.uuid4().hex}"
    concurrent_review_id = f"review_{uuid.uuid4().hex}"
    with service.coordinator.lock_page(page_id=before.page_id) as locked:
        generated = service._create_revision_locked(
            locked.conn,
            locked.page,
            content=before.raw_bytes + b"\nGenerated conflict candidate.\n",
            origin="generated",
            base_revision_id=before.current_revision_id,
            source_ids=before.metadata["source_ids"],
            actor="test-compiler",
            note=None,
            idempotency_key=f"fixture:generated-conflict:{before.page_id}",
            metadata=before.metadata,
        )
        concurrent = service._create_revision_locked(
            locked.conn,
            locked.page,
            content=before.raw_bytes + b"\nConcurrent conflict candidate.\n",
            origin="manual",
            base_revision_id=before.current_revision_id,
            source_ids=before.metadata["source_ids"],
            actor="alice",
            note=None,
            idempotency_key=f"fixture:concurrent-conflict:{before.page_id}",
            metadata=before.metadata,
        )
        updated = locked.conn.execute(
            """
            UPDATE wiki_pages SET generated_revision_id=?,updated_at=?
            WHERE page_id=? AND generated_revision_id=?
            """,
            (
                generated.id,
                "2026-07-14T00:00:00+00:00",
                before.page_id,
                before.generated_revision_id,
            ),
        )
        assert updated.rowcount == 1
        locked.page["generated_revision_id"] = generated.id
        expected_state = service._canonical_state_locked(locked.conn, locked.page)
        expected_state["pending_conflict"] = sorted(
            [content_review_id, concurrent_review_id]
        )
        expected_state_json = canonical_state_json(expected_state)
        timestamp = "2026-07-14T00:00:00+00:00"
        for review_id, issue_type, candidate_revision_id in (
            (content_review_id, "content_conflict", generated.id),
            (concurrent_review_id, "concurrent_write_conflict", concurrent.id),
        ):
            locked.conn.execute(
                """
                INSERT INTO review_items(
                  id,page_path,page_id,issue_type,status,owner,source_ids_json,
                  created_at,updated_at,base_revision_id,candidate_revision_id,
                  expected_state_json
                ) VALUES (?,?,?,?,'pending',?,?,?,?,?,?,?)
                """,
                (
                    review_id,
                    before.page_path,
                    before.page_id,
                    issue_type,
                    "alice",
                    json.dumps(before.metadata["source_ids"]),
                    timestamp,
                    timestamp,
                    before.current_revision_id,
                    candidate_revision_id,
                    expected_state_json,
                ),
            )
    page = service.get_page(before.page_path)
    assert len(service.list_conflicts(page.page_path, status="pending")) == 2
    return service, page


def test_rename_is_audit_only_revision_and_preserves_content_pointer(
    page_with_generated,
):
    service, before = page_with_generated
    old_absolute = service.settings.vault_path / before.page_path
    new_path = "wiki/product/faq/demo-renamed.md"
    new_absolute = service.settings.vault_path / new_path
    with connect_app(service.settings) as conn:
        revision_count_before = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
            (before.page_id,),
        ).fetchone()[0]
        conn.execute(
            """
            UPDATE wiki_pages
            SET rag_visible_revision_id=?,rag_visible_epoch=?
            WHERE page_id=?
            """,
            (before.current_revision_id, before.projection_epoch, before.page_id),
        )
    old_absolute.rename(new_absolute)

    result = service.rename_page("rename-1", before.page_path, new_path)
    with connect_app(service.settings) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,page_id,domain,page_type,title,source_ids_json,review_status,
              created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                before.page_path,
                "page_reused_old_path",
                "product",
                "faq",
                "Replacement",
                "[]",
                "draft",
                "t1",
                "t1",
            ),
        )
    replay = service.rename_page("rename-1", before.page_path, new_path)
    after = service.get_page(new_path)

    with connect_app(service.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
        audit_revision = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (result.audit_revision_id,),
        ).fetchone()
        revisions_after = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
            (before.page_id,),
        ).fetchone()[0]
        event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id='rename-1'"
        ).fetchone()
        jobs = conn.execute(
            """
            SELECT target,operation,revision_id,payload_json
            FROM knowledge_projection_jobs
            WHERE page_id=? AND projection_epoch=?
            ORDER BY target
            """,
            (before.page_id, after.projection_epoch),
        ).fetchall()

    assert result.status == "renamed"
    assert result.revision_id == result.audit_revision_id
    assert result.audit_revision_id == replay.audit_revision_id
    assert replay.page_id == result.page_id == before.page_id
    assert replay.replayed is True
    assert after.page_id == before.page_id
    assert after.current_revision_id == before.current_revision_id
    assert after.generated_revision_id == before.generated_revision_id
    assert (
        after.accepted_generated_revision_id
        == before.accepted_generated_revision_id
    )
    assert after.raw_bytes == before.raw_bytes == new_absolute.read_bytes()
    assert after.projection_epoch == before.projection_epoch + 1
    assert page["rag_visible_revision_id"] is None
    assert page["rag_visible_epoch"] is None
    assert revisions_after == revision_count_before + 1
    assert audit_revision["origin"] == "rename"
    assert audit_revision["page_path"] == new_path
    assert audit_revision["base_revision_id"] == before.current_revision_id
    assert audit_revision["content"].encode("utf-8") == before.raw_bytes
    assert audit_revision["revision_number"] == page["revision_number"]
    assert event["kind"] == "rename"
    assert event["old_page_path"] == before.page_path
    assert event["page_path"] == new_path
    assert event["status"] == "applied"
    assert event["result_revision_id"] == result.audit_revision_id
    assert {
        (job["target"], job["operation"], job["revision_id"])
        for job in jobs
    } == {
        ("rag", "rename", before.current_revision_id),
        ("gbrain", "rename", before.current_revision_id),
    }
    assert {
        tuple(sorted(json.loads(job["payload_json"]).items())) for job in jobs
    } == {
        tuple(sorted({"old_path": before.page_path, "path": new_path}.items()))
    }


def test_rename_supersedes_pending_invalid_frontmatter_review(
    page_with_generated,
):
    service, before = page_with_generated
    old_target = service.settings.vault_path / before.page_path
    invalid_bytes = b"---\ntitle: [invalid\n---\n# Broken\n"
    old_target.write_bytes(invalid_bytes)
    invalid = service.ingest_external_change(
        "invalid-before-rename",
        before.page_path,
        capture_file_observation(old_target, max_content_bytes=1_000_000),
    )
    invalid_page = service.get_page(before.page_path)
    old_target.write_bytes(invalid_page.raw_bytes)
    new_path = "wiki/product/faq/invalid-review-renamed.md"
    old_target.rename(service.settings.vault_path / new_path)

    renamed = service.rename_page(
        "rename-invalid-review",
        before.page_path,
        new_path,
    )
    after = service.get_page(new_path)

    with connect_app(service.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
        review = conn.execute(
            "SELECT * FROM review_items WHERE id=?",
            (invalid.conflict_review_id,),
        ).fetchone()
        pending = conn.execute(
            """
            SELECT COUNT(*) FROM review_items
            WHERE page_id=? AND status='pending'
            """,
            (before.page_id,),
        ).fetchone()[0]

    assert invalid.status == "invalid"
    assert renamed.status == "renamed"
    assert pending == 0
    assert review["status"] == "superseded"
    assert review["resolved_at"] is not None
    assert review["page_path"] == new_path
    assert page["path"] == new_path
    assert page["projection_epoch"] == invalid_page.projection_epoch + 1
    assert after.projection_epoch == invalid_page.projection_epoch + 1
    assert after.lifecycle_status == "invalid"
    assert after.current_revision_id == before.current_revision_id


def test_delete_and_restore_same_revision_each_create_new_projection_epoch(
    page_with_generated,
):
    service, before = page_with_generated
    target = service.settings.vault_path / before.page_path
    original = target.read_bytes()
    with connect_app(service.settings) as conn:
        revision_count_before = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
            (before.page_id,),
        ).fetchone()[0]
    target.unlink()

    deleted = service.delete_page("delete-1", before.page_path)
    deleted_replay = service.delete_page("delete-1", before.page_path)
    with connect_app(service.settings) as conn:
        deleted_page = dict(
            conn.execute(
                "SELECT * FROM wiki_pages WHERE page_id=?",
                (before.page_id,),
            ).fetchone()
        )
    target.write_bytes(original)
    observation = capture_file_observation(target, max_content_bytes=1_000_000)
    restored = service.ingest_external_change(
        "restore-1",
        before.page_path,
        observation,
    )
    restored_replay = service.ingest_external_change(
        "restore-1",
        before.page_path,
        observation,
    )
    after = service.get_page(before.page_path)

    with connect_app(service.settings) as conn:
        stored = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
        revisions_after = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
            (before.page_id,),
        ).fetchone()[0]
        events = conn.execute(
            """
            SELECT * FROM vault_change_events
            WHERE id IN ('delete-1','restore-1') ORDER BY id
            """
        ).fetchall()
        jobs = conn.execute(
            """
            SELECT target,operation,revision_id,projection_epoch
            FROM knowledge_projection_jobs
            WHERE page_id=? AND projection_epoch IN (?,?)
            ORDER BY projection_epoch,target
            """,
            (
                before.page_id,
                before.projection_epoch + 1,
                before.projection_epoch + 2,
            ),
        ).fetchall()

    assert deleted.status == "deleted"
    assert deleted.revision_id == before.current_revision_id
    assert deleted_replay.replayed is True
    assert deleted_replay.page_id == before.page_id
    assert deleted_page["lifecycle_status"] == "deleted"
    assert deleted_page["deleted_at"] is not None
    assert deleted_page["observed_file_hash"] is None
    assert deleted_page["current_revision_id"] == before.current_revision_id
    assert deleted_page["generated_revision_id"] == before.generated_revision_id
    assert (
        deleted_page["accepted_generated_revision_id"]
        == before.accepted_generated_revision_id
    )
    assert deleted_page["projection_epoch"] == before.projection_epoch + 1
    assert restored.status == "applied"
    assert restored.revision_id != before.current_revision_id
    assert restored.write_intent_id is not None
    assert restored_replay.replayed is True
    assert restored_replay.revision_id == restored.revision_id
    assert after.current_revision_id == restored.revision_id
    assert after.generated_revision_id == before.generated_revision_id
    assert (
        after.accepted_generated_revision_id
        == before.accepted_generated_revision_id
    )
    assert after.raw_bytes != original
    assert parse_wiki_bytes(after.raw_bytes).body == parse_wiki_bytes(original).body
    assert after.lifecycle_status == "active"
    assert after.sync_error is None
    assert after.projection_epoch == before.projection_epoch + 2
    assert stored["deleted_at"] is None
    assert stored["observed_file_hash"] == observation.file_hash
    assert revisions_after == revision_count_before + 1
    assert {event["id"]: event["status"] for event in events} == {
        "delete-1": "applied",
        "restore-1": "applied",
    }
    assert {event["id"]: event["result_revision_id"] for event in events} == {
        "delete-1": before.current_revision_id,
        "restore-1": restored.revision_id,
    }
    assert {
        (
            job["projection_epoch"],
            job["target"],
            job["operation"],
            job["revision_id"],
        )
        for job in jobs
    } == {
        (before.projection_epoch + 1, "rag", "delete", None),
        (before.projection_epoch + 1, "gbrain", "delete", None),
        (
            before.projection_epoch + 2,
            "rag",
            "upsert",
            restored.revision_id,
        ),
        (
            before.projection_epoch + 2,
            "gbrain",
            "upsert",
            restored.revision_id,
        ),
    }


@pytest.mark.parametrize("transition", ["rename", "delete"])
def test_path_or_lifecycle_change_supersedes_stale_reviews(
    page_with_two_conflict_types,
    transition,
):
    service, page = page_with_two_conflict_types
    target = service.settings.vault_path / page.page_path
    if transition == "rename":
        new_path = "wiki/product/faq/two-conflicts-renamed.md"
        target.rename(service.settings.vault_path / new_path)
        service.rename_page("rename-conflicted", page.page_path, new_path)
    else:
        target.unlink()
        service.delete_page("delete-conflicted", page.page_path)

    with connect_app(service.settings) as conn:
        pending = conn.execute(
            """
            SELECT COUNT(*) FROM review_items
            WHERE page_id=? AND status='pending'
            """,
            (page.page_id,),
        ).fetchone()[0]
        superseded = conn.execute(
            """
            SELECT issue_type FROM review_items
            WHERE page_id=? AND status='superseded'
            ORDER BY issue_type
            """,
            (page.page_id,),
        ).fetchall()

    assert pending == 0
    assert [row["issue_type"] for row in superseded] == [
        "concurrent_write_conflict",
        "content_conflict",
    ]


def test_ensure_projection_jobs_recreates_only_missing_desired_epoch(
    page_with_generated,
):
    service, page = page_with_generated
    with connect_app(service.settings) as conn:
        conn.execute(
            "DELETE FROM knowledge_projection_jobs WHERE page_id=?",
            (page.page_id,),
        )
        conn.execute(
            """
            UPDATE wiki_pages
            SET rag_visible_revision_id=?,rag_visible_epoch=?
            WHERE page_id=?
            """,
            (page.current_revision_id, page.projection_epoch, page.page_id),
        )

    first = service.ensure_projection_jobs(page.page_id)
    replay = service.ensure_projection_jobs(page.page_id)
    scanned = service.ensure_projection_jobs()

    with connect_app(service.settings) as conn:
        stored = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (page.page_id,),
        ).fetchone()
        jobs = conn.execute(
            """
            SELECT * FROM knowledge_projection_jobs
            WHERE page_id=? ORDER BY target
            """,
            (page.page_id,),
        ).fetchall()

    assert first == replay
    assert scanned == first
    assert len(first) == 2
    assert {job["id"] for job in jobs} == set(first)
    assert {(job["target"], job["operation"]) for job in jobs} == {
        ("rag", "upsert"),
        ("gbrain", "upsert"),
    }
    assert {job["revision_id"] for job in jobs} == {page.current_revision_id}
    assert {job["projection_epoch"] for job in jobs} == {page.projection_epoch}
    assert {json.loads(job["payload_json"])["path"] for job in jobs} == {
        page.page_path
    }
    assert stored["rag_visible_revision_id"] == page.current_revision_id
    assert stored["rag_visible_epoch"] == page.projection_epoch


@pytest.mark.parametrize("transition", ["rename", "delete"])
def test_lifecycle_event_stale_cas_supersedes_losing_occurrence(
    page_with_generated,
    monkeypatch,
    transition,
):
    service, before = page_with_generated
    old_path = before.page_path
    target = service.settings.vault_path / old_path
    new_path = "wiki/product/faq/stale-cas-renamed.md"
    if transition == "rename":
        target.rename(service.settings.vault_path / new_path)
    else:
        target.unlink()

    original_state = service._lifecycle_event_state_locked
    calls = 0

    def fail_first_transition(conn, page):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated lifecycle transition crash")
        return original_state(conn, page)

    losing_event_id = f"{transition}-losing-occurrence"
    with monkeypatch.context() as patch:
        patch.setattr(
            service,
            "_lifecycle_event_state_locked",
            fail_first_transition,
        )
        with pytest.raises(RuntimeError, match="lifecycle transition crash"):
            if transition == "rename":
                service.rename_page(losing_event_id, old_path, new_path)
            else:
                service.delete_page(losing_event_id, old_path)

    winner_event_id = f"{transition}-winning-occurrence"
    if transition == "rename":
        service.rename_page(winner_event_id, old_path, new_path)
        with pytest.raises(RevisionConflict):
            service.rename_page(losing_event_id, old_path, new_path)
    else:
        service.delete_page(winner_event_id, old_path)
        with pytest.raises(RevisionConflict):
            service.delete_page(losing_event_id, old_path)

    with connect_app(service.settings) as conn:
        events = conn.execute(
            """
            SELECT id,status FROM vault_change_events
            WHERE id IN (?,?) ORDER BY id
            """,
            (losing_event_id, winner_event_id),
        ).fetchall()
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()

    assert {row["id"]: row["status"] for row in events} == {
        losing_event_id: "superseded",
        winner_event_id: "applied",
    }
    if transition == "rename":
        assert page["path"] == new_path
        assert page["lifecycle_status"] == "active"
    else:
        assert page["path"] == old_path
        assert page["lifecycle_status"] == "deleted"
    assert page["current_revision_id"] == before.current_revision_id


def test_pending_rename_loser_supersedes_after_page_moves_again(
    page_with_generated,
    monkeypatch,
):
    service, before = page_with_generated
    old_path = before.page_path
    first_path = "wiki/product/faq/rename-winner.md"
    second_path = "wiki/product/faq/rename-after-winner.md"
    old_target = service.settings.vault_path / old_path
    first_target = service.settings.vault_path / first_path
    second_target = service.settings.vault_path / second_path
    old_target.rename(first_target)

    original_state = service._lifecycle_event_state_locked
    calls = 0

    def fail_loser_transition(conn, page):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated pending rename occurrence")
        return original_state(conn, page)

    loser_event_id = "rename-loser-before-second-move"
    with monkeypatch.context() as patch:
        patch.setattr(
            service,
            "_lifecycle_event_state_locked",
            fail_loser_transition,
        )
        with pytest.raises(RuntimeError, match="pending rename occurrence"):
            service.rename_page(loser_event_id, old_path, first_path)

    with connect_app(service.settings) as conn:
        pending = conn.execute(
            "SELECT status FROM vault_change_events WHERE id=?",
            (loser_event_id,),
        ).fetchone()[0]
    assert pending == "pending"

    service.rename_page("rename-winner-before-second-move", old_path, first_path)
    first_target.rename(second_target)
    service.rename_page("rename-page-second-move", first_path, second_path)

    with pytest.raises(WikiRevisionError) as exc_info:
        service.rename_page(loser_event_id, old_path, first_path)

    with connect_app(service.settings) as conn:
        loser = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (loser_event_id,),
        ).fetchone()
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
    assert loser["status"] == "superseded"
    assert isinstance(exc_info.value, RevisionConflict)
    assert page["path"] == second_path
    assert page["current_revision_id"] == before.current_revision_id
    assert second_target.read_bytes() == before.raw_bytes


def test_pending_delete_loser_supersedes_after_winner_restore(
    page_with_generated,
    monkeypatch,
):
    service, before = page_with_generated
    target = service.settings.vault_path / before.page_path
    target.unlink()

    original_state = service._lifecycle_event_state_locked
    calls = 0

    def fail_loser_transition(conn, page):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated pending delete occurrence")
        return original_state(conn, page)

    loser_event_id = "delete-loser-before-restore"
    with monkeypatch.context() as patch:
        patch.setattr(
            service,
            "_lifecycle_event_state_locked",
            fail_loser_transition,
        )
        with pytest.raises(RuntimeError, match="pending delete occurrence"):
            service.delete_page(loser_event_id, before.page_path)

    with connect_app(service.settings) as conn:
        pending = conn.execute(
            "SELECT status FROM vault_change_events WHERE id=?",
            (loser_event_id,),
        ).fetchone()[0]
    assert pending == "pending"

    service.delete_page("delete-winner-before-restore", before.page_path)
    target.write_bytes(before.raw_bytes)
    service.ingest_external_change(
        "restore-after-delete-winner",
        before.page_path,
        capture_file_observation(target, max_content_bytes=1_000_000),
    )

    with pytest.raises(RevisionConflict):
        service.delete_page(loser_event_id, before.page_path)

    with connect_app(service.settings) as conn:
        loser = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (loser_event_id,),
        ).fetchone()
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
    assert loser["status"] == "superseded"
    assert page["lifecycle_status"] == "active"
    assert page["current_revision_id"] != before.current_revision_id
    assert target.read_bytes() != before.raw_bytes


@pytest.mark.parametrize("lifecycle", ["deleted", "invalid"])
def test_ensure_projection_jobs_rebuilds_desired_delete_without_old_epoch(
    page_with_generated,
    lifecycle,
):
    service, before = page_with_generated
    target = service.settings.vault_path / before.page_path
    if lifecycle == "deleted":
        target.unlink()
        service.delete_page("delete-before-ensure", before.page_path)
    else:
        target.write_bytes(b"---\ntitle: [invalid\n---\n# Broken\n")
        service.ingest_external_change(
            "invalid-before-ensure",
            before.page_path,
            capture_file_observation(target, max_content_bytes=1_000_000),
        )

    with connect_app(service.settings) as conn:
        page_before = dict(
            conn.execute(
                "SELECT * FROM wiki_pages WHERE page_id=?",
                (before.page_id,),
            ).fetchone()
        )
        old_jobs_before = conn.execute(
            """
            SELECT id,status FROM knowledge_projection_jobs
            WHERE page_id=? AND projection_epoch<? ORDER BY id
            """,
            (before.page_id, page_before["projection_epoch"]),
        ).fetchall()
        conn.execute(
            """
            DELETE FROM knowledge_projection_jobs
            WHERE page_id=? AND projection_epoch=?
            """,
            (before.page_id, page_before["projection_epoch"]),
        )

    first = service.ensure_projection_jobs(before.page_id)
    replay = service.ensure_projection_jobs(before.page_id)

    with connect_app(service.settings) as conn:
        page_after = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (before.page_id,),
        ).fetchone()
        current_jobs = conn.execute(
            """
            SELECT * FROM knowledge_projection_jobs
            WHERE page_id=? AND projection_epoch=? ORDER BY target
            """,
            (before.page_id, page_after["projection_epoch"]),
        ).fetchall()
        old_jobs_after = conn.execute(
            """
            SELECT id,status FROM knowledge_projection_jobs
            WHERE page_id=? AND projection_epoch<? ORDER BY id
            """,
            (before.page_id, page_after["projection_epoch"]),
        ).fetchall()

    assert first == replay
    assert len(first) == 2
    assert {job["id"] for job in current_jobs} == set(first)
    assert {(job["target"], job["operation"]) for job in current_jobs} == {
        ("rag", "delete"),
        ("gbrain", "delete"),
    }
    assert {job["revision_id"] for job in current_jobs} == {None}
    assert [tuple(row) for row in old_jobs_after] == [
        tuple(row) for row in old_jobs_before
    ]
    assert all(
        row["status"] not in {"pending", "failed", "running"}
        for row in old_jobs_after
    )
    assert page_after["projection_epoch"] == page_before["projection_epoch"]
    assert (
        page_after["rag_visible_revision_id"],
        page_after["rag_visible_epoch"],
    ) == (
        page_before["rag_visible_revision_id"],
        page_before["rag_visible_epoch"],
    )


def test_deleted_identical_bytes_restore_creates_new_external_revision(
    legacy_page_fixture,
):
    service, before = legacy_page_fixture
    (service.settings.vault_path / before.page_path).unlink()
    service.delete_page("delete-before-restore", before.page_path)
    target = service.settings.vault_path / before.page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(before.raw_bytes)
    restored = service.ingest_external_change(
        "restore-identical",
        before.page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert restored.status == "applied"
    assert restored.revision_id != before.current_revision_id
    with connect_app(service.settings) as conn:
        revision = conn.execute(
            "SELECT origin FROM wiki_page_revisions WHERE id=?",
            (restored.revision_id,),
        ).fetchone()
    assert revision["origin"] == "external"


def test_invalid_identical_historical_bytes_restore_creates_new_external_revision(
    legacy_page_fixture,
):
    service, before = legacy_page_fixture
    target = service.settings.vault_path / before.page_path
    target.write_bytes(b"---\ntitle: [broken\n---\n# Broken\n")
    invalid = service.ingest_external_change(
        "invalid-before-identical-restore",
        before.page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert invalid.status == "invalid"
    target.write_bytes(before.raw_bytes)
    restored = service.ingest_external_change(
        "invalid-identical-restore",
        before.page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert restored.status == "applied"
    assert restored.revision_id != before.current_revision_id


def test_relocate_with_edit_is_one_external_revision_and_atomic(
    legacy_page_fixture,
):
    service, before = legacy_page_fixture
    old_path = before.page_path
    new_path = "wiki/product/faq/relocated.md"
    old_target = service.settings.vault_path / old_path
    new_target = service.settings.vault_path / new_path
    new_target.parent.mkdir(parents=True, exist_ok=True)
    edited = before.raw_bytes.replace(b"# Demo", b"# Relocated")
    old_target.rename(new_target)
    new_target.write_bytes(edited)
    result = service.relocate_external_change(
        "relocate-edited",
        old_path,
        new_path,
        before.page_id,
        capture_file_observation(
            new_target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert result.status == "applied"
    assert result.revision_id != before.current_revision_id
    with connect_app(service.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?", (before.page_id,)
        ).fetchone()
        revision = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?", (result.revision_id,)
        ).fetchone()
    assert page["path"] == new_path
    assert revision["origin"] == "external"
    assert revision["page_path"] == new_path


def test_exact_rename_stays_audit_only(legacy_page_fixture):
    service, before = legacy_page_fixture
    new_path = "wiki/product/faq/exact-renamed.md"
    old_target = service.settings.vault_path / before.page_path
    new_target = service.settings.vault_path / new_path
    new_target.parent.mkdir(parents=True, exist_ok=True)
    old_target.rename(new_target)
    result = service.rename_page("rename-exact", before.page_path, new_path)
    assert result.status == "renamed"
    assert result.current_revision_id == before.current_revision_id
    assert result.audit_revision_id != before.current_revision_id


def test_finalize_external_revision_syncs_all_page_fields_and_clears_owner(
    legacy_page_fixture,
):
    service, before = legacy_page_fixture
    seed_source(service.settings, "src_cross", domain="support", status="active")
    target = service.settings.vault_path / before.page_path
    target.write_bytes(
        external_page_bytes(
            title="Changed title",
            source_ids=("src_cross",),
            domain="operations",
            body="Changed",
        ).replace(b"page_type: feature", b"page_type: policy")
    )
    applied = service.ingest_external_change(
        "finalize-six-fields",
        before.page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert applied.status == "applied"
    with connect_app(service.settings) as conn:
        page = dict(
            conn.execute(
                "SELECT * FROM wiki_pages WHERE page_id=?", (before.page_id,)
            ).fetchone()
        )
    assert (
        page["title"],
        page["domain"],
        page["page_type"],
        json.loads(page["source_ids_json"]),
        page["review_status"],
        page["owner"],
    ) == ("Changed title", "operations", "policy", ["src_cross"], "draft", None)


class StopAfterRepairInstall(RuntimeError):
    pass


def create_real_invalid_issue(service, event_id: str, page_path: str):
    target = service.settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"---\ntitle: [broken\n---\n# Broken\n")
    invalid = service.ingest_external_change(
        event_id,
        page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert invalid.status == "invalid"
    assert invalid.sync_issue_id is not None
    with connect_app(service.settings) as conn:
        issue = dict(
            conn.execute(
                "SELECT * FROM vault_sync_issues WHERE id=?",
                (invalid.sync_issue_id,),
            ).fetchone()
        )
    assert issue["status"] == "open"
    assert issue["generation"] == 1
    return target, issue


def prepare_real_repair_without_finalize(
    monkeypatch, service, event_id: str, page_path: str, target
):
    installed: dict[str, str] = {}

    def install_only(executor, intent_id):
        installed["intent_id"] = intent_id
        installed["owner"] = executor.owner
        assert executor.claim(intent_id, lease_seconds=30)
        executor.capture_and_install(intent_id, stop_after="installed")
        raise StopAfterRepairInstall("repair installed before finalize")

    observation = capture_file_observation(
        target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(IntentExecutor, "execute", install_only)
        with pytest.raises(
            StopAfterRepairInstall,
            match="repair installed before finalize",
        ):
            service.ingest_external_change(event_id, page_path, observation)
    return installed


@pytest.mark.parametrize("bump_after_prepare", [False, True])
def test_successful_repair_resolves_only_captured_issue_generation(
    tmp_path, monkeypatch, bump_after_prepare
):
    settings = make_settings(tmp_path)
    init_app_db(settings)
    service = WikiRevisionService(settings)
    page_path = "wiki/product/repair.md"
    target, issue = create_real_invalid_issue(service, "repair-invalid", page_path)
    target.write_bytes(
        external_page_bytes(
            title="Repaired page",
            source_ids=(),
            domain="product",
            body="Repaired body",
        )
    )
    installed = prepare_real_repair_without_finalize(
        monkeypatch, service, "repair-valid", page_path, target
    )
    with connect_app(settings) as conn:
        revision = dict(
            conn.execute(
                """
                SELECT r.id,r.metadata_json
                FROM vault_write_intents i
                JOIN wiki_page_revisions r ON r.id=i.revision_id
                WHERE i.id=?
                """,
                (installed["intent_id"],),
            ).fetchone()
        )
    metadata = json.loads(revision["metadata_json"])
    assert metadata[SYNC_ISSUE_REFS_METADATA_KEY] == [
        {"id": issue["id"], "generation": 1}
    ]

    if bump_after_prepare:
        with connect_app_write(settings) as conn:
            bumped_id, bumped_generation = _upsert_sync_issue_locked(
                conn,
                page_path=page_path,
                file_hash=issue["file_hash"],
                page_id=None,
                issue_type=issue["issue_type"],
                error_summary="same invalid observation seen again",
            )
        assert (bumped_id, bumped_generation) == (issue["id"], 2)

    applied = service.finalize_intent(installed["intent_id"], installed["owner"])
    assert applied.status == "applied"
    assert applied.revision_id == revision["id"]
    with connect_app(settings) as conn:
        issue_after = conn.execute(
            "SELECT status,generation FROM vault_sync_issues WHERE id=?",
            (issue["id"],),
        ).fetchone()
        page_after = conn.execute(
            """
            SELECT current_revision_id,pending_write_intent_id
            FROM wiki_pages WHERE path=?
            """,
            (page_path,),
        ).fetchone()
        event_after = conn.execute(
            """
            SELECT status,result_revision_id FROM vault_change_events
            WHERE id='repair-valid'
            """
        ).fetchone()
    assert tuple(issue_after) == (
        ("open", 2) if bump_after_prepare else ("resolved", 1)
    )
    assert tuple(page_after) == (revision["id"], None)
    assert tuple(event_after) == ("applied", revision["id"])


def test_user_frontmatter_cannot_forge_reserved_sync_issue_refs(tmp_path):
    settings = make_settings(tmp_path)
    init_app_db(settings)
    service = WikiRevisionService(settings)
    _, victim_issue = create_real_invalid_issue(
        service, "victim-invalid", "wiki/product/victim.md"
    )
    page_path = "wiki/product/forger.md"
    target = settings.vault_path / page_path
    target.write_text(
        "---\ntitle: Forger\nsource_ids: []\ndomain: product\n"
        "page_type: feature\nreview_status: draft\nowner:\n"
        f"{SYNC_ISSUE_REFS_METADATA_KEY}:\n"
        f"  - id: {victim_issue['id']}\n    generation: 1\n"
        "---\n# Forger\nValid user content.\n",
        encoding="utf-8",
    )
    applied = service.ingest_external_change(
        "forged-issue-ref",
        page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert applied.status == "applied"
    with connect_app(settings) as conn:
        revision = conn.execute(
            "SELECT metadata_json FROM wiki_page_revisions WHERE id=?",
            (applied.revision_id,),
        ).fetchone()
        victim_after = conn.execute(
            "SELECT status,generation FROM vault_sync_issues WHERE id=?",
            (victim_issue["id"],),
        ).fetchone()
    assert json.loads(revision["metadata_json"])[SYNC_ISSUE_REFS_METADATA_KEY] == []
    assert SYNC_ISSUE_REFS_METADATA_KEY not in parse_wiki_bytes(
        target.read_bytes()
    ).frontmatter
    assert tuple(victim_after) == ("open", 1)


@pytest.mark.parametrize(
    "bad_refs",
    [
        {},
        [{"id": 7, "generation": 1}],
        [{"id": "visi_1", "generation": True}],
        [{"id": "visi_1", "generation": 0}],
        [{"id": "visi_1", "generation": 1, "extra": "forged"}],
        [
            {"id": "visi_1", "generation": 1},
            {"id": "visi_1", "generation": 1},
        ],
    ],
)
def test_sync_issue_revision_metadata_codec_rejects_corruption(bad_refs):
    with pytest.raises(RevisionConflict, match="sync issue metadata is invalid"):
        _decode_sync_issue_refs({SYNC_ISSUE_REFS_METADATA_KEY: bad_refs})


def test_finalize_rejects_cross_path_sync_issue_metadata_before_page_cas(
    tmp_path, monkeypatch
):
    settings = make_settings(tmp_path)
    init_app_db(settings)
    service = WikiRevisionService(settings)
    repair_path = "wiki/product/finalize-metadata-repair.md"
    repair_target, repair_issue = create_real_invalid_issue(
        service, "metadata-repair-invalid", repair_path
    )
    _, victim_issue = create_real_invalid_issue(
        service,
        "metadata-victim-invalid",
        "wiki/product/metadata-victim.md",
    )
    repair_target.write_bytes(
        external_page_bytes(
            title="Metadata repair",
            source_ids=(),
            domain="product",
            body="Valid repaired content",
        )
    )
    installed = prepare_real_repair_without_finalize(
        monkeypatch,
        service,
        "metadata-repair-valid",
        repair_path,
        repair_target,
    )
    with connect_app(settings) as conn:
        revision = dict(
            conn.execute(
                """
                SELECT r.id,r.metadata_json,i.page_id
                FROM vault_write_intents i
                JOIN wiki_page_revisions r ON r.id=i.revision_id
                WHERE i.id=?
                """,
                (installed["intent_id"],),
            ).fetchone()
        )
        metadata = json.loads(revision["metadata_json"])
        metadata[SYNC_ISSUE_REFS_METADATA_KEY] = [
            {"id": victim_issue["id"], "generation": 1}
        ]
        conn.execute(
            "UPDATE wiki_page_revisions SET metadata_json=? WHERE id=?",
            (
                json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                revision["id"],
            ),
        )
        page_before = dict(
            conn.execute(
                """
                SELECT current_revision_id,projection_epoch,rag_visible_revision_id,
                       rag_visible_epoch,pending_write_intent_id
                FROM wiki_pages WHERE page_id=?
                """,
                (revision["page_id"],),
            ).fetchone()
        )
        event_before = dict(
            conn.execute(
                """
                SELECT status,result_revision_id,result_payload_json
                FROM vault_change_events WHERE id='metadata-repair-valid'
                """
            ).fetchone()
        )
        jobs_before = conn.execute(
            "SELECT COUNT(*) FROM knowledge_projection_jobs WHERE page_id=?",
            (revision["page_id"],),
        ).fetchone()[0]
        issues_before = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id,status,generation,resolved_at FROM vault_sync_issues
                WHERE id IN (?,?) ORDER BY id
                """,
                (repair_issue["id"], victim_issue["id"]),
            ).fetchall()
        ]

    with pytest.raises(RevisionConflict, match="sync issue metadata is invalid"):
        service.finalize_intent(installed["intent_id"], installed["owner"])

    with connect_app(settings) as conn:
        page_after = dict(
            conn.execute(
                """
                SELECT current_revision_id,projection_epoch,rag_visible_revision_id,
                       rag_visible_epoch,pending_write_intent_id
                FROM wiki_pages WHERE page_id=?
                """,
                (revision["page_id"],),
            ).fetchone()
        )
        event_after = dict(
            conn.execute(
                """
                SELECT status,result_revision_id,result_payload_json
                FROM vault_change_events WHERE id='metadata-repair-valid'
                """
            ).fetchone()
        )
        jobs_after = conn.execute(
            "SELECT COUNT(*) FROM knowledge_projection_jobs WHERE page_id=?",
            (revision["page_id"],),
        ).fetchone()[0]
        issues_after = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id,status,generation,resolved_at FROM vault_sync_issues
                WHERE id IN (?,?) ORDER BY id
                """,
                (repair_issue["id"], victim_issue["id"]),
            ).fetchall()
        ]
    assert page_after == page_before
    assert event_after == event_before
    assert jobs_after == jobs_before
    assert issues_after == issues_before


def test_bound_legacy_page_missing_metadata_uses_locked_values_and_upgrades_frontmatter(
    tmp_path,
):
    settings = make_settings(tmp_path)
    page_path = seed_legacy_page(
        settings,
        b"---\ntitle: Demo\nreview_status: reviewed\n---\n# Demo\n",
    )
    with connect_app(settings) as conn:
        conn.execute(
            "UPDATE wiki_pages SET owner=NULL WHERE path=?",
            (page_path,),
        )
    service = WikiRevisionService(settings)
    before = service.get_page(page_path)
    target = settings.vault_path / page_path
    target.write_bytes(before.raw_bytes + b"\nExternal body edit.\n")
    result = service.ingest_external_change(
        "bound-fallback-upgrade",
        page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert result.status == "applied"
    document = parse_wiki_bytes(target.read_bytes())
    with connect_app(settings) as conn:
        page = dict(
            conn.execute(
                "SELECT * FROM wiki_pages WHERE page_id=?", (before.page_id,)
            ).fetchone()
        )
    expected = (
        "Demo",
        "product",
        "faq",
        ["src_1"],
        "reviewed",
        None,
    )
    assert (
        document.frontmatter["title"],
        document.frontmatter["domain"],
        document.frontmatter["page_type"],
        document.frontmatter["source_ids"],
        document.frontmatter["review_status"],
        document.frontmatter["owner"],
    ) == expected
    assert (
        page["title"],
        page["domain"],
        page["page_type"],
        json.loads(page["source_ids_json"]),
        page["review_status"],
        page["owner"],
    ) == expected


def test_bound_page_explicit_unknown_source_is_invalid_instead_of_falling_back(
    legacy_page_fixture,
):
    service, before = legacy_page_fixture
    target = service.settings.vault_path / before.page_path
    target.write_bytes(
        before.raw_bytes.replace(b"source_ids: [src_1]", b"source_ids: [src_unknown]")
        + b"\nUnknown source edit.\n"
    )
    result = service.ingest_external_change(
        "bound-explicit-unknown-source",
        before.page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert result.status == "invalid"


class InjectedRevisionCrash(RuntimeError):
    pass


def test_new_page_precommit_crash_rolls_back_every_domain_row(
    tmp_path, monkeypatch
):
    settings = make_settings(tmp_path)
    init_app_db(settings)
    page_path = "wiki/product/precommit-crash.md"
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(external_page_bytes(source_ids=()))
    observation = capture_file_observation(
        target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
    )
    service = WikiRevisionService(settings)

    def crash(point: str):
        if point == "new_page_before_prepare_commit":
            raise InjectedRevisionCrash(point)

    monkeypatch.setattr(service, "_fault", crash)
    with pytest.raises(
        InjectedRevisionCrash,
        match="new_page_before_prepare_commit",
    ):
        service.ingest_external_change("new-page-crash", page_path, observation)

    with connect_app(settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM vault_write_intents"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM vault_change_events"
        ).fetchone()[0] == 0

    monkeypatch.setattr(service, "_fault", lambda point: None)
    applied = service.ingest_external_change(
        "new-page-crash", page_path, observation
    )
    assert applied.status == "applied"


def test_relocate_postcommit_crash_leaves_one_recoverable_intent(
    legacy_page_fixture, monkeypatch
):
    service, before = legacy_page_fixture
    old_path = before.page_path
    new_path = "wiki/product/relocate-crash.md"
    old_target = service.settings.vault_path / old_path
    new_target = service.settings.vault_path / new_path
    new_target.parent.mkdir(parents=True, exist_ok=True)
    old_target.rename(new_target)
    new_target.write_bytes(before.raw_bytes + b"\nRelocate after commit.\n")
    observation = capture_file_observation(
        new_target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
    )

    def crash(point: str):
        if point == "relocate_after_prepare_commit_before_execute":
            raise InjectedRevisionCrash(point)

    monkeypatch.setattr(service, "_fault", crash)
    with pytest.raises(
        InjectedRevisionCrash,
        match="relocate_after_prepare_commit_before_execute",
    ):
        service.relocate_external_change(
            "relocate-postcommit-crash",
            old_path,
            new_path,
            before.page_id,
            observation,
        )

    with connect_app(service.settings) as conn:
        page = dict(
            conn.execute(
                "SELECT * FROM wiki_pages WHERE page_id=?", (before.page_id,)
            ).fetchone()
        )
        intent = dict(
            conn.execute(
                "SELECT * FROM vault_write_intents WHERE id=?",
                (page["pending_write_intent_id"],),
            ).fetchone()
        )
        event = dict(
            conn.execute(
                """
                SELECT * FROM vault_change_events
                WHERE id='relocate-postcommit-crash'
                """
            ).fetchone()
        )
    assert page["path"] == old_path
    assert intent["status"] in {"pending", "claimed", "installed"}
    assert event["status"] == "prepared"

    monkeypatch.setattr(service, "_fault", lambda point: None)
    IntentExecutor(service.settings).reconcile_all()
    replay = service.relocate_external_change(
        "relocate-postcommit-crash",
        old_path,
        new_path,
        before.page_id,
        observation,
    )
    assert replay.status == "applied"
    assert replay.replayed is True
    assert service.get_page(new_path).current_revision_id == replay.revision_id
