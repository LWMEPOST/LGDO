from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache
from typing import Any

from app.aliases import score_alias_context
from app.config import Settings
from app.db import connect_app, init_app_db, json_dump, rows_to_dicts
from app.timeutil import now_iso


STOP_WORDS = {
    "用户",
    "客服",
    "客户",
    "问题",
    "如何",
    "怎么",
    "什么",
    "处理",
    "需要",
    "可以",
    "建议",
    "说明",
    "一个",
    "这个",
    "那个",
    "以及",
    "如果",
    "多少",
    "哪些",
    "多久",
    "是谁",
    "是否",
    "以及",
    "其中",
    "还是",
    "没有",
    "没",
    "吗",
    "呢",
    "的",
    "是",
    "有",
    "在",
    "和",
    "与",
    "或",
    "the",
    "a",
    "an",
    "is",
    "to",
    "of",
    "and",
}

EMBEDDING_DIMENSION = 96
QUERY_SPLIT_RE = re.compile(
    r"[\s,，。；;：:?？!！、|]+|"
    r"(?:的|是什么|什么|是多少|多少|哪些|如何|怎么|为什么|是否|是谁|谁|有多少|有|和|与|以及|需要|建议)"
)
GENERIC_TITLE_RE = re.compile(r"^(page|slide)\s+\d+$", re.I)
DOC_CODE_RE = re.compile(r"\b(?:API|KB|PRD|TBL|PPT|SUP|TKT)[-_\s]\d{4}\b", re.I)
CANONICAL_DOCUMENT_TOPICS = [
    "全平台定价对比表",
    "积分系统完全指南",
    "内容安全审核引擎",
    "内容安全审核规则详解",
    "数据保留与隐私白皮书",
    "webhook回调文档",
    "文生图api完整参考",
    "文生视频api参考",
    "实时协作引擎",
    "售后服务政策",
    "行政管理制度",
    "费用报销制度",
    "采购管理制度",
    "办公行为规范",
    "企业客户入驻指南",
    "产品功能清单与版本对照",
]
CANONICAL_DOCUMENT_CODES = {
    "TBL-0063": "全平台定价对比表",
    "KB-0045": "积分系统完全指南",
    "API-0018": "数据保留与隐私白皮书",
    "PRD-0007": "内容安全审核引擎",
    "PRD-0006": "实时协作引擎",
    "API-0017": "内容安全审核规则详解",
    "API-0012": "webhook回调文档",
    "API-0010": "文生图api完整参考",
    "API-0011": "文生视频api参考",
    "PPT-0058": "企业客户入驻指南",
    "SUP-0072": "产品功能清单与版本对照",
}


def tokenize(text: str) -> list[str]:
    lowered = text.lower()
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", lowered)
    tokens: list[str] = []
    for word in words:
        if word not in STOP_WORDS and len(word) > 1:
            tokens.append(word)
        if re.search(r"[\u4e00-\u9fff]", word):
            tokens.extend(word[i : i + 2] for i in range(max(0, len(word) - 1)))
            tokens.extend(word[i : i + 3] for i in range(max(0, len(word) - 2)))
    return [token for token in tokens if token.strip() and token not in STOP_WORDS and len(token) > 1]


def score_text(question_tokens: list[str], text: str, *, title: str = "") -> int:
    haystack = f"{title}\n{text}".lower()
    token_counts = Counter(question_tokens)
    score = 0
    for token, count in token_counts.items():
        hits = haystack.count(token)
        if hits:
            weight = 6 if token in title.lower() else 1
            if len(token) >= 3 or re.search(r"[\u4e00-\u9fff]{2,}", token):
                weight += 3
            score += min(hits, 8) * weight * count
    return score


def extract_query_phrases(question: str) -> list[str]:
    lowered = question.lower()
    normalized = re.sub(r"([a-z0-9_])([\u4e00-\u9fff])", r"\1 \2", lowered)
    normalized = re.sub(r"([\u4e00-\u9fff])([a-z0-9_])", r"\1 \2", normalized)
    candidates = [part.strip(" -_/()（）") for part in QUERY_SPLIT_RE.split(normalized)]
    phrases: list[str] = []
    for candidate in candidates:
        compact = normalize_for_match(candidate)
        if len(compact) < 2 or compact in STOP_WORDS:
            continue
        if re.fullmatch(r"\d+", compact) and len(compact) < 3:
            continue
        phrases.append(candidate)

    for match in re.findall(r"[a-z]{2,}-\d{2,}|[a-z]+\s*key|bearer|roadmap|20\d{2}", lowered):
        phrases.append(match)
    for match in DOC_CODE_RE.findall(question):
        phrases.append(match)

    return dedupe(phrases)


