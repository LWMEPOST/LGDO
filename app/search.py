from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.aliases import build_alias_context, expand_query_with_aliases
from app.answer_modes import AnswerModeConfig, get_answer_mode_config
from app.auth import UserContext, apply_request_user_override, can_read_metadata
from app.citations import (
    MappedEvidence,
    all_sources_readable,
    dedupe_citations,
    map_gbrain_hit,
    map_local_hit,
)
from app.config import Settings
from app.db import audit, connect_app, init_app_db, json_dump
from app.external_embedding import ExternalEmbeddingError, rerank_rows_with_dashscope
from app.gbrain import GBrainHit, GBrainQueryResult, query_gbrain as _query_gbrain, query_gbrain_with_diagnostics
from app.llm import generate_answer, stream_generate_answer
from app.models import AskRequest, AskResponse, Citation, MemoryHit
from app.rag import clip_snippet, score_text, search_chunks, tokenize
from app.timeutil import now_iso
from app.vault import append_log

query_gbrain = _query_gbrain

CITATION_REFUSAL = (
    "当前用户权限范围内没有找到足够依据回答这个问题。"
    "请确认资料 ACL 标签或补充授权资料。"
)
NO_EVIDENCE_REFUSAL = (
    "当前知识库没有找到足够依据回答这个问题。"
    "建议补充相关产品/客服资料后重新编译知识库。"
)


def _query_gbrain_for_search(settings: Settings, question: str, limit: int) -> GBrainQueryResult:
    if query_gbrain is not _query_gbrain:
        return GBrainQueryResult(query_gbrain(settings, question, limit))
    return query_gbrain_with_diagnostics(settings, question, limit)

def find_wiki_page_by_source(conn, source_id: str, domain: str | None = None) -> dict | None:
    params: list[object] = [f'%"{source_id}"%']
    query = "SELECT * FROM wiki_pages WHERE source_ids_json LIKE ?"
    if domain:
        query += " AND domain = ?"
        params.append(domain)
    query += " ORDER BY updated_at DESC LIMIT 1"
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def active_source_ids(conn, source_ids: list[str], user_context: UserContext | None = None) -> list[str]:
    active: list[str] = []
    for source_id in source_ids:
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is not None and row["status"] != "deleted":
            metadata = __import__("json").loads(row["metadata_json"] or "{}")
            if not can_read_metadata(metadata, user_context, row["owner"]):
                continue
            active.append(source_id)
    return active


@dataclass
class AskAssembly:
    query_id: str
    timestamp: str
    request: AskRequest
    mode_config: AnswerModeConfig
    resolved_user: UserContext | None
    chunk_hits: list[dict[str, Any]]
    gbrain_hits: list[GBrainHit]
    memory_hits: list[MemoryHit]
    citations: list[Citation]
    answer_parts: list[str]
    context_blocks: list[str]
    gbrain_context_blocks: list[str]
    missing_info: list[str]
    confidence: str
    retrieval_strategy: dict[str, Any]
    answer_override: str | None = None


