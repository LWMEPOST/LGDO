from __future__ import annotations

from app.config import Settings
from app.db import audit, connect_app, init_app_db, row_to_dict, rows_to_dicts
from app.gbrain import get_gbrain_status
from app.models import GapUpdateRequest, ReviewUpdateRequest, WikiPageSaveRequest, WikiStatusUpdateRequest
from app.rag import delete_document_chunks
from app.timeutil import now_iso
from app.vault import append_log, ensure_vault


def list_sources(settings: Settings, domain: str | None = None, include_deleted: bool = False) -> list[dict]:
    init_app_db(settings)
    query = "SELECT * FROM sources"
    params: list[object] = []
    clauses: list[str] = []
    if domain:
        clauses.append("domain = ?")
        params.append(domain)
    if not include_deleted:
        clauses.append("(status IS NULL OR status != 'deleted')")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY updated_at DESC"
    with connect_app(settings) as conn:
        return rows_to_dicts(conn.execute(query, params).fetchall())


def read_source_preview(settings: Settings, source_id: str, max_chars: int = 8000) -> dict:
    init_app_db(settings)
    ensure_vault(settings.vault_path)
    with connect_app(settings) as conn:
        source = row_to_dict(conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone())
    if source is None:
        raise ValueError(f"source 不存在: {source_id}")

    metadata = source.get("metadata") or {}
    preview_path = metadata.get("normalized_path") or source.get("raw_path")
    if not preview_path:
        raise ValueError(f"source 缺少可预览文件: {source_id}")

    safe_preview_path = _validate_vault_rel_path(preview_path)
    absolute = settings.vault_path / safe_preview_path
    if not absolute.exists() or not absolute.is_file():
        raise ValueError(f"预览文件不存在: {preview_path}")

    content = absolute.read_text(encoding="utf-8", errors="replace")
    truncated = len(content) > max_chars
    return {
        "id": source["id"],
        "title": source["title"],
        "source_type": source["source_type"],
        "parser": metadata.get("parser"),
        "preview_path": str(safe_preview_path).replace("\\", "/"),
        "content": content[:max_chars],
        "truncated": truncated,
        "char_count": len(content),
        "warnings": metadata.get("warnings") or [],
    }


def delete_source(settings: Settings, source_id: str, note: str | None = None) -> dict:
    init_app_db(settings)
    timestamp = now_iso()
    with connect_app(settings) as conn:
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise ValueError(f"source 不存在: {source_id}")
        conn.execute(
            "UPDATE sources SET status = 'deleted', updated_at = ? WHERE id = ?",
            (timestamp, source_id),
        )
        wiki_rows = conn.execute(
            "SELECT path, source_ids_json FROM wiki_pages"
        ).fetchall()
        affected_pages: list[str] = []
        for wiki_row in wiki_rows:
            import json

            source_ids = json.loads(wiki_row["source_ids_json"] or "[]")
            if source_id in source_ids:
                affected_pages.append(wiki_row["path"])
                conn.execute(
                    "UPDATE wiki_pages SET review_status = 'stale', updated_at = ? WHERE path = ?",
                    (timestamp, wiki_row["path"]),
                )
        deleted_chunks = delete_document_chunks(settings, source_id, conn)
        audit(
            conn,
            "source_deleted",
            {
                "source_id": source_id,
                "affected_pages": affected_pages,
                "deleted_chunks": deleted_chunks,
                "note": note,
            },
            timestamp,
        )
        updated = row_to_dict(conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone())
    append_log(
        settings.vault_path,
        "ingest_log.md",
        f"- {timestamp} deleted source={source_id} affected_pages={','.join(affected_pages)} "
        f"deleted_chunks={deleted_chunks} note={note or ''}",
    )
    return updated or {}


def list_ingest_reports(settings: Settings, source_id: str | None = None) -> list[dict]:
    init_app_db(settings)
    query = "SELECT * FROM ingest_reports"
    params: list[object] = []
    if source_id:
        query += " WHERE source_id = ?"
        params.append(source_id)
    query += " ORDER BY created_at DESC"
    with connect_app(settings) as conn:
        return rows_to_dicts(conn.execute(query, params).fetchall())