def normalize_for_match(value: str) -> str:
    return re.sub(r"[\s_\-:：/\\|,，。；;?？!！()（）<>《》\"'`]+", "", value.lower())


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = normalize_for_match(value)
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def rank_search_rows(
    rows: list[dict[str, Any]],
    question: str,
    *,
    keyword_weight: float = 1.0,
    vector_weight: float = 12.0,
    alias_context: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    tokens = tokenize(question)
    phrases = extract_query_phrases(question)
    question_embedding, _ = embed_text_with_model(question, settings=settings)
    bm25_by_id = bm25_scores(rows, tokens)
    candidates: list[dict[str, Any]] = []

    for row in rows:
        title = row.get("title") or ""
        text = row.get("text") or ""
        keyword_score = score_text(tokens, text, title=title)
        metadata = row.get("metadata") or {}
        vector_score = cosine_similarity(question_embedding, row.get("embedding") or metadata.get("embedding"))
        phrase_score = exact_phrase_boost(phrases, text, title)
        table_score = table_row_boost(phrases, tokens, text)
        context_score = contextual_boost(question, text, title)
        bm25_score = bm25_by_id.get(str(row.get("id") or ""), 0.0)
        doc_code_score = document_code_boost(question, title, text, row.get("source_id"))
        alias_score = score_alias_context(alias_context, title, text)
        if (
            keyword_score <= 0
            and phrase_score <= 0
            and table_score <= 0
            and context_score <= 0
            and bm25_score <= 0
            and doc_code_score <= 0
            and alias_score <= 0
            and vector_score <= 0.08
        ):
            continue
        ranked_row = dict(row)
        ranked_row["keyword_score"] = keyword_score
        ranked_row["vector_score"] = round(vector_score, 6)
        ranked_row["bm25_score"] = round(bm25_score, 6)
        ranked_row["phrase_boost"] = round(phrase_score, 6)
        ranked_row["table_boost"] = round(table_score, 6)
        ranked_row["context_boost"] = round(context_score, 6)
        ranked_row["doc_code_boost"] = round(doc_code_score, 6)
        ranked_row["alias_boost"] = round(alias_score, 6)
        ranked_row["_lexical_rank_score"] = (
            keyword_score * keyword_weight
            + bm25_score * 18.0
            + phrase_score
            + table_score
            + context_score
            + doc_code_score
            + alias_score
        )
        candidates.append(ranked_row)

    lexical_order = sorted(
        candidates,
        key=lambda item: (item["_lexical_rank_score"], item["keyword_score"], len(item.get("text") or "") * -1),
        reverse=True,
    )
    vector_order = sorted(candidates, key=lambda item: item["vector_score"], reverse=True)
    lexical_ranks = {item["id"]: index for index, item in enumerate(lexical_order, start=1)}
    vector_ranks = {item["id"]: index for index, item in enumerate(vector_order, start=1)}

    for item in candidates:
        lexical_rank = lexical_ranks[item["id"]]
        vector_rank = vector_ranks[item["id"]]
        rrf_score = 1000.0 / (60 + lexical_rank) + 400.0 / (60 + vector_rank)
        vector_component = max(item["vector_score"], 0.0) * vector_weight
        keyword_component = min(item["keyword_score"], 120) * keyword_weight * 0.08
        bm25_component = item["bm25_score"] * 18.0
        item["rrf_score"] = round(rrf_score, 6)
        item["score"] = round(
            rrf_score
            + bm25_component
            + item["phrase_boost"]
            + item["table_boost"]
            + item["context_boost"]
            + item["doc_code_boost"]
            + item["alias_boost"]
            + keyword_component
            + vector_component,
            6,
        )
        item["snippet"] = clip_snippet(item.get("text") or "", tokens)
        item.pop("_lexical_rank_score", None)

    candidates.sort(
        key=lambda item: (
            item["score"],
            item["phrase_boost"] + item["table_boost"],
            item["doc_code_boost"],
            item["alias_boost"],
            item["keyword_score"],
            len(item.get("text") or "") * -1,
        ),
        reverse=True,
    )
    return candidates


def diversify_ranked_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []

    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    deferred: list[dict[str, Any]] = []
    for row in rows:
        key = document_identity_key(row)
        if key and key not in selected_keys:
            selected.append(row)
            selected_keys.add(key)
            if len(selected) >= limit:
                return selected
        else:
            deferred.append(row)

    for row in deferred:
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected[:limit]


def bm25_scores(rows: list[dict[str, Any]], query_tokens: list[str]) -> dict[str, float]:
    terms = [token for token in query_tokens if len(token) >= 2]
    if not rows or not terms:
        return {}

    row_terms: list[tuple[str, Counter[str], int]] = []
    document_frequency: Counter[str] = Counter()
    for index, row in enumerate(rows):
        row_id = str(row.get("id") or index)
        tokens = row_search_tokens(row)
        counts = Counter(tokens)
        row_terms.append((row_id, counts, max(1, len(tokens))))
        for term in set(terms):
            if counts.get(term, 0) > 0:
                document_frequency[term] += 1

    average_length = sum(length for _, _, length in row_terms) / max(1, len(row_terms))
    scores: dict[str, float] = {}
    total_docs = len(rows)
    k1 = 1.4
    b = 0.72
    for row_id, counts, length in row_terms:
        score = 0.0
        for term in terms:
            frequency = counts.get(term, 0)
            if frequency <= 0:
                continue
            idf = math.log(1 + (total_docs - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
            denominator = frequency + k1 * (1 - b + b * length / average_length)
            score += idf * (frequency * (k1 + 1)) / denominator
        if score > 0:
            scores[row_id] = score
    return scores


def row_search_tokens(row: dict[str, Any]) -> list[str]:
    raw_tokens = row.get("tokens")
    if isinstance(raw_tokens, list):
        tokens = [str(token) for token in raw_tokens if token]
    else:
        tokens = tokenize(f"{row.get('title') or ''}\n{row.get('text') or ''}")
    codes = extract_document_codes(f"{row.get('title') or ''}\n{row.get('text') or ''}")
    return tokens + [code.lower() for code in codes]


def extract_document_codes(text: str) -> list[str]:
    return [re.sub(r"[-_\s]+", "-", match.upper()) for match in DOC_CODE_RE.findall(text or "")]


def document_identity_key(row: dict[str, Any]) -> str:
    haystack = f"{row.get('source_id') or ''}\n{row.get('title') or ''}\n{row.get('text') or ''}"
    title_key = normalize_for_match(str(row.get("title") or ""))
    haystack_key = normalize_for_match(haystack)
    for topic in CANONICAL_DOCUMENT_TOPICS:
        topic_key = normalize_for_match(topic)
        if topic_key in title_key:
            return f"topic:{topic_key}"
    codes = extract_document_codes(haystack)
    if codes:
        topic = CANONICAL_DOCUMENT_CODES.get(codes[0])
        if topic:
            return f"topic:{normalize_for_match(topic)}"
        return f"code:{codes[0]}"
    for topic in CANONICAL_DOCUMENT_TOPICS:
        topic_key = normalize_for_match(topic)
        if topic_key in haystack_key:
            return f"topic:{topic_key}"
    title = str(row.get("title") or "").strip()
    if title_key and title_key not in {"未命名资料", "untitled", "unknown"} and not GENERIC_TITLE_RE.match(title):
        return f"title:{title_key}"
    return f"source:{row.get('source_id') or row.get('id') or ''}"


def document_code_boost(question: str, title: str, text: str, source_id: Any = None) -> float:
    query_codes = set(extract_document_codes(question))
    haystack = f"{source_id or ''}\n{title}\n{text}"
    row_codes = set(extract_document_codes(haystack))
    boost = 0.0
    if query_codes and row_codes:
        boost += len(query_codes & row_codes) * 96.0

    query = normalize_for_match(question)
    title_key = normalize_for_match(title)
    haystack_key = normalize_for_match(haystack)
    for code in row_codes:
        prefix, number = code.split("-", 1)
        compact = f"{prefix.lower()}{number}"
        if compact in query or code.lower() in query:
            boost += 96.0
        if compact in title_key or code.lower() in title_key:
            boost += 30.0
    return boost


def exact_phrase_boost(phrases: list[str], text: str, title: str = "") -> float:
    haystack = normalize_for_match(f"{title}\n{text}")
    title_haystack = normalize_for_match(title)
    boost = 0.0
    for phrase in phrases:
        key = normalize_for_match(phrase)
        if len(key) < 2:
            continue
        if key in haystack:
            boost += 30.0 + min(len(key), 16) * 3.0
            if key in title_haystack:
                boost += 35.0
    return boost


def table_row_boost(phrases: list[str], tokens: list[str], text: str) -> float:
    phrase_keys = [normalize_for_match(phrase) for phrase in phrases if len(normalize_for_match(phrase)) >= 2]
    token_keys = [normalize_for_match(token) for token in tokens if len(normalize_for_match(token)) >= 2]
    boost = 0.0
    for line in text.splitlines():
        if "|" not in line:
            continue
        if is_generic_lookup_table(line, text):
            continue
        compact = normalize_for_match(line)
        phrase_hits = sum(1 for phrase in phrase_keys if phrase in compact)
        token_hits = sum(1 for token in token_keys if token in compact)
        if phrase_hits >= 1 and token_hits >= 3:
            boost = max(boost, 45.0 + phrase_hits * 20.0 + min(token_hits, 8) * 5.0)
        elif phrase_hits >= 2:
            boost = max(boost, 40.0 + phrase_hits * 20.0)
    return boost


def contextual_boost(question: str, text: str, title: str = "") -> float:
    query = normalize_for_match(question)
    haystack = normalize_for_match(f"{title}\n{text}")
    title_key = normalize_for_match(title)
    boost = 0.0
    ticket_intent = any(term in query for term in ["客服", "工单", "处理记录", "用户问题", "客户投诉"])
    fact_intent = any(term in query for term in ["多少", "限制", "标准", "范围", "条件", "支持", "不支持"])
    policy_intent = any(term in query for term in ["政策", "规则", "制度", "条款", "条件", "退款", "套餐", "权限"])
    troubleshooting_intent = any(term in query for term in ["通常", "常见", "原因", "解决方案", "排查", "失败", "错误"])
    calculation_intent = any(term in query for term in ["成本", "价格", "费用", "年费", "月费", "用量", "调用量"])
    relation_intent = any(term in query for term in ["依赖", "影响", "引用", "关系", "共享组件", "复用"])

    if ticket_intent and not fact_intent and not policy_intent and not troubleshooting_intent and (
        "工单" in haystack or "处理记录" in haystack
    ):
        boost += 55.0
    if ticket_intent and "工单" in haystack and "处理记录" in haystack and not fact_intent and not policy_intent and not troubleshooting_intent:
        boost += 45.0
    if ("充值" in query or "少了" in query) and "充值" in haystack and "积分" in haystack and "处理记录" in haystack:
        boost += 80.0
    if ("客服" in query or "建议检查" in query or "工单" in query) and (
        "用户问题" in haystack and "处理记录" in haystack
    ):
        boost += 80.0
    if ("没过期" in query or "没有过期" in query) and (
        "确认没有过期" in haystack or "没有过期" in haystack or "没过期" in haystack
    ):
        boost += 70.0
    if ("roadmap" in query or "ppt" in query) and ("roadmap" in haystack or "ppt" in haystack):
        boost += 45.0
    if "roadmap" in query and "roadmap" in title_key:
        boost += 35.0
    if fact_intent and ("faq" in haystack or "常见问题" in haystack or "问答" in haystack):
        boost += 45.0
    if "积分" in query and any(term in query for term in ["不够", "不足", "补充", "获取", "用完", "继续生成"]) and "积分" in haystack:
        credit_evidence = sum(
            1
            for term in ["购买", "签到", "邀请", "精选", "套餐", "积分包", "获取", "赠送", "充值"]
            if term in haystack
        )
        if credit_evidence >= 2:
            boost += 65.0
    if "429" in query and ("rate_limited" in haystack or "频率限制" in haystack):
        boost += 80.0
    if any(term in query for term in ["超过", "逾期", "第"]) and any(term in haystack for term in ["期限", "时限", "窗口", "天", "小时"]):
        boost += 45.0
    if policy_intent and any(term in haystack for term in ["制度", "政策", "规则", "条款", "审批", "权限"]):
        boost += 35.0
    if troubleshooting_intent and (
        "错误" in haystack or "失败" in haystack or "原因" in haystack or "解决方案" in haystack or "排查" in haystack
    ):
        boost += 60.0
    if calculation_intent and any(
        term in haystack for term in ["定价", "年费", "价格", "月费", "api调用月", "api调用/月"]
    ):
        boost += 70.0
    if relation_intent and any(
        term in haystack for term in ["依赖", "调用", "引用", "webhook", "api", "模块", "组件", "影响"]
    ):
        boost += 45.0
    if any(term in query for term in ["隐私", "合规", "审计", "数据安全"]):
        privacy_evidence = sum(
            1
            for term in ["隐私", "合规", "审计", "数据", "加密", "保留", "安全", "训练", "上传", "素材", "声明", "目的", "期限", "个人网盘"]
            if term in haystack
        )
        if "数据" in haystack and privacy_evidence >= 4:
            boost += 75.0
        elif privacy_evidence >= 3:
            boost += 45.0
    if "套餐" in query and ("faq" in haystack or "定价" in haystack or "套餐" in haystack):
        boost += 20.0
    return boost


def is_generic_lookup_table(line: str, full_text: str = "") -> bool:
    compact = normalize_for_match(line)
    full = normalize_for_match(full_text)
    generic_sets = [
        ("http", "错误码", "解决方案"),
        ("错误码", "说明", "解决方案"),
        ("状态码", "类别", "说明"),
    ]
    if any(all(part in compact for part in parts) for parts in generic_sets):
        return True
    if "错误类型速查表" in full and all(part in compact for part in ["错误信息", "常见原因", "解决方案"]):
        return True
    return False


def embed_text(text: str, *, dimension: int = EMBEDDING_DIMENSION) -> list[float]:
    """Deterministic local embedding for baseline RAG without external APIs."""
    tokens = tokenize(text)
    if not tokens:
        return [0.0] * dimension

    vector = [0.0] * dimension
    for token in tokens:
        bucket = _stable_hash(token) % dimension
        sign = 1.0 if _stable_hash(f"{token}:sign") % 2 == 0 else -1.0
        weight = 1.0 + min(len(token), 8) / 8
        vector[bucket] += sign * weight

    norm = sum(value * value for value in vector) ** 0.5
    if norm == 0:
        return vector
    return [round(value / norm, 6) for value in vector]


def embed_text_with_model(text: str, settings: Settings | None = None) -> tuple[list[float], str]:
    provider = (getattr(settings, "rag_embedding_provider", "local-hash") if settings else "local-hash").lower()
    model_name = getattr(settings, "rag_embedding_model", "local-hash-v1") if settings else "local-hash-v1"
    dimension = int(getattr(settings, "rag_embedding_dimension", EMBEDDING_DIMENSION) if settings else EMBEDDING_DIMENSION)
    if provider in {"local", "local-hash", "local_hash", "hash"}:
        if dimension == EMBEDDING_DIMENSION:
            return embed_text(text), model_name or "local-hash-v1"
        return embed_text(text, dimension=dimension), model_name or "local-hash-v1"
    if provider in {"sentence-transformers", "sentence_transformers", "bge-m3", "bge_m3"}:
        transformer = _load_sentence_transformer(model_name)
        encoded = transformer.encode([text], normalize_embeddings=True)
        vector = _coerce_vector(encoded[0] if encoded else [])
        return vector, model_name
    raise ValueError(f"unsupported rag_embedding_provider: {provider}")


@lru_cache(maxsize=4)
def _load_sentence_transformer(model_name: str):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "rag_embedding_provider=sentence-transformers requires the optional sentence-transformers package"
        ) from exc
    return SentenceTransformer(model_name)


def _coerce_vector(values: Any) -> list[float]:
    if hasattr(values, "tolist"):
        values = values.tolist()
    return [round(float(value), 6) for value in values]


def cosine_similarity(left: list[float] | None, right: list[float] | None) -> float:
    if not left or not right:
        return 0.0
    length = min(len(left), len(right))
    if length == 0:
        return 0.0
    return float(sum(left[index] * right[index] for index in range(length)))


def hybrid_score(
    keyword_score: int | float,
    vector_score: float,
    *,
    keyword_weight: float = 1.0,
    vector_weight: float = 12.0,
) -> float:
    return round(float(keyword_score) * keyword_weight + max(vector_score, 0.0) * vector_weight, 6)


def clip_snippet(text: str, tokens: list[str], limit: int = 220) -> str:
    body = re.sub(r"---.*?---", "", text, count=1, flags=re.S)
    body = re.sub(r"\s+", " ", body).strip()
    lowered = body.lower()
    first_pos = min((lowered.find(t) for t in tokens if t and lowered.find(t) >= 0), default=0)
    start = max(0, first_pos - 40)
    snippet = body[start : start + limit].strip()
    return snippet + ("..." if len(body) > start + limit else "")


def upsert_document_chunks(
    settings: Settings,
    chunks: list[dict[str, Any]],
    conn: Any | None = None,
) -> None:
    timestamp = now_iso()
    if conn is not None:
        for chunk in chunks:
            _upsert_chunk(conn, chunk, timestamp, settings)
        upsert_configured_rag_store(settings, chunks)
        return

    init_app_db(settings)
    with connect_app(settings) as owned_conn:
        for chunk in chunks:
            _upsert_chunk(owned_conn, chunk, timestamp, settings)
    upsert_configured_rag_store(settings, chunks)


def _upsert_chunk(conn: Any, chunk: dict[str, Any], timestamp: str, settings: Settings) -> None:
    metadata = chunk.get("metadata") or {}
    domain = metadata.get("domain") or ""
    title = metadata.get("title") or ""
    text = chunk.get("text") or ""
    tokens = tokenize(f"{title}\n{text}")
    embedding, embedding_model = embed_text_with_model(f"{title}\n{text}", settings=settings)
    conn.execute(
        """
        INSERT INTO document_chunks(
          id, source_id, domain, title, chunk_index, text, token_json,
          metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          source_id = excluded.source_id,
          domain = excluded.domain,
          title = excluded.title,
          chunk_index = excluded.chunk_index,
          text = excluded.text,
          token_json = excluded.token_json,
          metadata_json = excluded.metadata_json,
          updated_at = excluded.updated_at
        """,
        (
            chunk["id"],
            chunk["source_id"],
            domain,
            title,
            chunk["chunk_index"],
            text,
            json_dump(tokens),
            json_dump({**metadata, "embedding": embedding, "embedding_model": embedding_model}),
            timestamp,
            timestamp,
        ),
    )


def search_chunks(
    settings: Settings,
    question: str,
    domain: str | None = None,
    limit: int = 5,
    *,
    keyword_weight: float = 1.0,
    vector_weight: float = 12.0,
    alias_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if settings.rag_store_backend == "postgres":
        from app.pg_rag import search_pg_chunks

        return search_pg_chunks(
            settings,
            question,
            domain,
            limit,
            keyword_weight=keyword_weight,
            vector_weight=vector_weight,
            alias_context=alias_context,
        )

    init_app_db(settings)
    with connect_app(settings) as conn:
        params: list[object] = []
        query = "SELECT * FROM document_chunks"
        if domain:
            query += " WHERE domain = ?"
            params.append(domain)
        rows = rows_to_dicts(conn.execute(query, params).fetchall())

    ranked = rank_search_rows(
        rows,
        question,
        keyword_weight=keyword_weight,
        vector_weight=vector_weight,
        alias_context=alias_context,
        settings=settings,
    )
    return diversify_ranked_rows(ranked, limit)


def upsert_configured_rag_store(settings: Settings, chunks: list[dict[str, Any]]) -> None:
    if settings.rag_store_backend != "postgres":
        return

    from app.pg_rag import upsert_pg_document_chunks

    upsert_pg_document_chunks(settings, chunks)


def delete_document_chunks(settings: Settings, source_id: str, conn: Any | None = None) -> int:
    deleted = 0
    if conn is not None:
        cursor = conn.execute("DELETE FROM document_chunks WHERE source_id = ?", (source_id,))
        deleted = cursor.rowcount if cursor.rowcount is not None else 0
    else:
        init_app_db(settings)
        with connect_app(settings) as owned_conn:
            cursor = owned_conn.execute("DELETE FROM document_chunks WHERE source_id = ?", (source_id,))
            deleted = cursor.rowcount if cursor.rowcount is not None else 0

    if settings.rag_store_backend == "postgres":
        from app.pg_rag import delete_pg_document_chunks

        delete_pg_document_chunks(settings, source_id)
    return deleted


def _stable_hash(value: str) -> int:
    import hashlib

    return int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:16], 16)
