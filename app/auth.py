from __future__ import annotations

import base64
import json
import re
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from fastapi import HTTPException, Request, status

from app.config import Settings


@dataclass(frozen=True)
class UserContext:
    user_id: str
    username: str | None = None
    role: str = "viewer"
    acl_tags: tuple[str, ...] = ()
    auth_provider: str = "development"
    raw_claims: dict[str, Any] | None = None

    @property
    def is_admin(self) -> bool:
        return self.role in {"admin", "owner"} or "*" in self.acl_tags

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "role": self.role,
            "acl_tags": list(self.acl_tags),
            "auth_provider": self.auth_provider,
        }


def resolve_user_context(settings: Settings, request: Request) -> UserContext:
    authorization = request.headers.get("authorization") or ""
    bearer = _extract_bearer_token(authorization)
    if bearer:
        local_user = _session_user(settings, bearer)
        if local_user:
            return local_user
        if not settings.oidc_enabled:
            if not settings.auth_dev_fallback_enabled:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="OIDC 未启用且开发鉴权回退已关闭",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return _decode_unverified_bearer(bearer)
        return _verify_oidc_token(settings, bearer)

    header_user = request.headers.get("x-lgdo-user") or request.headers.get("x-user-id")
    if header_user:
        return UserContext(
            user_id=header_user.strip(),
            username=(request.headers.get("x-lgdo-user-name") or header_user).strip(),
            role=normalize_role(request.headers.get("x-lgdo-role") or "viewer"),
            acl_tags=tuple(normalize_tags(_split_header_tags(request.headers.get("x-lgdo-acl-tags")))),
            auth_provider="trusted-header",
        )

    if not settings.auth_dev_fallback_enabled:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少认证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return UserContext(
        user_id=settings.auth_dev_user_id,
        username=settings.auth_dev_username,
        role="admin",
        acl_tags=("*",),
        auth_provider="development",
    )


def _session_user(settings: Settings, token: str) -> UserContext | None:
    try:
        from app.accounts import account_from_session

        return account_from_session(settings, token)
    except Exception:
        return None


def apply_request_user_override(settings: Settings, user: UserContext, request_model: Any) -> UserContext:
    if not settings.auth_trust_request_user_context and user.auth_provider != "development":
        return user

    user_id = getattr(request_model, "user_id", None)
    role = getattr(request_model, "role", None)
    acl_tags = getattr(request_model, "acl_tags", None)
    if user_id is None and role is None and acl_tags is None:
        return user

    return UserContext(
        user_id=(user_id or user.user_id),
        username=getattr(request_model, "username", None) or user.username or user_id,
        role=normalize_role(role or user.role),
        acl_tags=tuple(normalize_tags(acl_tags if acl_tags is not None else list(user.acl_tags))),
        auth_provider=f"{user.auth_provider}:request-context",
        raw_claims=user.raw_claims,
    )


def can_read_metadata(metadata: dict[str, Any] | None, user: UserContext | None, owner: str | None = None) -> bool:
    if user is None or user.is_admin:
        return True
    tags = normalize_tags((metadata or {}).get("acl_tags") or [])
    if not tags or "public" in tags:
        return True
    user_tags = set(user.acl_tags)
    if user_tags.intersection(tags):
        return True
    owner_values = {value for value in [owner, (metadata or {}).get("owner")] if value}
    return bool(owner_values.intersection({user.user_id, user.username}))


def filter_rows_by_acl(rows: list[dict[str, Any]], user: UserContext | None) -> list[dict[str, Any]]:
    if user is None or user.is_admin:
        return rows
    return [
        row
        for row in rows
        if can_read_metadata(row.get("metadata") or {}, user, owner=row.get("owner"))
    ]


def normalize_role(value: str | None) -> str:
    role = re.sub(r"[^a-zA-Z0-9_\-]+", "_", (value or "viewer").strip().lower()).strip("_")
    return role or "viewer"


def normalize_tags(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw_values = re.split(r"[,，;；\s]+", values)
    else:
        raw_values = list(values)

    tags: list[str] = []
    for value in raw_values:
        tag = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "-", str(value).strip()).strip("-").lower()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def _split_header_tags(value: str | None) -> list[str]:
    return [part for part in re.split(r"[,，;；\s]+", value or "") if part.strip()]


def _extract_bearer_token(authorization: str) -> str | None:
    match = re.match(r"^\s*Bearer\s+(.+?)\s*$", authorization, flags=re.I)
    return match.group(1) if match else None


def _decode_unverified_bearer(token: str) -> UserContext:
    claims = _decode_jwt_payload(token)
    return _claims_to_user(claims, provider="bearer-unverified")


def _verify_oidc_token(settings: Settings, token: str) -> UserContext:
    try:
        import jwt
        from jwt import PyJWKClient
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="OIDC 已启用，但缺少 PyJWT 依赖，请安装 pyjwt[crypto]",
        ) from exc

    jwks_url = settings.oidc_jwks_url or _discover_jwks_url(settings.oidc_issuer)
    if not jwks_url:
        raise HTTPException(status_code=500, detail="OIDC 已启用，但未配置 OIDC_JWKS_URL 或 OIDC_ISSUER")

    try:
        signing_key = PyJWKClient(jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
            audience=settings.oidc_audience or settings.oidc_client_id or None,
            issuer=settings.oidc_issuer or None,
            options={
                "verify_aud": bool(settings.oidc_audience or settings.oidc_client_id),
                "verify_iss": bool(settings.oidc_issuer),
            },
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"OIDC token 校验失败: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return _claims_to_user(claims, settings=settings, provider="oidc")


@lru_cache(maxsize=16)
def _discover_jwks_url(issuer: str | None) -> str | None:
    if not issuer:
        return None
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    with urllib.request.urlopen(url, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload.get("jwks_uri")


def _claims_to_user(claims: dict[str, Any], settings: Settings | None = None, provider: str = "oidc") -> UserContext:
    username_claim = settings.oidc_username_claim if settings else "preferred_username"
    role_claim = settings.oidc_role_claim if settings else "role"
    acl_claim = settings.oidc_acl_claim if settings else "acl_tags"
    groups_claim = settings.oidc_groups_claim if settings else "groups"

    user_id = str(claims.get("sub") or claims.get("user_id") or claims.get(username_claim) or "").strip()
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="OIDC token 缺少 sub/user_id")

    role_value = claims.get(role_claim)
    if isinstance(role_value, list):
        role_value = role_value[0] if role_value else "viewer"
    acl_values = []
    for claim_name in [acl_claim, groups_claim]:
        value = claims.get(claim_name)
        if isinstance(value, list):
            acl_values.extend(value)
        elif isinstance(value, str):
            acl_values.extend(_split_header_tags(value))

    return UserContext(
        user_id=user_id,
        username=claims.get(username_claim) or claims.get("name") or user_id,
        role=normalize_role(str(role_value or "viewer")),
        acl_tags=tuple(normalize_tags(acl_values)),
        auth_provider=provider,
        raw_claims=claims,
    )


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token 不是 JWT 格式")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无法解析 Bearer JWT payload") from exc
