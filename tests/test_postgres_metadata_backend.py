import socket

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import connect_postgres
from app.main import app


def pg_available() -> bool:
    try:
        with socket.create_connection(("localhost", 5432), timeout=1):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not pg_available(), reason="PostgreSQL 5432 is not available")
def test_postgres_metadata_backend_runs_internal_flow(tmp_path, monkeypatch):
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "refund_policy.md").write_text(
        "# 退款政策\n\n用户在 7 天内可以申请退款，客服需要核验订单号。",
        encoding="utf-8",
    )
    (sample_dir / "permission_export.md").write_text(
        "# 权限导出\n\n管理员可以在权限中心导出成员权限清单，导出前需要二次确认。",
        encoding="utf-8",
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "postgres")
    monkeypatch.setattr(settings, "postgres_host", "localhost")
    monkeypatch.setattr(settings, "postgres_port", 5432)
    monkeypatch.setattr(settings, "postgres_user", "postgres")
    monkeypatch.setattr(settings, "postgres_password", "postgres")
    monkeypatch.setattr(settings, "postgres_database", "lgdo_meta_test")
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "fallback.db")
    monkeypatch.setattr(settings, "vault_path", tmp_path / "vault")
    monkeypatch.setattr(settings, "upload_path", tmp_path / "uploads")
    monkeypatch.setattr(settings, "deepseek_api_key", None)
    monkeypatch.setattr(settings, "deepseek_model", None)

    from app.db import init_app_db

    init_app_db(settings)
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
            "acl_tags": ["internal", "product"],
            "metadata_defaults": {"source_system": "pg_metadata_test"},
        },
    )
    assert scan.status_code == 200
    assert scan.json()["new_files"] == 2

    status = client.get("/api/internal/rag/status")
    assert status.status_code == 200
    assert status.json()["database_backend"] == "postgres"
    assert status.json()["chunk_count"] >= 2
    assert status.json()["source_count"] == 2

    compile_result = client.post("/api/internal/wiki/compile", json={"domain": "product"})
    assert compile_result.status_code == 200
    assert compile_result.json()["created_pages"] >= 2

    sources = client.get("/api/internal/sources?domain=product")
    assert sources.status_code == 200
    permission_source = next(source for source in sources.json() if source["title"] == "permission_export")

    answer = client.post(
        "/api/internal/ask",
        json={"question": "管理员怎么导出成员权限清单？", "domain": "product"},
    )
    assert answer.status_code == 200
    body = answer.json()
    assert body["citations"]
    assert body["citations"][0]["source_id"] == permission_source["id"]

    delete_result = client.delete(f"/api/internal/sources/{permission_source['id']}?note=pg-metadata-test")
    assert delete_result.status_code == 200
    assert delete_result.json()["status"] == "deleted"

    with connect_postgres(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM document_chunks WHERE source_id = %s", (permission_source["id"],))
            assert cur.fetchone()[0] == 0
