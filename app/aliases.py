from __future__ import annotations

import json
import re
import uuid
from typing import Any

from app.config import Settings
from app.db import audit, connect_app, init_app_db, json_dump, row_to_dict, rows_to_dicts
from app.models import EntityAliasRequest
from app.timeutil import now_iso


def normalize_alias(value: str) -> str:
    return re.sub(r"[\s_\-:：/\\|,，。；;?？!！()（）<>《》\"'`]+", "", value.lower())


def _parse_alias_metadata(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("metadata")
    if isinstance(raw, dict):
        return raw
    raw_json = item.get("metadata_json")
    if not raw_json:
        return {}
    try:
        parsed = json.loads(raw_json)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _alias_terms(item: dict[str, Any]) -> list[str]:
    metadata = _parse_alias_metadata(item)
    terms: list[str] = []
    for value in [item.get("canonical_name"), item.get("alias"), item.get("entity_type")]:
        if value and str(value) not in terms:
            terms.append(str(value))
    for value in metadata.get("terms") or []:
        if value and str(value) not in terms:
            terms.append(str(value))
    return terms


def build_alias_context(question: str, aliases: list[dict[str, Any]]) -> dict[str, Any]:
    question_key = normalize_alias(question)
    matched: list[dict[str, Any]] = []
    expansion_terms: list[str] = []
    for item in aliases:
        alias_key = item.get("alias_key") or normalize_alias(item.get("alias") or "")
        canonical_key = item.get("canonical_key") or normalize_alias(item.get("canonical_name") or "")
        if not alias_key or not canonical_key:
            continue
        if alias_key not in question_key and canonical_key not in question_key:
            continue
        matched.append(item)
        for term in _alias_terms(item):
            if term not in expansion_terms:
                expansion_terms.append(term)
    return {"matched_aliases": matched, "expansion_terms": expansion_terms}


def score_alias_context(alias_context: dict[str, Any] | None, title: str, text: str) -> float:
    if not alias_context:
        return 0.0
    haystack = normalize_alias(f"{title}\n{text}")
    score = 0.0
    for item in alias_context.get("matched_aliases") or []:
        terms = _alias_terms(item)
        canonical = normalize_alias(item.get("canonical_name") or "")
        alias = normalize_alias(item.get("alias") or "")
        if canonical and canonical in haystack:
            score += 18.0
        if alias and alias in haystack:
            score += 12.0
        for term in terms:
            key = normalize_alias(term)
            if key and key in haystack:
                score += 6.0
    return min(score, 48.0)


def list_entity_aliases(settings: Settings, domain: str | None = None) -> list[dict]:
    init_app_db(settings)
    query = "SELECT * FROM entity_aliases"
    params: list[object] = []
    if domain:
        query += " WHERE domain = ? OR domain IS NULL OR domain = ''"
        params.append(domain)
    query += " ORDER BY domain, canonical_name, alias"
    with connect_app(settings) as conn:
        return rows_to_dicts(conn.execute(query, params).fetchall())


def upsert_entity_alias(settings: Settings, request: EntityAliasRequest, actor: str | None = None) -> dict:
    init_app_db(settings)
    timestamp = now_iso()
    canonical = request.canonical_name.strip()
    alias = request.alias.strip()
    if not canonical or not alias:
        raise ValueError("canonical_name 和 alias 不能为空")

    domain = request.domain or ""
    alias_key = normalize_alias(alias)
    canonical_key = normalize_alias(canonical)
    alias_id = f"alias_{uuid.uuid4().hex[:12]}"
    with connect_app(settings) as conn:
        existing = conn.execute(
            "SELECT * FROM entity_aliases WHERE domain = ? AND alias_key = ?",
            (domain, alias_key),
        ).fetchone()
        if existing:
            alias_id = existing["id"]
            conn.execute(
                """
                UPDATE entity_aliases
                SET canonical_name = ?, canonical_key = ?, alias = ?, entity_type = ?,
                    metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    canonical,
                    canonical_key,
                    alias,
                    request.entity_type,
                    json_dump(request.metadata),
                    timestamp,
                    alias_id,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO entity_aliases(
                  id, domain, canonical_name, canonical_key, alias, alias_key,
                  entity_type, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alias_id,
                    domain,
                    canonical,
                    canonical_key,
                    alias,
                    alias_key,
                    request.entity_type,
                    json_dump(request.metadata),
                    timestamp,
                    timestamp,
                ),
            )
        audit(
            conn,
            "entity_alias_upserted",
            {"alias_id": alias_id, "canonical_name": canonical, "alias": alias, "domain": domain, "actor": actor},
            timestamp,
        )
        return row_to_dict(conn.execute("SELECT * FROM entity_aliases WHERE id = ?", (alias_id,)).fetchone()) or {}


def delete_entity_alias(settings: Settings, alias_id: str, actor: str | None = None) -> dict:
    init_app_db(settings)
    timestamp = now_iso()
    with connect_app(settings) as conn:
        row = row_to_dict(conn.execute("SELECT * FROM entity_aliases WHERE id = ?", (alias_id,)).fetchone())
        if row is None:
            raise ValueError(f"entity alias 不存在: {alias_id}")
        conn.execute("DELETE FROM entity_aliases WHERE id = ?", (alias_id,))
        audit(conn, "entity_alias_deleted", {"alias_id": alias_id, "actor": actor}, timestamp)
    return row


DEFAULT_ENTITY_ALIASES: list[dict[str, Any]] = [
    {
        "domain": "customer_service",
        "canonical_name": "部门负责人",
        "alias": "部门总监",
        "entity_type": "role",
        "metadata": {"terms": ["审批", "报销", "采购", "请假", "远程", "招待"]},
    },
    {
        "domain": "customer_service",
        "canonical_name": "直属上级",
        "alias": "部门总监",
        "entity_type": "role",
        "metadata": {"terms": ["审批", "请假", "远程办公"]},
    },
]


def seed_default_entity_aliases(settings: Settings, actor: str | None = "system") -> int:
    count = 0
    for item in DEFAULT_ENTITY_ALIASES:
        upsert_entity_alias(settings, EntityAliasRequest(**item), actor=actor)
        count += 1
    return count


def expand_query_with_aliases(settings: Settings, question: str, domain: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    aliases = list_entity_aliases(settings, domain)
    context = build_alias_context(question, aliases)
    additions = context["expansion_terms"]
    matched = context["matched_aliases"]
    if not additions:
        return question, []
    return f"{question}\n\n实体别名扩展：{'、'.join(additions)}", matched