def rag_status(settings: Settings) -> dict:
    init_app_db(settings)
    gbrain = get_gbrain_status(settings)
    embedding_count = 0
    embedding_model = settings.rag_embedding_model
    if settings.rag_store_backend == "postgres":
        from app.pg_rag import pg_rag_status

        stats = pg_rag_status(settings)
        chunk_count = stats["chunk_count"]
        source_count = stats["source_count"]
        domains = stats["domains"]
        embedding_count = stats.get("embedding_count", 0)
        embedding_model = stats.get("embedding_model", embedding_model)
        vector_count = stats.get("vector_count", 0)
        vector_backend = stats.get("vector_backend", "jsonb")
        pgvector_enabled = stats.get("pgvector_enabled", False)
    else:
        vector_count = 0
        vector_backend = "jsonb"
        pgvector_enabled = False
        with connect_app(settings) as conn:
            chunk_count = conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
            source_count = conn.execute("SELECT COUNT(DISTINCT source_id) FROM document_chunks").fetchone()[0]
            domain_rows = conn.execute(
                "SELECT domain, COUNT(*) AS chunk_count FROM document_chunks GROUP BY domain ORDER BY domain"
            ).fetchall()
            metadata_rows = conn.execute("SELECT metadata_json FROM document_chunks").fetchall()
        domains = [dict(row) for row in domain_rows]
        import json

        model_counts: dict[str, int] = {}
        for row in metadata_rows:
            metadata = json.loads(row["metadata_json"] or "{}")
            if metadata.get("embedding"):
                embedding_count += 1
            model = metadata.get("embedding_model")
            if model:
                model_counts[model] = model_counts.get(model, 0) + 1
        if model_counts:
            embedding_model = sorted(model_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
    return {
        "database_backend": settings.database_backend,
        "rag_store_backend": settings.rag_store_backend,
        "chunk_count": chunk_count,
        "source_count": source_count,
        "embedding_count": embedding_count,
        "embedding_model": embedding_model,
        "vector_count": vector_count,
        "vector_backend": vector_backend,
        "pgvector_enabled": pgvector_enabled,
        "domains": domains,
        "external_system_apis": {
            "wecom": "not_connected",
            "dingtalk": "not_connected",
            "feishu": "not_connected",
            "business_writeback": "not_connected",
            "gbrain": "available" if gbrain.available else "not_connected",
        },
        "gbrain": {
            "enabled": gbrain.enabled,
            "available": gbrain.available,
            "endpoint_configured": gbrain.endpoint_configured,
            "note": gbrain.note,
        },
        "postgres": {
            "host": settings.postgres_host,
            "port": settings.postgres_port,
            "database": settings.postgres_database,
            "user": settings.postgres_user,
        },
    }


def list_wiki_pages(settings: Settings, domain: str | None = None) -> list[dict]:
    init_app_db(settings)
    query = "SELECT * FROM wiki_pages"
    params: list[object] = []
    if domain:
        query += " WHERE domain = ?"
        params.append(domain)
    query += " ORDER BY updated_at DESC"
    with connect_app(settings) as conn:
        return rows_to_dicts(conn.execute(query, params).fetchall())


def list_review_items(settings: Settings, status: str | None = None) -> list[dict]:
    init_app_db(settings)
    query = "SELECT * FROM review_items"
    params: list[object] = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC"
    with connect_app(settings) as conn:
        return rows_to_dicts(conn.execute(query, params).fetchall())


def update_review_item(settings: Settings, review_id: str, request: ReviewUpdateRequest) -> dict:
    init_app_db(settings)
    ensure_vault(settings.vault_path)
    timestamp = now_iso()
    with connect_app(settings) as conn:
        row = conn.execute("SELECT * FROM review_items WHERE id = ?", (review_id,)).fetchone()
        if row is None:
            raise ValueError(f"review item 不存在: {review_id}")

        conn.execute(
            """
            UPDATE review_items
            SET status = ?, owner = COALESCE(?, owner), updated_at = ?
            WHERE id = ?
            """,
            (request.status, request.owner, timestamp, review_id),
        )
        if request.status in {"approved", "resolved"}:
            conn.execute(
                "UPDATE wiki_pages SET review_status = 'reviewed', updated_at = ? WHERE path = ?",
                (timestamp, row["page_path"]),
            )
        elif request.status == "rejected":
            conn.execute(
                "UPDATE wiki_pages SET review_status = 'rejected', updated_at = ? WHERE path = ?",
                (timestamp, row["page_path"]),
            )

        audit(
            conn,
            "review_updated",
            {
                "review_id": review_id,
                "status": request.status,
                "owner": request.owner,
                "note": request.note,
            },
            timestamp,
        )
        updated = row_to_dict(conn.execute("SELECT * FROM review_items WHERE id = ?", (review_id,)).fetchone())

    append_log(
        settings.vault_path,
        "review_log.md",
        f"- {timestamp} {review_id}: status={request.status} owner={request.owner or ''} note={request.note or ''}",
    )
    return updated or {}


def update_wiki_page_status(settings: Settings, page_path: str, request: WikiStatusUpdateRequest) -> dict:
    init_app_db(settings)
    ensure_vault(settings.vault_path)
    timestamp = now_iso()
    with connect_app(settings) as conn:
        row = conn.execute("SELECT * FROM wiki_pages WHERE path = ?", (page_path,)).fetchone()
        if row is None:
            raise ValueError(f"wiki page 不存在: {page_path}")

        conn.execute(
            """
            UPDATE wiki_pages
            SET review_status = ?, owner = COALESCE(?, owner), updated_at = ?
            WHERE path = ?
            """,
            (request.review_status, request.owner, timestamp, page_path),
        )
        audit(
            conn,
            "wiki_status_updated",
            {
                "page_path": page_path,
                "review_status": request.review_status,
                "owner": request.owner,
                "note": request.note,
            },
            timestamp,
        )
        updated = row_to_dict(conn.execute("SELECT * FROM wiki_pages WHERE path = ?", (page_path,)).fetchone())

    append_log(
        settings.vault_path,
        "review_log.md",
        f"- {timestamp} {page_path}: review_status={request.review_status} "
        f"owner={request.owner or ''} note={request.note or ''}",
    )
    return updated or {}


def read_wiki_page(settings: Settings, page_path: str) -> dict:
    init_app_db(settings)
    ensure_vault(settings.vault_path)
    safe_page_path = _validate_vault_rel_path(page_path)
    absolute = settings.vault_path / safe_page_path
    if not absolute.exists() or not absolute.is_file():
        raise ValueError(f"wiki page 文件不存在: {page_path}")

    with connect_app(settings) as conn:
        row = row_to_dict(conn.execute("SELECT * FROM wiki_pages WHERE path = ?", (page_path,)).fetchone())

    return {
        "path": page_path,
        "content": absolute.read_text(encoding="utf-8"),
        "metadata": row or {},
    }


def save_wiki_page(settings: Settings, page_path: str, request: WikiPageSaveRequest) -> dict:
    init_app_db(settings)
    ensure_vault(settings.vault_path)
    safe_page_path = _validate_vault_rel_path(page_path)
    if not str(safe_page_path).replace("\\", "/").startswith("wiki/"):
        raise ValueError("只能编辑 wiki/ 目录下的知识页")

    absolute = settings.vault_path / safe_page_path
    if not absolute.exists() or not absolute.is_file():
        raise ValueError(f"wiki page 文件不存在: {page_path}")

    timestamp = now_iso()
    absolute.write_text(request.content, encoding="utf-8")
    with connect_app(settings) as conn:
        row = conn.execute("SELECT * FROM wiki_pages WHERE path = ?", (page_path,)).fetchone()
        if row is None:
            raise ValueError(f"wiki page 元数据不存在: {page_path}")
        conn.execute(
            """
            UPDATE wiki_pages
            SET review_status = ?, owner = COALESCE(?, owner), updated_at = ?
            WHERE path = ?
            """,
            (request.review_status, request.owner, timestamp, page_path),
        )
        audit(
            conn,
            "wiki_page_saved",
            {
                "page_path": page_path,
                "review_status": request.review_status,
                "owner": request.owner,
                "note": request.note,
            },
            timestamp,
        )
        updated = row_to_dict(conn.execute("SELECT * FROM wiki_pages WHERE path = ?", (page_path,)).fetchone())

    append_log(
        settings.vault_path,
        "review_log.md",
        f"- {timestamp} saved {page_path}: review_status={request.review_status} "
        f"owner={request.owner or ''} note={request.note or ''}",
    )
    return updated or {}


def list_knowledge_gaps(settings: Settings, status: str | None = None) -> list[dict]:
    init_app_db(settings)
    query = "SELECT * FROM knowledge_gaps"
    params: list[object] = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, updated_at DESC"
    with connect_app(settings) as conn:
        return rows_to_dicts(conn.execute(query, params).fetchall())


def update_knowledge_gap(settings: Settings, gap_id: str, request: GapUpdateRequest) -> dict:
    init_app_db(settings)
    ensure_vault(settings.vault_path)
    timestamp = now_iso()
    resolved_at = timestamp if request.status == "resolved" else None
    with connect_app(settings) as conn:
        row = conn.execute("SELECT * FROM knowledge_gaps WHERE id = ?", (gap_id,)).fetchone()
        if row is None:
            raise ValueError(f"knowledge gap 不存在: {gap_id}")
        conn.execute(
            """
            UPDATE knowledge_gaps
            SET status = ?,
                priority = COALESCE(?, priority),
                owner = COALESCE(?, owner),
                linked_page_path = COALESCE(?, linked_page_path),
                updated_at = ?,
                resolved_at = COALESCE(?, resolved_at)
            WHERE id = ?
            """,
            (
                request.status,
                request.priority,
                request.owner,
                request.linked_page_path,
                timestamp,
                resolved_at,
                gap_id,
            ),
        )
        audit(
            conn,
            "knowledge_gap_updated",
            {
                "gap_id": gap_id,
                "status": request.status,
                "priority": request.priority,
                "owner": request.owner,
                "linked_page_path": request.linked_page_path,
                "note": request.note,
            },
            timestamp,
        )
        updated = row_to_dict(conn.execute("SELECT * FROM knowledge_gaps WHERE id = ?", (gap_id,)).fetchone())

    append_log(
        settings.vault_path,
        "gap_log.md",
        f"- {timestamp} {gap_id}: status={request.status} priority={request.priority or ''} "
        f"owner={request.owner or ''} linked={request.linked_page_path or ''} note={request.note or ''}",
    )
    return updated or {}


def _validate_vault_rel_path(page_path: str):
    from pathlib import Path

    rel = Path(page_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("非法 vault 路径")
    return rel
