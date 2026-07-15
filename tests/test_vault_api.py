from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import app.api as api_module
import app.main as main_module
from app.api import current_user
from app.auth import UserContext
from app.config import Settings
from app.db import connect_app, init_app_db
from app.main import app
from app.projection_jobs import ProjectionOutbox
from app.vault_events import VaultEventStore


@pytest.fixture
def configured_client(tmp_path, monkeypatch):
    settings = Settings(
        database_backend="sqlite",
        rag_store_backend="sqlite",
        database_path=tmp_path / "data" / "vault-api.db",
        vault_path=tmp_path / "vault",
        upload_path=tmp_path / "uploads",
        vault_watch_enabled=False,
        projection_worker_enabled=False,
        gbrain_enabled=False,
        gbrain_endpoint=None,
        gbrain_api_key=None,
        gbrain_query_api_key=None,
        gbrain_projection_api_key=None,
        gbrain_managed_source_id=None,
        gbrain_import_allowed_root=None,
        gbrain_source_id=None,
        _env_file=None,
    )
    init_app_db(settings)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "settings", settings)
    app.dependency_overrides[current_user] = lambda: UserContext(
        user_id="admin",
        role="admin",
        acl_tags=("*",),
    )
    previous_state = dict(app.state._state)
    try:
        with TestClient(app) as client:
            yield settings, client
    finally:
        app.dependency_overrides.pop(current_user, None)
        app.state._state.clear()
        app.state._state.update(previous_state)


def _seed_projection_job(
    settings: Settings,
    *,
    status: str,
    index: int,
) -> str:
    outbox = ProjectionOutbox(settings)
    with connect_app(settings) as conn:
        job_id = outbox.enqueue(
            conn,
            target="gbrain",
            operation="upsert",
            page_id=f"page-{index}",
            revision_id=f"revision-{index}",
            projection_epoch=index,
            payload={"path": f"wiki/product/faq/page-{index}.md"},
        )
        if status != "pending":
            conn.execute(
                """
                UPDATE knowledge_projection_jobs
                SET status=?,attempts=1,last_error=?
                WHERE id=?
                """,
                (status, "disabled target failure", job_id),
            )
    return job_id


def test_reconcile_endpoint_is_persisted_single_flight(
    configured_client,
    monkeypatch,
):
    settings, client = configured_client
    runtime = app.state.vault_sync
    started = threading.Event()
    release = threading.Event()

    async def blocked_inventory(*, reconcile_intents=True, **_kwargs):
        assert reconcile_intents is False
        started.set()
        await asyncio.to_thread(release.wait)
        return {"ingested": 0, "failed": 0, "projection_jobs": 0}

    monkeypatch.setattr(runtime, "reconcile_startup", blocked_inventory)
    try:
        first = client.post("/api/internal/vault/reconcile")
        assert first.status_code == 202
        assert started.wait(timeout=2)

        second = client.post("/api/internal/vault/reconcile")

        assert second.status_code == 202
        assert second.json()["job_id"] == first.json()["job_id"]
        assert set(first.json()) == {
            "job_id",
            "status",
            "result",
            "error_summary",
        }
        with connect_app(settings) as conn:
            active = conn.execute(
                """
                SELECT COUNT(*) FROM vault_reconcile_jobs
                WHERE scope='full' AND status IN ('queued','running')
                """
            ).fetchone()[0]
        assert active == 1

        missing = client.get("/api/internal/vault/reconcile/missing-job")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "vault reconcile job not found"
    finally:
        release.set()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        completed = client.get(
            f"/api/internal/vault/reconcile/{first.json()['job_id']}"
        )
        if completed.json().get("status") == "succeeded":
            break
        time.sleep(0.01)
    assert completed.status_code == 200
    assert completed.json()["status"] == "succeeded"


def test_reconcile_get_reports_runtime_unavailable_instead_of_job_missing(
    configured_client,
):
    _settings, client = configured_client
    runtime = app.state.vault_sync
    persisted = runtime.events.latest_reconcile()
    assert persisted is not None
    delattr(app.state, "vault_sync")
    try:
        response = client.get(
            f"/api/internal/vault/reconcile/{persisted.id}"
        )
    finally:
        app.state.vault_sync = runtime

    assert response.status_code == 503
    assert response.json()["detail"] == "vault sync runtime unavailable"