def build_ask_assembly(
    settings: Settings,
    request: AskRequest,
    user_context: UserContext | None = None,
) -> AskAssembly:
    init_app_db(settings)
    timestamp = now_iso()
    query_id = f"qry_{uuid.uuid4().hex[:12]}"
    mode_config = get_answer_mode_config(request.answer_mode)
    resolved_user = apply_request_user_override(settings, user_context, request) if user_context else None
    try:
        expanded_question, matched_aliases = expand_query_with_aliases(settings, request.question, request.domain)
    except Exception:
        expanded_question, matched_aliases = request.question, []
    alias_context = build_alias_context(request.question, matched_aliases) if matched_aliases else None
    tokens = tokenize(expanded_question)
    with ThreadPoolExecutor(max_workers=2) as executor:
        chunk_future = executor.submit(
            search_chunks,
            settings,
            expanded_question,
            request.domain,
            mode_config.retrieval_limit,
            keyword_weight=mode_config.keyword_weight,
            vector_weight=mode_config.vector_weight,
            user_context=resolved_user,
            alias_context=alias_context,
        )
        gbrain_future = executor.submit(
            _query_gbrain_for_search, settings, expanded_question, settings.gbrain_query_limit
        )
        chunk_hits = chunk_future.result()
        try:
            gbrain_result = gbrain_future.result()
        except Exception:
            gbrain_result = GBrainQueryResult([], reason="exception")

    raw_gbrain_hits = gbrain_result.hits
    external_diagnostics: dict[str, Any] | None = None

    with connect_app(settings) as conn:
        mapped_evidence: list[MappedEvidence] = []
        memory_hits = search_query_memory(conn, request, tokens, mode_config, resolved_user)
        gbrain_diagnostics = {
            "mapped": 0,
            "stale": 0,
            "unauthorized": 0,
            "unmapped": 0,
        }
        mapped_gbrain: list[tuple[GBrainHit, MappedEvidence]] = []
        for hit in raw_gbrain_hits:
            evidence = map_gbrain_hit(conn, hit, resolved_user)
            gbrain_diagnostics[evidence.diagnostic] += 1
            if evidence.diagnostic == "mapped":
                mapped_gbrain.append((hit, evidence))
        gbrain_hits = [hit for hit, _ in mapped_gbrain]
        if not gbrain_hits and settings.dashscope_embedding_enabled and chunk_hits:
            try:
                chunk_hits, external_diagnostics = rerank_rows_with_dashscope(
                    settings,
                    expanded_question,
                    chunk_hits,
                )
            except ExternalEmbeddingError as exc:
                external_diagnostics = {
                    "channel": "bm25_keyword",
                    "degraded_from": "dashscope_embedding",
                    "reason": str(exc),
                }
        mapped_chunk_hits: list[dict[str, Any]] = []
        for item in chunk_hits[: mode_config.context_limit]:
            evidence = map_local_hit(conn, item, resolved_user)
            if evidence.diagnostic != "mapped" or not evidence.citations:
                continue
            mapped_chunk_hits.append(item)
            mapped_evidence.append(evidence)

        visible_gbrain = mapped_gbrain[: mode_config.context_limit]
        gbrain_context_blocks = [evidence.context for _, evidence in visible_gbrain]
        for _, evidence in visible_gbrain:
            if evidence.citations:
                mapped_evidence.append(evidence)

        if not mapped_evidence:
            mapped_evidence.extend(fallback_wiki_search(
                conn,
                request,
                tokens,
                mode_config,
                resolved_user,
            ))

        citations = dedupe_citations(
            [
                citation
                for evidence in mapped_evidence
                if evidence.diagnostic == "mapped" and evidence.citations
                for citation in evidence.citations
            ]
        )
        citations = dedupe_citations(
            filter_authorized_citations(conn, citations, resolved_user)
        )
        answer_parts = [
            evidence.answer_part
            for evidence in mapped_evidence
            if evidence.diagnostic == "mapped" and evidence.citations
        ]
        context_blocks = [
            evidence.context
            for evidence in mapped_evidence
            if evidence.diagnostic == "mapped" and evidence.citations
        ]

        missing_info: list[str] = []
        answer_override: str | None = None
        if request.require_citations and not citations:
            answer_override = CITATION_REFUSAL
            confidence = "low"
            missing_info = [request.question]
        elif not citations and not memory_hits:
            answer_override = NO_EVIDENCE_REFUSAL
            confidence = "low"
            missing_info = [request.question]
        else:
            best_score = chunk_hits[0]["score"] if chunk_hits else 0
            confidence = "high" if best_score >= mode_config.confidence_high else "medium"
            if not chunk_hits and citations:
                confidence = "medium"
            if not chunk_hits and memory_hits:
                confidence = "medium"

        retrieval_strategy = {
            "answer_mode": mode_config.key,
            "mode_label": mode_config.label,
            "chunk_hits": len(chunk_hits),
            "authorized_chunk_hits": len(mapped_chunk_hits),
            "context_limit": mode_config.context_limit,
            "memory_hits": len(memory_hits),
            "memory_origin": (
                "query_expansion_only"
                if request.require_citations and memory_hits
                else "authorized_history"
                if memory_hits
                else None
            ),
            "gbrain_hits": len(gbrain_hits),
            "gbrain_raw_hits": len(raw_gbrain_hits),
            "gbrain_mapped": gbrain_diagnostics["mapped"],
            "gbrain_stale": gbrain_diagnostics["stale"],
            "gbrain_unauthorized": gbrain_diagnostics["unauthorized"],
            "gbrain_unmapped": gbrain_diagnostics["unmapped"],
            "gbrain_enabled": settings.gbrain_enabled,
            "gbrain_reason": gbrain_result.reason if gbrain_result else "exception",
            "gbrain_circuit_open": gbrain_result.circuit_open if gbrain_result else False,
            "gbrain_cache_hit": gbrain_result.cache_hit if gbrain_result else False,
            "gbrain_latency_ms": gbrain_result.latency_ms if gbrain_result else 0,
            "fallback_retrieval": external_diagnostics,
            "keyword_weight": mode_config.keyword_weight,
            "vector_weight": mode_config.vector_weight,
            "alias_expanded": expanded_question != request.question,
            "matched_aliases": [
                {
                    "canonical_name": item.get("canonical_name"),
                    "alias": item.get("alias"),
                    "domain": item.get("domain"),
                    "entity_type": item.get("entity_type"),
                }
                for item in matched_aliases
            ],
            "top_hits": [
                {
                    "source_id": item.get("source_id"),
                    "title": item.get("title"),
                    "score": item.get("score"),
                    "keyword_score": item.get("keyword_score"),
                    "bm25_score": item.get("bm25_score"),
                    "vector_score": item.get("vector_score"),
                    "phrase_boost": item.get("phrase_boost"),
                    "table_boost": item.get("table_boost"),
                    "context_boost": item.get("context_boost"),
                    "doc_code_boost": item.get("doc_code_boost"),
                    "alias_boost": item.get("alias_boost"),
                    "rrf_score": item.get("rrf_score"),
                    "external_vector_score": item.get("external_vector_score"),
                    "external_rrf_score": item.get("external_rrf_score"),
                }
                for item in mapped_chunk_hits
            ],
            "gbrain_top_hits": [
                {
                    "slug": hit.slug,
                    "title": hit.title,
                    "score": hit.score,
                    "source_id": hit.source_id,
                    "page_type": hit.page_type,
                    "chunk_id": hit.chunk_id,
                    "relational_path": hit.relational_path,
                    "relational_via_link_types": hit.relational_via_link_types,
                }
                for hit in gbrain_hits[: mode_config.context_limit]
            ],
        }

    return AskAssembly(
        query_id=query_id,
        timestamp=timestamp,
        request=request,
        mode_config=mode_config,
        resolved_user=resolved_user,
        chunk_hits=chunk_hits,
        gbrain_hits=gbrain_hits,
        memory_hits=memory_hits,
        citations=citations,
        answer_parts=answer_parts,
        context_blocks=context_blocks,
        gbrain_context_blocks=gbrain_context_blocks,
        missing_info=missing_info,
        confidence=confidence,
        retrieval_strategy=retrieval_strategy,
        answer_override=answer_override,
    )


