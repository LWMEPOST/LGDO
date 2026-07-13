from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from app.config import Settings
from app.db import connect_app, connect_app_write, init_app_db, json_dump, rows_to_dicts
from app.wiki_revisions import (
    LockedPage,
    MetadataUpdateCommand,
    RevisionConflict,
    WikiRevisionService,
)


ADMINISTRATION_TITLE_MARKERS = (
    "行政",
    "费用报销",
    "采购管理",
    "办公行为",
    "office_norms",
    "expense",
    "purchase",
)


def reclassify_administration_documents(settings: Settings) -> dict[str, Any]:
    init_app_db(settings)
    with connect_app(settings) as conn:
        rows = rows_to_dicts(conn.execute("SELECT * FROM sources WHERE status IS NULL OR status != 'deleted'").fetchall())
        sources = [row for row in rows if _looks_administration(row)]
        source_ids = [source["id"] for source in sources]
        if not source_ids:
            return {"updated_sources": 0, "updated_chunks": 0, "updated_rag_chunks": 0, "updated_pages": 0, "copied_files": 0, "source_ids": []}
        page_rows = rows_to_dicts(conn.execute("SELECT * FROM wiki_pages").fetchall())

    domain = "administration"
    matching_source_ids = set(source_ids)
    revision_service = WikiRevisionService(settings)
    page_plans: list[dict[str, Any]] = []
    seen_page_ids: set[str] = set()
    for page in page_rows:
        matched_ids = sorted(
            matching_source_ids.intersection(page.get("source_ids") or [])
        )
        if not matched_ids:
            continue
        current = revision_service.get_page(page["path"])
        if current.page_id in seen_page_ids:
            continue
        seen_page_ids.add(current.page_id)
        target_path = _next_domain_path(current.page_path, domain) or current.page_path
        source_command_id = _source_command_id(matched_ids)
        request_id = (
            f"domain-reclassify:{current.page_id}:{domain}:{source_command_id}"
        )
        page_plans.append(
            {
                "page_id": current.page_id,
                "page_path": current.page_path,
                "target_path": target_path,
                "expected_revision_id": current.current_revision_id,
                "metadata_domain": current.metadata.get("domain"),
                "command": MetadataUpdateCommand(
                    page_path=current.page_path,
                    changes={"domain": domain},
                    expected_revision_id=current.current_revision_id,
                    request_id=request_id,
                    actor="domain-reclassifier",
                    note=None,
                ),
            }
        )

    source_plans: list[dict[str, Any]] = []
    for source in sources:
        source_metadata = source.get("metadata") or {}
        metadata = _with_domain(source_metadata, domain)
        raw_path = _next_domain_path(source.get("raw_path"), domain)
        normalized_path = _next_domain_path(
            source_metadata.get("normalized_path"), domain
        )
        jsonl_path = _next_domain_path(source_metadata.get("jsonl_path"), domain)
        if normalized_path:
            metadata["normalized_path"] = normalized_path
        if jsonl_path:
            metadata["jsonl_path"] = jsonl_path
        source_plans.append(
            {
                "source": source,
                "metadata": metadata,
                "raw_path": raw_path,
                "normalized_path": normalized_path,
                "jsonl_path": jsonl_path,
            }
        )

    updated_chunks = 0
    updated_rag_chunks = 0
    copied_files = 0
    page_plans.sort(key=lambda item: item["page_id"])
    with connect_app_write(settings) as conn:
        suffix = " FOR UPDATE" if settings.database_backend == "postgres" else ""
        locked_plans: list[tuple[dict[str, Any], LockedPage]] = []
        for plan in page_plans:
            row = conn.execute(
                "SELECT * FROM wiki_pages WHERE page_id=?" + suffix,
                (plan["page_id"],),
            ).fetchone()
            locked_page = dict(row) if row is not None else None
            if (
                locked_page is None
                or locked_page.get("current_revision_id")
                != plan["expected_revision_id"]
                or locked_page.get("path") != plan["page_path"]
            ):
                raise RevisionConflict(
                    "domain reclassify page precondition failed",
                    current_revision_id=(
                        locked_page.get("current_revision_id")
                        if locked_page is not None
                        else None
                    ),
                    pending_intent_id=(
                        locked_page.get("pending_write_intent_id")
                        if locked_page is not None
                        else None
                    ),
                )
            locked = LockedPage(conn=conn, page=locked_page)
            pending_intent_id = locked_page.get("pending_write_intent_id")
            if pending_intent_id is None:
                applied = conn.execute(
                    """
                    SELECT revision.id AS revision_id,revision.page_path,
                           revision.origin,revision.base_revision_id,
                           revision.actor,revision.metadata_json,
                           revision.idempotency_key,
                           intent.id AS intent_id,intent.expected_revision_id,
                           intent.target_path,intent.status
                    FROM wiki_page_revisions AS revision
                    JOIN vault_write_intents AS intent
                      ON intent.revision_id=revision.id
                    WHERE revision.id=? AND revision.page_id=?
                      AND intent.page_id=?
                    ORDER BY intent.created_at,intent.id
                    LIMIT 1
                    """,
                    (
                        plan["expected_revision_id"],
                        plan["page_id"],
                        plan["page_id"],
                    ),
                ).fetchone()
                if _is_applied_domain_transition(
                    plan,
                    locked_page,
                    dict(applied) if applied is not None else None,
                    domain,
                ):
                    plan["already_applied"] = True
            if pending_intent_id is not None:
                pending = conn.execute(
                    """
                    SELECT revision.id AS revision_id,revision.page_path,
                           revision.origin,revision.base_revision_id,
                           revision.actor,revision.metadata_json,
                           revision.idempotency_key,
                           intent.id AS intent_id,intent.expected_revision_id,
                           intent.target_path,intent.status
                    FROM vault_write_intents AS intent
                    JOIN wiki_page_revisions AS revision
                      ON revision.id=intent.revision_id
                    WHERE intent.id=? AND intent.page_id=?
                      AND intent.expected_revision_id=?
                      AND revision.base_revision_id=?
                    """,
                    (
                        pending_intent_id,
                        plan["page_id"],
                        plan["expected_revision_id"],
                        plan["expected_revision_id"],
                    ),
                ).fetchone()
                if not _is_pending_domain_transition(
                    plan,
                    locked_page,
                    dict(pending) if pending is not None else None,
                    domain,
                ):
                    raise RevisionConflict(
                        "domain reclassify page has an unrelated pending write",
                        current_revision_id=locked_page.get(
                            "current_revision_id"
                        ),
                        pending_intent_id=pending_intent_id,
                    )
                prepared = revision_service.update_metadata(
                    plan["command"],
                    execute_intent=False,
                    target_page_path=plan["target_path"],
                    page_domain=domain,
                    locked_page=locked,
                )
                if (
                    not prepared.replayed
                    or prepared.write_intent_id != pending_intent_id
                ):
                    raise RevisionConflict(
                        "domain reclassify pending write does not match",
                        current_revision_id=locked_page.get(
                            "current_revision_id"
                        ),
                        pending_intent_id=pending_intent_id,
                    )
                plan["prepared"] = prepared
            locked_plans.append((plan, locked))

        for plan, locked in locked_plans:
            if plan.get("already_applied") or "prepared" in plan:
                continue
            plan["prepared"] = revision_service.update_metadata(
                plan["command"],
                execute_intent=False,
                target_page_path=plan["target_path"],
                page_domain=domain,
                locked_page=locked,
            )

        for plan in source_plans:
            source = plan["source"]
            conn.execute(
                "UPDATE sources SET domain=?,raw_path=?,metadata_json=? WHERE id=?",
                (
                    domain,
                    plan["raw_path"] or source.get("raw_path"),
                    json_dump(plan["metadata"]),
                    source["id"],
                ),
            )
            conn.execute(
                """
                UPDATE ingest_reports SET normalized_path=?,jsonl_path=?
                WHERE source_id=?
                """,
                (
                    plan["normalized_path"],
                    plan["jsonl_path"],
                    source["id"],
                ),
            )

        for source_id in source_ids:
            chunks = rows_to_dicts(
                conn.execute(
                    "SELECT * FROM document_chunks WHERE source_id=?",
                    (source_id,),
                ).fetchall()
            )
            for chunk in chunks:
                metadata = _with_domain(chunk.get("metadata") or {}, domain)
                conn.execute(
                    "UPDATE document_chunks SET domain=?,metadata_json=? WHERE id=?",
                    (domain, json_dump(metadata), chunk["id"]),
                )
                updated_chunks += 1
            if settings.rag_store_backend == "postgres":
                updated_rag_chunks += _update_pg_rag_chunks(conn, source_id, domain)

        for plan in source_plans:
            source = plan["source"]
            for relative_path in (
                source.get("raw_path"),
                (source.get("metadata") or {}).get("normalized_path"),
                (source.get("metadata") or {}).get("jsonl_path"),
            ):
                _, copied = _copy_to_domain(settings, relative_path, domain)
                copied_files += copied

        for plan in page_plans:
            _, copied = _copy_to_domain(
                settings,
                plan["page_path"],
                domain,
                update_contents=False,
            )
            copied_files += copied

    for plan in page_plans:
        if plan.get("already_applied"):
            continue
        command = plan["command"]
        revision_service.update_metadata(
            MetadataUpdateCommand(
                page_path=plan["target_path"],
                changes=command.changes,
                expected_revision_id=command.expected_revision_id,
                request_id=command.request_id,
                actor=command.actor,
                note=command.note,
            ),
            target_page_path=plan["target_path"],
            page_domain=domain,
        )

    return {
        "updated_sources": len(source_ids),
        "updated_chunks": updated_chunks,
        "updated_rag_chunks": updated_rag_chunks,
        "updated_pages": len(page_plans),
        "copied_files": copied_files,
        "source_ids": source_ids,
    }


