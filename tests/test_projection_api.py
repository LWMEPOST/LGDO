from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.gbrain as gbrain_module
import app.main as main_module
from app.api import current_user
from app.auth import UserContext
from app.config import get_settings
from app.db import connect_app, init_app_db
from app.main import app
from app.projection_jobs import ProjectionOutbox


@pytest.fixture
def projection_api(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "api.db")
    monkeypatch.setattr(settings, "vault_path", tmp_path / "vault")
    monkeypatch.setattr(settings, "upload_path", tmp_path / "uploads")
    monkeypatch.setattr(settings, "projection_worker_enabled", False)
    monkeypatch.setattr(settings, "gbrain_enabled", False)
    monkeypatch.setattr(settings, "gbrain_endpoint", None)
    monkeypatch.setattr(settings, "gbrain_projection_api_key", None)
    monkeypatch.setattr(settings, "gbrain_managed_source_id", None)
    monkeypatch.setattr(settings, "gbrain_import_on_compile", False)
    monkeypatch.setattr("app.api.get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "settings", settings)
    init_app_db(settings)

    app.dependency_overrides[current_user] = lambda: UserContext(
        user_id="admin",
        role="admin",
        acl_tags=("*",),
    )
    try:
        with TestClient(app) as client:
            yield settings, client
    finally:
        app.dependency_overrides.pop(current_user, None)


def seed_job(
    settings,
    *,
    target: str,
    status: str = "pending",
    page_id: str,
    index: int,
) -> str:
    outbox = ProjectionOutbox(settings)
    with connect_app(settings) as conn:
        job_id = outbox.enqueue(
            conn,
            target=target,
            operation="upsert",
            page_id=page_id,
            revision_id=f"wrev_{index}",
            projection_epoch=index,
            payload={"path": f"wiki/product/faq/{page_id}-{index}.md"},
        )
        if status != "pending":
            lease_owner = "worker-a" if status == "running" else None
            lease_expires_at = (
                datetime.now(timezone.utc) + timedelta(minutes=3)
                if status == "running"
                else None
            )
            conn.execute(
                """
                UPDATE knowledge_projection_jobs
                SET status=?, attempts=?, last_error=?, lease_owner=?, lease_expires_at=?
                WHERE id=?
                """,
                (
                    status,
                    5 if status == "failed" else 1,
                    "projection failed" if status == "failed" else None,
                    lease_owner,
                    lease_expires_at.isoformat() if lease_expires_at else None,
                    job_id,
                ),
            )
    return job_id


def test_projection_jobs_filters_and_caps_limit_at_100(projection_api):
    settings, client = projection_api
    selected_id = seed_job(
        settings,
        target="gbrain",
        status="failed",
        page_id="page-selected",
        index=1,
    )
    seed_job(
        settings,
        target="rag",
        status="failed",
        page_id="page-selected",
        index=2,
    )
    seed_job(
        settings,
        target="gbrain",
        status="pending",
        page_id="page-selected",
        index=3,
    )
    for index in range(4, 109):
        seed_job(
            settings,
            target="gbrain",
            page_id=f"page-bulk-{index}",
            index=index,
        )

    app.dependency_overrides[current_user] = lambda: UserContext(
        user_id="viewer",
        role="viewer",
    )
    filtered = client.get(
        "/api/internal/projection-jobs",
        params={
            "target": "gbrain",
            "status": "failed",
            "page_id": "page-selected",
        },
    )
    assert filtered.status_code == 200
    assert [row["id"] for row in filtered.json()] == [selected_id]

    capped = client.get("/api/internal/projection-jobs", params={"limit": 1000})
    assert capped.status_code == 200
    assert len(capped.json()) == 100


def test_retry_resets_only_failed_jobs_and_reports_state_conflicts(projection_api):
    settings, client = projection_api
    failed_id = seed_job(
        settings,
        target="gbrain",
        status="failed",
        page_id="page-failed",
        index=1,
    )
    incompatible = {
        status: seed_job(
            settings,
            target="rag",
            status=status,
            page_id=f"page-{status}",
            index=index,
        )
        for index, status in enumerate(("pending", "running", "succeeded"), start=2)
    }

    retried = client.post(f"/api/internal/projection-jobs/{failed_id}/retry")
    assert retried.status_code == 200
    body = retried.json()
    assert body["status"] == "pending"
    assert body["attempts"] == 0
    assert body["last_error"] is None
    assert body["lease_owner"] is None
    assert body["lease_expires_at"] is None

    for job_id in incompatible.values():
        response = client.post(f"/api/internal/projection-jobs/{job_id}/retry")
        assert response.status_code == 409
    replay = client.post(f"/api/internal/projection-jobs/{failed_id}/retry")
    assert replay.status_code == 409
    missing = client.post("/api/internal/projection-jobs/missing-job/retry")
    assert missing.status_code == 404


def test_viewer_cannot_retry_projection_job(projection_api):
    settings, client = projection_api
    job_id = seed_job(
        settings,
        target="gbrain",
        status="failed",
        page_id="page-viewer",
        index=1,
    )
    app.dependency_overrides[current_user] = lambda: UserContext(
        user_id="viewer",
        role="viewer",
    )

    response = client.post(f"/api/internal/projection-jobs/{job_id}/retry")

    assert response.status_code == 403
    with connect_app(settings) as conn:
        row = conn.execute(
            "SELECT status,attempts FROM knowledge_projection_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    assert tuple(row) == ("failed", 5)


def test_health_separates_query_circuit_from_projection_backlog(
    projection_api,
    monkeypatch,
):
    settings, client = projection_api
    settings.gbrain_enabled = True
    settings.gbrain_endpoint = "http://127.0.0.1:39999/mcp"
    settings.gbrain_projection_api_key = "projection-token"
    settings.gbrain_managed_source_id = "lgdo-managed"
    seed_job(
        settings,
        target="gbrain",
        status="running",
        page_id="page-running",
        index=1,
    )
    seed_job(
        settings,
        target="gbrain",
        status="failed",
        page_id="page-failed",
        index=2,
    )
    seed_job(
        settings,
        target="rag",
        status="pending",
        page_id="page-rag",
        index=3,
    )
    monkeypatch.setattr(
        main_module,
        "_gbrain_circuit_is_open",
        lambda _settings: False,
        raising=False,
    )

    healthy_query = client.get("/health")

    assert healthy_query.status_code == 200
    payload = healthy_query.json()
    assert payload["gbrain"]["query"] == {
        "available": True,
        "circuit_open": False,
    }
    assert payload["gbrain"]["projection"] == {
        "pending": 0,
        "running": 1,
        "failed": 1,
        "enabled": True,
        "configured": True,
        "degraded": True,
    }
    assert payload["rag"]["projection"] == {
        "pending": 1,
        "running": 0,
        "failed": 0,
    }

    monkeypatch.setattr(
        main_module,
        "_gbrain_circuit_is_open",
        lambda _settings: True,
    )
    open_circuit = client.get("/health").json()
    assert open_circuit["gbrain"]["query"] == {
        "available": False,
        "circuit_open": True,
    }


def test_health_marks_projection_degraded_when_credentials_are_missing(
    projection_api,
    monkeypatch,
):
    settings, client = projection_api
    settings.gbrain_enabled = True
    settings.gbrain_endpoint = "http://127.0.0.1:39999/mcp"
    settings.gbrain_projection_api_key = None
    settings.gbrain_managed_source_id = None
    monkeypatch.setattr(
        main_module,
        "_gbrain_circuit_is_open",
        lambda _settings: False,
        raising=False,
    )

    payload = client.get("/health").json()

    assert payload["gbrain"]["query"]["available"] is True
    assert payload["gbrain"]["projection"]["configured"] is False
    assert payload["gbrain"]["projection"]["degraded"] is True


def test_compile_returns_queued_projection_ids_without_sync_gbrain(
    projection_api,
    tmp_path,
    monkeypatch,
):
    settings, client = projection_api
    settings.gbrain_enabled = True
    settings.gbrain_import_on_compile = True
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "queued_compile.md").write_text(
        "# Queued compile\n\nProjection work must run outside the request.",
        encoding="utf-8",
    )

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("compile called synchronous GBrain CLI")

    monkeypatch.setattr(gbrain_module, "_run_gbrain_cli", fail_if_called)
    scan = client.post(
        "/api/internal/sources/scan",
        json={"root_path": str(sample_dir), "domain": "product"},
    )
    assert scan.status_code == 200

    started = time.monotonic()
    response = client.post(
        "/api/internal/wiki/compile",
        json={"domain": "product"},
    )
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert elapsed < 1.0
    body = response.json()
    assert body["projection_status"] == "queued"
    assert body["projection_jobs"] == 2
    assert len(body["projection_job_ids"]) == 2
    assert len(set(body["projection_job_ids"])) == 2
    with connect_app(settings) as conn:
        placeholders = ",".join("?" for _ in body["projection_job_ids"])
        rows = conn.execute(
            f"""
            SELECT id,target,status FROM knowledge_projection_jobs
            WHERE id IN ({placeholders}) ORDER BY target
            """,
            body["projection_job_ids"],
        ).fetchall()
    assert [(row["target"], row["status"]) for row in rows] == [
        ("gbrain", "pending"),
        ("rag", "pending"),
    ]


def test_compile_with_gbrain_import_disabled_queues_only_rag(projection_api, tmp_path):
    settings, client = projection_api
    settings.gbrain_import_on_compile = False
    sample_dir = tmp_path / "samples-rag-only"
    sample_dir.mkdir()
    (sample_dir / "rag_only.md").write_text(
        "# RAG only\n\nCompile configuration disables only the GBrain member.",
        encoding="utf-8",
    )
    scan = client.post(
        "/api/internal/sources/scan",
        json={"root_path": str(sample_dir), "domain": "product"},
    )
    assert scan.status_code == 200

    response = client.post(
        "/api/internal/wiki/compile",
        json={"domain": "product"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["projection_status"] == "queued"
    assert body["projection_jobs"] == 1
    assert len(body["projection_job_ids"]) == 1
    with connect_app(settings) as conn:
        row = conn.execute(
            "SELECT target,status FROM knowledge_projection_jobs WHERE id=?",
            (body["projection_job_ids"][0],),
        ).fetchone()
    assert tuple(row) == ("rag", "pending")


def test_manual_save_still_queues_both_targets_when_compile_gbrain_is_disabled(
    projection_api,
    tmp_path,
):
    settings, client = projection_api
    settings.gbrain_import_on_compile = False
    sample_dir = tmp_path / "samples-manual"
    sample_dir.mkdir()
    (sample_dir / "manual_projection.md").write_text(
        "# Manual projection\n\nThe initial compile is RAG-only.",
        encoding="utf-8",
    )
    scan = client.post(
        "/api/internal/sources/scan",
        json={"root_path": str(sample_dir), "domain": "product"},
    )
    assert scan.status_code == 200
    compiled = client.post(
        "/api/internal/wiki/compile",
        json={"domain": "product"},
    )
    assert compiled.status_code == 200

    pages = client.get("/api/internal/wiki/pages", params={"domain": "product"})
    assert pages.status_code == 200
    page_path = pages.json()[0]["path"]
    current = client.get(f"/api/internal/wiki/pages/{page_path}")
    assert current.status_code == 200
    page = current.json()
    saved = client.put(
        f"/api/internal/wiki/pages/{page_path}",
        json={
            "content": page["content"] + "\n\nManual edit remains dual-projected.\n",
            "expected_revision_id": page["current_revision_id"],
            "request_id": "manual-projection-when-compile-disabled",
            "review_status": "draft",
        },
    )

    assert saved.status_code == 200
    job_ids = saved.json()["projection_job_ids"]
    assert len(job_ids) == 2
    with connect_app(settings) as conn:
        placeholders = ",".join("?" for _ in job_ids)
        rows = conn.execute(
            f"SELECT target FROM knowledge_projection_jobs WHERE id IN ({placeholders}) ORDER BY target",
            job_ids,
        ).fetchall()
    assert [row["target"] for row in rows] == ["gbrain", "rag"]
