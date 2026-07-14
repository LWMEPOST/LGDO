from __future__ import annotations

import json

from app.config import Settings
from app.db import connect_app_write, init_app_db
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


NOW = "2099-01-01T12:00:00+00:00"


def _seed_source(settings: Settings, source_id: str) -> None:
    init_app_db(settings)
    with connect_app_write(settings) as conn:
        conn.execute(
            """
            INSERT INTO sources(
              id,domain,owner,title,source_type,original_path,raw_path,
              content_hash,size_bytes,status,metadata_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source_id,
                "product",
                "tester",
                source_id,
                "markdown",
                f"{source_id}.md",
                f"raw/{source_id}.md",
                f"hash-{source_id}",
                100,
                "active",
                "{}",
                NOW,
                NOW,
            ),
        )


def _seed_current_gbrain_mapping(settings: Settings) -> None:
    _seed_source(settings, "source-refund")
    with connect_app_write(settings) as conn:
        conn.execute(
            """
            INSERT INTO wiki_page_revisions(
              id,page_id,page_path,revision_number,file_hash,semantic_hash,
              content,origin,base_revision_id,source_ids_json,actor,note,
              metadata_json,idempotency_key,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "wrev-refund",
                "page-refund",
                "wiki/product/policies/refund.md",
                1,
                "file-refund",
                "semantic-refund",
                "用户在 7 天内可以申请退款。",
                "manual",
                None,
                json.dumps(["source-refund"]),
                "tester",
                None,
                "{}",
                "gbrain-test:wrev-refund",
                NOW,
            ),
        )
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,page_id,domain,page_type,title,source_ids_json,review_status,
              created_at,updated_at,current_revision_id,revision_number,file_hash,
              semantic_hash,projection_epoch,lifecycle_status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "wiki/product/policies/refund.md",
                "page-refund",
                "product",
                "policy",
                "退款政策",
                json.dumps(["source-refund"]),
                "approved",
                NOW,
                NOW,
                "wrev-refund",
                1,
                "file-refund",
                "semantic-refund",
                3,
                "active",
            ),
        )
        conn.execute(
            """
            INSERT INTO gbrain_page_projections(
              id,page_id,revision_id,projection_epoch,page_path,file_hash,
              semantic_hash,gbrain_source_id,slug,source_path,
              gbrain_content_hash,gbrain_page_generation,status,imported_at,
              invalidated_at,last_job_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "gproj-refund",
                "page-refund",
                "wrev-refund",
                3,
                "wiki/product/policies/refund.md",
                "file-refund",
                "semantic-refund",
                "managed-test",
                "product/policies/refund",
                "product/policies/refund.md",
                "gbrain-refund",
                9,
                "current",
                NOW,
                None,
                "pjob-refund",
            ),
        )


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
        if query == "补充积分":
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

    assert hits[0].source_id == "default"
    assert hits[0].gbrain_source_id == "default"
    assert hits[0].slug.endswith("kb-0045_kb_积分系统完全指南")
    assert ("search", "补充积分") in calls


def test_query_gbrain_uses_explicit_domain_phrases_as_candidates(monkeypatch):
    settings = Settings(gbrain_enabled=True, gbrain_endpoint="http://gbrain.example/mcp")
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr("app.gbrain.get_gbrain_status", lambda _settings: type("Status", (), {"available": True})())

    def fake_call(_settings, tool_name, arguments, timeout):
        query = arguments["query"]
        calls.append((tool_name, query))
        if query == "套餐组合":
            return [
                {
                    "slug": "product/tables/tbl-0063_table_全平台定价对比表",
                    "title": "TBL-0063_TABLE_全平台定价对比表",
                    "chunk_text": "企业标准 API调用/月 30,000，年费 ¥59,999。",
                    "score": 1.0,
                    "source_id": "default",
                }
            ]
        if query == "内容安全审核引擎":
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

    assert pricing_hits[0].source_id == "default"
    assert pricing_hits[0].gbrain_source_id == "default"
    assert dependency_hits[0].source_id == "default"
    assert dependency_hits[0].gbrain_source_id == "default"
    assert ("search", "套餐组合") in calls
    assert ("search", "内容安全审核引擎") in calls


def test_gbrain_candidates_prioritize_domain_codes_before_generic_tokens():
    candidates = gbrain_query_candidates(
        "从 PRD 文档中提取所有明确标注了优先级（P0/P1/P2）的功能需求，并分析各 PRD 模块之间的依赖关系。",
        candidate_limit=10,
    )

    assert "PRD" in candidates
    assert "优先级" in candidates
    assert "依赖关系" in candidates
    assert len(candidates) <= 10


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


def test_query_gbrain_uses_short_ttl_cache(tmp_path, monkeypatch):
    _QUERY_CACHE.clear()
    settings = Settings(
        database_backend="sqlite",
        database_path=tmp_path / "cache.db",
        gbrain_enabled=True,
        gbrain_endpoint="http://gbrain.example/mcp",
        gbrain_query_cache_ttl_seconds=300,
    )
    init_app_db(settings)
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

    assert first[0].source_id == "default"
    assert first[0].gbrain_source_id == "default"
    assert second[0].source_id == "default"
    assert second[0].gbrain_source_id == "default"
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
    _seed_current_gbrain_mapping(settings)

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
                source_id="managed-test",
                gbrain_source_id="managed-test",
                source_path="product/policies/refund.md",
                content_hash="gbrain-refund",
                page_generation=9,
                page_type="policy",
                chunk_id=12,
            )
        ],
    )

    response = ask(settings, AskRequest(question="用户几天内可以退款？", domain="product"))

    assert response.confidence == "medium"
    assert response.retrieval_strategy["gbrain_hits"] == 1
    assert response.retrieval_strategy["gbrain_top_hits"][0]["slug"] == "product/policies/refund"
    assert "GBrain/退款政策" in response.answer


def test_query_gbrain_keeps_hits_when_one_candidate_search_fails(monkeypatch):
    from app.gbrain import GBrainError, _query_gbrain_with_caller

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


def test_gbrain_candidates_respect_zero_candidate_limit():
    assert gbrain_query_candidates("时间窗口和期限", candidate_limit=0) == []


def test_gbrain_circuit_opens_after_three_failures(monkeypatch):
    from app.gbrain import _reset_gbrain_circuit, query_gbrain_with_diagnostics

    _reset_gbrain_circuit()
    settings = Settings(
        gbrain_enabled=True,
        gbrain_endpoint="http://gbrain.example/mcp",
        gbrain_circuit_failure_threshold=3,
        gbrain_circuit_cooldown_seconds=30,
    )
    monkeypatch.setattr("app.gbrain.get_gbrain_status", lambda _settings: type("Status", (), {"available": True})())
    calls = 0

    def failing_call(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise GBrainError("offline")

    monkeypatch.setattr("app.gbrain.call_gbrain_tool", failing_call)

    results = [query_gbrain_with_diagnostics(settings, f"question-{index}") for index in range(4)]

    assert calls == 3
    assert results[2].reason == "request_failed"
    assert results[3].circuit_open is True
    assert results[3].reason == "circuit_open"


def test_gbrain_success_resets_circuit(monkeypatch):
    from app.gbrain import _reset_gbrain_circuit, query_gbrain_with_diagnostics

    _reset_gbrain_circuit()
    settings = Settings(gbrain_enabled=True, gbrain_endpoint="http://gbrain.example/mcp")
    monkeypatch.setattr("app.gbrain.get_gbrain_status", lambda _settings: type("Status", (), {"available": True})())
    monkeypatch.setattr(
        "app.gbrain.call_gbrain_tool",
        lambda *args, **kwargs: [{"slug": "refund", "title": "退款", "chunk_text": "七天退款", "score": 1.0}],
    )

    result = query_gbrain_with_diagnostics(settings, "退款")

    assert result.hits
    assert result.reason is None
    assert result.circuit_open is False


def test_ask_uses_dashscope_rerank_when_gbrain_has_no_hits(tmp_path, monkeypatch):
    from app.external_embedding import EmbeddingBatch
    from app.gbrain import GBrainQueryResult

    settings = Settings(
        database_backend="sqlite",
        rag_store_backend="sqlite",
        database_path=tmp_path / "data" / "test.db",
        vault_path=tmp_path / "vault",
        upload_path=tmp_path / "uploads",
        gbrain_enabled=True,
        dashscope_embedding_enabled=True,
        dashscope_api_key="secret",
        dashscope_embedding_dimension=3,
    )
    _seed_source(settings, "source-invoice")
    _seed_source(settings, "source-refund")
    rows = [
        {
            "id": "invoice",
            "source_id": "source-invoice",
            "title": "发票规则",
            "text": "企业客户填写税号后开票",
            "snippet": "企业客户填写税号后开票",
            "score": 100.0,
        },
        {
            "id": "refund",
            "source_id": "source-refund",
            "title": "退款政策",
            "text": "七天内可撤销订单并原路退回款项",
            "snippet": "七天内可撤销订单并原路退回款项",
            "score": 10.0,
        },
    ]
    monkeypatch.setattr("app.search.search_chunks", lambda *args, **kwargs: rows)
    monkeypatch.setattr(
        "app.search._query_gbrain_for_search",
        lambda *args, **kwargs: GBrainQueryResult([], reason="empty"),
    )

    vectors = {
        "怎么取消已经购买的服务": [1.0, 0.0, 0.0],
        "发票规则\n企业客户填写税号后开票": [0.0, 1.0, 0.0],
        "退款政策\n七天内可撤销订单并原路退回款项": [0.95, 0.05, 0.0],
    }

    def fake_embed(_settings, texts, cache_key=None):
        return EmbeddingBatch([vectors[text] for text in texts], "text-embedding-v3", False, 8)

    monkeypatch.setattr("app.external_embedding.embed_texts", fake_embed)
    result = ask(settings, AskRequest(question="怎么取消已经购买的服务"))

    assert result.retrieval_strategy["fallback_retrieval"]["channel"] == "dashscope_embedding"
    assert result.retrieval_strategy["top_hits"][0]["source_id"] == "source-refund"
