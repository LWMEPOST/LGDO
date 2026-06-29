import json

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


def _configure_sqlite_settings(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "test.db")
    monkeypatch.setattr(settings, "vault_path", tmp_path / "vault")
    monkeypatch.setattr(settings, "upload_path", tmp_path / "uploads")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(settings, "deepseek_model", "deepseek-test")
    return settings


def test_ask_stream_endpoint_emits_metadata_delta_and_done(tmp_path, monkeypatch):
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "policy.md").write_text(
        "# 退款制度\n\n客户在签收后 7 天内可以申请退款，逾期需要主管审批。",
        encoding="utf-8",
    )
    _configure_sqlite_settings(tmp_path, monkeypatch)
    monkeypatch.setattr("app.search.stream_generate_answer", lambda *args, **kwargs: iter(["答案", "片段"]))

    client = TestClient(app)
    assert (
        client.post(
            "/api/internal/sources/scan",
            json={
                "root_path": str(sample_dir),
                "domain": "customer_service",
                "owner": "tester",
                "acl_tags": ["internal"],
            },
        ).status_code
        == 200
    )
    assert client.post("/api/internal/wiki/compile", json={"domain": "customer_service"}).status_code == 200

    with client.stream(
        "POST",
        "/api/internal/ask/stream",
        json={"question": "退款期限是多久？", "domain": "customer_service", "acl_tags": ["internal"]},
    ) as response:
        assert response.status_code == 200
        events = [json.loads(line) for line in response.iter_lines() if line]

    assert [event["event"] for event in events] == ["metadata", "answer_delta", "answer_delta", "done"]
    assert events[1]["text"] == "答案"
    done = events[-1]["response"]
    assert done["answer"] == "答案片段"
    assert done["confidence"] in {"medium", "high"}
    assert done["retrieval_strategy"]["chunk_hits"] >= 1


def test_ask_stream_endpoint_falls_back_to_local_answer(tmp_path, monkeypatch):
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "policy.md").write_text("# 退款制度\n\n退款期限是 7 天。", encoding="utf-8")
    _configure_sqlite_settings(tmp_path, monkeypatch)
    monkeypatch.setattr("app.search.stream_generate_answer", lambda *args, **kwargs: iter([]))

    client = TestClient(app)
    client.post(
        "/api/internal/sources/scan",
        json={
            "root_path": str(sample_dir),
            "domain": "customer_service",
            "owner": "tester",
            "acl_tags": ["internal"],
        },
    )
    client.post("/api/internal/wiki/compile", json={"domain": "customer_service"})

    with client.stream(
        "POST",
        "/api/internal/ask/stream",
        json={"question": "退款期限？", "domain": "customer_service", "acl_tags": ["internal"]},
    ) as response:
        events = [json.loads(line) for line in response.iter_lines() if line]

    deltas = [event["text"] for event in events if event["event"] == "answer_delta"]
    assert deltas
    assert "退款" in events[-1]["response"]["answer"]