def _memory_blocks_for_assembly(assembly: AskAssembly) -> list[str]:
    if assembly.request.require_citations:
        return []
    return build_memory_context(assembly.memory_hits)


def _memory_hits_for_answer(assembly: AskAssembly) -> list[MemoryHit]:
    return [] if assembly.request.require_citations else assembly.memory_hits


def _refresh_assembly_citations(settings: Settings, assembly: AskAssembly) -> bool:
    with connect_app(settings) as conn:
        assembly.citations = revalidate_citations(
            conn,
            assembly.citations,
            assembly.resolved_user,
        )
    assembly.retrieval_strategy["post_generation_citations"] = len(assembly.citations)
    if assembly.request.require_citations and not assembly.citations:
        assembly.answer_override = CITATION_REFUSAL
        assembly.confidence = "low"
        assembly.missing_info = [assembly.request.question]
        return False
    return True


def finalize_ask_response(settings: Settings, assembly: AskAssembly, answer: str) -> AskResponse:
    citations_valid = _refresh_assembly_citations(settings, assembly)
    if not citations_valid:
        answer = CITATION_REFUSAL
    request = assembly.request
    mode_config = assembly.mode_config
    resolved_user = assembly.resolved_user
    citations = assembly.citations
    memory_hits = assembly.memory_hits
    confidence = assembly.confidence
    missing_info = assembly.missing_info
    retrieval_strategy = assembly.retrieval_strategy
    timestamp = assembly.timestamp
    query_id = assembly.query_id

    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO query_logs(
              id, question, domain, answer, citations_json, confidence, missing_info_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                query_id,
                request.question,
                request.domain,
                answer,
                json_dump([citation.model_dump() for citation in citations]),
                confidence,
                json_dump(missing_info),
                timestamp,
            ),
        )
        audit(
            conn,
            "question_answered",
            {
                "query_id": query_id,
                "question": request.question,
                "answer_mode": mode_config.key,
                "citation_count": len(citations),
                "confidence": confidence,
                "memory_hits": len(memory_hits),
                "user_context": resolved_user.to_public_dict() if resolved_user else None,
                "alias_expanded": retrieval_strategy.get("alias_expanded", False),
            },
            timestamp,
        )

    append_log(
        settings.vault_path,
        "qa_eval_log.md",
        f"- {timestamp} {query_id}: confidence={confidence} citations={len(citations)} question={request.question}",
    )
    return AskResponse(
        query_id=query_id,
        answer=answer,
        citations=citations,
        confidence=confidence,
        missing_info=missing_info,
        memory_hits=memory_hits,
        retrieval_strategy=retrieval_strategy,
        user_context=resolved_user.to_public_dict() if resolved_user else {},
    )


