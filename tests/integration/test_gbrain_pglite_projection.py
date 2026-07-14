from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import pytest

from app.config import Settings
from app.db import connect_app, connect_app_write
from app.gbrain import call_gbrain_tool
from app.gbrain_projection import to_gbrain_manifest_path
from app.projection_jobs import ProjectionOutbox
from app.projection_worker import ProjectionWorker, WorkerRunResult


pytestmark = pytest.mark.gbrain_e2e
MIN_IN_FLIGHT_PROBE_SAMPLES = 20
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class SeededPage:
    page_id: str
    revision_id: str
    projection_epoch: int
    source_path: str
    database_path: str
    file_hash: str
    semantic_hash: str
    content: str
    disk_path: Path
    job_id: str

    @property
    def slug(self) -> str:
        return self.source_path.removesuffix(".md")


def _sha256(value: str | bytes) -> str:
    payload = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _page_content(page_id: str, revision_id: str, title: str, body: str) -> str:
    return (
        "---\n"
        f"id: {page_id}\n"
        f"lgdo_page_id: {page_id}\n"
        f"lgdo_revision_id: {revision_id}\n"
        "type: faq\n"
        f"title: {title}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{body}\n"
    )


def _write_page(
    settings: Settings,
    *,
    page_id: str,
    revision_id: str,
    projection_epoch: int,
    source_path: str,
    body: str,
    operation: str | None = None,
) -> SeededPage:
    manifest_path = to_gbrain_manifest_path(f"wiki/{source_path}")
    database_path = f"wiki/{manifest_path}"
    title = Path(manifest_path).stem.replace("-", " ").title()
    content = _page_content(page_id, revision_id, title, body)
    raw = content.encode("utf-8")
    file_hash = _sha256(raw)
    semantic_hash = _sha256(body.strip())
    timestamp = datetime.now(timezone.utc).isoformat()

    with connect_app_write(settings) as conn:
        existing = conn.execute(
            "SELECT path,current_revision_id,revision_number,lifecycle_status "
            "FROM wiki_pages WHERE page_id=?",
            (page_id,),
        ).fetchone()
        revision_number = int(existing["revision_number"] or 0) + 1 if existing else 1
        base_revision_id = str(existing["current_revision_id"]) if existing else None
        old_database_path = str(existing["path"]) if existing else None

        if old_database_path and old_database_path != database_path:
            old_disk_path = settings.vault_path / old_database_path
            if old_disk_path.exists() or old_disk_path.is_symlink():
                old_disk_path.unlink()

        disk_path = settings.vault_path / database_path
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_bytes(raw)

        conn.execute(
            """
            INSERT INTO wiki_page_revisions(
              id,page_id,page_path,revision_number,file_hash,semantic_hash,content,
              origin,base_revision_id,source_ids_json,actor,note,metadata_json,
              idempotency_key,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                revision_id,
                page_id,
                database_path,
                revision_number,
                file_hash,
                semantic_hash,
                content,
                "manual",
                base_revision_id,
                "[]",
                "gbrain-e2e",
                None,
                "{}",
                f"e2e:{revision_id}:{uuid.uuid4().hex}",
                timestamp,
            ),
        )
        if existing is None:
            conn.execute(
                """
                INSERT INTO wiki_pages(
                  path,page_id,domain,page_type,title,source_ids_json,review_status,
                  created_at,updated_at,current_revision_id,revision_number,file_hash,
                  semantic_hash,projection_epoch,lifecycle_status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    database_path,
                    page_id,
                    "product",
                    "faq",
                    title,
                    "[]",
                    "approved",
                    timestamp,
                    timestamp,
                    revision_id,
                    revision_number,
                    file_hash,
                    semantic_hash,
                    projection_epoch,
                    "active",
                ),
            )
            resolved_operation = operation or "upsert"
        else:
            conn.execute(
                """
                UPDATE wiki_pages
                SET path=?,title=?,updated_at=?,current_revision_id=?,revision_number=?,
                    file_hash=?,semantic_hash=?,projection_epoch=?,lifecycle_status='active'
                WHERE page_id=?
                """,
                (
                    database_path,
                    title,
                    timestamp,
                    revision_id,
                    revision_number,
                    file_hash,
                    semantic_hash,
                    projection_epoch,
                    page_id,
                ),
            )
            resolved_operation = operation or (
                "rename" if old_database_path != database_path else "upsert"
            )

        job_id = ProjectionOutbox(settings).enqueue(
            conn,
            target="gbrain",
            operation=resolved_operation,
            page_id=page_id,
            revision_id=revision_id,
            projection_epoch=projection_epoch,
            payload={"path": database_path},
        )

    return SeededPage(
        page_id=page_id,
        revision_id=revision_id,
        projection_epoch=projection_epoch,
        source_path=manifest_path,
        database_path=database_path,
        file_hash=file_hash,
        semantic_hash=semantic_hash,
        content=content,
        disk_path=disk_path,
        job_id=job_id,
    )


