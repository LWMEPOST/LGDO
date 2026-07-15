from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Any, Callable
import urllib.request

import httpx
import pytest

from app.config import Settings
from app.db import connect_app, connect_app_write, init_app_db
from app.models import AskRequest
from app.projection_worker import ProjectionWorker
from app.search import CITATION_REFUSAL, ask
from app.vault_sync import VaultSyncService


POLL_TIMEOUT_SECONDS = 5.0


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(
        database_backend="sqlite",
        database_path=tmp_path / "db" / "obsidian-e2e.db",
        vault_path=tmp_path / "vault",
        rag_store_backend="sqlite",
        rag_embedding_provider="local-hash",
        dashscope_embedding_enabled=False,
        vault_watch_enabled=True,
        vault_watch_debounce_ms=50,
        vault_watch_stability_timeout_seconds=0.25,
        vault_rename_grace_ms=5000,
        projection_worker_enabled=True,
        projection_poll_seconds=0.05,
        gbrain_enabled=False,
        gbrain_endpoint=None,
        gbrain_api_key=None,
        gbrain_query_api_key=None,
        gbrain_projection_api_key=None,
        gbrain_managed_source_id=None,
        deepseek_api_key=None,
        deepseek_model=None,
        _env_file=None,
    )
    (settings.vault_path / "wiki").mkdir(parents=True)
    init_app_db(settings)
    timestamp = "2026-07-16T00:00:00+00:00"
    with connect_app_write(settings) as conn:
        conn.execute(
            """
            INSERT INTO sources(
              id,domain,title,source_type,original_path,raw_path,content_hash,
              size_bytes,status,metadata_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "src_refund",
                "product",
                "Refund source",
                "markdown",
                "refund.md",
                "raw/product/refund.md",
                "a" * 64,
                1,
                "active",
                "{}",
                timestamp,
                timestamp,
            ),
        )
    return settings


def _forbid_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("external network access is forbidden in local Vault E2E")

    async def async_forbidden(*_args: Any, **_kwargs: Any) -> Any:
        forbidden()

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", async_forbidden)


async def _wait_for_state(
    label: str,
    read: Callable[[], Any],
    ready: Callable[[Any], bool],
    *,
    timeout: float = POLL_TIMEOUT_SECONDS,
) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last: Any = None
    while loop.time() < deadline:
        last = await asyncio.wait_for(
            asyncio.to_thread(read),
            timeout=min(1.0, max(0.01, deadline - loop.time())),
        )
        if ready(last):
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {label}; last state={last!r}")


def _page_state(
    settings: Settings,
    *,
    page_path: str | None = None,
    page_id: str | None = None,
) -> dict[str, Any]:
    with connect_app(settings) as conn:
        if page_id is not None:
            page = conn.execute(
                "SELECT * FROM wiki_pages WHERE page_id=?",
                (page_id,),
            ).fetchone()
        else:
            page = conn.execute(
                "SELECT * FROM wiki_pages WHERE path=?",
                (page_path,),
            ).fetchone()
        if page is None:
            return {"page": None}
        values = dict(page)
        stable_page_id = str(values["page_id"])
        current_revision_id = values["current_revision_id"]
        epoch = int(values["projection_epoch"])
        return {
            "page": values,
            "revision_count": conn.execute(
                "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
                (stable_page_id,),
            ).fetchone()[0],
            "content_revision_count": conn.execute(
                """
                SELECT COUNT(*) FROM wiki_page_revisions
                WHERE page_id=? AND origin!='rename'
                """,
                (stable_page_id,),
            ).fetchone()[0],
            "current_chunks": conn.execute(
                """
                SELECT COUNT(*) FROM wiki_chunks
                WHERE page_id=? AND revision_id=? AND projection_epoch=?
                """,
                (stable_page_id, current_revision_id, epoch),
            ).fetchone()[0],
            "all_chunks": conn.execute(
                "SELECT COUNT(*) FROM wiki_chunks WHERE page_id=?",
                (stable_page_id,),
            ).fetchone()[0],
            "active_intents": conn.execute(
                """
                SELECT COUNT(*) FROM vault_write_intents
                WHERE page_id=? AND status IN (
                  'pending','captured','installed','recovery_required'
                )
                """,
                (stable_page_id,),
            ).fetchone()[0],
            "pending_occurrences": conn.execute(
                "SELECT COUNT(*) FROM vault_watch_occurrences WHERE status='pending'"
            ).fetchone()[0],
            "occurrence_count": conn.execute(
                "SELECT COUNT(*) FROM vault_watch_occurrences"
            ).fetchone()[0],
            "pending_deletes": conn.execute(
                """
                SELECT COUNT(*) FROM pending_vault_deletes
                WHERE page_id=? AND status='pending'
                """,
                (stable_page_id,),
            ).fetchone()[0],
            "current_chunk_paths": [
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT DISTINCT page_path FROM wiki_chunks
                    WHERE page_id=? AND revision_id=? AND projection_epoch=?
                    ORDER BY page_path
                    """,
                    (stable_page_id, current_revision_id, epoch),
                ).fetchall()
            ],
            "rag_delete_statuses": [
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT status FROM knowledge_projection_jobs
                    WHERE page_id=? AND target='rag' AND operation='delete'
                      AND projection_epoch=?
                    ORDER BY created_at,id
                    """,
                    (stable_page_id, epoch),
                ).fetchall()
            ],
        }


def _occurrence_state(settings: Settings, page_path: str) -> dict[str, Any]:
    with connect_app(settings) as conn:
        latest = conn.execute(
            """
            SELECT * FROM vault_watch_occurrences
            WHERE page_path=? ORDER BY rowid DESC LIMIT 1
            """,
            (page_path,),
        ).fetchone()
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE path=?",
            (page_path,),
        ).fetchone()
        page_id = str(page["page_id"]) if page is not None else None
        revision_count = (
            conn.execute(
                "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=?",
                (page_id,),
            ).fetchone()[0]
            if page_id is not None
            else 0
        )
        active_intents = (
            conn.execute(
                """
                SELECT COUNT(*) FROM vault_write_intents
                WHERE page_id=? AND status IN (
                  'pending','captured','installed','recovery_required'
                )
                """,
                (page_id,),
            ).fetchone()[0]
            if page_id is not None
            else 0
        )
        return {
            "page": dict(page) if page is not None else None,
            "revision_count": revision_count,
            "active_intents": active_intents,
            "pending_occurrences": conn.execute(
                "SELECT COUNT(*) FROM vault_watch_occurrences WHERE status='pending'"
            ).fetchone()[0],
            "occurrence_count": conn.execute(
                "SELECT COUNT(*) FROM vault_watch_occurrences WHERE page_path=?",
                (page_path,),
            ).fetchone()[0],
            "latest": dict(latest) if latest is not None else None,
        }


async def _ask_once(settings: Settings, token: str):
    return await asyncio.wait_for(
        asyncio.to_thread(
            ask,
            settings,
            AskRequest(
                question=f"What policy is identified by {token}?",
                domain="product",
                require_citations=True,
            ),
        ),
        timeout=2.0,
    )


def _assert_current_wiki_citation(
    response: Any,
    *,
    token: str,
    page_id: str,
    page_path: str,
    revision_id: str,
) -> None:
    citation = next(
        (
            item
            for item in response.citations
            if item.origin == "wiki"
            and item.source_id == "src_refund"
            and item.page_id == page_id
        ),
        None,
    )
    assert citation is not None
    assert citation.wiki_page == page_path
    assert citation.revision_id == revision_id
    assert citation.chunk_id is not None
    assert token in citation.snippet


@pytest.mark.asyncio
async def test_real_watcher_converges_add_modify_rename_delete_into_local_citations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    _forbid_external_network(monkeypatch)
    worker = ProjectionWorker(settings)
    service = VaultSyncService(settings)
    old_page_path = "wiki/product/refund-live.md"
    new_page_path = "wiki/product/refund-current.md"
    old_target = settings.vault_path / old_page_path
    new_target = settings.vault_path / new_page_path

    await asyncio.wait_for(worker.start(), timeout=POLL_TIMEOUT_SECONDS)
    await asyncio.wait_for(service.start(), timeout=POLL_TIMEOUT_SECONDS)
    await asyncio.sleep(0.1)
    try:
        old_target.parent.mkdir(parents=True, exist_ok=True)
        old_target.write_text(
            "---\n"
            "title: Refund watcher policy\n"
            "source_ids: [src_refund]\n"
            "domain: product\n"
            "page_type: policy\n"
            "review_status: draft\n"
            "owner:\n"
            "---\n"
            "# Refund watcher policy\n\n"
            "refundalpha731 is the original watcher refund marker.\n",
            encoding="utf-8",
        )

        added = await _wait_for_state(
            "add projection and managed writeback",
            lambda: _page_state(settings, page_path=old_page_path),
            lambda state: bool(
                state.get("page")
                and state["page"]["lifecycle_status"] == "active"
                and state["page"]["current_revision_id"] is not None
                and state["page"]["current_revision_id"]
                == state["page"]["rag_visible_revision_id"]
                and state["page"]["rag_visible_epoch"] is not None
                and int(state["page"]["projection_epoch"])
                == int(state["page"]["rag_visible_epoch"])
                and state["current_chunks"] > 0
                and state["revision_count"] == 1
                and state["active_intents"] == 0
                and state["pending_occurrences"] == 0
                and state["occurrence_count"] >= 2
                and b"lgdo_page_id:" in old_target.read_bytes()
            ),
        )
        page_id = str(added["page"]["page_id"])
        first_revision_id = str(added["page"]["current_revision_id"])
        added_answer = await _ask_once(settings, "refundalpha731")
        _assert_current_wiki_citation(
            added_answer,
            token="refundalpha731",
            page_id=page_id,
            page_path=old_page_path,
            revision_id=first_revision_id,
        )

        managed_bytes = old_target.read_bytes()
        assert b"refundalpha731" in managed_bytes
        old_target.write_bytes(
            managed_bytes.replace(b"refundalpha731", b"refundbeta842")
        )
        modified = await _wait_for_state(
            "modified revision and citation projection",
            lambda: _page_state(settings, page_id=page_id),
            lambda state: bool(
                state.get("page")
                and state["page"]["path"] == old_page_path
                and state["page"]["current_revision_id"] != first_revision_id
                and state["page"]["current_revision_id"]
                == state["page"]["rag_visible_revision_id"]
                and int(state["page"]["projection_epoch"])
                == int(state["page"]["rag_visible_epoch"])
                and state["current_chunks"] > 0
                and state["revision_count"] == 2
                and state["active_intents"] == 0
                and state["page"]["pending_write_intent_id"] is None
                and state["pending_occurrences"] == 0
                and state["occurrence_count"] >= added["occurrence_count"] + 2
                and hashlib.sha256(old_target.read_bytes()).hexdigest()
                == state["page"]["file_hash"]
            ),
        )
        second_revision_id = str(modified["page"]["current_revision_id"])
        modified_answer = await _ask_once(settings, "refundbeta842")
        _assert_current_wiki_citation(
            modified_answer,
            token="refundbeta842",
            page_id=page_id,
            page_path=old_page_path,
            revision_id=second_revision_id,
        )

        old_target.rename(new_target)
        renamed = await _wait_for_state(
            "exact-byte rename projection",
            lambda: _page_state(settings, page_id=page_id),
            lambda state: bool(
                state.get("page")
                and state["page"]["path"] == new_page_path
                and state["page"]["current_revision_id"] == second_revision_id
                and state["page"]["current_revision_id"]
                == state["page"]["rag_visible_revision_id"]
                and int(state["page"]["projection_epoch"])
                == int(state["page"]["rag_visible_epoch"])
                and state["content_revision_count"] == 2
                and state["current_chunk_paths"] == [new_page_path]
                and state["pending_deletes"] == 0
                and state["pending_occurrences"] == 0
            ),
        )
        renamed_answer = await _ask_once(settings, "refundbeta842")
        _assert_current_wiki_citation(
            renamed_answer,
            token="refundbeta842",
            page_id=page_id,
            page_path=new_page_path,
            revision_id=second_revision_id,
        )

        new_target.unlink()
        await _wait_for_state(
            "one deferred pending delete",
            lambda: _page_state(settings, page_id=page_id),
            lambda state: bool(
                state.get("page")
                and state["pending_deletes"] == 1
                and state["pending_occurrences"] == 0
            ),
        )
        expired = await asyncio.wait_for(
            service.expire_deletes(
                datetime.now(timezone.utc) + timedelta(seconds=6)
            ),
            timeout=POLL_TIMEOUT_SECONDS,
        )
        assert expired == 1

        deleted = await _wait_for_state(
            "delete projection physical cleanup",
            lambda: _page_state(settings, page_id=page_id),
            lambda state: bool(
                state.get("page")
                and state["page"]["lifecycle_status"] == "deleted"
                and state["page"]["rag_visible_revision_id"] is None
                and state["page"]["rag_visible_epoch"] is None
                and state["all_chunks"] == 0
                and state["rag_delete_statuses"] == ["succeeded"]
            ),
        )
        assert deleted["content_revision_count"] == 2
        deleted_answer = await _ask_once(settings, "refundbeta842")
        assert deleted_answer.citations == []
        assert deleted_answer.answer == CITATION_REFUSAL
        assert deleted_answer.confidence == "low"
    finally:
        await asyncio.wait_for(service.stop(), timeout=POLL_TIMEOUT_SECONDS)
        await asyncio.wait_for(worker.stop(), timeout=POLL_TIMEOUT_SECONDS)


@pytest.mark.asyncio
async def test_real_watcher_ignores_managed_exact_byte_writeback_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    _forbid_external_network(monkeypatch)
    service = VaultSyncService(settings)
    page_path = "wiki/product/exact-loop.md"
    target = settings.vault_path / page_path

    await asyncio.wait_for(service.start(), timeout=POLL_TIMEOUT_SECONDS)
    await asyncio.sleep(0.1)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "---\n"
            "title: Exact loop\n"
            "source_ids: []\n"
            "domain: product\n"
            "page_type: policy\n"
            "review_status: draft\n"
            "owner:\n"
            "---\n"
            "# Exact loop\n\n"
            "Managed exact-byte loop body.\n",
            encoding="utf-8",
        )

        settled = await _wait_for_state(
            "managed writeback loop to become idle",
            lambda: _occurrence_state(settings, page_path),
            lambda state: bool(
                state["page"]
                and state["revision_count"] == 1
                and state["active_intents"] == 0
                and state["pending_occurrences"] == 0
                and state["occurrence_count"] >= 2
                and state["latest"]
                and state["latest"]["status"] == "ignored"
                and state["page"]["pending_write_intent_id"] is None
                and b"lgdo_page_id:" in target.read_bytes()
                and b"lgdo_revision_id:" in target.read_bytes()
            ),
        )
        managed_bytes = target.read_bytes()
        occurrence_count = int(settled["occurrence_count"])

        target.write_bytes(managed_bytes)
        rewritten = await _wait_for_state(
            "explicit exact-byte rewrite terminal occurrence",
            lambda: _occurrence_state(settings, page_path),
            lambda state: bool(
                state["occurrence_count"] > occurrence_count
                and state["pending_occurrences"] == 0
                and state["active_intents"] == 0
                and state["revision_count"] == 1
                and state["latest"]
                and state["latest"]["status"] == "ignored"
            ),
        )
        assert rewritten["latest"]["result_revision_id"] == rewritten["page"][
            "current_revision_id"
        ]
        with connect_app(settings) as conn:
            event = conn.execute(
                "SELECT status FROM vault_change_events WHERE id=?",
                (rewritten["latest"]["id"],),
            ).fetchone()
            observation = conn.execute(
                """
                SELECT parse_status FROM wiki_file_observations
                WHERE page_path=? ORDER BY rowid DESC LIMIT 1
                """,
                (page_path,),
            ).fetchone()
        assert event["status"] == "ignored"
        assert observation["parse_status"] == "valid"
    finally:
        await asyncio.wait_for(service.stop(), timeout=POLL_TIMEOUT_SECONDS)
