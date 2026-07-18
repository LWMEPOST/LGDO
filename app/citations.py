from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.auth import UserContext, can_read_metadata
from app.gbrain import GBrainHit
from app.models import Citation


EvidenceDiagnostic = Literal["mapped", "stale", "unauthorized", "unmapped"]


@dataclass(frozen=True)
class MappedEvidence:
    citations: Sequence[Citation]
    context: str
    answer_part: str
    diagnostic: EvidenceDiagnostic


def all_sources_readable(
    conn: Any,
    source_ids: Sequence[str],
    user: UserContext | None,
) -> bool:
    unique = tuple(dict.fromkeys(str(source_id) for source_id in source_ids if source_id))
    if not unique:
        return False
    for source_id in unique:
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None or row["status"] != "active":
            return False
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(metadata, dict) or not can_read_metadata(metadata, user, row["owner"]):
            return False
    return True


def map_gbrain_hit(
    conn: Any,
    hit: GBrainHit,
    user: UserContext | None = None,
) -> MappedEvidence:
    snippet = hit.snippet.strip()
    namespace = (hit.gbrain_source_id or "").strip()
    slug = hit.slug.strip()
    if not snippet or not namespace or not slug:
        return _rejected("unmapped")

    mappings = conn.execute(
        """
        SELECT * FROM gbrain_page_projections
        WHERE gbrain_source_id = ? AND slug = ?
        """,
        (namespace, slug),
    ).fetchall()
    if len(mappings) != 1:
        return _rejected("unmapped")
    mapping = dict(mappings[0])
    if mapping["status"] != "current":
        return _rejected("stale")
    if not hit.content_hash or hit.content_hash != mapping["gbrain_content_hash"]:
        return _rejected("stale")
    if hit.page_generation is None or int(hit.page_generation) != int(
        mapping["gbrain_page_generation"]
    ):
        return _rejected("stale")
    source_path = _nonempty_string(hit.source_path)
    if source_path is None or source_path != mapping["source_path"]:
        return _rejected("stale")

    pages = conn.execute(
        "SELECT * FROM wiki_pages WHERE page_id = ?",
        (mapping["page_id"],),
    ).fetchall()
    if len(pages) != 1:
        return _rejected("stale")
    page = dict(pages[0])
    if not _mapping_is_current(mapping, page):
        return _rejected("stale")

    source_ids = _revision_source_ids(
        conn,
        page_id=str(mapping["page_id"]),
        revision_id=str(mapping["revision_id"]),
    )
    if source_ids is None or not all_sources_readable(conn, source_ids, user):
        return _rejected("unauthorized")

    chunk_id = str(hit.chunk_id) if hit.chunk_id is not None else None
    citations = tuple(
        Citation(
            source_id=source_id,
            wiki_page=str(page["path"]),
            snippet=snippet,
            page_id=str(mapping["page_id"]),
            revision_id=str(mapping["revision_id"]),
            chunk_id=chunk_id,
            origin="gbrain",
        )
        for source_id in source_ids
    )
    title = hit.title.strip() or str(page["title"])
    return MappedEvidence(
        citations=citations,
        context=(
            f"标题：{title}\n页面：{page['path']}\n"
            f"来源：{', '.join(source_ids)}\nGBrain 片段：\n{snippet}"
        ),
        answer_part=f"- GBrain/{title}: {snippet}",
        diagnostic="mapped",
    )


def map_local_hit(
    conn: Any,
    hit: Mapping[str, Any],
    user: UserContext | None = None,
) -> MappedEvidence:
    origin = str(hit.get("origin") or "document")
    snippet = str(hit.get("snippet") or hit.get("text") or "").strip()
    if not snippet:
        return _rejected("unmapped")
    if origin == "wiki":
        return _map_wiki_hit(conn, hit, snippet, user)
    if origin == "document":
        return _map_document_hit(conn, hit, snippet, user)
    return _rejected("unmapped")


