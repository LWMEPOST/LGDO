from __future__ import annotations

import base64
import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.accounts import ensure_bootstrap_admin
from app.auth import resolve_user_context
from app.config import Settings
from app.db import connect_app


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "app_env": "development",
        "database_backend": "sqlite",
        "database_path": tmp_path / "security.db",
        "oidc_enabled": False,
        "auth_dev_fallback_enabled": False,
        "auth_bootstrap_admin_password": "bootstrap-secret",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (name.lower().encode("ascii"), value.encode("utf-8"))
        for name, value in (headers or {}).items()
    ]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw_headers})


def _unsigned_jwt(claims: dict[str, object]) -> str:
    def encode(value: dict[str, object]) -> str:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(claims)}."


def test_authentication_defaults_fail_closed():
    assert Settings.model_fields["auth_dev_fallback_enabled"].default is False


def test_bootstrap_admin_password_has_no_default():
    assert Settings.model_fields["auth_bootstrap_admin_password"].default is None


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-LGDO-User": "forged-user", "X-LGDO-Role": "admin"},
    ],
    ids=["anonymous", "lgdo-headers"],
)
def test_production_rejects_development_identities_even_when_fallback_is_enabled(tmp_path, headers):
    settings = _settings(tmp_path, app_env="production", auth_dev_fallback_enabled=True)

    with pytest.raises(HTTPException) as exc_info:
        resolve_user_context(settings, _request(headers))

    assert exc_info.value.status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-LGDO-User": "dev-user", "X-LGDO-Role": "editor"},
    ],
    ids=["anonymous", "lgdo-headers"],
)
def test_development_identities_require_explicit_fallback(tmp_path, headers):
    settings = _settings(tmp_path, app_env="development", auth_dev_fallback_enabled=False)

    with pytest.raises(HTTPException) as exc_info:
        resolve_user_context(settings, _request(headers))

    assert exc_info.value.status_code == 401


def test_explicit_development_fallback_allows_anonymous_identity(tmp_path):
    settings = _settings(tmp_path, app_env="development", auth_dev_fallback_enabled=True)

    user = resolve_user_context(settings, _request())

    assert user.user_id == settings.auth_dev_user_id
    assert user.auth_provider == "development"


def test_explicit_development_fallback_allows_lgdo_headers(tmp_path):
    settings = _settings(tmp_path, app_env="development", auth_dev_fallback_enabled=True)

    user = resolve_user_context(
        settings,
        _request({"X-LGDO-User": "dev-user", "X-LGDO-Role": "editor"}),
    )

    assert user.user_id == "dev-user"
    assert user.role == "editor"
    assert user.auth_provider == "trusted-header"


def test_oidc_disabled_rejects_unsigned_bearer_even_with_development_fallback(tmp_path):
    settings = _settings(tmp_path, app_env="development", auth_dev_fallback_enabled=True)
    token = _unsigned_jwt({"sub": "forged-admin", "role": "admin", "acl_tags": ["*"]})

    with pytest.raises(HTTPException) as exc_info:
        resolve_user_context(settings, _request({"Authorization": f"Bearer {token}"}))

    assert exc_info.value.status_code == 401


def test_missing_bootstrap_password_does_not_create_admin(tmp_path):
    settings = _settings(tmp_path)
    settings.auth_bootstrap_admin_password = None

    ensure_bootstrap_admin(settings)

    with connect_app(settings) as conn:
        account = conn.execute(
            "SELECT user_id FROM accounts WHERE user_id = ?",
            (settings.auth_dev_user_id,),
        ).fetchone()
    assert account is None


def test_bootstrap_password_preserves_leading_and_trailing_spaces(tmp_path):
    settings = _settings(
        tmp_path,
        auth_bootstrap_admin_password="  deliberate passphrase  ",
    )

    ensure_bootstrap_admin(settings)

    from app.accounts import authenticate_account

    token, account = authenticate_account(
        settings,
        settings.auth_dev_user_id,
        "  deliberate passphrase  ",
    )
    assert token
    assert account["user_id"] == settings.auth_dev_user_id
    with pytest.raises(PermissionError):
        authenticate_account(settings, settings.auth_dev_user_id, "deliberate passphrase")
