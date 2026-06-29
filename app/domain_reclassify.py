from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from app.config import Settings
from app.db import connect_app, init_app_db, json_dump, rows_to_dicts


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

        updated_chunks = 0
        updated_rag_chunks = 0
        updated_pages = 0
        copied_files = 0
        for source in sources:
            metadata = _with_domain(source.get("metadata") or {}, "administration")
            raw_path, raw_copied = _copy_to_domain(settings, source.get("raw_path"), "administration")
            normalized_path, normalized_copied = _copy_to_domain(settings, metadata.get("normalized_path"), "administration")
            jsonl_path, jsonl_copied = _copy_to_domain(settings, metadata.get("jsonl_path"), "administration")
            copied_files += raw_copied + normalized_copied + jsonl_copied
            if normalized_path:
                metadata["normalized_path"] = normalized_path
            if jsonl_path:
                metadata["jsonl_path"] = jsonl_path
            conn.execute(
                "UPDATE sources SET domain = ?, raw_path = ?, metadata_json = ? WHERE id = ?",
                ("administration", raw_path or source.get("raw_path"), json_dump(metadata), source["id"]),
            )
            conn.execute(
                "UPDATE ingest_reports SET normalized_path = ?, jsonl_path = ? WHERE source_id = ?",
                (normalized_path or metadata.get("normalized_path"), jsonl_path or metadata.get("jsonl_path"), source["id"]),
            )

        for source_id in source_ids:
            chunks = rows_to_dicts(conn.execute("SELECT * FROM document_chunks WHERE source_id = ?", (source_id,)).fetchall())
            for chunk in chunks:
                metadata = _with_domain(chunk.get("metadata") or {}, "administration")
                conn.execute(
                    "UPDATE document_chunks SET domain = ?, metadata_json = ? WHERE id = ?",
                    ("administration", json_dump(metadata), chunk["id"]),
                )
                updated_chunks += 1
            if settings.rag_store_backend == "postgres":
                updated_rag_chunks += _update_pg_rag_chunks(conn, source_id, "administration")

            pages = rows_to_dicts(
                conn.execute(
                    "SELECT * FROM wiki_pages WHERE source_ids_json LIKE ?",
                    (f"%{source_id}%",),
                ).fetchall()
            )
            for page in pages:
                if source_id not in (page.get("source_ids") or []):
                    continue
                next_path, page_copied = _copy_to_domain(settings, page["path"], "administration")
                copied_files += page_copied
                _update_wiki_frontmatter(settings, next_path or page["path"], "administration")
                conn.execute(
                    "UPDATE wiki_pages SET domain = ?, path = ? WHERE path = ?",
                    ("administration", next_path or page["path"], page["path"]),
                )
                conn.execute(
                    "UPDATE review_items SET page_path = ? WHERE page_path = ?",
                    (next_path or page["path"], page["path"]),
                )
                updated_pages += 1

        return {
            "updated_sources": len(source_ids),
            "updated_chunks": updated_chunks,
            "updated_rag_chunks": updated_rag_chunks,
            "updated_pages": updated_pages,
            "copied_files": copied_files,
            "source_ids": source_ids,
        }


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


def _update_wiki_frontmatter(settings: Settings, page_path: str, domain: str) -> None:
    path = settings.vault_path / page_path
    if not path.exists() or not path.is_file():
        return
    content = path.read_text(encoding="utf-8")
    next_content = content.replace("domain: customer_service", f"domain: {domain}")
    next_content = next_content.replace("domain: product", f"domain: {domain}")
    if next_content != content:
        path.write_text(next_content, encoding="utf-8")


def _copy_to_domain(settings: Settings, rel_path: str | None, domain: str) -> tuple[str | None, int]:
    if not rel_path:
        return rel_path, 0
    normalized = str(rel_path).replace("\\", "/")
    next_path = _domain_path(normalized, domain)
    if next_path == normalized:
        _update_domain_in_vault_file(settings.vault_path / normalized, domain)
        return normalized, 0
    source = settings.vault_path / normalized
    target = settings.vault_path / next_path
    if not source.exists() or not source.is_file():
        return next_path, 0
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copy2(source, target)
        _update_domain_in_vault_file(target, domain)
        return next_path, 1
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
