from fastapi.testclient import TestClient

import app.accounts as account_service
from app.config import get_settings
from app.main import app
from app.migration import _upsert_row


class RecordingCursor:
    def __init__(self):
        self.calls = []

    def execute(self, query, params=()):
        self.calls.append((query, params))


def test_bootstrap_login_and_current_user(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "test.db")
    monkeypatch.setattr(settings, "auth_dev_fallback_enabled", False)
    monkeypatch.setattr(settings, "auth_bootstrap_admin_password", "admin")

    client = TestClient(app)

    unauthenticated = client.get("/api/internal/auth/me")
    assert unauthenticated.status_code == 401

    login = client.post("/api/internal/auth/login", json={"username": "admin", "password": "admin"})
    assert login.status_code == 200
    body = login.json()
    assert body["token"]
    assert body["user"]["user_id"] == "admin"
    assert body["user"]["role"] == "admin"
    assert body["user"]["acl_tags"] == ["*"]

    me = client.get("/api/internal/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200
    assert me.json()["user_id"] == "admin"
    assert me.json()["auth_provider"] == "local-session"


def test_internal_console_endpoints_require_auth_when_dev_fallback_disabled(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "test.db")
    monkeypatch.setattr(settings, "auth_dev_fallback_enabled", False)

    client = TestClient(app)

    checks = [
        ("GET", "/api/internal/sources", None),
        ("GET", "/api/internal/ingest/reports", None),
        ("GET", "/api/internal/rag/status", None),
        ("GET", "/api/internal/wiki/pages", None),
        ("GET", "/api/internal/gaps", None),
        ("GET", "/api/internal/reviews?status=pending", None),
        ("POST", "/api/internal/sources/scan", {"root_path": "samples/product_service", "domain": "product"}),
        ("POST", "/api/internal/wiki/compile", {"domain": "product"}),
    ]

    for method, path, json_body in checks:
        response = client.request(method, path, json=json_body)
        assert response.status_code == 401, path


def test_admin_can_manage_accounts_and_viewer_cannot(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "test.db")
    monkeypatch.setattr(settings, "auth_dev_fallback_enabled", False)
    monkeypatch.setattr(settings, "auth_bootstrap_admin_password", "admin")

    client = TestClient(app)
    token = client.post("/api/internal/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    created = client.post(
        "/api/internal/accounts",
        headers=auth,
        json={
            "user_id": "ops_user",
            "username": "运营同学",
            "password": "ops-pass",
            "role": "viewer",
            "acl_tags": ["internal", "product"],
            "status": "active",
        },
    )
    assert created.status_code == 200
    assert created.json()["user_id"] == "ops_user"
    assert created.json()["password_configured"] is True
    assert created.json()["acl_tags"] == ["internal", "product"]

    accounts = client.get("/api/internal/accounts", headers=auth)
    assert accounts.status_code == 200
    assert {item["user_id"] for item in accounts.json()} >= {"admin", "ops_user"}

    viewer_login = client.post("/api/internal/auth/login", json={"username": "ops_user", "password": "ops-pass"})
    assert viewer_login.status_code == 200
    viewer_token = viewer_login.json()["token"]
    viewer_auth = {"Authorization": f"Bearer {viewer_token}"}

    forbidden = client.get("/api/internal/accounts", headers=viewer_auth)
    assert forbidden.status_code == 403
    forbidden_create = client.post(
        "/api/internal/accounts",
        headers=viewer_auth,
        json={"user_id": "blocked", "username": "blocked", "role": "viewer", "acl_tags": []},
    )
    assert forbidden_create.status_code == 403

    updated = client.patch(
        "/api/internal/accounts/ops_user",
        headers=auth,
        json={"role": "editor", "acl_tags": ["internal", "customer_service"], "status": "active"},
    )
    assert updated.status_code == 200
    assert updated.json()["role"] == "editor"
    assert updated.json()["acl_tags"] == ["internal", "customer_service"]

    disabled = client.patch("/api/internal/accounts/ops_user", headers=auth, json={"status": "disabled"})
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"

    disabled_login = client.post("/api/internal/auth/login", json={"username": "ops_user", "password": "ops-pass"})
    assert disabled_login.status_code == 401


def test_migration_upsert_accepts_account_tables():
    cursor = RecordingCursor()

    _upsert_row(
        cursor,
        "accounts",
        {
            "user_id": "admin",
            "username": "管理员",
            "role": "admin",
            "acl_tags_json": "[\"*\"]",
            "password_hash": "hash",
            "status": "active",
            "auth_provider": "local",
            "created_at": "2026-07-01T10:00:00+08:00",
            "updated_at": "2026-07-01T10:00:00+08:00",
            "last_login_at": None,
        },
    )
    _upsert_row(
        cursor,
        "auth_sessions",
        {
            "token": "session-token",
            "user_id": "admin",
            "created_at": "2026-07-01T10:00:00+08:00",
            "expires_at": "2026-07-01T11:00:00+08:00",
            "revoked_at": None,
        },
    )

    assert len(cursor.calls) == 2


def test_session_expiry_uses_same_offset_as_now_iso(monkeypatch):
    monkeypatch.setattr(account_service, "now_iso", lambda: "2026-07-01T10:00:00+08:00")

    assert account_service._future_iso(1) == "2026-07-01T11:00:00+08:00"


def test_admin_cannot_remove_own_last_admin_access(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "test.db")
    monkeypatch.setattr(settings, "auth_dev_fallback_enabled", False)
    monkeypatch.setattr(settings, "auth_bootstrap_admin_password", "admin")

    client = TestClient(app)
    token = client.post("/api/internal/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    disabled = client.patch("/api/internal/accounts/admin", headers=auth, json={"status": "disabled"})
    assert disabled.status_code == 400
    assert "当前登录管理员" in disabled.json()["detail"]

    demoted = client.patch("/api/internal/accounts/admin", headers=auth, json={"role": "viewer"})
    assert demoted.status_code == 400
    assert "当前登录管理员" in demoted.json()["detail"]


def test_ask_accepts_legacy_string_acl_tags_from_console(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "test.db")
    monkeypatch.setattr(settings, "auth_dev_fallback_enabled", False)
    monkeypatch.setattr(settings, "auth_bootstrap_admin_password", "admin")

    client = TestClient(app)
    token = client.post("/api/internal/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}

    response = client.post(
        "/api/internal/ask",
        headers=auth,
        json={
            "question": "用户如何处理退款问题？",
            "domain": "product",
            "answer_mode": "detail",
            "require_citations": True,
            "user_id": "admin",
            "role": "admin",
            "acl_tags": "*",
        },
    )

    assert response.status_code == 200
    assert response.json()["user_context"]["user_id"] == "admin"
    assert response.json()["user_context"]["acl_tags"] == ["*"]
