from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import connect
from app.main import app


def test_rag_chunks_are_indexed_and_retrieval_prefers_matching_source(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    vault_dir = tmp_path / "vault"
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "refund_policy.md").write_text(
        "# 退款政策\n\n用户在 7 天内可以申请退款，客服需要核验订单号并记录退款原因。",
        encoding="utf-8",
    )
    (sample_dir / "permission_export.md").write_text(
        "# 权限导出\n\n管理员可以在权限中心导出成员权限清单，导出前需要二次确认。",
        encoding="utf-8",
    )
    (sample_dir / "invoice_rule.md").write_text(
        "# 发票规则\n\n企业客户申请发票时，需要填写抬头、税号和邮箱。",
        encoding="utf-8",
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", data_dir / "test.db")
    monkeypatch.setattr(settings, "postgres_port", 54322)
    monkeypatch.setattr(settings, "vault_path", vault_dir)
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
            "metadata_defaults": {"source_system": "retrieval_test"},
        },
    )
    assert scan.status_code == 200
    assert scan.json()["new_files"] == 3

    compile_result = client.post("/api/internal/wiki/compile", json={"domain": "product"})
    assert compile_result.status_code == 200

    with connect(settings.database_path) as conn:
        chunk_count = conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
        titles = {
            row["title"]
            for row in conn.execute("SELECT DISTINCT title FROM document_chunks").fetchall()
        }
    assert chunk_count >= 3
    assert {"退款政策", "权限导出", "发票规则"}.issubset(titles)

    status = client.get("/api/internal/rag/status")
    assert status.status_code == 200
    assert status.json()["chunk_count"] >= 3
    assert status.json()["source_count"] == 3
    assert status.json()["embedding_count"] >= 3
    assert status.json()["embedding_model"] == "local-hash-v1"
    assert status.json()["external_system_apis"]["feishu"] == "not_connected"
    assert status.json()["postgres"]["port"] == 54322

    with connect(settings.database_path) as conn:
        embedding_rows = conn.execute("SELECT metadata_json FROM document_chunks").fetchall()
    assert all('"embedding"' in row["metadata_json"] for row in embedding_rows)

    permission_answer = client.post(
        "/api/internal/ask",
        json={"question": "管理员怎么导出成员权限清单？", "domain": "product"},
    )
    assert permission_answer.status_code == 200
    permission_body = permission_answer.json()
    assert permission_body["citations"]
    permission_source = next(source for source in client.get("/api/internal/sources?domain=product").json() if source["title"] == "permission_export")
    assert permission_body["citations"][0]["source_id"] == permission_source["id"]
    assert "权限" in permission_body["citations"][0]["snippet"]
    assert "导出" in permission_body["citations"][0]["snippet"]

    refund_answer = client.post(
        "/api/internal/ask",
        json={"question": "用户几天内可以申请退款？", "domain": "product"},
    )
    assert refund_answer.status_code == 200
    refund_body = refund_answer.json()
    assert refund_body["citations"]
    refund_source = next(source for source in client.get("/api/internal/sources?domain=product").json() if source["title"] == "refund_policy")
    assert refund_body["citations"][0]["source_id"] == refund_source["id"]
    assert "退款" in refund_body["citations"][0]["snippet"]
    assert "7 天" in refund_body["citations"][0]["snippet"]


def test_answer_modes_change_local_answer_and_use_query_memory(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    vault_dir = tmp_path / "vault"
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "refund_policy.md").write_text(
        "# 退款政策\n\n用户在 7 天内可以申请退款，客服需要核验订单号并记录退款原因。",
        encoding="utf-8",
    )
    (sample_dir / "service_reply.md").write_text(
        "# 客服口径\n\n遇到退款咨询时，客服应先致歉并说明会根据订单状态核验是否符合退款条件。",
        encoding="utf-8",
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", data_dir / "test.db")
    monkeypatch.setattr(settings, "vault_path", vault_dir)
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
            "metadata_defaults": {"source_system": "mode_test"},
        },
    )
    assert scan.status_code == 200
    compile_result = client.post("/api/internal/wiki/compile", json={"domain": "product"})
    assert compile_result.status_code == 200

    detail_answer = client.post(
        "/api/internal/ask",
        json={"question": "用户如何申请退款？", "domain": "product", "answer_mode": "detail"},
    )
    assert detail_answer.status_code == 200
    detail_body = detail_answer.json()
    assert detail_body["retrieval_strategy"]["answer_mode"] == "detail"
    assert detail_body["retrieval_strategy"]["context_limit"] == 6
    assert "基于当前已入库资料" in detail_body["answer"]

    short_answer = client.post(
        "/api/internal/ask",
        json={"question": "用户几天内可以申请退款？", "domain": "product", "answer_mode": "short"},
    )
    assert short_answer.status_code == 200
    short_body = short_answer.json()
    assert short_body["retrieval_strategy"]["answer_mode"] == "short"
    assert short_body["retrieval_strategy"]["context_limit"] == 2
    assert short_body["answer"].startswith("简短回答：")
    assert short_body["answer"] != detail_body["answer"]
    assert short_body["memory_hits"]
    assert short_body["memory_hits"][0]["query_id"] == detail_body["query_id"]

    draft_answer = client.post(
        "/api/internal/ask",
        json={"question": "客户咨询退款时客服怎么回复？", "domain": "product", "answer_mode": "customer_reply_draft"},
    )
    assert draft_answer.status_code == 200
    draft_body = draft_answer.json()
    assert draft_body["retrieval_strategy"]["answer_mode"] == "customer_reply_draft"
    assert draft_body["retrieval_strategy"]["context_limit"] == 5
    assert "您好" in draft_body["answer"]
    assert draft_body["answer"] != short_body["answer"]
    assert "相似历史口径：" not in draft_body["answer"]


