from __future__ import annotations

import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.answer_modes import AnswerModeConfig, get_answer_mode_config
from app.aliases import build_alias_context, expand_query_with_aliases
from app.config import Settings
from app.db import audit, connect_app, init_app_db, json_dump
from app.gbrain import GBrainHit, query_gbrain
from app.llm import generate_answer
from app.models import AskRequest, AskResponse, Citation, MemoryHit
from app.rag import clip_snippet, score_text, search_chunks, tokenize
from app.timeutil import now_iso
from app.vault import append_log


def load_page_text(vault_path: Path, rel_path: str) -> str:
    path = vault_path / rel_path
    return path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""


def find_wiki_page_by_source(conn, source_id: str, domain: str | None = None) -> dict | None:
    params: list[object] = [f'%"{source_id}"%']
    query = "SELECT * FROM wiki_pages WHERE source_ids_json LIKE ?"
    if domain:
        query += " AND domain = ?"
        params.append(domain)
    query += " ORDER BY updated_at DESC LIMIT 1"
    row = conn.execute(query, params).fetchone()
    return dict(row) if row else None


def active_source_ids(conn, source_ids: list[str]) -> list[str]:
    active: list[str] = []
    for source_id in source_ids:
        row = conn.execute("SELECT status FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is not None and row["status"] != "deleted":
            active.append(source_id)
    return active


def ask(settings: Settings, request: AskRequest) -> AskResponse:
    init_app_db(settings)
    timestamp = now_iso()
    query_id = f"qry_{uuid.uuid4().hex[:12]}"
    mode_config = get_answer_mode_config(request.answer_mode)
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
            alias_context=alias_context,
        )
        gbrain_future = executor.submit(query_gbrain, settings, request.question, settings.gbrain_query_limit)
        chunk_hits = chunk_future.result()
        try:
            gbrain_hits = gbrain_future.result()
        except Exception:
            gbrain_hits = []

    with connect_app(settings) as conn:
        citations: list[Citation] = []
        answer_parts: list[str] = []
        context_blocks: list[str] = []
        memory_hits = search_query_memory(conn, request, tokens, mode_config)
        for item in chunk_hits[: mode_config.context_limit]:
            source_id = item["source_id"]
            page = find_wiki_page_by_source(conn, source_id, request.domain)
            page_path = page["path"] if page else None
            snippet = item["snippet"]
            context_text = clip_context(item["text"])
            citations.append(
                Citation(
                    source_id=source_id,
                    wiki_page=page_path,
                    snippet=snippet,
                )
            )
            title = page["title"] if page else item["title"]
            answer_parts.append(f"- {title}: {snippet}")
            context_blocks.append(
                f"标题：{title}\n页面：{page_path or '未生成知识页'}\n来源：{source_id}\n资料正文：\n{context_text}"
            )

        gbrain_context_blocks = build_gbrain_context(gbrain_hits, mode_config.context_limit)
        context_blocks.extend(gbrain_context_blocks)
        answer_parts.extend(build_gbrain_answer_parts(gbrain_hits, mode_config.context_limit))

        if not citations:
            citations, answer_parts, context_blocks = fallback_wiki_search(conn, settings, request, tokens, mode_config)
            context_blocks.extend(gbrain_context_blocks)
            answer_parts.extend(build_gbrain_answer_parts(gbrain_hits, mode_config.context_limit))

        missing_info: list[str] = []
        if not citations and not gbrain_context_blocks:
            answer = "当前知识库没有找到足够依据回答这个问题。建议补充相关产品/客服资料后重新编译知识库。"
            confidence = "low"
            missing_info.append(request.question)
        else:
            memory_blocks = build_memory_context(memory_hits)
            generated = generate_answer(
                settings,
                request.question,
                context_blocks,
                mode_config.key,
                memory_blocks=memory_blocks,
            )
            answer = generated or build_local_answer(mode_config, answer_parts, memory_hits)
            best_score = chunk_hits[0]["score"] if chunk_hits else 0
            confidence = "high" if best_score >= mode_config.confidence_high else "medium"
            if not chunk_hits and citations:
                confidence = "medium"
            if not chunk_hits and gbrain_context_blocks:
                confidence = "medium"

        retrieval_strategy = {
            "answer_mode": mode_config.key,
            "mode_label": mode_config.label,
            "chunk_hits": len(chunk_hits),
            "context_limit": mode_config.context_limit,
            "memory_hits": len(memory_hits),
            "gbrain_hits": len(gbrain_hits),
            "gbrain_enabled": settings.gbrain_enabled,
            "matched_aliases": [
                {
                    "domain": item.get("domain"),
                    "canonical_name": item.get("canonical_name"),
                    "alias": item.get("alias"),
                    "entity_type": item.get("entity_type"),
                }
                for item in matched_aliases
            ],
            "keyword_weight": mode_config.keyword_weight,
            "vector_weight": mode_config.vector_weight,
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
                }
                for item in chunk_hits[: mode_config.context_limit]
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
    )


def fallback_wiki_search(
    conn,
    settings: Settings,
    request: AskRequest,
    tokens: list[str],
    mode_config: AnswerModeConfig,
):
    params: list[object] = []
    query = "SELECT * FROM wiki_pages"
    if request.domain:
        query += " WHERE domain = ?"
        params.append(request.domain)
    pages = conn.execute(query, params).fetchall()
    ranked: list[dict] = []
    for page in pages:
        text = load_page_text(settings.vault_path, page["path"])
        source_ids = __import__("json").loads(page["source_ids_json"] or "[]")
        active_ids = active_source_ids(conn, source_ids)
        if not active_ids:
            continue
        score = score_text(tokens, text, title=page["title"])
        if score <= 0 and tokens:
            continue
        ranked.append({"page": dict(page), "text": text, "source_ids": active_ids, "score": score})

    ranked.sort(key=lambda item: item["score"], reverse=True)
    citations: list[Citation] = []
    answer_parts: list[str] = []
    context_blocks: list[str] = []
    for item in ranked[: mode_config.context_limit]:
        source_id = item["source_ids"][0] if item["source_ids"] else ""
        snippet = clip_snippet(item["text"], tokens)
        citations.append(Citation(source_id=source_id, wiki_page=item["page"]["path"], snippet=snippet))
        answer_parts.append(f"- {item['page']['title']}: {snippet}")
        context_blocks.append(
            f"标题：{item['page']['title']}\n页面：{item['page']['path']}\n来源：{source_id}\n片段：{snippet}"
        )
    return citations, answer_parts, context_blocks


def search_query_memory(
    conn,
    request: AskRequest,
    tokens: list[str],
    mode_config: AnswerModeConfig,
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
