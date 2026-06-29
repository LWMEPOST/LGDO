from __future__ import annotations

import re
import uuid
import json
from pathlib import Path

from app.config import Settings
from app.db import audit, connect_app, init_app_db, json_dump, rows_to_dicts
from app.gbrain import import_vault_to_gbrain
from app.models import CompileRequest, CompileResponse
from app.timeutil import now_iso
from app.vault import append_log, ensure_vault, slugify


def read_raw(settings: Settings, raw_rel: str) -> str:
    path = settings.vault_path / raw_rel
    return path.read_text(encoding="utf-8", errors="ignore")


def read_source_text(settings: Settings, source: dict) -> str:
    metadata = source.get("metadata") or json.loads(source.get("metadata_json") or "{}")
    normalized_path = metadata.get("normalized_path")
    if normalized_path:
        path = settings.vault_path / normalized_path
        if path.exists():
            return path.read_text(encoding="utf-8", errors="ignore")
    return read_raw(settings, source["raw_path"])


def summarize(text: str, limit: int = 480) -> str:
    body = re.sub(r"---.*?---", "", text, count=1, flags=re.S).strip()
    body = re.sub(r"\s+", " ", body)
    return body[:limit] + ("..." if len(body) > limit else "")


def pick_page_type(title: str, text: str) -> str:
    combined = f"{title}\n{text}".lower()
    if any(word in combined for word in ["退款", "售后", "政策", "policy", "sla"]):
        return "policy"
    if any(word in combined for word in ["故障", "bug", "已知问题", "known issue", "异常"]):
        return "known_issue"
    if any(word in combined for word in ["faq", "常见问题", "问题", "问答"]):
        return "faq"
    return "feature"


def wiki_rel_path(domain: str, page_type: str, title: str, source_id: str) -> Path:
    folder = {
        "policy": "policies",
        "known_issue": "known_issues",
        "faq": "faq",
        "feature": "features",
    }.get(page_type, "features")
    return Path("wiki") / domain / folder / f"{slugify(title, source_id)}.md"


def build_page(source: dict, raw_text: str, page_type: str) -> str:
    summary = summarize(raw_text)
    title = source["title"]
    source_metadata = source.get("metadata") or json.loads(source.get("metadata_json") or "{}")
    normalized_path = source_metadata.get("normalized_path", "")
    parser = source_metadata.get("parser", "")
    return f"""---
title: {title}
source_ids:
  - {source["id"]}
domain: {source["domain"]}
page_type: {page_type}
review_status: draft
owner: {source.get("owner") or ""}
---

# {title}

## 摘要

{summary or "暂无可用摘要。"}

## 适用范围

- 业务域：{source["domain"]}
- 来源文件：`{source["original_path"]}`

## 关键依据

- source_id: `{source["id"]}`
- raw_path: `{source["raw_path"]}`
- normalized_path: `{normalized_path}`
- parser: `{parser}`
- content_hash: `{source["content_hash"]}`

## 待人工审阅

- [ ] 核对摘要是否准确
- [ ] 补充适用版本、边界条件和标准话术
- [ ] 确认是否需要拆分为 FAQ、功能页或政策页
"""


def compile_wiki(settings: Settings, request: CompileRequest) -> CompileResponse:
    init_app_db(settings)
    ensure_vault(settings.vault_path)
    job_id = f"job_compile_{uuid.uuid4().hex[:10]}"
    timestamp = now_iso()
    created_pages = 0
    updated_pages = 0
    review_items = 0

    with connect_app(settings) as conn:
        params: list[object] = [request.domain]
        query = "SELECT * FROM sources WHERE domain = ? AND status = 'active'"
        if request.source_ids:
            placeholders = ",".join("?" for _ in request.source_ids)
            query += f" AND id IN ({placeholders})"
            params.extend(request.source_ids)
        else:
            query += " AND last_compiled_at IS NULL"

        sources = rows_to_dicts(conn.execute(query, params).fetchall())
        for source in sources:
            raw_text = read_source_text(settings, source)
            page_type = pick_page_type(source["title"], raw_text)
            if request.page_types and page_type not in request.page_types:
                continue

            rel_path = wiki_rel_path(source["domain"], page_type, source["title"], source["id"])
            page_path = settings.vault_path / rel_path
            page_path.parent.mkdir(parents=True, exist_ok=True)
            existed = page_path.exists()
            page_path.write_text(build_page(source, raw_text, page_type), encoding="utf-8")

            rel = str(rel_path).replace("\\", "/")
            conn.execute(
                """
                INSERT INTO wiki_pages(
                  path, domain, page_type, title, source_ids_json, review_status,
                  owner, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                  page_type = excluded.page_type,
                  title = excluded.title,
                  source_ids_json = excluded.source_ids_json,
                  review_status = 'draft',
                  owner = excluded.owner,
                  updated_at = excluded.updated_at
                """,
                (
                    rel,
                    source["domain"],
                    page_type,
                    source["title"],
                    json_dump([source["id"]]),
                    "draft",
                    source["owner"],
                    timestamp,
                    timestamp,
                ),
            )

            pending_review = conn.execute(
                "SELECT id FROM review_items WHERE page_path = ? AND status = 'pending'",
                (rel,),
            ).fetchone()
            if pending_review is None:
                review_id = f"rev_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    """
                    INSERT INTO review_items(
                      id, page_path, issue_type, status, owner, source_ids_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review_id,
                        rel,
                        "changed_page" if existed else "new_page",
                        "pending",
                        source["owner"],
                        json_dump([source["id"]]),
                        timestamp,
                        timestamp,
                    ),
                )
                review_items += 1
            conn.execute(
                "UPDATE sources SET last_compiled_at = ?, updated_at = ? WHERE id = ?",
                (timestamp, timestamp, source["id"]),
            )
            audit(
                conn,
                "wiki_compiled",
                {"source_id": source["id"], "page": rel, "job_id": job_id},
                timestamp,
            )
            if existed:
                updated_pages += 1
            else:
                created_pages += 1

        write_indexes(settings, conn, request.domain, timestamp)

    append_log(
        settings.vault_path,
        "compile_log.md",
        f"- {timestamp} {job_id}: created={created_pages} updated={updated_pages} review_items={review_items}",
    )
    if settings.gbrain_import_on_compile:
        gbrain_import = import_vault_to_gbrain(settings)
        append_log(
            settings.vault_path,
            "gbrain_log.md",
            f"- {timestamp} {job_id}: import_on_compile ok={gbrain_import.ok} "
            f"attempted={gbrain_import.attempted} imported={gbrain_import.imported} "
            f"skipped={gbrain_import.skipped} errors={gbrain_import.errors} chunks={gbrain_import.chunks} "
            f"note={gbrain_import.note}",
        )
    return CompileResponse(
        job_id=job_id,
        created_pages=created_pages,
        updated_pages=updated_pages,
        review_items=review_items,
    )


def write_indexes(settings: Settings, conn, domain: str, timestamp: str) -> None:
    rows = conn.execute(
        "SELECT path, page_type, title, review_status FROM wiki_pages WHERE domain = ? ORDER BY page_type, title",
        (domain,),
    ).fetchall()
    lines = [f"# {domain} Index", "", f"Updated: {timestamp}", ""]
    for row in rows:
        lines.append(f"- [{row['title']}](../{row['path']}) `{row['page_type']}` `{row['review_status']}`")
    index_path = settings.vault_path / "index" / f"{domain}_index.md"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
