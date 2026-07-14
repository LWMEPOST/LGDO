import json

import pytest

from app.config import get_settings
from app.db import connect_app, init_app_db
from app.wiki_markdown import capture_file_observation
from app.wiki_revisions import (
    CompileCandidateCommand,
    ManualSaveCommand,
    ResolveConflictCommand,
    StatusUpdateCommand,
    WikiRevisionService,
)


@pytest.fixture
def traceable_generated_page(tmp_path):
    settings = get_settings().model_copy()
    settings.database_backend = "sqlite"
    settings.rag_store_backend = "sqlite"
    settings.database_path = tmp_path / "traceability.db"
    settings.vault_path = tmp_path / "vault"
    settings.upload_path = tmp_path / "uploads"
    settings.gbrain_enabled = False
    settings.gbrain_import_on_compile = False
    page_path = "wiki/product/faq/traceability.md"
    (settings.vault_path / page_path).parent.mkdir(parents=True)
    init_app_db(settings)

    service = WikiRevisionService(settings)
    generated = service.apply_generated_candidate(
        CompileCandidateCommand(
            page_path=page_path,
            content=(
                "---\n"
                "title: Traceability\n"
                "source_ids: [src_trace]\n"
                "review_status: draft\n"
                "---\n"
                "# Traceability\n\nGenerated baseline.\n"
            ),
            domain="product",
            page_type="faq",
            title="Traceability",
            source_ids=["src_trace"],
            owner=None,
            source_hash="trace-generated-v1",
            compiler_version="wiki-revision-v1",
            compile_job_id="trace-compile-initial",
        )
    )
    assert generated.status == "applied"
    return service, generated, service.get_page(page_path)