def dedupe_citations(citations: Sequence[Citation]) -> list[Citation]:
    deduped: list[Citation] = []
    seen: set[tuple[str | None, ...]] = set()
    for citation in citations:
        key = (
            citation.source_id,
            citation.wiki_page,
            citation.page_id,
            citation.revision_id,
            citation.chunk_id,
            citation.origin,
            citation.snippet,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(citation)
    return deduped


def _map_wiki_hit(
    conn: Any,
    hit: Mapping[str, Any],
    snippet: str,
    user: UserContext | None,
) -> MappedEvidence:
    page_id = _nonempty_string(hit.get("page_id"))
    revision_id = _nonempty_string(hit.get("revision_id"))
    projection_epoch = _integer(hit.get("projection_epoch"))
    if page_id is None or revision_id is None or projection_epoch is None:
        return _rejected("unmapped")

    pages = conn.execute(
        "SELECT * FROM wiki_pages WHERE page_id = ?",
        (page_id,),
    ).fetchall()
    if len(pages) != 1:
        return _rejected("stale")
    page = dict(pages[0])
    if not _wiki_hit_is_current(page, revision_id, projection_epoch):
        return _rejected("stale")
    page_path = _nonempty_string(hit.get("page_path"))
    if page_path is not None and page_path != page["path"]:
        return _rejected("stale")

    source_ids = _revision_source_ids(
        conn,
        page_id=page_id,
        revision_id=revision_id,
    )
    if source_ids is None or not all_sources_readable(conn, source_ids, user):
        return _rejected("unauthorized")

    chunk_id = _nonempty_string(hit.get("id") or hit.get("chunk_id"))
    citations = tuple(
        Citation(
            source_id=source_id,
            wiki_page=str(page["path"]),
            snippet=snippet,
            page_id=page_id,
            revision_id=revision_id,
            chunk_id=chunk_id,
            origin="wiki",
        )
        for source_id in source_ids
    )
    title = str(hit.get("title") or page["title"])
    context_text = str(hit.get("text") or snippet).strip()
    return MappedEvidence(
        citations=citations,
        context=(
            f"标题：{title}\n页面：{page['path']}\n"
            f"来源：{', '.join(source_ids)}\n资料正文：\n{context_text}"
        ),
        answer_part=f"- {title}: {snippet}",
        diagnostic="mapped",
    )


def _map_document_hit(
    conn: Any,
    hit: Mapping[str, Any],
    snippet: str,
    user: UserContext | None,
) -> MappedEvidence:
    source_id = _nonempty_string(hit.get("source_id"))
    if source_id is None:
        return _rejected("unmapped")
    if not all_sources_readable(conn, (source_id,), user):
        return _rejected("unauthorized")

    title = str(hit.get("title") or source_id)
    context_text = str(hit.get("text") or snippet).strip()
    wiki_page = _nonempty_string(hit.get("page_path") or hit.get("wiki_page"))
    page_id = _nonempty_string(hit.get("page_id"))
    revision_id = _nonempty_string(hit.get("revision_id"))
    chunk_id = _nonempty_string(hit.get("id") or hit.get("chunk_id"))
    citation = Citation(
        source_id=source_id,
        wiki_page=wiki_page,
        snippet=snippet,
        page_id=page_id,
        revision_id=revision_id,
        chunk_id=chunk_id,
        origin="document",
    )
    return MappedEvidence(
        citations=(citation,),
        context=(
            f"标题：{title}\n页面：{wiki_page or '未生成知识页'}\n"
            f"来源：{source_id}\n资料正文：\n{context_text}"
        ),
        answer_part=f"- {title}: {snippet}",
        diagnostic="mapped",
    )


def _mapping_is_current(mapping: Mapping[str, Any], page: Mapping[str, Any]) -> bool:
    return bool(
        page.get("lifecycle_status") == "active"
        and page.get("current_revision_id") == mapping.get("revision_id")
        and int(page.get("projection_epoch") or 0) == int(mapping.get("projection_epoch") or -1)
        and page.get("path") == mapping.get("page_path")
    )


def _wiki_hit_is_current(
    page: Mapping[str, Any],
    revision_id: str,
    projection_epoch: int,
) -> bool:
    return bool(
        page.get("lifecycle_status") == "active"
        and page.get("current_revision_id") == revision_id
        and page.get("rag_visible_revision_id") == revision_id
        and int(page.get("projection_epoch") or 0) == projection_epoch
        and int(page.get("rag_visible_epoch") or -1) == projection_epoch
    )


def _revision_source_ids(
    conn: Any,
    *,
    page_id: str,
    revision_id: str,
) -> tuple[str, ...] | None:
    row = conn.execute(
        """
        SELECT source_ids_json FROM wiki_page_revisions
        WHERE id = ? AND page_id = ?
        """,
        (revision_id, page_id),
    ).fetchone()
    if row is None:
        return None
    try:
        values = json.loads(row["source_ids_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(values, list):
        return None
    return tuple(dict.fromkeys(str(value) for value in values if value))


def _nonempty_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rejected(diagnostic: EvidenceDiagnostic) -> MappedEvidence:
    return MappedEvidence(
        citations=(),
        context="",
        answer_part="",
        diagnostic=diagnostic,
    )