def _delete_page(settings: Settings, page: SeededPage) -> str:
    timestamp = datetime.now(timezone.utc).isoformat()
    next_epoch = page.projection_epoch + 1
    if page.disk_path.exists() or page.disk_path.is_symlink():
        page.disk_path.unlink()
    with connect_app_write(settings) as conn:
        conn.execute(
            """
            UPDATE wiki_pages
            SET lifecycle_status='deleted',projection_epoch=?,updated_at=?
            WHERE page_id=?
            """,
            (next_epoch, timestamp, page.page_id),
        )
        return ProjectionOutbox(settings).enqueue(
            conn,
            target="gbrain",
            operation="delete",
            page_id=page.page_id,
            revision_id=page.revision_id,
            projection_epoch=next_epoch,
            payload={"path": page.database_path},
        )


def _run_worker(settings: Settings, deadline_seconds: float) -> WorkerRunResult:
    async def run() -> WorkerRunResult:
        worker = ProjectionWorker(settings)
        return await asyncio.wait_for(
            worker.run_once("gbrain"),
            timeout=deadline_seconds,
        )

    return asyncio.run(run())


def _query(settings: Settings, text: str, *, limit: int = 20) -> list[dict[str, Any]]:
    payload = call_gbrain_tool(
        settings,
        "query",
        {
            "query": text,
            "limit": limit,
            "detail": "high",
            "expand": False,
            "relational": False,
        },
        timeout=settings.gbrain_query_timeout_seconds,
    )
    assert isinstance(payload, list), f"query returned {type(payload).__name__}"
    return [dict(item) for item in payload if isinstance(item, dict)]


def _hit_text(hit: dict[str, Any]) -> str:
    return str(hit.get("chunk_text") or hit.get("snippet") or hit.get("compiled_truth") or "")


