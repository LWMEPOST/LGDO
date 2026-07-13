import hashlib
import json

import pytest

import app.domain_reclassify as domain_reclassify_module
from app.config import get_settings
from app.db import connect_app, init_app_db, json_dump, row_to_dict
from app.domain_reclassify import reclassify_administration_documents
from app.models import AskRequest, CompileRequest, EntityAliasRequest, ScanRequest
from app.wiki_revisions import (
    ManualSaveCommand,
    MetadataUpdateCommand,
    RevisionConflict,
    WikiRevisionService,
)


def test_administration_domain_is_accepted_by_request_models():
    assert ScanRequest(root_path=".", domain="administration").domain == "administration"
    assert CompileRequest(domain="administration").domain == "administration"
    assert AskRequest(question="报销流程是什么？", domain="administration").domain == "administration"
    assert EntityAliasRequest(canonical_name="费用报销制度", alias="报销规则", domain="administration").domain == "administration"


def test_reclassify_administration_documents_updates_metadata_and_vault_paths(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "test.db")
    monkeypatch.setattr(settings, "vault_path", tmp_path / "vault")

    init_app_db(settings)
    raw = settings.vault_path / "raw" / "customer_service" / "src_admin.md"
    normalized = settings.vault_path / "normalized" / "customer_service" / "src_admin.md"
    jsonl = settings.vault_path / "jsonl" / "customer_service" / "src_admin.jsonl"
    wiki = settings.vault_path / "wiki" / "customer_service" / "policies" / "admin.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    normalized.parent.mkdir(parents=True, exist_ok=True)
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    wiki.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text('---\ndomain: customer_service\nacl_tags:\n- customer_service\n---\n行政制度', encoding="utf-8")
    normalized.write_text('---\ndomain: "customer_service"\nacl_tags:\n- customer_service\n---\n行政制度', encoding="utf-8")
    jsonl.write_text(
        json.dumps({"id": "chunk_admin", "metadata": {"domain": "customer_service", "acl_tags": ["internal", "customer_service"]}}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    wiki.write_text(
        "---\ndomain: customer_service\n"
        "source_ids: [src_admin, src_admin_2]\n---\n# 行政制度",
        encoding="utf-8",
    )

    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO sources(
              id, domain, owner, title, source_type, original_path, raw_path,
              content_hash, size_bytes, status, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "src_admin",
                "customer_service",
                "ops",
                "06_POLICY_行政管理制度",
                "md",
                "admin.md",
                "raw/customer_service/src_admin.md",
                "hash",
                10,
                "active",
                json_dump({
                    "domain": "customer_service",
                    "acl_tags": ["internal", "customer_service"],
                    "normalized_path": "normalized/customer_service/src_admin.md",
                    "jsonl_path": "jsonl/customer_service/src_admin.jsonl",
                    "cleaned": {"domain": "customer_service"},
                }),
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO sources(
              id, domain, owner, title, source_type, original_path, raw_path,
              content_hash, size_bytes, status, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "src_admin_2",
                "customer_service",
                "ops",
                "采购管理制度",
                "md",
                "admin_2.md",
                "raw/customer_service/src_admin_2.md",
                "hash_2",
                10,
                "active",
                "{}",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO document_chunks(
              id, source_id, domain, title, chunk_index, text, token_json,
              metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "chunk_admin",
                "src_admin",
                "customer_service",
                "06_POLICY_行政管理制度",
                0,
                "行政制度",
                "[]",
                json_dump({"domain": "customer_service", "acl_tags": ["internal", "customer_service"]}),
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path, domain, page_type, title, source_ids_json, review_status,
              owner, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "wiki/customer_service/policies/admin.md",
                "customer_service",
                "policy",
                "06_POLICY_行政管理制度",
                json_dump(["src_admin", "src_admin_2"]),
                "draft",
                "ops",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )

    revision_service = WikiRevisionService(settings)
    before = revision_service.get_page("wiki/customer_service/policies/admin.md")
    with connect_app(settings) as conn:
        conn.execute(
            """
            UPDATE wiki_pages
            SET generated_revision_id=?,accepted_generated_revision_id=?
            WHERE page_id=?
            """,
            (before.current_revision_id, before.current_revision_id, before.page_id),
        )
    before = revision_service.get_page(before.page_path)

    result = reclassify_administration_documents(settings)
    assert result["updated_sources"] == 2
    assert result["updated_pages"] == 1

    with connect_app(settings) as conn:
        source = row_to_dict(conn.execute("SELECT * FROM sources WHERE id = ?", ("src_admin",)).fetchone())
        chunk = row_to_dict(conn.execute("SELECT * FROM document_chunks WHERE id = ?", ("chunk_admin",)).fetchone())
        page = row_to_dict(conn.execute("SELECT * FROM wiki_pages WHERE title = ?", ("06_POLICY_行政管理制度",)).fetchone())
        manual_revisions = conn.execute(
            """
            SELECT * FROM wiki_page_revisions
            WHERE page_id=? AND origin='manual'
            """,
            (page["page_id"],),
        ).fetchall()
        projection_jobs = conn.execute(
            """
            SELECT * FROM knowledge_projection_jobs
            WHERE page_id=? AND projection_epoch=?
            """,
            (page["page_id"], page["projection_epoch"]),
        ).fetchall()

    assert source["domain"] == "administration"
    assert source["raw_path"] == "raw/administration/src_admin.md"
    assert source["metadata"]["normalized_path"] == "normalized/administration/src_admin.md"
    assert chunk["domain"] == "administration"
    assert chunk["metadata"]["domain"] == "administration"
    assert page["domain"] == "administration"
    assert page["path"] == "wiki/administration/policies/admin.md"
    assert page["current_revision_id"] != before.current_revision_id
    assert page["generated_revision_id"] == before.generated_revision_id
    assert page["accepted_generated_revision_id"] == before.accepted_generated_revision_id
    assert len(manual_revisions) == 1
    assert manual_revisions[0]["id"] == page["current_revision_id"]
    source_command_id = hashlib.sha256(
        json.dumps(
            ["src_admin", "src_admin_2"],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert manual_revisions[0]["idempotency_key"].startswith(
        f"metadata:domain-reclassify:{page['page_id']}:"
        f"administration:{source_command_id}:"
    )
    assert len(projection_jobs) == 2
    assert {job["revision_id"] for job in projection_jobs} == {page["current_revision_id"]}
    normalized_text = (settings.vault_path / "normalized" / "administration" / "src_admin.md").read_text(encoding="utf-8")
    assert 'domain: "administration"' in normalized_text or "domain: administration" in normalized_text
    assert '"domain":"administration"' in (settings.vault_path / "jsonl" / "administration" / "src_admin.jsonl").read_text(encoding="utf-8")


def _seed_guarded_reclassify(settings, suffix: str):
    source_id = f"src_{suffix}"
    page_path = f"wiki/customer_service/policies/{suffix}.md"
    relative_paths = {
        "raw": f"raw/customer_service/{suffix}.md",
        "normalized": f"normalized/customer_service/{suffix}.md",
        "jsonl": f"jsonl/customer_service/{suffix}.jsonl",
        "wiki": page_path,
    }
    source_paths = {
        name: settings.vault_path / relative
        for name, relative in relative_paths.items()
    }
    for path in source_paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    source_paths["raw"].write_text(
        "---\ndomain: customer_service\n---\n行政制度\n",
        encoding="utf-8",
    )
    source_paths["normalized"].write_text(
        "---\ndomain: customer_service\n---\n行政制度\n",
        encoding="utf-8",
    )
    source_paths["jsonl"].write_text(
        json.dumps(
            {"id": f"chunk_{suffix}", "metadata": {"domain": "customer_service"}},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    source_paths["wiki"].write_text(
        f"---\ndomain: customer_service\nsource_ids: [{source_id}]\n"
        "review_status: draft\n---\n# 行政制度\n",
        encoding="utf-8",
    )
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO sources(
              id,domain,owner,title,source_type,original_path,raw_path,
              content_hash,size_bytes,status,metadata_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source_id,
                "customer_service",
                "ops",
                "行政管理制度",
                "md",
                f"{suffix}.md",
                relative_paths["raw"],
                f"hash_{suffix}",
                10,
                "active",
                json_dump(
                    {
                        "domain": "customer_service",
                        "normalized_path": relative_paths["normalized"],
                        "jsonl_path": relative_paths["jsonl"],
                    }
                ),
                "t0",
                "t0",
            ),
        )
        conn.execute(
            """
            INSERT INTO document_chunks(
              id,source_id,domain,title,chunk_index,text,token_json,
              metadata_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"chunk_{suffix}",
                source_id,
                "customer_service",
                "行政管理制度",
                0,
                "行政制度",
                "[]",
                json_dump({"domain": "customer_service"}),
                "t0",
                "t0",
            ),
        )
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,domain,page_type,title,source_ids_json,review_status,
              owner,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                page_path,
                "customer_service",
                "policy",
                "行政管理制度",
                json_dump([source_id]),
                "draft",
                "ops",
                "t0",
                "t0",
            ),
        )
    destination_paths = {
        name: settings.vault_path
        / relative.replace("/customer_service/", "/administration/")
        for name, relative in relative_paths.items()
    }
    return {
        "source_id": source_id,
        "chunk_id": f"chunk_{suffix}",
        "page_path": page_path,
        "source_paths": source_paths,
        "destination_paths": destination_paths,
    }


def _insert_guarded_review(settings, page):
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO review_items(
              id,page_path,page_id,issue_type,status,source_ids_json,
              created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                f"review_{page.page_id}",
                page.page_path,
                page.page_id,
                "metadata_review",
                "pending",
                "[]",
                "t0",
                "t0",
            ),
        )


def _guarded_state(settings, case, page_id):
    with connect_app(settings) as conn:
        return {
            "source": row_to_dict(
                conn.execute(
                    "SELECT * FROM sources WHERE id=?",
                    (case["source_id"],),
                ).fetchone()
            ),
            "chunk": row_to_dict(
                conn.execute(
                    "SELECT * FROM document_chunks WHERE id=?",
                    (case["chunk_id"],),
                ).fetchone()
            ),
            "page": row_to_dict(
                conn.execute(
                    "SELECT * FROM wiki_pages WHERE page_id=?",
                    (page_id,),
                ).fetchone()
            ),
            "review": row_to_dict(
                conn.execute(
                    "SELECT * FROM review_items WHERE page_id=?",
                    (page_id,),
                ).fetchone()
            ),
        }


def _assert_no_reclassify_side_effects(case, before, after):
    assert after["source"] == before["source"]
    assert after["chunk"] == before["chunk"]
    assert after["page"]["path"] == before["page"]["path"]
    assert after["page"]["domain"] == before["page"]["domain"]
    assert after["review"]["page_path"] == before["review"]["page_path"]
    assert all(not path.exists() for path in case["destination_paths"].values())


def test_reclassify_pending_intent_has_zero_side_effects(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "test.db")
    monkeypatch.setattr(settings, "vault_path", tmp_path / "vault")
    init_app_db(settings)
    case = _seed_guarded_reclassify(settings, "pending")

    service = WikiRevisionService(settings)
    page = service.get_page(case["page_path"])
    prepared = service.prepare_manual_save(
        ManualSaveCommand(
            page_path=page.page_path,
            content=page.content + "\npending edit",
            expected_revision_id=page.current_revision_id,
            request_id="pending_save",
            actor="alice",
            owner=None,
            note=None,
            review_status="draft",
        ),
        execute_intent=False,
    )
    _insert_guarded_review(settings, page)
    before = _guarded_state(settings, case, page.page_id)
    source_bytes = {
        name: path.read_bytes() for name, path in case["source_paths"].items()
    }

    with pytest.raises(RevisionConflict):
        reclassify_administration_documents(settings)

    after = _guarded_state(settings, case, page.page_id)
    _assert_no_reclassify_side_effects(case, before, after)
    assert after["page"]["pending_write_intent_id"] == prepared.write_intent_id
    assert {
        name: path.read_bytes() for name, path in case["source_paths"].items()
    } == source_bytes


def test_reclassify_rejects_foreign_pending_metadata_transition(
    tmp_path,
    monkeypatch,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "test.db")
    monkeypatch.setattr(settings, "vault_path", tmp_path / "vault")
    init_app_db(settings)
    case = _seed_guarded_reclassify(settings, "foreign_pending")
    service = WikiRevisionService(settings)
    page = service.get_page(case["page_path"])
    _insert_guarded_review(settings, page)
    source_command_id = hashlib.sha256(
        json.dumps(
            [case["source_id"]],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    request_id = (
        f"domain-reclassify:{page.page_id}:administration:{source_command_id}"
    )
    target_path = case["page_path"].replace(
        "/customer_service/",
        "/administration/",
    )
    foreign = service.update_metadata(
        MetadataUpdateCommand(
            page_path=page.page_path,
            changes={"domain": "customer_service"},
            expected_revision_id=page.current_revision_id,
            request_id=request_id,
            actor="other-actor",
        ),
        execute_intent=False,
        target_page_path=target_path,
        page_domain="administration",
    )
    before = _guarded_state(settings, case, page.page_id)

    with pytest.raises(RevisionConflict):
        reclassify_administration_documents(settings)

    after = _guarded_state(settings, case, page.page_id)
    _assert_no_reclassify_side_effects(case, before, after)
    assert after["page"]["pending_write_intent_id"] == foreign.write_intent_id
    with connect_app(settings) as conn:
        revision = row_to_dict(
            conn.execute(
                "SELECT * FROM wiki_page_revisions WHERE id=?",
                (foreign.revision_id,),
            ).fetchone()
        )
        reclassifier_revisions = conn.execute(
            """
            SELECT COUNT(*) FROM wiki_page_revisions
            WHERE page_id=? AND actor='domain-reclassifier'
            """,
            (page.page_id,),
        ).fetchone()[0]
    assert revision["actor"] == "other-actor"
    assert revision["metadata"]["domain"] == "customer_service"
    assert reclassifier_revisions == 0


def test_reclassify_stale_expected_revision_has_zero_side_effects(
    tmp_path,
    monkeypatch,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "test.db")
    monkeypatch.setattr(settings, "vault_path", tmp_path / "vault")
    init_app_db(settings)
    case = _seed_guarded_reclassify(settings, "stale")
    service = WikiRevisionService(settings)
    page = service.get_page(case["page_path"])
    _insert_guarded_review(settings, page)
    before = _guarded_state(settings, case, page.page_id)
    original_get_page = WikiRevisionService.get_page
    advanced = False

    def get_then_advance(self, page_path):
        nonlocal advanced
        current = original_get_page(self, page_path)
        if page_path == case["page_path"] and not advanced:
            advanced = True
            self.prepare_manual_save(
                ManualSaveCommand(
                    page_path=page_path,
                    content=current.content + "\nconcurrent edit",
                    expected_revision_id=current.current_revision_id,
                    request_id="concurrent_save",
                    actor="concurrent-writer",
                    owner=None,
                    note=None,
                    review_status="draft",
                )
            )
        return current

    monkeypatch.setattr(WikiRevisionService, "get_page", get_then_advance)

    with pytest.raises(RevisionConflict):
        reclassify_administration_documents(settings)

    after = _guarded_state(settings, case, page.page_id)
    _assert_no_reclassify_side_effects(case, before, after)
    assert after["page"]["current_revision_id"] != before["page"][
        "current_revision_id"
    ]
    assert after["page"]["pending_write_intent_id"] is None
    with connect_app(settings) as conn:
        reclassifier_revisions = conn.execute(
            """
            SELECT COUNT(*) FROM wiki_page_revisions
            WHERE page_id=? AND actor='domain-reclassifier'
            """,
            (page.page_id,),
        ).fetchone()[0]
    assert reclassifier_revisions == 0


@pytest.mark.parametrize("failure_phase", ["copy", "execute"])
def test_reclassify_retry_recovers_interrupted_prepared_intent(
    tmp_path,
    monkeypatch,
    failure_phase,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "test.db")
    monkeypatch.setattr(settings, "vault_path", tmp_path / "vault")
    init_app_db(settings)
    case = _seed_guarded_reclassify(settings, f"crash_{failure_phase}")
    service = WikiRevisionService(settings)
    before = service.get_page(case["page_path"])

    if failure_phase == "copy":
        original = domain_reclassify_module._copy_to_domain
        interrupted = False

        def fail_once(*args, **kwargs):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise RuntimeError("copy interrupted")
            return original(*args, **kwargs)

        monkeypatch.setattr(domain_reclassify_module, "_copy_to_domain", fail_once)
    else:
        original = WikiRevisionService.update_metadata
        interrupted = False

        def fail_once(self, command, **kwargs):
            nonlocal interrupted
            if kwargs.get("execute_intent", True) and not interrupted:
                interrupted = True
                raise RuntimeError("intent execution interrupted")
            return original(self, command, **kwargs)

        monkeypatch.setattr(WikiRevisionService, "update_metadata", fail_once)

    with pytest.raises(RuntimeError):
        reclassify_administration_documents(settings)

    with connect_app(settings) as conn:
        interrupted_page = row_to_dict(
            conn.execute(
                "SELECT * FROM wiki_pages WHERE page_id=?",
                (before.page_id,),
            ).fetchone()
        )
        interrupted_source = row_to_dict(
            conn.execute(
                "SELECT * FROM sources WHERE id=?",
                (case["source_id"],),
            ).fetchone()
        )
    if failure_phase == "copy":
        assert interrupted_page["path"] == case["page_path"]
        assert interrupted_page["pending_write_intent_id"] is None
        assert interrupted_source["domain"] == "customer_service"
        monkeypatch.setattr(domain_reclassify_module, "_copy_to_domain", original)
    else:
        assert interrupted_page["pending_write_intent_id"] is not None
        monkeypatch.setattr(WikiRevisionService, "update_metadata", original)

    result = reclassify_administration_documents(settings)
    assert result["updated_pages"] == 1
    with connect_app(settings) as conn:
        page = row_to_dict(
            conn.execute(
                "SELECT * FROM wiki_pages WHERE page_id=?",
                (before.page_id,),
            ).fetchone()
        )
        revisions = conn.execute(
            """
            SELECT * FROM wiki_page_revisions
            WHERE page_id=? AND actor='domain-reclassifier'
            """,
            (before.page_id,),
        ).fetchall()
        jobs = conn.execute(
            """
            SELECT * FROM knowledge_projection_jobs
            WHERE page_id=? AND revision_id=?
            """,
            (before.page_id, page["current_revision_id"]),
        ).fetchall()
    assert page["path"].startswith("wiki/administration/")
    assert page["pending_write_intent_id"] is None
    assert len(revisions) == 1
    assert len(jobs) == 2


def test_reclassify_retry_skips_applied_page_and_resumes_pending_page(
    tmp_path,
    monkeypatch,
):
    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "test.db")
    monkeypatch.setattr(settings, "vault_path", tmp_path / "vault")
    init_app_db(settings)
    cases = [
        _seed_guarded_reclassify(settings, "partial_a"),
        _seed_guarded_reclassify(settings, "partial_b"),
    ]
    service = WikiRevisionService(settings)
    page_ids = [service.get_page(case["page_path"]).page_id for case in cases]
    original = WikiRevisionService.update_metadata
    executions = 0

    def apply_one_then_fail(self, command, **kwargs):
        nonlocal executions
        if kwargs.get("execute_intent", True):
            executions += 1
            if executions == 2:
                raise RuntimeError("second intent execution interrupted")
        return original(self, command, **kwargs)

    monkeypatch.setattr(
        WikiRevisionService,
        "update_metadata",
        apply_one_then_fail,
    )
    with pytest.raises(RuntimeError):
        reclassify_administration_documents(settings)

    with connect_app(settings) as conn:
        interrupted_pages = [
            row_to_dict(
                conn.execute(
                    "SELECT * FROM wiki_pages WHERE page_id=?",
                    (page_id,),
                ).fetchone()
            )
            for page_id in page_ids
        ]
        interrupted_revision_counts = [
            conn.execute(
                """
                SELECT COUNT(*) FROM wiki_page_revisions
                WHERE page_id=? AND actor='domain-reclassifier'
                """,
                (page_id,),
            ).fetchone()[0]
            for page_id in page_ids
        ]
    assert sorted(
        page["pending_write_intent_id"] is not None
        for page in interrupted_pages
    ) == [False, True]
    assert interrupted_revision_counts == [1, 1]

    monkeypatch.setattr(WikiRevisionService, "update_metadata", original)
    result = reclassify_administration_documents(settings)

    assert result["updated_pages"] == 2
    with connect_app(settings) as conn:
        for page_id in page_ids:
            page = row_to_dict(
                conn.execute(
                    "SELECT * FROM wiki_pages WHERE page_id=?",
                    (page_id,),
                ).fetchone()
            )
            revision_count = conn.execute(
                """
                SELECT COUNT(*) FROM wiki_page_revisions
                WHERE page_id=? AND actor='domain-reclassifier'
                """,
                (page_id,),
            ).fetchone()[0]
            job_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM knowledge_projection_jobs AS job
                JOIN wiki_page_revisions AS revision
                  ON revision.id=job.revision_id
                WHERE revision.page_id=?
                  AND revision.actor='domain-reclassifier'
                """,
                (page_id,),
            ).fetchone()[0]
            assert page["path"].startswith("wiki/administration/")
            assert page["domain"] == "administration"
            assert page["pending_write_intent_id"] is None
            assert revision_count == 1
            assert job_count == 2
            assert service.get_page(page["path"]).metadata["domain"] == (
                "administration"
            )
