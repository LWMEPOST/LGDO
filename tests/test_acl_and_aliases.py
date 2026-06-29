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
        item
        for item in aliases
        if item["alias"] == "部门总监" and item["canonical_name"] in {"部门负责人", "直属上级", "审批人"}
    ]
    assert first >= 1
    assert second >= 0
    assert role_aliases
    assert len({item["alias_key"] for item in role_aliases}) == 1


def test_acl_filters_sources_chunks_citations_and_query_memory(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    vault_dir = tmp_path / "vault"
    public_dir = tmp_path / "public"
    finance_dir = tmp_path / "finance"
    public_dir.mkdir()
    finance_dir.mkdir()
    (public_dir / "general_policy.md").write_text(
        "# 通用政策\n\n所有内部用户都可以查看基础退款说明。",
        encoding="utf-8",
    )
    (finance_dir / "finance_policy.md").write_text(
        "# 财务政策\n\n财务用户可以查看企业客户退款审批金额上限为 50000 元。",
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
        json={
            "root_path": str(public_dir),
            "domain": "product",
            "owner": "ops",
            "acl_tags": ["internal"],
        },
    ).status_code == 200
    assert client.post(
        "/api/internal/sources/scan",
        json={
            "root_path": str(finance_dir),
            "domain": "product",
            "owner": "finance_owner",
            "acl_tags": ["finance"],
        },
    ).status_code == 200
    assert client.post("/api/internal/wiki/compile", json={"domain": "product"}).status_code == 200

    finance_answer = client.post(
        "/api/internal/ask",
        json={
            "question": "企业客户退款审批金额上限是多少？",
            "domain": "product",
            "user_id": "finance_user",
            "role": "viewer",
            "acl_tags": ["finance"],
        },
    )
    assert finance_answer.status_code == 200
    finance_body = finance_answer.json()
    assert finance_body["citations"]
    assert "50000" in finance_body["citations"][0]["snippet"]
    assert finance_body["user_context"]["user_id"] == "finance_user"
    assert finance_body["retrieval_strategy"]["authorized_chunk_hits"] >= 1

    ops_answer = client.post(
        "/api/internal/ask",
        json={
            "question": "企业客户退款审批金额上限是多少？",
            "domain": "product",
            "user_id": "ops_user",
            "role": "viewer",
            "acl_tags": ["internal"],
        },
    )
    assert ops_answer.status_code == 200
    ops_body = ops_answer.json()
    assert all("50000" not in citation["snippet"] for citation in ops_body["citations"])
    assert not ops_body["memory_hits"]

    visible_sources = client.get(
        "/api/internal/sources?domain=product",
        headers={"X-LGDO-User": "ops_user", "X-LGDO-Role": "viewer", "X-LGDO-ACL-Tags": "internal"},
    )
    assert visible_sources.status_code == 200
    assert {source["title"] for source in visible_sources.json()} == {"general_policy"}


def test_entity_alias_expands_query_to_canonical_concept(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    vault_dir = tmp_path / "vault"
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "owner_rule.md").write_text(
        "# 部门负责人规则\n\n部门负责人需要在 24 小时内确认跨部门审批请求。",
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
        json={
            "root_path": str(sample_dir),
            "domain": "product",
            "owner": "tester",
            "acl_tags": ["internal"],
        },
    ).status_code == 200
    assert client.post("/api/internal/wiki/compile", json={"domain": "product"}).status_code == 200
    alias = client.post(
        "/api/internal/aliases",
        json={
            "domain": "product",
            "canonical_name": "部门负责人",
            "alias": "部门总监",
            "entity_type": "role",
        },
    )
    assert alias.status_code == 200
    assert alias.json()["canonical_name"] == "部门负责人"

    answer = client.post(
        "/api/internal/ask",
        json={"question": "部门总监多久内确认跨部门审批？", "domain": "product", "acl_tags": ["internal"]},
    )
    assert answer.status_code == 200
    body = answer.json()
    assert body["citations"]
    assert "24 小时" in body["citations"][0]["snippet"]
    assert body["retrieval_strategy"]["alias_expanded"] is True
    assert body["retrieval_strategy"]["matched_aliases"][0]["alias"] == "部门总监"


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
        json={"question": "部门总监有哪些审批权限？", "domain": "customer_service", "acl_tags": ["internal"]},
    )

    assert answer.status_code == 200
    top_hits = answer.json()["retrieval_strategy"]["top_hits"]
    assert top_hits
    assert top_hits[0]["alias_boost"] > 0
