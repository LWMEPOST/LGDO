from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Any

from app.auth import UserContext, normalize_role, normalize_tags
from app.config import Settings
from app.db import audit, connect_app, init_app_db, json_dump, row_to_dict, rows_to_dicts
from app.models import AccountCreateRequest, AccountUpdateRequest
from app.timeutil import now_iso


def ensure_bootstrap_admin(settings: Settings) -> None:
    init_app_db(settings)
    with connect_app(settings) as conn:
        existing = conn.execute("SELECT user_id FROM accounts WHERE user_id = ?", (settings.auth_dev_user_id,)).fetchone()
        if existing:
            return
        bootstrap_password = settings.auth_bootstrap_admin_password or ""
        if not bootstrap_password:
            return
        timestamp = now_iso()
        conn.execute(
            """
            INSERT INTO accounts(
              user_id, username, role, acl_tags_json, password_hash,
              status, auth_provider, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                settings.auth_dev_user_id,
                settings.auth_dev_username,
                "admin",
                json_dump(["*"]),
                hash_password(bootstrap_password),
                "active",
                "local",
                timestamp,
                timestamp,
            ),
        )
        audit(conn, "account_bootstrapped", {"user_id": settings.auth_dev_user_id, "role": "admin"}, timestamp)


def authenticate_account(settings: Settings, username: str, password: str) -> tuple[str, dict[str, Any]]:
    ensure_bootstrap_admin(settings)
    normalized = username.strip()
    if not normalized or not password:
        raise PermissionError("用户名或密码错误")
    with connect_app(settings) as conn:
        row = row_to_dict(
            conn.execute(
                """
                SELECT * FROM accounts
                WHERE user_id = ? OR username = ?
                ORDER BY CASE WHEN user_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (normalized, normalized, normalized),
            ).fetchone()
        )
        if not row or row.get("status") != "active" or not verify_password(password, row.get("password_hash")):
            raise PermissionError("用户名或密码错误")
        token = secrets.token_urlsafe(32)
        timestamp = now_iso()
        expires_at = _future_iso(hours=settings.auth_session_ttl_hours)
        conn.execute(
            """
            INSERT INTO auth_sessions(token, user_id, created_at, expires_at, revoked_at)
            VALUES (?, ?, ?, ?, NULL)
            """,
            (token, row["user_id"], timestamp, expires_at),
        )
        conn.execute(
            "UPDATE accounts SET last_login_at = ?, updated_at = ? WHERE user_id = ?",
            (timestamp, timestamp, row["user_id"]),
        )
        audit(conn, "account_logged_in", {"user_id": row["user_id"]}, timestamp)
        row["last_login_at"] = timestamp
    return token, public_account(row)


def account_from_session(settings: Settings, token: str) -> UserContext | None:
    if not token:
        return None
    ensure_bootstrap_admin(settings)
    with connect_app(settings) as conn:
        row = row_to_dict(
            conn.execute(
                """
                SELECT accounts.*
                FROM auth_sessions
                JOIN accounts ON accounts.user_id = auth_sessions.user_id
                WHERE auth_sessions.token = ?
                  AND auth_sessions.revoked_at IS NULL
                  AND auth_sessions.expires_at > ?
                  AND accounts.status = 'active'
                """,
                (token, now_iso()),
            ).fetchone()
        )
    if not row:
        return None
    return UserContext(
        user_id=row["user_id"],
        username=row.get("username") or row["user_id"],
        role=normalize_role(row.get("role")),
        acl_tags=tuple(_account_tags(row.get("acl_tags") or [])),
        auth_provider="local-session",
        raw_claims={"account": public_account(row)},
    )


def revoke_session(settings: Settings, token: str, actor: str | None = None) -> None:
    if not token:
        return
    init_app_db(settings)
    timestamp = now_iso()
    with connect_app(settings) as conn:
        conn.execute("UPDATE auth_sessions SET revoked_at = ? WHERE token = ?", (timestamp, token))
        audit(conn, "account_logged_out", {"actor": actor, "token_prefix": token[:8]}, timestamp)


def list_accounts(settings: Settings) -> list[dict[str, Any]]:
    ensure_bootstrap_admin(settings)
    with connect_app(settings) as conn:
        rows = rows_to_dicts(conn.execute("SELECT * FROM accounts ORDER BY role, user_id").fetchall())
    return [public_account(row) for row in rows]