def ask(settings: Settings, request: AskRequest, user_context: UserContext | None = None) -> AskResponse:
    assembly = build_ask_assembly(settings, request, user_context)
    if assembly.answer_override:
        answer = assembly.answer_override
    else:
        generated = generate_answer(
            settings,
            request.question,
            assembly.context_blocks,
            assembly.mode_config.key,
            memory_blocks=_memory_blocks_for_assembly(assembly),
        )
        answer = generated or build_local_answer(
            assembly.mode_config,
            assembly.answer_parts,
            _memory_hits_for_answer(assembly),
        )
    return finalize_ask_response(settings, assembly, answer)


def _ndjson_event(payload: dict[str, Any]) -> str:
    return f"{json_dump(payload)}\n"


def stream_ask_events(
    settings: Settings,
    request: AskRequest,
    user_context: UserContext | None = None,
) -> Iterator[str]:
    assembly = build_ask_assembly(settings, request, user_context)
    chunks: list[str] = []
    if assembly.answer_override:
        chunks.append(assembly.answer_override)
    else:
        for chunk in stream_generate_answer(
            settings,
            request.question,
            assembly.context_blocks,
            assembly.mode_config.key,
            memory_blocks=_memory_blocks_for_assembly(assembly),
        ):
            if not chunk:
                continue
            chunks.append(chunk)
        if not chunks:
            fallback = build_local_answer(
                assembly.mode_config,
                assembly.answer_parts,
                _memory_hits_for_answer(assembly),
            )
            chunks.append(fallback)

    if not _refresh_assembly_citations(settings, assembly):
        chunks = [CITATION_REFUSAL]

    yield _ndjson_event(
        {
            "event": "metadata",
            "query_id": assembly.query_id,
            "confidence": assembly.confidence,
            "citations": [citation.model_dump() for citation in assembly.citations],
            "retrieval_strategy": assembly.retrieval_strategy,
        }
    )
    for chunk in chunks:
        yield _ndjson_event({"event": "answer_delta", "text": chunk})

    response = finalize_ask_response(settings, assembly, "".join(chunks))
    yield _ndjson_event({"event": "done", "response": response.model_dump()})


