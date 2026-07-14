from __future__ import annotations

from dataclasses import asdict

from app.config import Settings
from app.auth import UserContext, can_read_metadata, filter_rows_by_acl
from app.db import audit, connect_app, init_app_db, row_to_dict, rows_to_dicts
from app.gbrain import get_gbrain_status
from app.models import (
    BackupReleaseRequest,
    ConflictResolveRequest,
    GapUpdateRequest,
    ReviewUpdateRequest,
    WikiPageSaveRequest,
    WikiStatusUpdateRequest,
)
from app.rag import delete_document_chunks
from app.timeutil import now_iso
from app.vault import append_log, ensure_vault
from app.wiki_revisions import (
    ManualSaveCommand,
    PageNotFound,
    PageReadResult,
    PreconditionRequired,
    ResolveConflictCommand,
    StatusUpdateCommand,
    WikiRevisionError,
    WikiRevisionService,
)


def list_sources(
    settings: Settings,
    domain: str | None = None,
    include_deleted: bool = False,
    user_context: UserContext | None = None,
) -> list[dict]:
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
        return filter_rows_by_acl(rows_to_dicts(conn.execute(query, params).fetchall()), user_context)


def read_source_preview(
    settings: Settings,
    source_id: str,
    max_chars: int = 8000,
    user_context: UserContext | None = None,
) -> dict:
    init_app_db(settings)
    ensure_vault(settings.vault_path)
    with connect_app(settings) as conn:
        source = row_to_dict(conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone())
    if source is None:
        raise ValueError(f"source 不存在: {source_id}")
    if not can_read_metadata(source.get("metadata") or {}, user_context, source.get("owner")):
        raise PermissionError(f"当前用户无权预览 source: {source_id}")

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


def list_wiki_pages(
    settings: Settings,
    domain: str | None = None,
    user_context: UserContext | None = None,
) -> list[dict]:
    init_app_db(settings)
    query = "SELECT * FROM wiki_pages"
    params: list[object] = []
    if domain:
        query += " WHERE domain = ?"
        params.append(domain)
    query += " ORDER BY updated_at DESC"
    with connect_app(settings) as conn:
        rows = rows_to_dicts(conn.execute(query, params).fetchall())
    if user_context is None or user_context.is_admin:
        return rows
    service = _wiki_revision_service(settings)
    visible: list[dict] = []
    for row in rows:
        try:
            page = service.get_page(row["path"])
            _require_wiki_page_readable(settings, page, user_context)
        except PageNotFound:
            continue
        visible.append(row)
    return visible


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
        if row["issue_type"] in {"content_conflict", "concurrent_write_conflict"}:
            raise ValueError(
                "冲突 review 必须通过 "
                f"/wiki/conflicts/{review_id}/resolve 处理"
            )

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


def _wiki_revision_service(settings: Settings) -> WikiRevisionService:
    init_app_db(settings)
    ensure_vault(settings.vault_path)
    return WikiRevisionService(settings)


def _require_wiki_page_readable(
    settings: Settings,
    page: PageReadResult,
    user_context: UserContext | None,
) -> None:
    _require_wiki_artifact_readable(
        settings,
        artifact_path=page.page_path,
        metadata=page.metadata,
        source_ids=None,
        owner=page.metadata.get("owner"),
        user_context=user_context,
    )


def _artifact_source_ids(
    metadata: dict,
    source_ids: object | None,
) -> list[str] | None:
    values = (
        metadata.get("source_ids") or []
        if source_ids is None
        else source_ids
    )
    if not isinstance(values, list) or not all(
        isinstance(source_id, str) and source_id for source_id in values
    ):
        return None
    return list(dict.fromkeys(values))


def _load_source_acl_rows(
    settings: Settings,
    source_ids: list[str],
) -> dict[str, dict]:
    unique_source_ids = list(dict.fromkeys(source_ids))
    if not unique_source_ids:
        return {}
    placeholders = ",".join("?" for _ in unique_source_ids)
    with connect_app(settings) as conn:
        rows = conn.execute(
            f"SELECT id,owner,metadata_json FROM sources WHERE id IN ({placeholders})",
            unique_source_ids,
        ).fetchall()
    return {
        source["id"]: source
        for row in rows
        if (source := row_to_dict(row)) is not None
    }