def _is_applied_domain_transition(
    plan: dict[str, Any],
    page: dict[str, Any],
    transition: dict[str, Any] | None,
    domain: str,
) -> bool:
    if transition is None:
        return False
    prefix = f"metadata:{plan['command'].request_id}:"
    idempotency_key = str(transition.get("idempotency_key") or "")
    digest = (
        idempotency_key[len(prefix) :]
        if idempotency_key.startswith(prefix)
        else ""
    )
    return (
        len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
        and transition.get("revision_id") == page.get("current_revision_id")
        and transition.get("origin") == "manual"
        and transition.get("base_revision_id") is not None
        and transition.get("actor") == plan["command"].actor
        and transition.get("expected_revision_id")
        == transition.get("base_revision_id")
        and transition.get("status") == "applied"
        and transition.get("page_path") == plan["target_path"]
        and transition.get("target_path") == plan["target_path"]
        and page.get("path") == plan["target_path"]
        and page.get("domain") == domain
        and plan.get("metadata_domain") == domain
        and _revision_metadata_domain(transition) == domain
    )


def _is_pending_domain_transition(
    plan: dict[str, Any],
    page: dict[str, Any],
    transition: dict[str, Any] | None,
    domain: str,
) -> bool:
    if transition is None:
        return False
    return (
        transition.get("intent_id") == page.get("pending_write_intent_id")
        and transition.get("origin") == "manual"
        and transition.get("base_revision_id") == plan["expected_revision_id"]
        and transition.get("actor") == plan["command"].actor
        and transition.get("expected_revision_id")
        == plan["expected_revision_id"]
        and transition.get("status")
        in {"pending", "captured", "installed", "recovery_required"}
        and transition.get("page_path") == plan["target_path"]
        and transition.get("target_path") == plan["target_path"]
        and page.get("path") == plan["target_path"]
        and page.get("domain") == domain
        and _revision_metadata_domain(transition) == domain
    )