def fallback_wiki_search(
    conn,
    request: AskRequest,
    tokens: list[str],
    mode_config: AnswerModeConfig,
    user_context: UserContext | None = None,
) -> list[MappedEvidence]:
    params: list[object] = []
    query = """
        SELECT wp.*, wr.content AS revision_content
        FROM wiki_pages wp
        JOIN wiki_page_revisions wr
          ON wr.id = wp.current_revision_id AND wr.page_id = wp.page_id
        WHERE wp.lifecycle_status = 'active'
          AND wp.rag_visible_revision_id = wp.current_revision_id
          AND wp.rag_visible_epoch = wp.projection_epoch
    """
    if request.domain:
        query += " AND wp.domain = ?"
        params.append(request.domain)
    pages = conn.execute(query, params).fetchall()
    ranked: list[dict] = []
    for page in pages:
        text = str(page["revision_content"] or "")
        score = score_text(tokens, text, title=page["title"])
        if score <= 0 and tokens:
            continue
        ranked.append({"page": dict(page), "text": text, "score": score})

    ranked.sort(key=lambda item: item["score"], reverse=True)
    evidence_rows: list[MappedEvidence] = []
    for item in ranked[: mode_config.context_limit]:
        snippet = clip_snippet(item["text"], tokens)
        evidence = map_local_hit(
            conn,
            {
                "origin": "wiki",
                "id": (
                    f"fallback:{item['page']['page_id']}:"
                    f"{item['page']['current_revision_id']}:"
                    f"{item['page']['projection_epoch']}"
                ),
                "page_id": item["page"]["page_id"],
                "revision_id": item["page"]["current_revision_id"],
                "projection_epoch": item["page"]["projection_epoch"],
                "page_path": item["page"]["path"],
                "title": item["page"]["title"],
                "snippet": snippet,
                "text": item["text"],
            },
            user_context,
        )
        if evidence.diagnostic == "mapped" and evidence.citations:
            evidence_rows.append(evidence)
    return evidence_rows


def search_query_memory(
    conn,
    request: AskRequest,
    tokens: list[str],
    mode_config: AnswerModeConfig,
    user_context: UserContext | None = None,
) -> list[MemoryHit]:
    if mode_config.memory_limit <= 0 or not tokens:
        return []

    params: list[object] = []
    query = "SELECT * FROM query_logs WHERE confidence != 'low'"
    if request.domain:
        query += " AND domain = ?"
        params.append(request.domain)
    query += " ORDER BY created_at DESC LIMIT 80"
    rows = conn.execute(query, params).fetchall()

    ranked: list[MemoryHit] = []
    for row in rows:
        if not query_log_is_authorized(conn, row["citations_json"] or "[]", user_context):
            continue
        row_question = row["question"] or ""
        if row_question.strip() == request.question.strip():
            continue
        question_score = score_text(tokens, row_question)
        answer_score = score_text(tokens, row["answer"] or "")
        score = round(question_score * 1.4 + answer_score * 0.35, 6)
        if score <= 0:
            continue
        ranked.append(
            MemoryHit(
                query_id=row["id"],
                question=row_question,
                answer_snippet=clip_answer(row["answer"] or ""),
                score=score,
                confidence=row["confidence"],
                created_at=row["created_at"],
            )
        )

    ranked.sort(key=lambda item: (item.score, item.created_at), reverse=True)
    return ranked[: mode_config.memory_limit]


