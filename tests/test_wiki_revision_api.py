import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db import connect_app, init_app_db
from app.main import app
from app.models import BackupReleaseRequest
from app.vault_writer import IntentExecutor
from app.wiki_revisions import (
    CompileCandidateCommand,
    ManualSaveCommand,
    WikiRevisionService,
)


@dataclass(frozen=True)
class ApiWikiPage:
    settings: Settings
    encoded_path: str
    absolute_path: Path
    content: str
    current_revision_id: str


@pytest.fixture
def api_wiki_page(tmp_path, monkeypatch):
    settings = get_settings().model_copy()
    settings.database_backend = "sqlite"
    settings.rag_store_backend = "sqlite"
    settings.database_path = tmp_path / "api.db"
    settings.vault_path = tmp_path / "vault"
    settings.upload_path = tmp_path / "uploads"
    settings.gbrain_enabled = False
    settings.gbrain_import_on_compile = False
    settings.auth_dev_fallback_enabled = True
    settings.auth_dev_user_id = "admin"
    settings.auth_dev_username = "Administrator"

    page_path = "wiki/product/faq/api.md"
    absolute_path = settings.vault_path / page_path
    absolute_path.parent.mkdir(parents=True)
    absolute_path.write_text(
        "---\ntitle: API\nsource_ids: [src_api]\nreview_status: draft\n---\n# API\n",
        encoding="utf-8",
    )
    init_app_db(settings)
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO sources(
              id,domain,owner,title,source_type,original_path,raw_path,
              content_hash,size_bytes,status,metadata_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "src_api",
                "product",
                "finance_owner",
                "API Source",
                "markdown",
                "api-source.md",
                "raw/api-source.md",
                "source-hash",
                1,
                "active",
                json.dumps({"acl_tags": ["finance"]}),
                "t0",
                "t0",
            ),
        )
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,domain,page_type,title,source_ids_json,review_status,
              created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                page_path,
                "product",
                "faq",
                "API",
                '["src_api"]',
                "draft",
                "t0",
                "t0",
            ),
        )

    monkeypatch.setattr("app.api.get_settings", lambda: settings)
    monkeypatch.setattr("app.main.settings", settings)
    current = WikiRevisionService(settings).get_page(page_path)
    return TestClient(app), ApiWikiPage(
        settings=settings,
        encoded_path=quote(page_path, safe="/"),
        absolute_path=absolute_path,
        content=current.content,
        current_revision_id=current.current_revision_id,
    )


@dataclass(frozen=True)
class ApiConflict:
    review_id: str
    encoded_path: str
    current_revision_id: str


@pytest.fixture
def api_content_conflict(api_wiki_page):
    client, api_page = api_wiki_page
    service = WikiRevisionService(api_page.settings)
    page_path = "wiki/product/faq/api.md"
    legacy = service.get_page(page_path)
    service.apply_generated_candidate(
        CompileCandidateCommand(
            page_path=page_path,
            content=legacy.content,
            domain="product",
            page_type="faq",
            title="API",
            source_ids=["src_api"],
            owner=None,
            source_hash="api-generated-1",
            compiler_version="wiki-revision-v1",
            compile_job_id="api-compile-1",
        )
    )
    generated = service.get_page(page_path)
    service.prepare_manual_save(
        ManualSaveCommand(
            page_path=page_path,
            content=generated.content + "\nAPI human edit.\n",
            expected_revision_id=generated.current_revision_id,
            request_id="api-human",
            actor="fixture",
            owner=None,
            note=None,
            review_status="reviewed",
        )
    )
    current = service.get_page(page_path)
    service.apply_generated_candidate(
        CompileCandidateCommand(
            page_path=page_path,
            content=legacy.content + "\nGenerated v2.\n",
            domain="product",
            page_type="faq",
            title="API",
            source_ids=["src_api"],
            owner=None,
            source_hash="api-generated-2",
            compiler_version="wiki-revision-v1",
            compile_job_id="api-compile-2",
        )
    )
    conflict = service.list_conflicts(page_path, status="pending")[0]
    assert conflict["issue_type"] == "content_conflict"
    return client, ApiConflict(
        review_id=conflict["id"],
        encoded_path=quote(page_path, safe="/"),
        current_revision_id=current.current_revision_id,
    )


def test_put_requires_expected_revision_and_returns_428(api_wiki_page):
    client, page = api_wiki_page

    response = client.put(
        f"/api/internal/wiki/pages/{page.encoded_path}",
        json={"content": page.content, "request_id": "missing-precondition"},
    )

    assert response.status_code == 428
    assert response.json()["detail"]["code"] == "expected_revision_required"


