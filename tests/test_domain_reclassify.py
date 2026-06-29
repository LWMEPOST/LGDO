import json

from app.config import get_settings
from app.db import connect_app, init_app_db, json_dump, row_to_dict
from app.domain_reclassify import reclassify_administration_documents
from app.models import AskRequest, CompileRequest, EntityAliasRequest, ScanRequest


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
    wiki.write_text("---\ndomain: customer_service\n---\n# 行政制度", encoding="utf-8")

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
                json_dump(["src_admin"]),
                "draft",
                "ops",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )

    result = reclassify_administration_documents(settings)
    assert result["updated_sources"] == 1

    with connect_app(settings) as conn:
        source = row_to_dict(conn.execute("SELECT * FROM sources WHERE id = ?", ("src_admin",)).fetchone())
        chunk = row_to_dict(conn.execute("SELECT * FROM document_chunks WHERE id = ?", ("chunk_admin",)).fetchone())
        page = row_to_dict(conn.execute("SELECT * FROM wiki_pages WHERE title = ?", ("06_POLICY_行政管理制度",)).fetchone())

    assert source["domain"] == "administration"
    assert source["raw_path"] == "raw/administration/src_admin.md"
    assert source["metadata"]["normalized_path"] == "normalized/administration/src_admin.md"
    assert chunk["domain"] == "administration"
    assert chunk["metadata"]["domain"] == "administration"
    assert page["domain"] == "administration"
    assert page["path"] == "wiki/administration/policies/admin.md"
    normalized_text = (settings.vault_path / "normalized" / "administration" / "src_admin.md").read_text(encoding="utf-8")
    assert 'domain: "administration"' in normalized_text or "domain: administration" in normalized_text
    assert '"domain":"administration"' in (settings.vault_path / "jsonl" / "administration" / "src_admin.jsonl").read_text(encoding="utf-8")
