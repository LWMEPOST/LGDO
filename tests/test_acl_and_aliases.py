from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


def test_alias_context_matches_role_alias_and_scores_candidate_text():
    from app.aliases import build_alias_context, score_alias_context

    aliases = [
        {
            "domain": "customer_service",
            "canonical_name": "部门负责人",
            "canonical_key": "部门负责人",
            "alias": "部门总监",
            "alias_key": "部门总监",
            "entity_type": "role",
            "metadata_json": '{"terms":["审批","报销","采购"]}',
        }
    ]

    context = build_alias_context("部门总监有哪些审批权限？", aliases)

    assert context["matched_aliases"][0]["alias"] == "部门总监"
    assert "部门负责人" in context["expansion_terms"]
    assert "审批" in context["expansion_terms"]
    score = score_alias_context(
        context,
        "费用报销制度",
        "部门负责人审批超过 10000 元的报销，采购申请也需要审批。",
    )
    assert 0 < score <= 48


def test_seed_role_aliases_is_idempotent(tmp_path, monkeypatch):
    from app.aliases import list_entity_aliases, seed_default_entity_aliases

    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "test.db")

    first = seed_default_entity_aliases(settings)
    second = seed_default_entity_aliases(settings)
    aliases = list_entity_aliases(settings, "customer_service")

    role_aliases = [
        item for item in aliases
        if item["alias"] == "部门总监" and item["canonical_name"] in {"部门负责人", "直属上级", "审批人"}
    ]
    assert first >= 1
    assert second >= 0
    assert role_aliases
    assert len({item["alias_key"] for item in role_aliases}) == 1


def test_ask_reports_alias_boost_in_top_hits(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    vault_dir = tmp_path / "vault"
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "role_policy.md").write_text(
        "# 权限规则\n\n部门负责人审批报销、采购、请假和远程办公申请。",
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
    assert client.post(
        "/api/internal/sources/scan",
        json={"root_path": str(sample_dir), "domain": "customer_service", "owner": "tester", "acl_tags": ["internal"]},
    ).status_code == 200
    assert client.post("/api/internal/wiki/compile", json={"domain": "customer_service"}).status_code == 200
    assert client.post(
        "/api/internal/aliases",
        json={
            "domain": "customer_service",
            "canonical_name": "部门负责人",
            "alias": "部门总监",
            "entity_type": "role",
            "metadata": {"terms": ["报销", "采购", "请假", "远程"]},
        },
    ).status_code == 200

    answer = client.post(
        "/api/internal/ask",
        json={"question": "部门总监有哪些审批权限？", "domain": "customer_service"},
    )

    assert answer.status_code == 200
    top_hits = answer.json()["retrieval_strategy"]["top_hits"]
    assert top_hits
    assert top_hits[0]["alias_boost"] > 0
