from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from app.config import Settings


class ExternalEmbeddingError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: list[list[float]]
    model: str
    cache_hit: bool
    latency_ms: int


_CACHE_LOCK = threading.Lock()
_EMBEDDING_CACHE: dict[str, tuple[float, EmbeddingBatch]] = {}


def clear_embedding_cache() -> None:
    with _CACHE_LOCK:
        _EMBEDDING_CACHE.clear()


def embed_texts(settings: Settings, texts: list[str], cache_key: str | None = None) -> EmbeddingBatch:
    if not texts:
        return EmbeddingBatch([], settings.dashscope_embedding_model, False, 0)
    if not settings.dashscope_embedding_enabled or not settings.dashscope_api_key:
        raise ExternalEmbeddingError("DashScope embedding is not configured")
    cached = _get_cached(settings, cache_key)
    if cached is not None:
        return cached

    started = time.monotonic()
    batch_size = max(1, min(int(settings.dashscope_embedding_batch_size), 10))
    vectors: list[list[float]] = []
    for offset in range(0, len(texts), batch_size):
        vectors.extend(_request_embeddings(settings, texts[offset : offset + batch_size]))
    result = EmbeddingBatch(
        vectors=vectors,
        model=settings.dashscope_embedding_model,
        cache_hit=False,
        latency_ms=round((time.monotonic() - started) * 1000),
    )
    if cache_key:
        _set_cached(settings, cache_key, result)
    return result


def rerank_rows_with_dashscope(
    settings: Settings,
    question: str,
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not rows:
        return [], {"channel": "dashscope_embedding", "cache_hit": False, "latency_ms": 0, "candidate_count": 0}
    limited = rows[: max(1, settings.dashscope_rerank_candidate_limit)]
    query_batch = embed_texts(settings, [question], cache_key=f"query:{question.strip()}")
    documents = [f"{row.get('title') or ''}\n{row.get('text') or ''}" for row in limited]
    document_batch = embed_texts(settings, documents)
    query_vector = query_batch.vectors[0]
    semantic_order: list[dict[str, Any]] = []
    for row, vector in zip(limited, document_batch.vectors):
        candidate = dict(row)
        candidate["external_vector_score"] = round(_cosine(query_vector, vector), 6)
        semantic_order.append(candidate)
    semantic_order.sort(key=lambda item: item["external_vector_score"], reverse=True)
    semantic_ranks = {str(item.get("id")): index for index, item in enumerate(semantic_order, start=1)}
    lexical_ranks = {str(item.get("id")): index for index, item in enumerate(limited, start=1)}
    for item in semantic_order:
        item_id = str(item.get("id"))
        fusion = 700.0 / (60 + lexical_ranks[item_id]) + 1000.0 / (60 + semantic_ranks[item_id])
        item["external_rrf_score"] = round(fusion, 6)
        item["score"] = round(fusion + max(item["external_vector_score"], 0.0) * 12.0, 6)
    semantic_order.sort(key=lambda item: item["score"], reverse=True)
    remainder = [row for row in rows if str(row.get("id")) not in semantic_ranks]
    diagnostics = {
        "channel": "dashscope_embedding",
        "cache_hit": query_batch.cache_hit,
        "latency_ms": query_batch.latency_ms + document_batch.latency_ms,
        "candidate_count": len(limited),
        "model": query_batch.model,
    }
    return semantic_order + remainder, diagnostics


def _request_embeddings(settings: Settings, texts: list[str]) -> list[list[float]]:
    payload = {
        "model": settings.dashscope_embedding_model,
        "input": texts,
        "dimensions": settings.dashscope_embedding_dimension,
    }
    request = urlrequest.Request(
        f"{settings.dashscope_base_url.rstrip('/')}/embeddings",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.dashscope_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(request, timeout=settings.dashscope_embedding_timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ExternalEmbeddingError(f"DashScope HTTP {exc.code}: {detail[:300]}") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise ExternalEmbeddingError(f"DashScope request failed: {exc}") from exc

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or len(data) != len(texts):
        raise ExternalEmbeddingError("DashScope embedding response count mismatch")
    ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
    vectors: list[list[float]] = []
    for item in ordered:
        vector = item.get("embedding") if isinstance(item, dict) else None
        if not isinstance(vector, list) or len(vector) != settings.dashscope_embedding_dimension:
            raise ExternalEmbeddingError("DashScope embedding dimension mismatch")
        vectors.append([float(value) for value in vector])
    return vectors


def _get_cached(settings: Settings, cache_key: str | None) -> EmbeddingBatch | None:
    if not cache_key or settings.dashscope_embedding_cache_ttl_seconds <= 0:
        return None
    with _CACHE_LOCK:
        cached = _EMBEDDING_CACHE.get(cache_key)
        if not cached:
            return None
        cached_at, batch = cached
        if time.monotonic() - cached_at > settings.dashscope_embedding_cache_ttl_seconds:
            _EMBEDDING_CACHE.pop(cache_key, None)
            return None
        return EmbeddingBatch(list(batch.vectors), batch.model, True, 0)


def _set_cached(settings: Settings, cache_key: str, batch: EmbeddingBatch) -> None:
    with _CACHE_LOCK:
        _EMBEDDING_CACHE[cache_key] = (time.monotonic(), batch)
        while len(_EMBEDDING_CACHE) > max(1, settings.dashscope_embedding_cache_max_entries):
            oldest = min(_EMBEDDING_CACHE, key=lambda key: _EMBEDDING_CACHE[key][0])
            _EMBEDDING_CACHE.pop(oldest, None)


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)