def _wiki_artifact_is_readable(
    *,
    metadata: dict,
    source_ids: object | None,
    owner: str | None,
    user_context: UserContext | None,
    sources: dict[str, dict],
) -> bool:
    if user_context is None or user_context.is_admin:
        return True
    if not can_read_metadata(metadata, user_context, owner=owner):
        return False
    artifact_source_ids = _artifact_source_ids(metadata, source_ids)
    if artifact_source_ids is None:
        return False
    return all(
        source_id in sources
        and can_read_metadata(
            sources[source_id].get("metadata") or {},
            user_context,
            owner=sources[source_id].get("owner"),
        )
        for source_id in artifact_source_ids
    )


def _require_wiki_artifact_readable(
    settings: Settings,
    *,
    artifact_path: str,
    metadata: dict,
    source_ids: object | None,
    owner: str | None,
    user_context: UserContext | None,
) -> None:
    if user_context is None or user_context.is_admin:
        return
    artifact_source_ids = _artifact_source_ids(metadata, source_ids)
    sources = _load_source_acl_rows(settings, artifact_source_ids or [])
    if not _wiki_artifact_is_readable(
        metadata=metadata,
        source_ids=source_ids,
        owner=owner,
        user_context=user_context,
        sources=sources,
    ):
        raise PageNotFound(artifact_path)


def _load_revision_artifacts(
    settings: Settings,
    revision_ids: list[str],
) -> dict[str, dict]:
    unique_revision_ids = list(dict.fromkeys(revision_ids))
    if not unique_revision_ids:
        return {}
    placeholders = ",".join("?" for _ in unique_revision_ids)
    with connect_app(settings) as conn:
        rows = conn.execute(
            f"""
            SELECT id,source_ids_json,metadata_json
            FROM wiki_page_revisions
            WHERE id IN ({placeholders})
            """,
            unique_revision_ids,
        ).fetchall()
    return {
        artifact["id"]: artifact
        for row in rows
        if (artifact := row_to_dict(row)) is not None
    }


def _conflict_artifacts_readable(
    review: dict,
    revisions: dict[str, dict],
    sources: dict[str, dict],
    user_context: UserContext | None,
) -> bool:
    if not _wiki_artifact_is_readable(
        metadata=review.get("metadata") or {},
        source_ids=review.get("source_ids"),
        owner=review.get("owner"),
        user_context=user_context,
        sources=sources,
    ):
        return False
    for field in ("base_revision_id", "candidate_revision_id"):
        revision_id = review.get(field)
        revision = revisions.get(revision_id) if isinstance(revision_id, str) else None
        if revision is None or not _wiki_artifact_is_readable(
            metadata=revision.get("metadata") or {},
            source_ids=revision.get("source_ids"),
            owner=(revision.get("metadata") or {}).get("owner"),
            user_context=user_context,
            sources=sources,
        ):
            return False
    return True


def _wiki_mutation_payload(
    service: WikiRevisionService,
    result,
) -> dict:
    page = service.get_page(result.page_path)
    payload = asdict(result)
    payload["path"] = result.page_path
    payload["page_path"] = result.page_path
    payload["projection_job_ids"] = list(result.projection_job_ids)
    payload["review_status"] = page.metadata.get("review_status")
    return payload


def update_wiki_page_status(
    settings: Settings,
    page_path: str,
    request: WikiStatusUpdateRequest,
    *,
    actor: str,
    user_context: UserContext | None = None,
) -> dict:
    if request.expected_revision_id is None:
        raise PreconditionRequired("expected_revision_id is required")
    service = _wiki_revision_service(settings)
    _require_wiki_page_readable(
        settings,
        service.get_page(page_path),
        user_context,
    )
    result = service.update_status(
        StatusUpdateCommand(
            page_path=page_path,
            review_status=request.review_status,
            expected_revision_id=request.expected_revision_id,
            request_id=request.request_id,
            actor=actor,
            owner=request.owner,
            note=request.note,
        )
    )
    return _wiki_mutation_payload(service, result)