def test_stale_put_returns_latest_revision_without_changing_file(api_wiki_page):
    client, page = api_wiki_page
    before = page.absolute_path.read_bytes()

    response = client.put(
        f"/api/internal/wiki/pages/{page.encoded_path}",
        json={
            "content": page.content + "\nstale\n",
            "expected_revision_id": "wrev_stale",
            "request_id": "stale-save",
            "review_status": "draft",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["current_revision_id"] == page.current_revision_id
    assert page.absolute_path.read_bytes() == before


def test_revision_list_paginates_metadata_and_detail_contains_content(api_wiki_page):
    client, page = api_wiki_page
    changed = client.patch(
        f"/api/internal/wiki/pages/{page.encoded_path}/status",
        json={
            "review_status": "reviewed",
            "expected_revision_id": page.current_revision_id,
            "request_id": "revision-page-2",
        },
    )
    assert changed.status_code == 200

    first = client.get(
        f"/api/internal/wiki/pages/{page.encoded_path}/revisions?limit=1&offset=0"
    )
    second = client.get(
        f"/api/internal/wiki/pages/{page.encoded_path}/revisions?limit=1&offset=1"
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["limit"] == 1
    assert first.json()["offset"] == 0
    assert first.json()["total"] >= 2
    assert first.json()["items"][0]["id"] != second.json()["items"][0]["id"]
    metadata = first.json()["items"][0]
    assert "content" not in metadata
    assert "source_ids_json" not in metadata
    assert "metadata_json" not in metadata
    assert isinstance(metadata["source_ids"], list)
    assert isinstance(metadata["metadata"], dict)

    detail = client.get(f"/api/internal/wiki/revisions/{metadata['id']}")
    assert detail.status_code == 200
    assert detail.json()["content"]
    assert detail.json()["id"] == metadata["id"]


def test_conflict_api_exposes_type_and_rejects_stale_generated_revision(
    api_content_conflict,
):
    client, conflict = api_content_conflict

    listing = client.get(
        f"/api/internal/wiki/pages/{conflict.encoded_path}/conflicts"
    )
    assert listing.status_code == 200
    assert listing.json()[0]["issue_type"] == "content_conflict"
    assert isinstance(listing.json()[0]["source_ids"], list)
    assert isinstance(listing.json()[0]["expected_state"], dict)

    response = client.post(
        f"/api/internal/wiki/conflicts/{conflict.review_id}/resolve",
        json={
            "resolution": "accept_candidate",
            "expected_current_revision_id": conflict.current_revision_id,
            "expected_generated_revision_id": "wrev_stale",
            "request_id": "resolve-stale",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "revision_conflict"


def test_legacy_review_endpoint_refuses_conflict_resolution(api_content_conflict):
    client, conflict = api_content_conflict

    response = client.patch(
        f"/api/internal/reviews/{conflict.review_id}",
        json={"status": "resolved", "note": "wrong endpoint"},
    )

    assert response.status_code == 400
    assert f"/wiki/conflicts/{conflict.review_id}/resolve" in response.json()["detail"]


def test_status_api_creates_revision_and_updates_frontmatter(api_wiki_page):
    client, page = api_wiki_page

    response = client.patch(
        f"/api/internal/wiki/pages/{page.encoded_path}/status",
        json={
            "review_status": "stale",
            "expected_revision_id": page.current_revision_id,
            "request_id": "status-api-1",
            "note": "expired",
            "actor": "spoofed-client",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["current_revision_id"] != page.current_revision_id
    assert body["review_status"] == "stale"
    content = client.get(
        f"/api/internal/wiki/pages/{page.encoded_path}"
    ).json()["content"]
    assert "review_status: stale" in content
    assert "review_status: stale" in page.absolute_path.read_text(encoding="utf-8")
    with connect_app(page.settings) as conn:
        revision = conn.execute(
            "SELECT origin,actor FROM wiki_page_revisions WHERE id=?",
            (body["current_revision_id"],),
        ).fetchone()
    assert revision["origin"] == "manual"
    assert revision["actor"] == "admin"


def test_backup_release_requires_auth_admin_hash_and_uses_current_user(
    api_wiki_page,
):
    client, page = api_wiki_page
    with connect_app(page.settings) as conn:
        intent = conn.execute(
            """
            SELECT id,backup_last_observed_hash FROM vault_write_intents
            WHERE status='applied' AND backup_retention_status='retained'
            ORDER BY created_at DESC,id DESC LIMIT 1
            """
        ).fetchone()
    assert intent is not None
    endpoint = (
        f"/api/internal/wiki/write-intents/{intent['id']}/release-backup"
    )
    payload = {"expected_backup_hash": intent["backup_last_observed_hash"]}

    page.settings.auth_dev_fallback_enabled = False
    page.settings.auth_bootstrap_admin_password = "admin"
    unauthorized = client.post(endpoint, json=payload)
    assert unauthorized.status_code == 401

    login = client.post(
        "/api/internal/auth/login",
        json={"username": "admin", "password": "admin"},
    )
    assert login.status_code == 200
    admin_headers = {"Authorization": f"Bearer {login.json()['token']}"}
    created = client.post(
        "/api/internal/accounts",
        headers=admin_headers,
        json={
            "user_id": "viewer",
            "username": "Viewer",
            "password": "viewer-pass",
            "role": "viewer",
            "acl_tags": ["internal"],
            "status": "active",
        },
    )
    assert created.status_code == 200
    viewer_login = client.post(
        "/api/internal/auth/login",
        json={"username": "viewer", "password": "viewer-pass"},
    )
    viewer_headers = {
        "Authorization": f"Bearer {viewer_login.json()['token']}"
    }
    forbidden = client.post(endpoint, headers=viewer_headers, json=payload)
    assert forbidden.status_code == 403

    missing_hash = client.post(endpoint, headers=admin_headers, json={})
    assert missing_hash.status_code == 422
    released = client.post(
        endpoint,
        headers=admin_headers,
        json={**payload, "actor": "spoofed-client"},
    )
    assert released.status_code == 200
    assert released.json()["status"] == "released"
    with connect_app(page.settings) as conn:
        audit_row = conn.execute(
            """
            SELECT payload_json FROM audit_logs
            WHERE event_type='vault_backup_released'
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
    assert json.loads(audit_row["payload_json"])["actor"] == "admin"


@pytest.mark.parametrize("window", ["captured", "installed"])
def test_page_read_uses_database_current_during_write_window(
    api_wiki_page,
    window,
):
    client, page = api_wiki_page
    service = WikiRevisionService(page.settings)
    before = service.get_page("wiki/product/faq/api.md")
    prepared = service.prepare_manual_save(
        ManualSaveCommand(
            page_path=before.page_path,
            content=before.content + f"\n{window} candidate.\n",
            expected_revision_id=before.current_revision_id,
            request_id=f"window-{window}",
            actor="fixture",
            owner=None,
            note=None,
            review_status="draft",
        ),
        execute_intent=False,
    )
    executor = IntentExecutor(page.settings, owner=f"reader-{window}")
    assert executor.claim(prepared.write_intent_id, lease_seconds=30)
    executor.capture_and_install(prepared.write_intent_id, stop_after=window)

    response = client.get(f"/api/internal/wiki/pages/{page.encoded_path}")

    assert response.status_code == 200
    body = response.json()
    assert body["current_revision_id"] == before.current_revision_id
    assert body["content"] == before.content
    assert body["write_in_progress"] is True
    assert body["write_intent_id"] == prepared.write_intent_id


def test_specific_wiki_routes_are_registered_before_greedy_page_routes():
    routes = [route for route in app.routes if hasattr(route, "methods")]

    def route_index(path: str, method: str) -> int:
        return next(
            index
            for index, route in enumerate(routes)
            if route.path == path and method in route.methods
        )

    greedy_get = route_index(
        "/api/internal/wiki/pages/{page_path:path}", "GET"
    )
    greedy_put = route_index(
        "/api/internal/wiki/pages/{page_path:path}", "PUT"
    )
    specific_routes = [
        ("/api/internal/wiki/revisions/{revision_id}", "GET"),
        ("/api/internal/wiki/conflicts/{review_id}/resolve", "POST"),
        ("/api/internal/wiki/pages/{page_path:path}/revisions", "GET"),
        ("/api/internal/wiki/pages/{page_path:path}/conflicts", "GET"),
        ("/api/internal/wiki/pages/{page_path:path}/status", "PATCH"),
    ]

    for path, method in specific_routes:
        assert route_index(path, method) < greedy_get
        assert route_index(path, method) < greedy_put


def test_wiki_reads_require_page_and_all_source_acl(api_content_conflict):
    client, conflict = api_content_conflict
    admin_revisions = client.get(
        f"/api/internal/wiki/pages/{conflict.encoded_path}/revisions"
    ).json()
    revision_id = admin_revisions["items"][0]["id"]
    outsider = {
        "X-LGDO-User": "outsider",
        "X-LGDO-Role": "viewer",
        "X-LGDO-ACL-Tags": "support",
    }
    finance = {
        "X-LGDO-User": "finance_user",
        "X-LGDO-Role": "viewer",
        "X-LGDO-ACL-Tags": "finance",
    }

    hidden = [
        client.get(
            f"/api/internal/wiki/pages/{conflict.encoded_path}",
            headers=outsider,
        ),
        client.get(
            f"/api/internal/wiki/pages/{conflict.encoded_path}/revisions",
            headers=outsider,
        ),
        client.get(
            f"/api/internal/wiki/revisions/{revision_id}",
            headers=outsider,
        ),
        client.get(
            f"/api/internal/wiki/pages/{conflict.encoded_path}/conflicts",
            headers=outsider,
        ),
    ]
    assert [response.status_code for response in hidden] == [404, 404, 404, 404]
    assert all(
        response.json()["detail"]["code"] == "wiki_page_not_found"
        for response in hidden
    )
    assert all("expected_state" not in response.text for response in hidden)
    outsider_pages = client.get("/api/internal/wiki/pages", headers=outsider)
    assert outsider_pages.status_code == 200
    assert outsider_pages.json() == []

    visible = [
        client.get(
            f"/api/internal/wiki/pages/{conflict.encoded_path}",
            headers=finance,
        ),
        client.get(
            f"/api/internal/wiki/pages/{conflict.encoded_path}/revisions",
            headers=finance,
        ),
        client.get(
            f"/api/internal/wiki/revisions/{revision_id}",
            headers=finance,
        ),
        client.get(
            f"/api/internal/wiki/pages/{conflict.encoded_path}/conflicts",
            headers=finance,
        ),
    ]
    assert [response.status_code for response in visible] == [200, 200, 200, 200]
    finance_pages = client.get("/api/internal/wiki/pages", headers=finance)
    assert finance_pages.status_code == 200
    assert [page["path"] for page in finance_pages.json()] == [
        "wiki/product/faq/api.md"
    ]


def test_editor_wiki_mutations_require_target_acl(api_content_conflict):
    client, conflict = api_content_conflict
    admin_page = client.get(
        f"/api/internal/wiki/pages/{conflict.encoded_path}"
    ).json()
    outsider_editor = {
        "X-LGDO-User": "outside_editor",
        "X-LGDO-Role": "editor",
        "X-LGDO-ACL-Tags": "support",
    }

    save = client.put(
        f"/api/internal/wiki/pages/{conflict.encoded_path}",
        headers=outsider_editor,
        json={
            "content": admin_page["content"] + "\nUnauthorized edit.\n",
            "expected_revision_id": admin_page["current_revision_id"],
            "request_id": "unauthorized-save",
        },
    )
    status = client.patch(
        f"/api/internal/wiki/pages/{conflict.encoded_path}/status",
        headers=outsider_editor,
        json={
            "review_status": "stale",
            "expected_revision_id": admin_page["current_revision_id"],
            "request_id": "unauthorized-status",
        },
    )
    resolve = client.post(
        f"/api/internal/wiki/conflicts/{conflict.review_id}/resolve",
        headers=outsider_editor,
        json={
            "resolution": "keep_current",
            "expected_current_revision_id": conflict.current_revision_id,
            "request_id": "unauthorized-resolve",
        },
    )

    assert [save.status_code, status.status_code, resolve.status_code] == [
        404,
        404,
        404,
    ]
    after = client.get(f"/api/internal/wiki/pages/{conflict.encoded_path}").json()
    assert after["content"] == admin_page["content"]
    assert "expected_state" not in resolve.text


def test_wiki_request_identifiers_are_bounded(api_content_conflict):
    client, conflict = api_content_conflict
    page = client.get(f"/api/internal/wiki/pages/{conflict.encoded_path}").json()

    responses = [
        client.put(
            f"/api/internal/wiki/pages/{conflict.encoded_path}",
            json={
                "content": page["content"],
                "expected_revision_id": "   ",
                "request_id": "bounded-save",
            },
        ),
        client.put(
            f"/api/internal/wiki/pages/{conflict.encoded_path}",
            json={
                "content": page["content"],
                "expected_revision_id": page["current_revision_id"],
                "request_id": "r" * 129,
            },
        ),
        client.patch(
            f"/api/internal/wiki/pages/{conflict.encoded_path}/status",
            json={
                "review_status": "stale",
                "expected_revision_id": "w" * 129,
                "request_id": "bounded-status",
            },
        ),
        client.post(
            f"/api/internal/wiki/conflicts/{conflict.review_id}/resolve",
            json={
                "resolution": "keep_current",
                "expected_current_revision_id": " ",
                "request_id": "bounded-conflict",
            },
        ),
        client.post(
            f"/api/internal/wiki/conflicts/{conflict.review_id}/resolve",
            json={
                "resolution": "keep_current",
                "expected_current_revision_id": conflict.current_revision_id,
                "expected_generated_revision_id": "g" * 129,
                "request_id": "bounded-conflict",
            },
        ),
        client.post(
            f"/api/internal/wiki/conflicts/{conflict.review_id}/resolve",
            json={
                "resolution": "keep_current",
                "expected_current_revision_id": conflict.current_revision_id,
                "request_id": "r" * 129,
            },
        ),
    ]

    assert [response.status_code for response in responses] == [422] * len(responses)


def test_backup_hash_is_normalized_and_rejects_non_sha256(api_wiki_page):
    client, _ = api_wiki_page

    invalid = client.post(
        "/api/internal/wiki/write-intents/wint_fake/release-backup",
        json={"expected_backup_hash": "not-a-sha256"},
    )

    assert invalid.status_code == 422
    assert BackupReleaseRequest(
        expected_backup_hash="A" * 64
    ).expected_backup_hash == "a" * 64


def _insert_public_source(settings: Settings) -> None:
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO sources(
              id,domain,owner,title,source_type,original_path,raw_path,
              content_hash,size_bytes,status,metadata_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "src_public",
                "product",
                None,
                "Public Source",
                "markdown",
                "public-source.md",
                "raw/public-source.md",
                "public-source-hash",
                1,
                "active",
                json.dumps({"acl_tags": ["public"]}),
                "t0",
                "t0",
            ),
        )


@pytest.fixture
def api_public_page_with_private_history(api_wiki_page):
    client, api_page = api_wiki_page
    _insert_public_source(api_page.settings)
    service = WikiRevisionService(api_page.settings)
    private_revision = service.get_page("wiki/product/faq/api.md")
    service.prepare_manual_save(
        ManualSaveCommand(
            page_path=private_revision.page_path,
            content=(
                private_revision.content.replace("src_api", "src_public")
                + "\nPublic current content.\n"
            ),
            expected_revision_id=private_revision.current_revision_id,
            request_id="make-current-public",
            actor="fixture",
            owner=None,
            note=None,
            review_status="reviewed",
        )
    )
    current = service.get_page(private_revision.page_path)
    return client, api_page, private_revision, current


def test_revision_history_filters_private_artifacts_before_pagination(
    api_public_page_with_private_history,
):
    client, api_page, private_revision, current = (
        api_public_page_with_private_history
    )
    public_viewer = {
        "X-LGDO-User": "public_viewer",
        "X-LGDO-Role": "viewer",
        "X-LGDO-ACL-Tags": "support",
    }

    page = client.get(
        f"/api/internal/wiki/pages/{api_page.encoded_path}",
        headers=public_viewer,
    )
    listing = client.get(
        f"/api/internal/wiki/pages/{api_page.encoded_path}/revisions?limit=1&offset=0",
        headers=public_viewer,
    )
    private_detail = client.get(
        f"/api/internal/wiki/revisions/{private_revision.current_revision_id}",
        headers=public_viewer,
    )

    assert page.status_code == 200
    assert (
        listing.json()["total"],
        [item["id"] for item in listing.json()["items"]],
        private_detail.status_code,
    ) == (1, [current.current_revision_id], 404)
    assert "source_ids" not in private_detail.text


def test_finance_viewer_can_read_private_revision_history(
    api_public_page_with_private_history,
):
    client, api_page, private_revision, _ = api_public_page_with_private_history
    finance_viewer = {
        "X-LGDO-User": "finance_user",
        "X-LGDO-Role": "viewer",
        "X-LGDO-ACL-Tags": "finance",
    }

    listing = client.get(
        f"/api/internal/wiki/pages/{api_page.encoded_path}/revisions",
        headers=finance_viewer,
    )
    private_detail = client.get(
        f"/api/internal/wiki/revisions/{private_revision.current_revision_id}",
        headers=finance_viewer,
    )

    assert listing.status_code == 200
    assert listing.json()["total"] == 2
    assert private_detail.status_code == 200
    assert private_detail.json()["source_ids"] == ["src_api"]


@dataclass(frozen=True)
class ApiPrivateConflict:
    review_id: str
    encoded_path: str
    current_revision_id: str
    candidate_revision_id: str


@pytest.fixture
def api_public_page_with_private_conflict(api_wiki_page):
    client, api_page = api_wiki_page
    _insert_public_source(api_page.settings)
    service = WikiRevisionService(api_page.settings)
    page_path = "wiki/product/faq/api.md"
    private_legacy = service.get_page(page_path)
    service.apply_generated_candidate(
        CompileCandidateCommand(
            page_path=page_path,
            content=private_legacy.content,
            domain="product",
            page_type="faq",
            title="API",
            source_ids=["src_api"],
            owner=None,
            source_hash="private-baseline",
            compiler_version="wiki-revision-v1",
            compile_job_id="private-baseline",
        )
    )
    generated = service.get_page(page_path)
    service.prepare_manual_save(
        ManualSaveCommand(
            page_path=page_path,
            content=(
                generated.content.replace("src_api", "src_public")
                + "\nPublic human edit.\n"
            ),
            expected_revision_id=generated.current_revision_id,
            request_id="public-human-edit",
            actor="fixture",
            owner=None,
            note=None,
            review_status="reviewed",
        )
    )
    current = service.get_page(page_path)
    service.apply_generated_candidate(
        CompileCandidateCommand(
            page_path=page_path,
            content=generated.content + "\nPrivate generated candidate.\n",
            domain="product",
            page_type="faq",
            title="API",
            source_ids=["src_api"],
            owner=None,
            source_hash="private-candidate",
            compiler_version="wiki-revision-v1",
            compile_job_id="private-candidate",
        )
    )
    conflict = service.list_conflicts(page_path, status="pending")[0]
    return client, ApiPrivateConflict(
        review_id=conflict["id"],
        encoded_path=quote(page_path, safe="/"),
        current_revision_id=current.current_revision_id,
        candidate_revision_id=conflict["candidate_revision_id"],
    )


def test_conflict_list_filters_private_review_and_candidate_artifacts(
    api_public_page_with_private_conflict,
):
    client, conflict = api_public_page_with_private_conflict
    public_viewer = {
        "X-LGDO-User": "public_viewer",
        "X-LGDO-Role": "viewer",
        "X-LGDO-ACL-Tags": "support",
    }
    finance_viewer = {
        "X-LGDO-User": "finance_user",
        "X-LGDO-Role": "viewer",
        "X-LGDO-ACL-Tags": "finance",
    }

    hidden = client.get(
        f"/api/internal/wiki/pages/{conflict.encoded_path}/conflicts",
        headers=public_viewer,
    )
    visible = client.get(
        f"/api/internal/wiki/pages/{conflict.encoded_path}/conflicts",
        headers=finance_viewer,
    )

    assert hidden.status_code == 200
    assert hidden.json() == []
    assert "expected_state" not in hidden.text
    assert visible.status_code == 200
    assert [item["id"] for item in visible.json()] == [conflict.review_id]


def test_conflict_resolve_requires_review_candidate_and_base_acl(
    api_public_page_with_private_conflict,
):
    client, conflict = api_public_page_with_private_conflict
    public_editor = {
        "X-LGDO-User": "public_editor",
        "X-LGDO-Role": "editor",
        "X-LGDO-ACL-Tags": "support",
    }
    finance_editor = {
        "X-LGDO-User": "finance_editor",
        "X-LGDO-Role": "editor",
        "X-LGDO-ACL-Tags": "finance",
    }
    payload = {
        "resolution": "keep_current",
        "expected_current_revision_id": conflict.current_revision_id,
        "expected_generated_revision_id": conflict.candidate_revision_id,
        "request_id": "resolve-private-candidate",
    }

    hidden = client.post(
        f"/api/internal/wiki/conflicts/{conflict.review_id}/resolve",
        headers=public_editor,
        json=payload,
    )
    visible = client.post(
        f"/api/internal/wiki/conflicts/{conflict.review_id}/resolve",
        headers=finance_editor,
        json=payload,
    )

    assert hidden.status_code == 404
    assert hidden.json()["detail"]["code"] == "wiki_page_not_found"
    assert "expected_state" not in hidden.text
    assert visible.status_code == 200
    assert visible.json()["status"] == "resolved"