def test_revision_lifecycle_is_auditable_reachable_and_immutable(
    traceable_generated_page,
):
    service, initial_generated, generated_page = traceable_generated_page
    page_id = generated_page.page_id
    immutable_fields = ("content", "file_hash", "origin", "base_revision_id")
    snapshots: dict[str, tuple] = {}

    def remember_revision(revision_id: str | None) -> str:
        assert revision_id is not None
        with connect_app(service.settings) as conn:
            row = conn.execute(
                "SELECT * FROM wiki_page_revisions WHERE id=? AND page_id=?",
                (revision_id, page_id),
            ).fetchone()
        assert row is not None
        fingerprint = tuple(row[field] for field in immutable_fields)
        if revision_id in snapshots:
            assert snapshots[revision_id] == fingerprint
        else:
            snapshots[revision_id] = fingerprint
        return revision_id

    remember_revision(initial_generated.revision_id)

    manual = service.prepare_manual_save(
        ManualSaveCommand(
            page_path=generated_page.page_path,
            content=generated_page.content + "\nManual traceability edit.\n",
            expected_revision_id=generated_page.current_revision_id,
            request_id="trace-manual",
            actor="alice",
            owner=None,
            note="manual trace",
            review_status="reviewed",
        )
    )
    remember_revision(manual.revision_id)
    manual_page = service.get_page(generated_page.page_path)

    status = service.update_status(
        StatusUpdateCommand(
            page_path=manual_page.page_path,
            review_status="stale",
            expected_revision_id=manual_page.current_revision_id,
            request_id="trace-status",
            actor="alice",
            note="status trace",
        )
    )
    remember_revision(status.revision_id)
    status_page = service.get_page(manual_page.page_path)

    old_target = service.settings.vault_path / status_page.page_path
    old_target.write_bytes(status_page.raw_bytes + b"\nExternal traceability edit.\n")
    external = service.ingest_external_change(
        "trace-external",
        status_page.page_path,
        capture_file_observation(old_target, max_content_bytes=1_000_000),
    )
    remember_revision(external.revision_id)
    external_page = service.get_page(status_page.page_path)

    renamed_path = "wiki/product/faq/traceability-renamed.md"
    renamed_target = service.settings.vault_path / renamed_path
    old_target.rename(renamed_target)
    renamed = service.rename_page(
        "trace-rename",
        external_page.page_path,
        renamed_path,
    )
    remember_revision(renamed.revision_id)
    renamed_page = service.get_page(renamed_path)

    restored_bytes = renamed_page.raw_bytes
    renamed_target.unlink()
    deleted = service.delete_page("trace-delete", renamed_path)
    remember_revision(deleted.revision_id)

    renamed_target.write_bytes(restored_bytes)
    restored = service.ingest_external_change(
        "trace-restore",
        renamed_path,
        capture_file_observation(renamed_target, max_content_bytes=1_000_000),
    )
    remember_revision(restored.revision_id)
    restored_page = service.get_page(renamed_path)

    generated = service.apply_generated_candidate(
        CompileCandidateCommand(
            page_path=renamed_path,
            content=generated_page.content + "\nGenerated traceability candidate.\n",
            domain="product",
            page_type="faq",
            title=restored_page.metadata["title"],
            source_ids=restored_page.metadata["source_ids"],
            owner=restored_page.metadata.get("owner"),
            source_hash="trace-generated-v2",
            compiler_version="wiki-revision-v1",
            compile_job_id="trace-compile-conflict",
        )
    )
    remember_revision(generated.revision_id)
    assert generated.status == "conflicted"
    pending = service.list_conflicts(renamed_path, status="pending")
    assert len(pending) == 1
    conflict = pending[0]
    assert conflict["issue_type"] == "content_conflict"

    resolved = service.resolve_conflict(
        ResolveConflictCommand(
            review_id=conflict["id"],
            resolution="merged_content",
            merged_content=restored_page.content + "\nMerged traceability result.\n",
            expected_current_revision_id=conflict["base_revision_id"],
            expected_generated_revision_id=conflict["candidate_revision_id"],
            request_id="trace-resolve",
            actor="alice",
            note="resolution trace",
        )
    )
    remember_revision(resolved.revision_id)

    mutation_results = (
        initial_generated,
        manual,
        status,
        external,
        renamed,
        deleted,
        restored,
        generated,
        resolved,
    )
    returned_revision_ids = {
        remember_revision(result.revision_id) for result in mutation_results
    }
    with connect_app(service.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (page_id,),
        ).fetchone()
        review = conn.execute(
            "SELECT * FROM review_items WHERE id=?",
            (conflict["id"],),
        ).fetchone()
        revision_rows = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE page_id=?",
            (page_id,),
        ).fetchall()
        audit_rows = conn.execute(
            "SELECT event_type,payload_json FROM audit_logs ORDER BY id"
        ).fetchall()

    revisions_by_id = {row["id"]: row for row in revision_rows}
    assert returned_revision_ids <= revisions_by_id.keys()
    assert snapshots.keys() <= revisions_by_id.keys()
    for revision_id, fingerprint in snapshots.items():
        stored = revisions_by_id[revision_id]
        assert stored["page_id"] == page_id
        assert tuple(stored[field] for field in immutable_fields) == fingerprint

    pointer_ids = {
        page["current_revision_id"],
        page["generated_revision_id"],
        page["accepted_generated_revision_id"],
        review["base_revision_id"],
        review["candidate_revision_id"],
        review["resolution_revision_id"],
    }
    assert None not in pointer_ids
    assert pointer_ids <= revisions_by_id.keys()
    assert review["status"] == "resolved"

    audit_payloads = [
        (row["event_type"], json.loads(row["payload_json"]))
        for row in audit_rows
    ]
    applied_transition_ids = {
        payload.get("transition_id")
        for event_type, payload in audit_payloads
        if event_type == "wiki_revision_applied"
    }
    assert {
        "trace-compile-initial",
        "trace-manual",
        "trace-status",
        "trace-external",
        "trace-resolve",
    } <= applied_transition_ids
    assert any(
        event_type == "wiki_page_renamed"
        and payload.get("event_id") == "trace-rename"
        for event_type, payload in audit_payloads
    )
    assert any(
        event_type == "wiki_page_deleted"
        and payload.get("event_id") == "trace-delete"
        for event_type, payload in audit_payloads
    )
    assert any(
        event_type == "wiki_external_change_reused_current"
        and payload.get("event_id") == "trace-restore"
        for event_type, payload in audit_payloads
    )
    assert any(
        event_type == "wiki_conflict_resolved"
        and payload.get("review_id") == conflict["id"]
        and payload.get("request_id") == "trace-resolve"
        for event_type, payload in audit_payloads
    )
