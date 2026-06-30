from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.config import Settings
from app.db import audit, connect_app, init_app_db, json_dump, rows_to_dicts
from app.models import EntityAliasRequest
from app.timeutil import now_iso


def normalize_alias(value: str) -> str:
    return re.sub(r"[\s_\-:：/\\|,，。；;?？!！()（）<>《》\"'`]+", "", (value or "").lower())


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
            term_key = normalize_alias(term)
            if term_key in {alias_key, canonical_key} and term_key in question_key:
                continue
            if term_key and term not in expansion_terms:
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


def expand_query_with_aliases(
    settings: Settings,
    question: str,
    domain: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    aliases = list_entity_aliases(settings, domain)
    context = build_alias_context(question, aliases)
    additions = context["expansion_terms"]
    matched = context["matched_aliases"]
    if not additions:
        return question, matched
    return f"{question}\n\n实体别名扩展：{'、'.join(additions)}", matched


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
    {
        "domain": "customer_service",
        "canonical_name": "审批人",
        "alias": "部门总监",
        "entity_type": "role",
        "metadata": {"terms": ["审批", "报销", "采购", "招待"]},
    },
]


def seed_default_entity_aliases(settings: Settings, actor: str | None = "system") -> int:
    count = 0
    for item in DEFAULT_ENTITY_ALIASES:
        upsert_entity_alias(settings, EntityAliasRequest(**item), actor=actor)
        count += 1
    return count


def list_entity_aliases(settings: Settings, domain: str | None = None) -> list[dict[str, Any]]:
    init_app_db(settings)
    params: list[object] = []
    query = "SELECT * FROM entity_aliases"
    if domain:
        query += " WHERE domain IS NULL OR domain = ?"
        params.append(domain)
    query += " ORDER BY domain, entity_type, canonical_name, alias"
    with connect_app(settings) as conn:
        return rows_to_dicts(conn.execute(query, params).fetchall())


def upsert_entity_alias(
    settings: Settings,
    request: EntityAliasRequest,
    actor: str | None = None,
) -> dict[str, Any]:
    init_app_db(settings)
    timestamp = now_iso()
    domain = request.domain
    canonical_key = normalize_alias(request.canonical_name)
    alias_key = normalize_alias(request.alias)
    entity_type = request.entity_type or "entity"
    alias_id = _alias_id(domain, canonical_key, alias_key, entity_type)
    metadata = request.metadata or {}

    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO entity_aliases(
              id, domain, canonical_name, canonical_key, alias, alias_key,
              entity_type, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              domain = excluded.domain,
              canonical_name = excluded.canonical_name,
              canonical_key = excluded.canonical_key,
              alias = excluded.alias,
              alias_key = excluded.alias_key,
              entity_type = excluded.entity_type,
              metadata_json = excluded.metadata_json,
              updated_at = excluded.updated_at
            """,
            (
                alias_id,
                domain,
                request.canonical_name,
                canonical_key,
                request.alias,
                alias_key,
                entity_type,
                json_dump(metadata),
                timestamp,
                timestamp,
            ),
        )
        audit(
            conn,
            "entity_alias_upserted",
            {
                "alias_id": alias_id,
                "domain": domain,
                "canonical_name": request.canonical_name,
                "alias": request.alias,
                "actor": actor,
            },
            timestamp,
        )
    return {
        "id": alias_id,
        "domain": domain,
        "canonical_name": request.canonical_name,
        "canonical_key": canonical_key,
        "alias": request.alias,
        "alias_key": alias_key,
        "entity_type": entity_type,
        "metadata": metadata,
    }


def _alias_id(domain: str | None, canonical_key: str, alias_key: str, entity_type: str) -> str:
    raw = f"{domain or '*'}|{entity_type}|{canonical_key}|{alias_key}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"alias_{digest}"