def test_configured_bge_m3_embedding_provider_is_used(monkeypatch):
    import app.rag as rag

    class FakeSentenceTransformer:
        def encode(self, texts, normalize_embeddings=True):
            assert texts == ["测试文本"]
            assert normalize_embeddings is True
            return [[0.1, 0.2, 0.3, 0.4]]

    settings = get_settings()
    monkeypatch.setattr(settings, "rag_embedding_provider", "sentence-transformers", raising=False)
    monkeypatch.setattr(settings, "rag_embedding_model", "BAAI/bge-m3", raising=False)
    monkeypatch.setattr(settings, "rag_embedding_dimension", 4, raising=False)
    monkeypatch.setattr(rag, "_load_sentence_transformer", lambda model_name: FakeSentenceTransformer(), raising=False)

    vector, model_name = rag.embed_text_with_model("测试文本", settings=settings)

    assert vector == [0.1, 0.2, 0.3, 0.4]
    assert model_name == "BAAI/bge-m3"


def test_upsert_document_chunks_records_configured_embedding_model(tmp_path, monkeypatch):
    import json
    import app.rag as rag

    class FakeSentenceTransformer:
        def encode(self, texts, normalize_embeddings=True):
            return [[0.25, 0.25, 0.25, 0.25]]

    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "test.db")
    monkeypatch.setattr(settings, "rag_embedding_provider", "sentence-transformers", raising=False)
    monkeypatch.setattr(settings, "rag_embedding_model", "BAAI/bge-m3", raising=False)
    monkeypatch.setattr(settings, "rag_embedding_dimension", 4, raising=False)
    monkeypatch.setattr(rag, "_load_sentence_transformer", lambda model_name: FakeSentenceTransformer(), raising=False)

    rag.upsert_document_chunks(
        settings,
        [
            {
                "id": "chunk_1",
                "source_id": "src_1",
                "chunk_index": 0,
                "text": "部门负责人审批采购。",
                "metadata": {"domain": "customer_service", "title": "采购制度"},
            }
        ],
    )

    with connect(settings.database_path) as conn:
        row = conn.execute("SELECT metadata_json FROM document_chunks WHERE id = ?", ("chunk_1",)).fetchone()

    metadata = json.loads(row["metadata_json"])
    assert metadata["embedding"] == [0.25, 0.25, 0.25, 0.25]
    assert metadata["embedding_model"] == "BAAI/bge-m3"


def test_rag_status_reports_configured_embedding_model(tmp_path, monkeypatch):
    import app.rag as rag
    from app.catalog import rag_status

    class FakeSentenceTransformer:
        def encode(self, texts, normalize_embeddings=True):
            return [[0.25, 0.25, 0.25, 0.25]]

    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "test.db")
    monkeypatch.setattr(settings, "rag_embedding_provider", "sentence-transformers")
    monkeypatch.setattr(settings, "rag_embedding_model", "BAAI/bge-m3")
    monkeypatch.setattr(settings, "rag_embedding_dimension", 4)
    monkeypatch.setattr(rag, "_load_sentence_transformer", lambda model_name: FakeSentenceTransformer())

    rag.upsert_document_chunks(
        settings,
        [
            {
                "id": "chunk_1",
                "source_id": "src_1",
                "chunk_index": 0,
                "text": "部门负责人审批采购。",
                "metadata": {"domain": "customer_service", "title": "采购制度"},
            }
        ],
    )

    assert rag_status(settings)["embedding_model"] == "BAAI/bge-m3"
