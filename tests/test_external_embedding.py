from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.external_embedding import (
    ExternalEmbeddingError,
    clear_embedding_cache,
    embed_texts,
    rerank_rows_with_dashscope,
)


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload
        self.headers = {"content-type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def settings() -> Settings:
    return Settings(
        dashscope_embedding_enabled=True,
        dashscope_api_key="secret",
        dashscope_base_url="https://dashscope.example/compatible-mode/v1",
        dashscope_embedding_model="text-embedding-v3",
        dashscope_embedding_dimension=3,
        dashscope_embedding_batch_size=10,
        dashscope_embedding_cache_ttl_seconds=600,
    )


def test_embed_texts_splits_batches_at_ten(monkeypatch):
    calls: list[list[str]] = []

    def fake_urlopen(request, timeout):
        body = json.loads(request.data.decode("utf-8"))
        calls.append(body["input"])
        return FakeResponse(
            {
                "model": body["model"],
                "data": [
                    {"index": index, "embedding": [float(index + 1), 0.0, 0.0]}
                    for index, _ in enumerate(body["input"])
                ],
            }
        )

    monkeypatch.setattr("app.external_embedding.urlrequest.urlopen", fake_urlopen)
    result = embed_texts(settings(), [f"text-{index}" for index in range(23)])

    assert [len(call) for call in calls] == [10, 10, 3]
    assert len(result.vectors) == 23
    assert result.model == "text-embedding-v3"


def test_embed_texts_rejects_dimension_mismatch(monkeypatch):
    monkeypatch.setattr(
        "app.external_embedding.urlrequest.urlopen",
        lambda request, timeout: FakeResponse({"data": [{"index": 0, "embedding": [1.0, 2.0]}]}),
    )

    with pytest.raises(ExternalEmbeddingError, match="dimension"):
        embed_texts(settings(), ["query"])


def test_query_embedding_uses_ttl_cache(monkeypatch):
    clear_embedding_cache()
    call_count = 0

    def fake_urlopen(request, timeout):
        nonlocal call_count
        call_count += 1
        return FakeResponse({"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]})

    monkeypatch.setattr("app.external_embedding.urlrequest.urlopen", fake_urlopen)
    first = embed_texts(settings(), ["same query"], cache_key="query:same query")
    second = embed_texts(settings(), ["same query"], cache_key="query:same query")

    assert call_count == 1
    assert first.cache_hit is False
    assert second.cache_hit is True


def test_rerank_rows_promotes_semantically_matching_candidate(monkeypatch):
    def fake_embed(_settings, texts, cache_key=None):
        from app.external_embedding import EmbeddingBatch

        vectors = {
            "怎么取消已经购买的服务": [1.0, 0.0, 0.0],
            "退款政策\n七天内可撤销订单并原路退回款项": [0.95, 0.05, 0.0],
            "发票规则\n企业客户填写税号后开票": [0.0, 1.0, 0.0],
        }
        return EmbeddingBatch([vectors[text] for text in texts], "text-embedding-v3", False, 12)

    monkeypatch.setattr("app.external_embedding.embed_texts", fake_embed)
    rows = [
        {"id": "invoice", "title": "发票规则", "text": "企业客户填写税号后开票", "score": 100.0},
        {"id": "refund", "title": "退款政策", "text": "七天内可撤销订单并原路退回款项", "score": 10.0},
    ]

    reranked, diagnostics = rerank_rows_with_dashscope(settings(), "怎么取消已经购买的服务", rows)

    assert reranked[0]["id"] == "refund"
    assert diagnostics["channel"] == "dashscope_embedding"
    assert reranked[0]["external_vector_score"] > reranked[1]["external_vector_score"]