def create_account(settings: Settings, request: AccountCreateRequest, actor: str) -> dict[str, Any]:
    ensure_bootstrap_admin(settings)
    user_id = _normalize_user_id(request.user_id)
    timestamp = now_iso()
    row = {
        "user_id": user_id,
        "username": (request.username or user_id).strip(),
        "role": normalize_role(request.role),
        "acl_tags": normalize_tags(request.acl_tags),
        "password_hash": hash_password(request.password) if request.password else None,
        "status": request.status,
        "auth_provider": "local",
        "created_at": timestamp,
        "updated_at": timestamp,
        "last_login_at": None,
    }
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO accounts(
              user_id, username, role, acl_tags_json, password_hash,
              status, auth_provider, created_at, updated_at, last_login_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["user_id"],
                row["username"],
                row["role"],
                json_dump(row["acl_tags"]),
                row["password_hash"],
                row["status"],
                row["auth_provider"],
                row["created_at"],
                row["updated_at"],
                row["last_login_at"],
            ),
        )
        audit(conn, "account_created", {"user_id": user_id, "actor": actor, "role": row["role"]}, timestamp)
    return public_account(row)


def update_account(settings: Settings, user_id: str, request: AccountUpdateRequest, actor: str) -> dict[str, Any]:
    ensure_bootstrap_admin(settings)
    target = _normalize_user_id(user_id)
    timestamp = now_iso()
    with connect_app(settings) as conn:
        current = row_to_dict(conn.execute("SELECT * FROM accounts WHERE user_id = ?", (target,)).fetchone())
        if not current:
            raise ValueError(f"account not found: {target}")
        username = current.get("username")
        role = current.get("role")
        acl_tags = current.get("acl_tags") or []
        status = current.get("status")
        password_hash = current.get("password_hash")
        if request.username is not None:
            username = request.username.strip() or target
        if request.role is not None:
            role = normalize_role(request.role)
        if request.acl_tags is not None:
            acl_tags = normalize_tags(request.acl_tags)
        if request.status is not None:
            status = request.status
        if request.password:
            password_hash = hash_password(request.password)
        _ensure_admin_access_remains(conn, target, actor, current, role, status)
        conn.execute(
            """
            UPDATE accounts
            SET username = ?, role = ?, acl_tags_json = ?, password_hash = ?,
                status = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (username, role, json_dump(acl_tags), password_hash, status, timestamp, target),
        )
        if status == "disabled":
            conn.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (timestamp, target),
            )
        audit(conn, "account_updated", {"user_id": target, "actor": actor, "status": status, "role": role}, timestamp)
        row = row_to_dict(conn.execute("SELECT * FROM accounts WHERE user_id = ?", (target,)).fetchone()) or {}
    return public_account(row)


def public_account(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": row["user_id"],
        "username": row.get("username"),
        "role": normalize_role(row.get("role")),
        "acl_tags": _account_tags(row.get("acl_tags") or []),
        "status": row.get("status") or "active",
        "auth_provider": row.get("auth_provider") or "local",
        "password_configured": bool(row.get("password_hash")),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "last_login_at": row.get("last_login_at"),
    }


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return "pbkdf2_sha256$120000$" + base64.b64encode(salt).decode("ascii") + "$" + base64.b64encode(digest).decode("ascii")


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        scheme, iterations_raw, salt_raw, digest_raw = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_raw.encode("ascii"))
        expected = base64.b64decode(digest_raw.encode("ascii"))
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations_raw))
    except Exception:
        return False
    return hmac.compare_digest(digest, expected)


def _account_tags(values: Any) -> list[str]:
    if isinstance(values, list) and "*" in values:
        return ["*"]
    if isinstance(values, tuple) and "*" in values:
        return ["*"]
    if isinstance(values, str) and values.strip() == "*":
        return ["*"]
    return normalize_tags(values)


def _normalize_user_id(value: str) -> str:
    user_id = value.strip()
    if not user_id:
        raise ValueError("user_id is required")
    return user_id


def _ensure_admin_access_remains(
    conn: Any,
    target: str,
    actor: str,
    current: dict[str, Any],
    next_role: str | None,
    next_status: str | None,
) -> None:
    removes_admin_access = next_role != "admin" or next_status != "active"
    if current.get("role") != "admin" or not removes_admin_access:
        return
    if target == actor:
        raise ValueError("不能移除当前登录管理员的管理权限")
    count_row = conn.execute(
        "SELECT COUNT(*) AS admin_count FROM accounts WHERE role = ? AND status = ?",
        ("admin", "active"),
    ).fetchone()
    active_admin_count = int(count_row[0] if count_row else 0)
    if active_admin_count <= 1:
        raise ValueError("至少保留一个启用管理员账户")


def _future_iso(hours: int) -> str:
    return (datetime.fromisoformat(now_iso()) + timedelta(hours=max(1, hours))).isoformat(timespec="seconds")
