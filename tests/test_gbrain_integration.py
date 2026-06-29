from __future__ import annotations

from app.config import Settings
from app.gbrain import (
    GBrainError,
    GBrainHit,
    _QUERY_CACHE,
    _query_gbrain_with_caller,
    gbrain_query_candidates,
    normalize_gbrain_hits,
    query_gbrain,
)
from app.models import AskRequest
from app.search import ask


def test_normalize_gbrain_hits_maps_search_results():
    hits = normalize_gbrain_hits(
        [
            {
                "slug": "product/policies/refund",
                "title": "退款政策",
                "chunk_text": "用户在 7 天内可以申请退款。",
                "score": 3.5,
                "source_id": "default",
                "type": "policy",
                "chunk_id": 12,
                "relational_path": ["product", "refund"],
                "relational_via_link_types": ["mentions"],
            }
        ]
    )

    assert len(hits) == 1
    assert hits[0].slug == "product/policies/refund"
    assert hits[0].title == "退款政策"
    assert hits[0].score == 3.5
    assert hits[0].relational_path == ["product", "refund"]


def test_query_gbrain_falls_back_to_keyword_search(monkeypatch):
    settings = Settings(gbrain_enabled=True, gbrain_endpoint="http://gbrain.example/mcp")
    calls: list[str] = []

    monkeypatch.setattr("app.gbrain.get_gbrain_status", lambda _settings: type("Status", (), {"available": True})())

    def fake_call(_settings, tool_name, arguments, timeout):
        calls.append(tool_name)
        if tool_name == "query":
            return []
        return [
            {
                "slug": "product/policies/refund",
                "title": "退款政策",
                "chunk_text": "用户在 7 天内可以申请退款。",
                "score": 2.0,
            }
        ]

    monkeypatch.setattr("app.gbrain.call_gbrain_tool", fake_call)

    hits = query_gbrain(settings, "退款", limit=1)

    assert calls[0] == "query"
    assert "search" in calls
    assert hits[0].slug == "product/policies/refund"


def test_query_gbrain_merges_candidates_and_reranks_domain_hits(monkeypatch):
    settings = Settings(gbrain_enabled=True, gbrain_endpoint="http://gbrain.example/mcp")
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr("app.gbrain.get_gbrain_status", lambda _settings: type("Status", (), {"available": True})())

    def fake_call(_settings, tool_name, arguments, timeout):
        query = arguments["query"]
        calls.append((tool_name, query))
        if tool_name == "query":
            return [
                {
                    "slug": "api-rate-limit",
                    "title": "API频率限制排查",
                    "chunk_text": "当接口返回 429 时需要降低并发。",
                    "score": 0.9,
                    "source_id": "default",
                }
            ]
        if query == "积分系统":
            return [
                {
                    "slug": "product/features/kb-0045_kb_积分系统完全指南",
                    "title": "KB-0045_KB_积分系统完全指南",
                    "chunk_text": "积分获取方式包括每日签到、邀请好友、作品被精选、付费购买。",
                    "score": 1.0,
                    "source_id": "default",
                }
            ]
        return []

    monkeypatch.setattr("app.gbrain.call_gbrain_tool", fake_call)

    hits = query_gbrain(settings, "专业套餐的用户生成图片时积分不够了，有哪些补充积分的办法？", limit=2)

    assert hits[0].source_id == "KB-0045"
    assert hits[0].slug.endswith("kb-0045_kb_积分系统完全指南")
    assert ("search", "积分系统") in calls


def test_query_gbrain_adds_pricing_and_content_safety_domain_candidates(monkeypatch):
    settings = Settings(gbrain_enabled=True, gbrain_endpoint="http://gbrain.example/mcp")
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr("app.gbrain.get_gbrain_status", lambda _settings: type("Status", (), {"available": True})())

    def fake_call(_settings, tool_name, arguments, timeout):
        query = arguments["query"]
        calls.append((tool_name, query))
        if query == "全平台定价对比表":
            return [
                {
                    "slug": "product/tables/tbl-0063_table_全平台定价对比表",
                    "title": "TBL-0063_TABLE_全平台定价对比表",
                    "chunk_text": "企业标准 API调用/月 30,000，年费 ¥59,999。",
                    "score": 1.0,
                    "source_id": "default",
                }
            ]
        if query == "API-0010":
            return [
                {
                    "slug": "product/apis/api-0010_文生图api",
                    "title": "API-0010 文生图 API",
                    "chunk_text": "文生图 API 依赖内容安全审核引擎的内容审核结果。",
                    "score": 1.0,
                    "source_id": "default",
                }
            ]
        return []

    monkeypatch.setattr("app.gbrain.call_gbrain_tool", fake_call)

    pricing_hits = query_gbrain(settings, "日均 500 次文生图调用应该选择什么套餐组合？", limit=2)
    dependency_hits = query_gbrain(settings, "哪些子系统或 API 直接依赖内容安全审核引擎？", limit=2)

    assert pricing_hits[0].source_id == "TBL-0063"
    assert dependency_hits[0].source_id == "API-0010"
    assert ("search", "全平台定价对比表") in calls
    assert ("search", "API-0010") in calls


