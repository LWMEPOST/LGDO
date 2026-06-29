import socket

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.pg_rag import connect_postgres


def pg_available() -> bool:
    try:
        with socket.create_connection(("localhost", 54322), timeout=1):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not pg_available(), reason="PostgreSQL 54322 is not available")
def test_postgres_rag_store_indexes_and_retrieves_chunks(tmp_path, monkeypatch):
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "service_sla.md").write_text(
        "# 服务 SLA\n\n企业客户 P1 故障需要在 30 分钟内响应，并同步值班负责人。",
        encoding="utf-8",
    )
    (sample_dir / "refund_policy.md").write_text(
        "# 退款政策\n\n用户 7 天内可以申请退款，客服需要记录订单号。",
        encoding="utf-8",
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "test.db")
    monkeypatch.setattr(settings, "vault_path", tmp_path / "vault")
    monkeypatch.setattr(settings, "upload_path", tmp_path / "uploads")
    monkeypatch.setattr(settings, "rag_store_backend", "postgres")
    monkeypatch.setattr(settings, "postgres_host", "localhost")
    monkeypatch.setattr(settings, "postgres_port", 54322)
    monkeypatch.setattr(settings, "postgres_user", "postgres")
    monkeypatch.setattr(settings, "postgres_password", "postgres")
    monkeypatch.setattr(settings, "postgres_database", "lgdo_test")
    monkeypatch.setattr(settings, "deepseek_api_key", None)
    monkeypatch.setattr(settings, "deepseek_model", None)

    from app.pg_rag import init_pg_rag

    init_pg_rag(settings)
    with connect_postgres(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM rag_document_chunks")

    client = TestClient(app)
    scan = client.post(
        "/api/internal/sources/scan",
        json={
            "root_path": str(sample_dir),
            "domain": "product",
            "owner": "tester",
            "acl_tags": ["internal", "product"],
            "metadata_defaults": {"source_system": "pg_rag_test"},
        },
    )
    assert scan.status_code == 200

    with connect_postgres(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM rag_document_chunks WHERE domain = %s", ("product",))
            assert cur.fetchone()[0] >= 2
            cur.execute("SELECT COUNT(*) FROM rag_document_chunks WHERE domain = %s AND embedding IS NOT NULL", ("product",))
            assert cur.fetchone()[0] >= 2

    status = client.get("/api/internal/rag/status")
    assert status.status_code == 200
    assert status.json()["rag_store_backend"] == "postgres"
    assert status.json()["chunk_count"] >= 2
    assert status.json()["embedding_count"] >= 2
    assert status.json()["embedding_model"] == "local-hash-v1"
    assert status.json()["vector_backend"] in {"pgvector", "jsonb"}
    assert isinstance(status.json()["pgvector_enabled"], bool)
    if status.json()["pgvector_enabled"]:
        assert status.json()["vector_count"] >= 2

    compile_result = client.post("/api/internal/wiki/compile", json={"domain": "product"})
    assert compile_result.status_code == 200

    answer = client.post(
        "/api/internal/ask",
        json={"question": "P1 故障多久内响应？", "domain": "product"},
    )
    assert answer.status_code == 200
    body = answer.json()
    assert body["citations"]
    service_source = next(source for source in client.get("/api/internal/sources?domain=product").json() if source["title"] == "service_sla")
    assert body["citations"][0]["source_id"] == service_source["id"]
    assert "30 分钟" in body["citations"][0]["snippet"]

    delete_result = client.delete(f"/api/internal/sources/{service_source['id']}?note=pg-test")
    assert delete_result.status_code == 200

    with connect_postgres(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM rag_document_chunks WHERE source_id = %s", (service_source["id"],))
            assert cur.fetchone()[0] == 0

    answer_after_delete = client.post(
        "/api/internal/ask",
        json={"question": "P1 故障多久内响应？", "domain": "product"},
    )
    assert answer_after_delete.status_code == 200
    assert all(
        citation["source_id"] != service_source["id"]
        for citation in answer_after_delete.json()["citations"]
    )