def _revision_metadata_domain(transition: dict[str, Any]) -> Any:
    try:
        metadata = json.loads(transition.get("metadata_json") or "{}")
    except (TypeError, ValueError):
        return None
    return metadata.get("domain")


def _looks_administration(source: dict[str, Any]) -> bool:
    haystack = f"{source.get('title') or ''}\n{source.get('original_path') or ''}".lower()
    return any(marker.lower() in haystack for marker in ADMINISTRATION_TITLE_MARKERS)


def _with_domain(metadata: dict[str, Any], domain: str) -> dict[str, Any]:
    next_metadata = {**metadata, "domain": domain}
    cleaned = next_metadata.get("cleaned")
    if isinstance(cleaned, dict):
        next_metadata["cleaned"] = {**cleaned, "domain": domain}
    tags = next_metadata.get("acl_tags")
    if isinstance(tags, list):
        next_metadata["acl_tags"] = _replace_domain_tag(tags, domain)
    return next_metadata


def _replace_domain_tag(tags: list[Any], domain: str) -> list[Any]:
    replaced: list[Any] = []
    for tag in tags:
        if str(tag) in {"product", "customer_service", "administration"}:
            if domain not in replaced:
                replaced.append(domain)
            continue
        if tag not in replaced:
            replaced.append(tag)
    if domain not in replaced:
        replaced.append(domain)
    return replaced