def test_reconcile_post_returns_503_while_runtime_is_stopping(
    configured_client,
):
    _settings, client = configured_client
    runtime = app.state.vault_sync
    runtime._stopping = True
    runtime._accepting_reconciles = False
    try:
        response = client.post("/api/internal/vault/reconcile")
    finally:
        runtime._stopping = False
        runtime._accepting_reconciles = True

    assert response.status_code == 503
    assert response.json()["detail"] == "vault sync runtime stopping"


def test_disabled_gbrain_failed_backlog_is_raw_diagnostic_only(
    configured_client,
):
    settings, client = configured_client
    _seed_projection_job(settings, status="pending", index=1)
    _seed_projection_job(settings, status="failed", index=2)

    response = client.get("/api/internal/vault/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["projection"]["gbrain"] == {
        "pending": 1,
        "running": 0,
        "failed": 1,
        "enabled": False,
        "configured": False,
        "degraded": False,
    }
    assert payload["projection_backlog"] == 0
    assert payload["configured"] is False
    assert payload["running"] is False
    assert payload["degraded"] is False
    assert payload["clean"] is True


def test_vault_status_falls_back_without_state_or_absolute_path_leak(
    configured_client,
):
    settings, client = configured_client
    store = VaultEventStore(settings)
    occurrence = store.begin_occurrence(
        "modify",
        "wiki/product/faq/fallback.md",
        detected_at=datetime.now(timezone.utc),
    )
    store.finish_occurrence(
        occurrence.id,
        "failed",
        error_summary=f"failed to watch {settings.vault_path.resolve()}",
    )
    previous_state = dict(app.state._state)
    app.state._state.clear()
    try:
        response = client.get("/api/internal/vault/status")
    finally:
        app.state._state.clear()
        app.state._state.update(previous_state)

    assert response.status_code == 200
    assert str(settings.vault_path.resolve()) not in response.text
    payload = response.json()
    assert payload["configured"] is False
    assert payload["running"] is False
    assert payload["clean"] is False
    assert payload["degraded"] is True
    assert payload["last_event_at"] is not None
    assert payload["last_error"] == "failed to watch <vault>"
    assert payload["pending_occurrences"] == 0
    assert payload["failed_occurrences"] == 1
    assert payload["pending_deletes"] == 0
    assert payload["open_issues"] == 0
    assert payload["invalid_pages"] == 0
    assert payload["projection_backlog"] == 0
    assert payload["obsidian"] == {
        "installed": [],
        "drifted": [],
        "vault_name": settings.effective_obsidian_vault_name,
    }


def test_vault_status_sanitizes_failed_reconcile_paths_but_admin_get_does_not(
    configured_client,
    monkeypatch,
):
    settings, client = configured_client
    store = app.state.vault_sync.events
    fixed_uuid = type("FixedUuid", (), {"hex": "f" * 32})()
    monkeypatch.setattr("app.vault_events.uuid.uuid4", lambda: fixed_uuid)
    job = store.request_reconcile("admin")
    owner = "status-path-test"
    now = datetime.now(timezone.utc)
    assert store.claim_reconcile(
        job.id,
        owner,
        now=now,
        lease_seconds=30,
    )
    vault_backslash = str(settings.vault_path.resolve())
    vault_forward = vault_backslash.replace("\\", "/")
    error = (
        f"inventory failed for {vault_backslash}\\wiki\\private\\secret.md; "
        "recovery backup at "
        f"{vault_forward}/.lgdo/obsidian-backups/job-1/page.md"
    )
    assert store.fail_reconcile(job.id, owner, error)
    assert store.latest_reconcile().id == job.id

    status = client.get("/api/internal/vault/status")
    admin_get = client.get(f"/api/internal/vault/reconcile/{job.id}")

    assert status.status_code == 200
    summary = status.json()["reconcile"]["error_summary"]
    assert "inventory failed" in summary
    assert "recovery backup" in summary
    assert vault_backslash not in summary
    assert vault_forward not in summary
    assert "wiki/private/secret.md" not in summary.replace("\\", "/")
    assert "obsidian-backups" not in summary
    assert vault_forward not in status.text
    assert vault_backslash.replace("\\", "\\\\") not in status.text

    assert admin_get.status_code == 200
    assert admin_get.json()["error_summary"] == error