def can_read_source_id(conn, source_id: str | None, user_context: UserContext | None = None) -> bool:
    if not source_id:
        return False
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None or row["status"] != "active":
        return False
    metadata = __import__("json").loads(row["metadata_json"] or "{}")
    return can_read_metadata(metadata, user_context, row["owner"])


def filter_authorized_citations(
    conn,
    citations: list[Citation],
    user_context: UserContext | None = None,
) -> list[Citation]:
    return [citation for citation in citations if can_read_source_id(conn, citation.source_id, user_context)]


def filter_authorized_gbrain_hits(
    conn,
    hits: list[GBrainHit],
    user_context: UserContext | None = None,
) -> list[GBrainHit]:
    return [
        hit
        for hit in hits
        if map_gbrain_hit(conn, hit, user_context).diagnostic == "mapped"
    ]


def revalidate_citations(
    conn,
    citations: list[Citation],
    user_context: UserContext | None = None,
) -> list[Citation]:
    if not citations:
        return []

    valid_indexes: set[int] = set()
    page_groups: dict[tuple[str | None, ...], list[tuple[int, Citation]]] = {}
    for index, citation in enumerate(citations):
        if citation.origin not in {"wiki", "gbrain"}:
            if all_sources_readable(conn, (citation.source_id,), user_context):
                valid_indexes.add(index)
            continue
        key = (
            citation.origin,
            citation.wiki_page,
            citation.page_id,
            citation.revision_id,
            citation.chunk_id,
            citation.snippet,
        )
        page_groups.setdefault(key, []).append((index, citation))

    for group in page_groups.values():
        if _page_citation_group_is_current(conn, group, user_context):
            valid_indexes.update(index for index, _ in group)
    return [
        citation
        for index, citation in enumerate(citations)
        if index in valid_indexes
    ]


