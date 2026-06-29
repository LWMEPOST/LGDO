from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from app.config import Settings
from app.db import audit, connect_app, init_app_db, json_dump
from app.models import ScanRequest, ScanResponse
from app.normalization import normalize_document, write_jsonl
from app.parsers import SUPPORTED_EXTENSIONS, read_document_text
from app.rag import upsert_document_chunks
from app.timeutil import now_iso
from app.vault import append_log, ensure_vault, slugify


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

def scan_sources(settings: Settings, request: ScanRequest) -> ScanResponse:
    init_app_db(settings)
    ensure_vault(settings.vault_path)
    root = Path(request.root_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"资料目录不存在或不是目录: {root}")

    job_id = f"job_scan_{uuid.uuid4().hex[:10]}"
    counters = {
        "new_files": 0,
        "changed_files": 0,
        "skipped_files": 0,
        "unsupported_files": 0,
    }
    timestamp = now_iso()

    with connect_app(settings) as conn:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            ext = path.suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                counters["unsupported_files"] += 1
                continue

            content_hash = file_hash(path)
            existing = conn.execute(
                "SELECT * FROM sources WHERE original_path = ?",
                (str(path),),
            ).fetchone()
            if existing and existing["content_hash"] == content_hash and not request.force_reindex:
                counters["skipped_files"] += 1
                continue

            source_id = existing["id"] if existing else f"src_{uuid.uuid4().hex[:12]}"
            parsed = read_document_text(path, settings.ocr_enabled)
            normalized = normalize_document(
                source_id=source_id,
                title=path.stem,
                original_path=path,
                domain=request.domain,
                owner=request.owner,
                acl_tags=request.acl_tags,
                content_hash=content_hash,
                parsed=parsed,
                metadata_defaults=request.metadata_defaults,
                chunk_size=settings.rag_chunk_size,
                chunk_overlap=settings.rag_chunk_overlap,
            )
            raw_name = f"{source_id}_{slugify(path.stem)}.md"
            raw_rel = Path("raw") / request.domain / raw_name
            raw_path = settings.vault_path / raw_rel
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(
                f"---\nsource_id: {source_id}\noriginal_path: {path}\n"
                f"content_hash: {content_hash}\ndomain: {request.domain}\n---\n\n{parsed.text}",
                encoding="utf-8",
            )
            normalized_rel = Path("normalized") / request.domain / raw_name
            normalized_path = settings.vault_path / normalized_rel
            normalized_path.parent.mkdir(parents=True, exist_ok=True)
            normalized_path.write_text(normalized.markdown, encoding="utf-8")

            jsonl_rel = Path("jsonl") / request.domain / f"{source_id}_{slugify(path.stem)}.jsonl"
            jsonl_path = settings.vault_path / jsonl_rel
            write_jsonl(jsonl_path, normalized.chunks)
            upsert_document_chunks(settings, normalized.chunks, conn)

            metadata = {
                "extension": ext,
                "relative_to_scan_root": str(path.relative_to(root)),
                "scan_job_id": job_id,
                "acl_tags": request.acl_tags,
                "parser": parsed.parser,
                "ocr_used": parsed.ocr_used,
                "warnings": normalized.warnings,
                "normalized_path": str(normalized_rel).replace("\\", "/"),
                "jsonl_path": str(jsonl_rel).replace("\\", "/"),
                "chunk_count": len(normalized.chunks),
                "cleaned": normalized.metadata,
            }
            values = (
                source_id,
                request.domain,
                request.owner,
                path.stem,
                ext.lstrip(".") or "file",
                str(path),
                str(raw_rel).replace("\\", "/"),
                content_hash,
                path.stat().st_size,
                "active",
                json_dump(metadata),
                timestamp,
                timestamp,
            )
            if existing:
                conn.execute(
                    """
                    UPDATE sources
                    SET domain = ?, owner = ?, title = ?, source_type = ?, raw_path = ?,
                        content_hash = ?, size_bytes = ?, status = ?, metadata_json = ?,
                        updated_at = ?, last_compiled_at = NULL
                    WHERE id = ?
                    """,
                    (
                        request.domain,
                        request.owner,
                        path.stem,
                        ext.lstrip(".") or "file",
                        str(raw_rel).replace("\\", "/"),
                        content_hash,
                        path.stat().st_size,
                        "active",
                        json_dump(metadata),
                        timestamp,
                        source_id,
                    ),
                )
                counters["changed_files"] += 1
            else:
                conn.execute(
                    """
                    INSERT INTO sources(
                      id, domain, owner, title, source_type, original_path, raw_path,
                      content_hash, size_bytes, status, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                counters["new_files"] += 1

            audit(
                conn,
                "source_scanned",
                {"source_id": source_id, "path": str(path), "job_id": job_id},
                timestamp,
            )
            conn.execute(
                """
                INSERT INTO ingest_reports(
                  id, source_id, job_id, parser, normalized_path, jsonl_path,
                  chunk_count, char_count, warnings_json, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"ing_{uuid.uuid4().hex[:12]}",
                    source_id,
                    job_id,
                    parsed.parser,
                    str(normalized_rel).replace("\\", "/"),
                    str(jsonl_rel).replace("\\", "/"),
                    len(normalized.chunks),
                    normalized.metadata["char_count"],
                    json_dump(normalized.warnings),
                    json_dump(normalized.metadata),
                    timestamp,
                ),
            )

    append_log(
        settings.vault_path,
        "ingest_log.md",
        f"- {timestamp} {job_id}: new={counters['new_files']} changed={counters['changed_files']} "
        f"skipped={counters['skipped_files']} unsupported={counters['unsupported_files']}",
    )
    return ScanResponse(job_id=job_id, **counters)