def _wait_for_query(
    settings: Settings,
    text: str,
    predicate: Callable[[list[dict[str, Any]]], bool],
    *,
    timeout_seconds: float = 15,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    last: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        last = _query(settings, text)
        if predicate(last):
            return last
        time.sleep(0.1)
    raise AssertionError(f"query condition was not met for {text!r}: {json.dumps(last)}")


def _percentile(values: list[float], percentile: float) -> float:
    assert values
    ordered = sorted(values)
    index = max(0, math.ceil((percentile / 100) * len(ordered)) - 1)
    return ordered[index]


def _job_error(settings: Settings, job_id: str) -> str:
    with connect_app(settings) as conn:
        row = conn.execute(
            "SELECT status,last_error FROM knowledge_projection_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    assert row is not None
    return f"{row['status']}: {row['last_error'] or ''}"


def _is_directory_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _create_directory_link(link: Path, target: Path) -> None:
    if os.name != "nt":
        link.symlink_to(target, target_is_directory=True)
        return
    env = {
        **os.environ,
        "LGDO_E2E_LINK": os.fspath(link),
        "LGDO_E2E_TARGET": os.fspath(target),
    }
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "New-Item -ItemType Junction -Path $env:LGDO_E2E_LINK "
            "-Target $env:LGDO_E2E_TARGET -ErrorAction Stop | Out-Null",
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(
            "failed to create Windows junction: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )


def _remove_directory_link(link: Path) -> None:
    if not _is_directory_link(link):
        if link.exists():
            raise AssertionError(f"refusing to remove non-link directory: {link}")
        return
    if os.name == "nt":
        link.rmdir()
    else:
        link.unlink()
    if link.exists() or _is_directory_link(link):
        raise AssertionError(f"directory link was not removed: {link}")


def _run_responsiveness_probe(
    settings: Settings,
    health_url: str,
    query_text: str,
    expected_slug: str,
    start_path: Path,
    stop_path: Path,
    ready_path: Path,
) -> dict[str, list[float]]:
    health_latencies: list[float] = []
    query_latencies: list[float] = []
    with httpx.Client(timeout=5, trust_env=False) as health_client:
        ready_path.write_text("ready\n", encoding="ascii")
        start_deadline = time.monotonic() + 30
        while not start_path.exists():
            if time.monotonic() >= start_deadline:
                raise AssertionError("responsiveness probe did not receive start signal")
            time.sleep(0.01)
        while not stop_path.exists():
            health_started = time.monotonic()
            response = health_client.get(health_url)
            health_latencies.append(time.monotonic() - health_started)
            response.raise_for_status()

            query_started = time.monotonic()
            hits = _query(settings, query_text)
            query_latencies.append(time.monotonic() - query_started)
            if not any(row.get("slug") == expected_slug for row in hits):
                raise AssertionError(
                    f"responsiveness sentinel {expected_slug!r} was not queryable"
                )
    return {
        "health_latencies": health_latencies,
        "query_latencies": query_latencies,
    }


def _run_probe_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--probe", action="store_true", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--start", type=Path, required=True)
    parser.add_argument("--stop", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    args = parser.parse_args(argv)
    settings = Settings(
        gbrain_enabled=True,
        gbrain_endpoint=args.endpoint,
        gbrain_api_key=None,
        gbrain_query_api_key=args.token,
        gbrain_query_expand=False,
        gbrain_query_detail="high",
        gbrain_query_limit=20,
        gbrain_query_timeout_seconds=30,
        _env_file=None,
    )
    payload = _run_responsiveness_probe(
        settings,
        args.endpoint.removesuffix("/mcp") + "/health",
        args.query,
        args.slug,
        args.start,
        args.stop,
        args.ready,
    )
    sys.stdout.write(json.dumps(payload, separators=(",", ":")))
    return 0


def test_live_projection_stays_up_and_query_returns_projection_identity(
    gbrain_pglite_server,
    gbrain_e2e_settings: Settings,
):
    token = f"liveprojection{uuid.uuid4().hex}"
    page = _write_page(
        gbrain_e2e_settings,
        page_id=f"page-{uuid.uuid4().hex}",
        revision_id=f"rev-{uuid.uuid4().hex}",
        projection_epoch=1,
        source_path="product/faq/live-demo.md",
        body=f"{token} imported through the live projection server.",
    )

    assert gbrain_pglite_server.process.poll() is None
    result = _run_worker(gbrain_e2e_settings, 120)
    assert result.succeeded == 1
    assert result.failed == 0
    assert gbrain_pglite_server.process.poll() is None

    hits = _wait_for_query(
        gbrain_e2e_settings,
        token,
        lambda rows: any(row.get("slug") == page.slug for row in rows),
    )
    hit = next(row for row in hits if row.get("slug") == page.slug)
    assert token in _hit_text(hit)
    assert hit["source_id"] == gbrain_pglite_server.source_id
    assert hit["source_path"] == "product/faq/live-demo.md"
    assert hit["content_hash"]
    assert int(hit["page_generation"]) > 0


def test_rename_removes_the_old_source_slug(
    gbrain_pglite_server,
    gbrain_e2e_settings: Settings,
):
    token = f"renameprojection{uuid.uuid4().hex}"
    page_id = f"page-{uuid.uuid4().hex}"
    original = _write_page(
        gbrain_e2e_settings,
        page_id=page_id,
        revision_id=f"rev-{uuid.uuid4().hex}",
        projection_epoch=1,
        source_path="product/faq/before-rename.md",
        body=f"{token} survives a path rename.",
    )
    assert _run_worker(gbrain_e2e_settings, 120).succeeded == 1
    assert any(row.get("slug") == original.slug for row in _query(gbrain_e2e_settings, token))

    renamed = _write_page(
        gbrain_e2e_settings,
        page_id=page_id,
        revision_id=f"rev-{uuid.uuid4().hex}",
        projection_epoch=2,
        source_path="product/faq/after-rename.md",
        body=f"{token} survives a path rename.",
        operation="rename",
    )
    result = _run_worker(gbrain_e2e_settings, 600)
    assert result.succeeded == 1

    hits = _wait_for_query(
        gbrain_e2e_settings,
        token,
        lambda rows: any(row.get("slug") == renamed.slug for row in rows)
        and all(row.get("slug") != original.slug for row in rows),
    )
    assert {row.get("slug") for row in hits if token in _hit_text(row)} == {renamed.slug}
    assert next(row for row in hits if row.get("slug") == renamed.slug)["source_path"] == renamed.source_path


def test_reconcile_delete_removes_ghost_and_restore_is_queryable(
    gbrain_pglite_server,
    gbrain_e2e_settings: Settings,
):
    token = f"restoreprojection{uuid.uuid4().hex}"
    page_id = f"page-{uuid.uuid4().hex}"
    original = _write_page(
        gbrain_e2e_settings,
        page_id=page_id,
        revision_id=f"rev-{uuid.uuid4().hex}",
        projection_epoch=1,
        source_path="product/faq/delete-restore.md",
        body=f"{token} original revision.",
    )
    assert _run_worker(gbrain_e2e_settings, 120).succeeded == 1
    assert any(token in _hit_text(row) for row in _query(gbrain_e2e_settings, token))

    delete_job_id = _delete_page(gbrain_e2e_settings, original)
    deleted = _run_worker(gbrain_e2e_settings, 600)
    assert deleted.succeeded == 1, _job_error(gbrain_e2e_settings, delete_job_id)
    _wait_for_query(
        gbrain_e2e_settings,
        token,
        lambda rows: all(row.get("slug") != original.slug for row in rows),
    )

    restored = _write_page(
        gbrain_e2e_settings,
        page_id=page_id,
        revision_id=f"rev-{uuid.uuid4().hex}",
        projection_epoch=3,
        source_path="product/faq/delete-restore.md",
        body=f"{token} restored revision.",
        operation="restore",
    )
    assert _run_worker(gbrain_e2e_settings, 120).succeeded == 1
    hits = _wait_for_query(
        gbrain_e2e_settings,
        token,
        lambda rows: any("restored revision" in _hit_text(row) for row in rows),
    )
    assert any(row.get("slug") == restored.slug for row in hits)


def test_oauth_tokens_cannot_cross_scope_or_source(
    gbrain_pglite_server,
    gbrain_mcp_call: Callable[..., Awaitable[dict]],
):
    base_payload = {
        "source_id": gbrain_pglite_server.source_id,
        "root": os.fspath(gbrain_pglite_server.root),
        "mode": "reconcile",
        "expected_pages": [],
        "protected_mappings": [],
        "no_embed": True,
        "idempotency_key": f"security-{uuid.uuid4().hex}",
    }

    scope_error = asyncio.run(
        gbrain_mcp_call(
            gbrain_pglite_server,
            token=gbrain_pglite_server.query_token,
            tool="lgdo_vault_sync",
            arguments=base_payload,
        )
    )
    assert scope_error["is_error"] is True
    assert scope_error["error"] == "insufficient_scope"

    source_error = asyncio.run(
        gbrain_mcp_call(
            gbrain_pglite_server,
            token=gbrain_pglite_server.projection_token,
            tool="lgdo_vault_sync",
            arguments={
                **base_payload,
                "source_id": "other-source",
                "idempotency_key": f"cross-source-{uuid.uuid4().hex}",
            },
        )
    )
    assert source_error["is_error"] is True
    assert (
        source_error["message"]
        == "operation context source does not match input source_id"
    )


def test_manifest_excludes_unlisted_markdown_and_ignores_parent_gitignore(
    gbrain_pglite_server,
    gbrain_e2e_settings: Settings,
):
    listed_token = f"listedprojection{uuid.uuid4().hex}"
    unlisted_token = f"unlistedprojection{uuid.uuid4().hex}"
    (gbrain_e2e_settings.vault_path / ".gitignore").write_text(
        "wiki/**/*.md\n",
        encoding="utf-8",
    )
    unlisted = gbrain_pglite_server.root / "product" / "faq" / "unlisted.md"
    unlisted.parent.mkdir(parents=True, exist_ok=True)
    unlisted.write_bytes(
        _page_content(
            f"page-{uuid.uuid4().hex}",
            f"rev-{uuid.uuid4().hex}",
            "Unlisted",
            unlisted_token,
        ).encode("utf-8")
    )

    listed = _write_page(
        gbrain_e2e_settings,
        page_id=f"page-{uuid.uuid4().hex}",
        revision_id=f"rev-{uuid.uuid4().hex}",
        projection_epoch=1,
        source_path="product/faq/gitignored-but-listed.md",
        body=f"{listed_token} remains governed by the manifest.",
    )
    assert _run_worker(gbrain_e2e_settings, 120).succeeded == 1
    assert any(row.get("slug") == listed.slug for row in _query(gbrain_e2e_settings, listed_token))
    assert all(unlisted_token not in _hit_text(row) for row in _query(gbrain_e2e_settings, unlisted_token))


def test_symlink_manifest_entry_is_rejected(
    gbrain_pglite_server,
    gbrain_e2e_settings: Settings,
):
    token = f"symlinkprojection{uuid.uuid4().hex}"
    page = _write_page(
        gbrain_e2e_settings,
        page_id=f"page-{uuid.uuid4().hex}",
        revision_id=f"rev-{uuid.uuid4().hex}",
        projection_epoch=1,
        source_path="product/junction-segment/symlink.md",
        body=f"{token} must never be imported through a symlink.",
    )
    link = page.disk_path.parent
    target = gbrain_e2e_settings.vault_path / "outside-root-dir"
    target_file = target / page.disk_path.name
    target.mkdir()
    target_file.write_bytes(page.content.encode("utf-8"))
    page.disk_path.unlink()
    link.rmdir()
    _create_directory_link(link, target)
    try:
        assert _is_directory_link(link)
        result = _run_worker(gbrain_e2e_settings, 120)
        assert result.failed == 1
        assert (
            f"symlink path components are not allowed: {page.source_path}"
            in _job_error(gbrain_e2e_settings, page.job_id)
        )
        assert all(token not in _hit_text(row) for row in _query(gbrain_e2e_settings, token))
    finally:
        _remove_directory_link(link)
        target_file.unlink()
        target.rmdir()


def test_cached_old_snippet_disappears_after_revision_update(
    gbrain_pglite_server,
    gbrain_e2e_settings: Settings,
    fake_llama_server,
):
    token = f"cacheprojection{uuid.uuid4().hex}"
    page_id = f"page-{uuid.uuid4().hex}"
    original = _write_page(
        gbrain_e2e_settings,
        page_id=page_id,
        revision_id=f"rev-{uuid.uuid4().hex}",
        projection_epoch=1,
        source_path="product/faq/cache-demo.md",
        body=f"{token} old-snippet.",
    )
    assert _run_worker(gbrain_e2e_settings, 120).succeeded == 1
    embedding_requests = fake_llama_server.request_count
    first_hits = _query(gbrain_e2e_settings, token)
    assert fake_llama_server.request_count - embedding_requests == 2
    assert any("old-snippet" in _hit_text(row) for row in first_hits)

    embedding_requests = fake_llama_server.request_count
    cached_hits = _query(gbrain_e2e_settings, token)
    assert fake_llama_server.request_count - embedding_requests == 1
    assert any("old-snippet" in _hit_text(row) for row in cached_hits)

    updated = _write_page(
        gbrain_e2e_settings,
        page_id=page_id,
        revision_id=f"rev-{uuid.uuid4().hex}",
        projection_epoch=2,
        source_path=original.source_path,
        body=f"{token} new-snippet.",
    )
    assert _run_worker(gbrain_e2e_settings, 120).succeeded == 1
    embedding_requests = fake_llama_server.request_count
    hits = _query(gbrain_e2e_settings, token)
    assert fake_llama_server.request_count - embedding_requests == 2
    assert any("new-snippet" in _hit_text(row) for row in hits)
    assert all("old-snippet" not in _hit_text(row) for row in hits)
    assert any(row.get("slug") == updated.slug for row in hits)


def test_97_page_sync_keeps_health_and_query_responsive(
    gbrain_pglite_server,
    gbrain_e2e_settings: Settings,
):
    sentinel_token = f"responsiveness{uuid.uuid4().hex}"
    sentinel = _write_page(
        gbrain_e2e_settings,
        page_id=f"page-{uuid.uuid4().hex}",
        revision_id=f"rev-{uuid.uuid4().hex}",
        projection_epoch=1,
        source_path="product/faq/responsiveness-sentinel.md",
        body=f"{sentinel_token} remains queryable during bulk projection.",
    )
    assert _run_worker(gbrain_e2e_settings, 120).succeeded == 1
    assert any(row.get("slug") == sentinel.slug for row in _query(gbrain_e2e_settings, sentinel_token))

    bulk_token = f"bulkprojection{uuid.uuid4().hex}"
    last_page_token = f"bulkfinal{uuid.uuid4().hex}"
    pages = [
        _write_page(
            gbrain_e2e_settings,
            page_id=f"bulk-page-{index:03d}-{uuid.uuid4().hex}",
            revision_id=f"bulk-rev-{index:03d}-{uuid.uuid4().hex}",
            projection_epoch=1,
            source_path=f"product/faq/bulk-{index:03d}.md",
            body=(
                f"{bulk_token} page {index:03d}."
                + (f" {last_page_token}." if index == 96 else "")
            ),
        )
        for index in range(97)
    ]

    control_root = gbrain_e2e_settings.database_path.parent / "probe-control"
    control_root.mkdir()
    start_path = control_root / "start"
    stop_path = control_root / "stop"
    ready_path = control_root / "ready"
    probe_env = os.environ.copy()
    probe_env["PYTHONPATH"] = os.pathsep.join(
        value
        for value in (os.fspath(PROJECT_ROOT), probe_env.get("PYTHONPATH"))
        if value
    )
    no_proxy = ",".join(
        value
        for value in (probe_env.get("NO_PROXY") or probe_env.get("no_proxy"), "127.0.0.1", "localhost")
        if value
    )
    probe_env["NO_PROXY"] = no_proxy
    probe_env["no_proxy"] = no_proxy
    probe = subprocess.Popen(
        [
            sys.executable,
            os.fspath(Path(__file__).resolve()),
            "--probe",
            "--endpoint",
            gbrain_pglite_server.endpoint,
            "--token",
            gbrain_pglite_server.query_token,
            "--query",
            sentinel_token,
            "--slug",
            sentinel.slug,
            "--start",
            os.fspath(start_path),
            "--stop",
            os.fspath(stop_path),
            "--ready",
            os.fspath(ready_path),
        ],
        cwd=PROJECT_ROOT,
        env=probe_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    probe_stdout = ""
    probe_stderr = ""
    try:
        ready_deadline = time.monotonic() + 30
        while not ready_path.exists():
            if probe.poll() is not None:
                probe_stdout, probe_stderr = probe.communicate()
                raise AssertionError(
                    "responsiveness probe exited before ready "
                    f"({probe.returncode}):\n{probe_stderr or probe_stdout}"
                )
            if time.monotonic() >= ready_deadline:
                raise AssertionError("responsiveness probe did not become ready")
            time.sleep(0.01)
        start_path.write_text("start\n", encoding="ascii")
        assert probe.poll() is None, "responsiveness probe exited before projection"
        result = _run_worker(gbrain_e2e_settings, 120)
    finally:
        stop_path.write_text("stop\n", encoding="ascii")
        try:
            probe_stdout, probe_stderr = probe.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            probe.terminate()
            try:
                probe_stdout, probe_stderr = probe.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                probe.kill()
                probe_stdout, probe_stderr = probe.communicate(timeout=10)
        for control_path in (start_path, stop_path, ready_path):
            control_path.unlink(missing_ok=True)
        control_root.rmdir()

    assert result.succeeded == 97
    assert probe.returncode == 0, (
        f"responsiveness probe exited {probe.returncode}:\n{probe_stderr or probe_stdout}"
    )
    try:
        probe_payload = json.loads(probe_stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"responsiveness probe returned invalid JSON: {probe_stdout!r}\n{probe_stderr}"
        ) from exc
    health_latencies = probe_payload["health_latencies"]
    query_latencies = probe_payload["query_latencies"]
    assert len(health_latencies) == len(query_latencies)
    assert len(health_latencies) >= MIN_IN_FLIGHT_PROBE_SAMPLES, (
        f"only {len(health_latencies)} in-flight samples were collected"
    )
    health_p95 = _percentile(health_latencies, 95)
    query_p95 = _percentile(query_latencies, 95)
    print(
        f"GBrain live sync latency: health_p95={health_p95:.3f}s "
        f"query_p95={query_p95:.3f}s samples={len(health_latencies)} "
        f"health_max={max(health_latencies):.3f}s "
        f"query_max={max(query_latencies):.3f}s"
    )
    assert health_p95 < 1, f"health P95 was {health_p95:.3f}s"
    assert query_p95 < 5, f"query P95 was {query_p95:.3f}s"

    hits = _wait_for_query(
        gbrain_e2e_settings,
        last_page_token,
        lambda rows: any(row.get("slug") == pages[-1].slug for row in rows),
    )
    assert any(row.get("source_path") == pages[-1].source_path for row in hits)


if __name__ == "__main__":
    try:
        raise SystemExit(_run_probe_cli(sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc(file=sys.stderr)
        raise SystemExit(1)