def read_wiki_page(
    settings: Settings,
    page_path: str,
    user_context: UserContext | None = None,
) -> dict:
    page = _wiki_revision_service(settings).get_page(page_path)
    _require_wiki_page_readable(settings, page, user_context)
    return {
        "path": page.page_path,
        "page_id": page.page_id,
        "content": page.content,
        "current_revision_id": page.current_revision_id,
        "generated_revision_id": page.generated_revision_id,
        "accepted_generated_revision_id": page.accepted_generated_revision_id,
        "lifecycle_status": page.lifecycle_status,
        "projection_epoch": page.projection_epoch,
        "write_in_progress": page.write_in_progress,
        "write_intent_id": page.write_intent_id,
        "metadata": page.metadata,
    }


def save_wiki_page(
    settings: Settings,
    page_path: str,
    request: WikiPageSaveRequest,
    *,
    actor: str,
    user_context: UserContext | None = None,
) -> dict:
    if request.expected_revision_id is None:
        raise PreconditionRequired("expected_revision_id is required")
    service = _wiki_revision_service(settings)
    _require_wiki_page_readable(
        settings,
        service.get_page(page_path),
        user_context,
    )
    result = service.prepare_manual_save(
        ManualSaveCommand(
            page_path=page_path,
            content=request.content,
            expected_revision_id=request.expected_revision_id,
            request_id=request.request_id,
            actor=actor,
            owner=request.owner,
            note=request.note,
            review_status=request.review_status,
        )
    )
    return _wiki_mutation_payload(service, result)