def _page_citation_group_is_current(
    conn,
    group: list[tuple[int, Citation]],
    user_context: UserContext | None,
) -> bool:
    first = group[0][1]
    if not first.page_id or not first.revision_id or not first.wiki_page:
        return False
    pages = conn.execute(
        "SELECT * FROM wiki_pages WHERE page_id = ?",
        (first.page_id,),
    ).fetchall()
    if len(pages) != 1:
        return False
    page = dict(pages[0])
    if (
        page.get("lifecycle_status") != "active"
        or page.get("current_revision_id") != first.revision_id
        or page.get("path") != first.wiki_page
    ):
        return False

    revision = conn.execute(
        """
        SELECT source_ids_json FROM wiki_page_revisions
        WHERE id = ? AND page_id = ?
        """,
        (first.revision_id, first.page_id),
    ).fetchone()
    if revision is None:
        return False
    try:
        stored_source_ids = json.loads(revision["source_ids_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(stored_source_ids, list):
        return False
    expected_sources = tuple(
        dict.fromkeys(str(source_id) for source_id in stored_source_ids if source_id)
    )
    actual_sources = tuple(
        dict.fromkeys(citation.source_id for _, citation in group if citation.source_id)
    )
    if (
        not expected_sources
        or set(actual_sources) != set(expected_sources)
        or not all_sources_readable(conn, expected_sources, user_context)
    ):
        return False

    if first.origin == "wiki":
        return bool(
            page.get("rag_visible_revision_id") == first.revision_id
            and page.get("rag_visible_epoch") is not None
            and int(page["rag_visible_epoch"]) == int(page["projection_epoch"])
        )

    mapping = conn.execute(
        """
        SELECT COUNT(*) AS count FROM gbrain_page_projections
        WHERE page_id = ? AND revision_id = ? AND projection_epoch = ?
          AND page_path = ? AND status = 'current'
        """,
        (
            first.page_id,
            first.revision_id,
            page["projection_epoch"],
            first.wiki_page,
        ),
    ).fetchone()
    return bool(mapping is not None and int(mapping["count"]) >= 1)


def query_log_is_authorized(conn, citations_json: str, user_context: UserContext | None = None) -> bool:
    try:
        payload = json.loads(citations_json or "[]")
        if not isinstance(payload, list) or not payload:
            return False
        citations = [Citation.model_validate(item) for item in payload]
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return len(revalidate_citations(conn, citations, user_context)) == len(citations)


def build_memory_context(memory_hits: list[MemoryHit]) -> list[str]:
    return [
        (
            f"历史问题：{hit.question}\n"
            f"历史回答摘要：{hit.answer_snippet}\n"
            f"置信度：{hit.confidence}\n"
            "注意：历史问答只用于复用表达口径，事实依据仍以本次资料片段为准。"
        )
        for hit in memory_hits
    ]


def build_gbrain_context(gbrain_hits: list[GBrainHit], limit: int) -> list[str]:
    blocks: list[str] = []
    for hit in gbrain_hits[:limit]:
        relation = ""
        if hit.relational_path:
            relation = f"\n图谱路径：{' -> '.join(hit.relational_path)}"
        link_types = ""
        if hit.relational_via_link_types:
            link_types = f"\n关系类型：{', '.join(hit.relational_via_link_types)}"
        blocks.append(
            "GBrain 图谱增强资料：\n"
            f"标题：{hit.title}\n"
            f"Slug：{hit.slug or '未提供'}\n"
            f"来源：{hit.source_id or 'gbrain'}\n"
            f"类型：{hit.page_type or 'unknown'}\n"
            f"分数：{hit.score:.6f}"
            f"{relation}{link_types}\n"
            f"资料正文：\n{clip_context(hit.snippet)}"
        )
    return blocks


def build_gbrain_answer_parts(gbrain_hits: list[GBrainHit], limit: int) -> list[str]:
    return [
        f"- GBrain/{hit.title}: {hit.snippet}"
        for hit in gbrain_hits[:limit]
        if hit.snippet
    ]


def build_local_answer(
    mode_config: AnswerModeConfig,
    answer_parts: list[str],
    memory_hits: list[MemoryHit],
) -> str:
    if mode_config.key == "short":
        first = answer_parts[0].removeprefix("- ").strip() if answer_parts else "未找到可引用的资料片段。"
        return f"简短回答：{first}"

    if mode_config.key == "customer_reply_draft":
        bullet_lines = "\n".join(answer_parts[: mode_config.context_limit])
        memory_note = ""
        if memory_hits:
            memory_note = f"\n\n相似历史口径：{memory_hits[0].answer_snippet}"
        return (
            "您好，关于这个问题，当前知识库中可参考的处理口径如下：\n"
            f"{bullet_lines}\n\n"
            "建议您先按上述规则核对关键信息；如场景不完全一致，我会继续为您确认补充依据。"
            f"{memory_note}"
        )

    evidence = "\n".join(answer_parts)
    memory_section = ""
    if memory_hits:
        memory_section = "\n\n历史问答记忆：\n" + "\n".join(
            f"- {hit.question}: {hit.answer_snippet}" for hit in memory_hits
        )
    return f"基于当前已入库资料，建议参考以下内容：\n{evidence}{memory_section}"


def clip_answer(answer: str, limit: int = 180) -> str:
    body = re.sub(r"\s+", " ", answer).strip()
    if len(body) <= limit:
        return body
    return body[:limit].rstrip() + "..."


def clip_context(text: str, limit: int = 1800) -> str:
    body = re.sub(r"\s+\n", "\n", text or "").strip()
    if len(body) <= limit:
        return body
    return body[:limit].rstrip() + "\n..."