def _next_domain_path(rel_path: str | None, domain: str) -> str | None:
    if not rel_path:
        return rel_path
    normalized = str(rel_path).replace("\\", "/")
    return _domain_path(normalized, domain)


def _source_command_id(source_ids: list[str]) -> str:
    canonical = json.dumps(
        sorted(set(source_ids)),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _update_pg_rag_chunks(conn: Any, source_id: str, domain: str) -> int:
    chunks = conn.execute(
        "SELECT id, metadata FROM rag_document_chunks WHERE source_id = ?",
        (source_id,),
    ).fetchall()
    updated = 0
    for chunk in chunks:
        metadata = _with_domain(chunk["metadata"] or {}, domain)
        conn.execute(
            "UPDATE rag_document_chunks SET domain = ?, metadata = ?::jsonb WHERE id = ?",
            (domain, json_dump(metadata), chunk["id"]),
        )
        updated += 1
    return updated


def _copy_to_domain(
    settings: Settings,
    rel_path: str | None,
    domain: str,
    *,
    update_contents: bool = True,
) -> tuple[str | None, int]:
    if not rel_path:
        return rel_path, 0
    normalized = str(rel_path).replace("\\", "/")
    next_path = _domain_path(normalized, domain)
    if next_path == normalized:
        if update_contents:
            _update_domain_in_vault_file(settings.vault_path / normalized, domain)
        return normalized, 0
    source = settings.vault_path / normalized
    target = settings.vault_path / next_path
    if not source.exists() or not source.is_file():
        return next_path, 0
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copy2(source, target)
        if update_contents:
            _update_domain_in_vault_file(target, domain)
        return next_path, 1
    if update_contents:
        _update_domain_in_vault_file(target, domain)
    return next_path, 0


def _domain_path(rel_path: str, domain: str) -> str:
    parts = Path(rel_path).parts
    if len(parts) < 3:
        return rel_path
    root = parts[0]
    if root in {"raw", "normalized", "jsonl", "wiki"} and parts[1] in {"product", "customer_service", "administration"}:
        return str(Path(root, domain, *parts[2:])).replace("\\", "/")
    return rel_path


def _update_domain_in_vault_file(path: Path, domain: str) -> None:
    if not path.exists() or not path.is_file():
        return
    if path.suffix.lower() == ".jsonl":
        lines: list[str] = []
        changed = False
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                lines.append(line)
                continue
            payload = json.loads(line)
            metadata = payload.get("metadata")
            if isinstance(metadata, dict):
                payload["metadata"] = _with_domain(metadata, domain)
                changed = True
            lines.append(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        if changed:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    if path.suffix.lower() != ".md":
        return
    content = path.read_text(encoding="utf-8")
    next_content = content.replace('domain: "customer_service"', f'domain: "{domain}"')
    next_content = next_content.replace("domain: customer_service", f"domain: {domain}")
    next_content = next_content.replace('domain: "product"', f'domain: "{domain}"')
    next_content = next_content.replace("domain: product", f"domain: {domain}")
    next_content = next_content.replace("- customer_service", f"- {domain}")
    next_content = next_content.replace("- product", f"- {domain}")
    if next_content != content:
        path.write_text(next_content, encoding="utf-8")
