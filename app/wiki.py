from __future__ import annotations

import json
import re
from pathlib import Path

from app.config import Settings
from app.db import audit, connect_app, init_app_db, rows_to_dicts
from app.models import CompileRequest, CompileResponse
from app.timeutil import now_iso
from app.vault import append_log, ensure_vault, slugify
from app.wiki_revisions import CompileCandidateCommand, WikiRevisionService

COMPILER_VERSION = "wiki-revision-v1"


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
    job_id = request.compile_job_id
    timestamp = now_iso()
    created_pages = 0
    updated_pages = 0
    conflicted_pages = 0
    projection_jobs = 0
    projection_job_ids: list[str] = []
    seen_projection_job_ids: set[str] = set()

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

    service = WikiRevisionService(settings)
    for source in sources:
        raw_text = read_source_text(settings, source)
        page_type = pick_page_type(source["title"], raw_text)
        if request.page_types and page_type not in request.page_types:
            continue

        rel_path = wiki_rel_path(
            source["domain"],
            page_type,
            source["title"],
            source["id"],
        )
        rel = str(rel_path).replace("\\", "/")
        with connect_app(settings) as conn:
            before = conn.execute(
                "SELECT current_revision_id FROM wiki_pages WHERE path=?",
                (rel,),
            ).fetchone()
        transition = service.apply_generated_candidate(
            CompileCandidateCommand(
                page_path=rel,
                content=build_page(source, raw_text, page_type),
                domain=source["domain"],
                page_type=page_type,
                title=source["title"],
                source_ids=[source["id"]],
                owner=source["owner"],
                source_hash=str(source["content_hash"] or ""),
                compiler_version=COMPILER_VERSION,
                compile_job_id=job_id,
            )
        )

        with connect_app(settings) as conn:
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
        if transition.status == "conflicted":
            conflicted_pages += 1
        elif transition.replayed:
            pass
        elif before is None:
            created_pages += 1
        else:
            updated_pages += 1
        for projection_job_id in transition.projection_job_ids:
            if projection_job_id not in seen_projection_job_ids:
                seen_projection_job_ids.add(projection_job_id)
                projection_job_ids.append(projection_job_id)
        if not transition.replayed:
            projection_jobs += len(transition.projection_job_ids)

    with connect_app(settings) as conn:
        write_indexes(settings, conn, request.domain, timestamp)

    review_items = conflicted_pages
    append_log(
        settings.vault_path,
        "compile_log.md",
        f"- {timestamp} {job_id}: created={created_pages} updated={updated_pages} "
        f"review_items={review_items} conflicted={conflicted_pages} "
        f"projection_jobs={projection_jobs}",
    )
    return CompileResponse(
        job_id=job_id,
        created_pages=created_pages,
        updated_pages=updated_pages,
        review_items=review_items,
        conflicted_pages=conflicted_pages,
        projection_jobs=projection_jobs,
        projection_job_ids=projection_job_ids,
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
