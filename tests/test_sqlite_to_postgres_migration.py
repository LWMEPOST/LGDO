import sqlite3
import socket

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.aliases import DEFAULT_ENTITY_ALIASES
from app.db import connect_postgres
from app.main import app
from app.migration import migrate_sqlite_to_postgres


def pg_available() -> bool:
    try:
        with socket.create_connection(("localhost", 5432), timeout=1):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not pg_available(), reason="PostgreSQL 5432 is not available")
def test_migrate_sqlite_metadata_to_postgres_preserves_core_queries(tmp_path, monkeypatch):
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "refund_policy.md").write_text(
        "# 退款政策\n\n用户 7 天内可以申请退款，客服需要核验订单号。",
        encoding="utf-8",
    )
    (sample_dir / "permission_export.md").write_text(
        "# 权限导出\n\n管理员可以在权限中心导出成员权限清单，导出前需要二次确认。",
        encoding="utf-8",
    )

    settings = get_settings()
    sqlite_path = tmp_path / "data" / "source.db"
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", sqlite_path)
    monkeypatch.setattr(settings, "vault_path", tmp_path / "vault")
    monkeypatch.setattr(settings, "upload_path", tmp_path / "uploads")
    monkeypatch.setattr(settings, "deepseek_api_key", None)
    monkeypatch.setattr(settings, "deepseek_model", None)

    client = TestClient(app)
    scan = client.post(
        "/api/internal/sources/scan",
        json={
            "root_path": str(sample_dir),
            "domain": "product",
            "owner": "tester",
            "acl_tags": ["internal", "product"],
            "metadata_defaults": {"source_system": "migration_test"},
        },
    )
    assert scan.status_code == 200
    compile_result = client.post("/api/internal/wiki/compile", json={"domain": "product"})
    assert compile_result.status_code == 200

    with sqlite3.connect(sqlite_path) as conn:
        page_path, source_ids_json = conn.execute(
            "SELECT path, source_ids_json FROM wiki_pages ORDER BY path DESC LIMIT 1"
        ).fetchone()
        page_id = "page_migration_fixture"
        revision_id = "wrev_migration_fixture"
        content = (settings.vault_path / page_path).read_text(encoding="utf-8")
        conn.execute(
            """
            UPDATE wiki_pages
            SET page_id=?, current_revision_id=?, revision_number=1,
                file_hash='fixture_file_hash', semantic_hash='fixture_semantic_hash'
            WHERE path=?
            """,
            (page_id, revision_id, page_path),
        )
        conn.execute(
            """
            INSERT INTO wiki_page_revisions(
              id,page_id,page_path,revision_number,file_hash,semantic_hash,content,origin,
              base_revision_id,source_ids_json,actor,note,metadata_json,idempotency_key,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                revision_id, page_id, page_path, 1, "fixture_file_hash", "fixture_semantic_hash",
                content, "legacy", None, source_ids_json, "migration-test", None, "{}",
                "migration:test:revision", "t0",
            ),
        )
        source_revision_count = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions"
        ).fetchone()[0]
        source_revision_ids = {
            row[0] for row in conn.execute("SELECT id FROM wiki_page_revisions").fetchall()
        }
        source_page = conn.execute(
            """
            SELECT page_id, path, current_revision_id, revision_number,
                   projection_epoch, lifecycle_status
            FROM wiki_pages
            ORDER BY path
            LIMIT 1
            """
        ).fetchone()
        source_page_id, source_page_path = source_page[:2]
        source_page_state = source_page[2:]

    answer_before = client.post(
        "/api/internal/ask",
        json={"question": "管理员怎么导出成员权限清单？", "domain": "product"},
    )
    assert answer_before.status_code == 200
    expected_source_id = answer_before.json()["citations"][0]["source_id"]
    alias_response = client.post(
        "/api/internal/aliases",
        json={
            "domain": "product",
            "canonical_name": "权限清单",
            "alias": "成员权限导出",
            "entity_type": "feature",
            "metadata": {"terms": ["管理员"]},
        },
    )
    assert alias_response.status_code == 200

    monkeypatch.setattr(settings, "postgres_database", "lgdo_migration_test")
    from app.db import init_postgres_schema

    init_postgres_schema(settings)
    with connect_postgres(settings) as conn:
        with conn.cursor() as cur:
            for table in [
                "audit_logs",
                "auth_sessions",
                "accounts",
                "feedback",
                "knowledge_gaps",
                "query_logs",
                "knowledge_projection_jobs",
                "vault_write_intents",
                "vault_change_events",
                "wiki_file_observations",
                "wiki_page_revisions",
                "review_items",
                "wiki_pages",
                "ingest_reports",
                "document_chunks",
                "sources",
                "eval_questions",
                "entity_aliases",
            ]:
                cur.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")

    result = migrate_sqlite_to_postgres(settings, sqlite_path)
    assert result["tables"]["sources"] == 2
    assert result["tables"]["wiki_pages"] >= 2
    assert result["tables"]["document_chunks"] >= 2
    assert result["tables"]["entity_aliases"] == len(DEFAULT_ENTITY_ALIASES) + 1
    assert result["total_rows"] >= 6

    with connect_postgres(settings) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM wiki_page_revisions")
        target_revision_count = cur.fetchone()[0]
        cur.execute("SELECT id FROM wiki_page_revisions")
        target_revision_ids = {row[0] for row in cur.fetchall()}
        cur.execute(
            """
            SELECT current_revision_id, revision_number, projection_epoch, lifecycle_status
            FROM wiki_pages
            WHERE page_id=%s AND path=%s
            """,
            (source_page_id, source_page_path),
        )
        target_page_state = cur.fetchone()
    assert (
        result["tables"]["wiki_page_revisions"]
        == target_revision_count
        == source_revision_count
    )
    assert target_revision_ids == source_revision_ids
    assert target_page_state == source_page_state
    assert target_page_state[0] in target_revision_ids

    monkeypatch.setattr(settings, "database_backend", "postgres")
    migrated_sources = client.get("/api/internal/sources?domain=product")
    assert migrated_sources.status_code == 200
    assert {source["title"] for source in migrated_sources.json()} == {"refund_policy", "permission_export"}

    status = client.get("/api/internal/rag/status")
    assert status.status_code == 200
    assert status.json()["database_backend"] == "postgres"
    assert status.json()["chunk_count"] >= 2

    answer_after = client.post(
        "/api/internal/ask",
        json={"question": "管理员怎么导出成员权限清单？", "domain": "product"},
    )
    assert answer_after.status_code == 200
    assert answer_after.json()["citations"][0]["source_id"] == expected_source_id


@pytest.mark.skipif(not pg_available(), reason="PostgreSQL 5432 is not available")
def test_migration_endpoint_returns_table_counts(tmp_path, monkeypatch):
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "refund_policy.md").write_text("# 退款政策\n\n用户 7 天内可以申请退款。", encoding="utf-8")

    settings = get_settings()
    sqlite_path = tmp_path / "data" / "source.db"
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", sqlite_path)
    monkeypatch.setattr(settings, "postgres_database", "lgdo_migration_endpoint_test")
    monkeypatch.setattr(settings, "postgres_password", "postgres")
    monkeypatch.setattr(settings, "vault_path", tmp_path / "vault")
    monkeypatch.setattr(settings, "upload_path", tmp_path / "uploads")
    monkeypatch.setattr(settings, "deepseek_api_key", None)
    monkeypatch.setattr(settings, "deepseek_model", None)

    from app.db import init_postgres_schema

    init_postgres_schema(settings)
    with connect_postgres(settings) as conn:
        with conn.cursor() as cur:
            for table in [
                "audit_logs",
                "auth_sessions",
                "accounts",
                "feedback",
                "knowledge_gaps",
                "query_logs",
                "review_items",
                "wiki_pages",
                "ingest_reports",
                "document_chunks",
                "sources",
                "eval_questions",
                "entity_aliases",
            ]:
                cur.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")

    client = TestClient(app)
    scan = client.post(
        "/api/internal/sources/scan",
        json={
            "root_path": str(sample_dir),
            "domain": "product",
            "owner": "tester",
            "acl_tags": ["internal"],
        },
    )
    assert scan.status_code == 200

    response = client.post(f"/api/internal/database/migrate-sqlite-to-postgres?sqlite_path={sqlite_path}")
    assert response.status_code == 200
    body = response.json()
    assert body["tables"]["sources"] == 1
    assert body["tables"]["document_chunks"] >= 1
    assert body["total_rows"] >= 3