def list_wiki_page_revisions(
    settings: Settings,
    page_path: str,
    *,
    limit: int,
    offset: int,
    user_context: UserContext | None = None,
) -> dict:
    service = _wiki_revision_service(settings)
    page = service.get_page(page_path)
    _require_wiki_page_readable(settings, page, user_context)
    if user_context is None or user_context.is_admin:
        with connect_app(settings) as conn:
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
                    (page.page_id,),
                ).fetchone()[0]
            )
            rows = conn.execute(
                """
                SELECT id,page_id,page_path,revision_number,file_hash,semantic_hash,
                       origin,base_revision_id,source_ids_json,actor,note,
                       metadata_json,idempotency_key,created_at
                FROM wiki_page_revisions
                WHERE page_id=?
                ORDER BY revision_number DESC,id DESC
                LIMIT ? OFFSET ?
                """,
                (page.page_id, limit, offset),
            ).fetchall()
        return {
            "items": [row_to_dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    with connect_app(settings) as conn:
        rows = conn.execute(
            """
            SELECT id,page_id,page_path,revision_number,file_hash,semantic_hash,
                   origin,base_revision_id,source_ids_json,actor,note,
                   metadata_json,idempotency_key,created_at
            FROM wiki_page_revisions
            WHERE page_id=?
            ORDER BY revision_number DESC,id DESC
            """,
            (page.page_id,),
        ).fetchall()
    revisions = [row_to_dict(row) or {} for row in rows]
    source_ids: list[str] = []
    for revision in revisions:
        artifact_source_ids = _artifact_source_ids(
            revision.get("metadata") or {},
            revision.get("source_ids"),
        )
        if artifact_source_ids is not None:
            source_ids.extend(artifact_source_ids)
    sources = _load_source_acl_rows(settings, source_ids)
    visible = [
        revision
        for revision in revisions
        if _wiki_artifact_is_readable(
            metadata=revision.get("metadata") or {},
            source_ids=revision.get("source_ids"),
            owner=(revision.get("metadata") or {}).get("owner"),
            user_context=user_context,
            sources=sources,
        )
    ]
    return {
        "items": visible[offset : offset + limit],
        "total": len(visible),
        "limit": limit,
        "offset": offset,
    }


def get_wiki_revision(
    settings: Settings,
    revision_id: str,
    user_context: UserContext | None = None,
) -> dict:
    service = _wiki_revision_service(settings)
    with connect_app(settings) as conn:
        row = conn.execute(
            """
            SELECT revision.*,page.path AS current_page_path
            FROM wiki_page_revisions AS revision
            JOIN wiki_pages AS page ON page.page_id=revision.page_id
            WHERE revision.id=?
            """,
            (revision_id,),
        ).fetchone()
    if row is None:
        raise PageNotFound(revision_id)
    revision = row_to_dict(row) or {}
    current_page_path = revision.pop("current_page_path")
    page = service.get_page(current_page_path)
    _require_wiki_page_readable(settings, page, user_context)
    _require_wiki_artifact_readable(
        settings,
        artifact_path=current_page_path,
        metadata=revision.get("metadata") or {},
        source_ids=revision.get("source_ids"),
        owner=(revision.get("metadata") or {}).get("owner"),
        user_context=user_context,
    )
    return revision


def list_wiki_page_conflicts(
    settings: Settings,
    page_path: str,
    user_context: UserContext | None = None,
) -> list[dict]:
    service = _wiki_revision_service(settings)
    page = service.get_page(page_path)
    _require_wiki_page_readable(settings, page, user_context)
    conflicts = service.list_conflicts(page_path, status="pending")
    if user_context is None or user_context.is_admin:
        return conflicts
    revision_ids = [
        revision_id
        for conflict in conflicts
        for revision_id in (
            conflict.get("base_revision_id"),
            conflict.get("candidate_revision_id"),
        )
        if isinstance(revision_id, str)
    ]
    revisions = _load_revision_artifacts(settings, revision_ids)
    source_ids: list[str] = []
    for artifact in [*conflicts, *revisions.values()]:
        artifact_source_ids = _artifact_source_ids(
            artifact.get("metadata") or {},
            artifact.get("source_ids"),
        )
        if artifact_source_ids is not None:
            source_ids.extend(artifact_source_ids)
    sources = _load_source_acl_rows(settings, source_ids)
    return [
        conflict
        for conflict in conflicts
        if _conflict_artifacts_readable(
            conflict,
            revisions,
            sources,
            user_context,
        )
    ]


def resolve_wiki_conflict(
    settings: Settings,
    review_id: str,
    request: ConflictResolveRequest,
    *,
    actor: str,
    user_context: UserContext | None = None,
) -> dict:
    service = _wiki_revision_service(settings)
    with connect_app(settings) as conn:
        review_row = conn.execute(
            "SELECT * FROM review_items WHERE id=?",
            (review_id,),
        ).fetchone()
    if review_row is None:
        raise PageNotFound(review_id)
    review = row_to_dict(review_row) or {}
    page = service.get_page(review["page_path"])
    _require_wiki_page_readable(settings, page, user_context)
    if review["issue_type"] not in {
        "content_conflict",
        "concurrent_write_conflict",
    }:
        raise WikiRevisionError(
            f"review item is not a resolvable conflict: {review_id}"
        )
    if user_context is not None and not user_context.is_admin:
        revision_ids = [
            revision_id
            for revision_id in (
                review.get("base_revision_id"),
                review.get("candidate_revision_id"),
            )
            if isinstance(revision_id, str)
        ]
        revisions = _load_revision_artifacts(settings, revision_ids)
        source_ids: list[str] = []
        for artifact in [review, *revisions.values()]:
            artifact_source_ids = _artifact_source_ids(
                artifact.get("metadata") or {},
                artifact.get("source_ids"),
            )
            if artifact_source_ids is not None:
                source_ids.extend(artifact_source_ids)
        sources = _load_source_acl_rows(settings, source_ids)
        if not _conflict_artifacts_readable(
            review,
            revisions,
            sources,
            user_context,
        ):
            raise PageNotFound(review["page_path"])
    result = service.resolve_conflict(
        ResolveConflictCommand(
            review_id=review_id,
            resolution=request.resolution,
            merged_content=request.merged_content,
            expected_current_revision_id=request.expected_current_revision_id,
            expected_generated_revision_id=request.expected_generated_revision_id,
            request_id=request.request_id,
            actor=actor,
            note=request.note,
        )
    )
    return _wiki_mutation_payload(service, result)


def release_wiki_backup(
    settings: Settings,
    intent_id: str,
    request: BackupReleaseRequest,
    *,
    actor: str,
) -> dict:
    service = _wiki_revision_service(settings)
    return asdict(
        service.release_retained_backup(
            intent_id,
            request.expected_backup_hash,
            actor=actor,
        )
    )


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