def test_gbrain_candidates_prioritize_domain_codes_before_generic_tokens():
    candidates = gbrain_query_candidates(
        "从 PRD 文档中提取所有明确标注了优先级（P0/P1/P2）的功能需求，并分析各 PRD 模块之间的依赖关系。",
        candidate_limit=10,
    )

    assert "PRD-0001" in candidates
    assert "PRD-0006" in candidates
    assert "PRD-0007" in candidates
    assert "API-0010" in candidates
    assert "API-0011" in candidates
    assert candidates.index("PRD-0001") < candidates.index("从 PRD 文档中提取所有明确标注了优先级 P0 P1 P2 的功能需求 并分析各 PRD 模块之间的依赖关系")


def test_query_gbrain_keeps_hits_when_one_candidate_search_fails(monkeypatch):
    calls = []

    def caller(tool_name, params):
        calls.append((tool_name, params["query"]))
        if tool_name == "query":
            return [{"slug": "main", "title": "时间窗口制度", "content": "退款窗口期 7天。", "score": 0.8}]
        if "失败候选" in params["query"]:
            raise GBrainError("candidate failed")
        return [{"slug": "other", "title": "报销制度", "content": "次月5日提交。", "score": 0.7}]

    monkeypatch.setattr(
        "app.gbrain.gbrain_query_candidates",
        lambda question, candidate_limit=8: ["失败候选", "期限"],
    )

    hits = _query_gbrain_with_caller(caller, {"query": "时间窗口和期限", "limit": 5}, "时间窗口和期限", 5, 8)

    assert [hit.slug for hit in hits] == ["main", "other"]
    assert ("search", "期限") in calls


def test_gbrain_candidates_trim_low_information_cjk_windows():
    candidates = gbrain_query_candidates(
        "梳理公司所有制度文档中，与时间窗口或期限相关的全部条款，并判断是否存在相互矛盾。",
        candidate_limit=8,
    )

    assert "时间窗口" in candidates
    assert "期限" in candidates
    assert len(candidates) <= 8
    assert not any(len(item) == 4 and item in {"梳理公司", "公司所有", "所有制度"} for item in candidates)


def test_query_gbrain_uses_short_ttl_cache(monkeypatch):
    _QUERY_CACHE.clear()
    settings = Settings(
        gbrain_enabled=True,
        gbrain_endpoint="http://gbrain.example/mcp",
        gbrain_query_cache_ttl_seconds=300,
    )
    calls: list[str] = []

    monkeypatch.setattr("app.gbrain.get_gbrain_status", lambda _settings: type("Status", (), {"available": True})())

    def fake_call(_settings, tool_name, arguments, timeout):
        calls.append(tool_name)
        return [
            {
                "slug": "product/features/kb-0045_kb_积分系统完全指南",
                "title": "KB-0045_KB_积分系统完全指南",
                "chunk_text": "积分获取方式包括每日签到。",
                "score": 1.0,
                "source_id": "default",
            }
        ]

    monkeypatch.setattr("app.gbrain.call_gbrain_tool", fake_call)

    first = query_gbrain(settings, "积分不够怎么办？", limit=1)
    second = query_gbrain(settings, "积分不够怎么办？", limit=1)

    assert first[0].source_id == "KB-0045"
    assert second[0].source_id == "KB-0045"
    assert calls.count("query") == 1


def test_query_gbrain_retries_short_chinese_candidates(monkeypatch):
    settings = Settings(gbrain_enabled=True, gbrain_endpoint="http://gbrain.example/mcp")
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr("app.gbrain.get_gbrain_status", lambda _settings: type("Status", (), {"available": True})())

    def fake_call(_settings, tool_name, arguments, timeout):
        query = arguments["query"]
        calls.append((tool_name, query))
        if query != "生成失败":
            return []
        return [
            {
                "slug": "product/policies/generation-failed",
                "title": "常见生成失败原因",
                "chunk_text": "错误信息速查表可用于排查生成失败。",
                "score": 4.0,
            }
        ]

    monkeypatch.setattr("app.gbrain.call_gbrain_tool", fake_call)

    hits = query_gbrain(settings, "生成失败原因怎么排查？", limit=1)

    assert hits[0].slug == "product/policies/generation-failed"
    assert ("search", "生成失败") in calls


def test_ask_includes_gbrain_context_when_enabled(tmp_path, monkeypatch):
    settings = Settings(
        database_backend="sqlite",
        rag_store_backend="sqlite",
        database_path=tmp_path / "data" / "test.db",
        vault_path=tmp_path / "vault",
        upload_path=tmp_path / "uploads",
        deepseek_api_key=None,
        deepseek_model=None,
        gbrain_enabled=True,
    )

    monkeypatch.setattr(
        "app.search.search_chunks",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "app.search.query_gbrain",
        lambda *args, **kwargs: [
            GBrainHit(
                slug="product/policies/refund",
                title="退款政策",
                snippet="用户在 7 天内可以申请退款。",
                score=2.0,
                source_id="default",
                page_type="policy",
            )
        ],
    )

    response = ask(settings, AskRequest(question="用户几天内可以退款？", domain="product"))

    assert response.confidence == "medium"
    assert response.retrieval_strategy["gbrain_hits"] == 1
    assert response.retrieval_strategy["gbrain_top_hits"][0]["slug"] == "product/policies/refund"
    assert "GBrain/退款政策" in response.answer
