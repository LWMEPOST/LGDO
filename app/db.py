from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.config import Settings


MAIN_TABLES = [
    "sources",
    "wiki_pages",
    "review_items",
    "query_logs",
    "feedback",
    "knowledge_gaps",
    "eval_questions",
    "accounts",
    "auth_sessions",
    "entity_aliases",
    "ingest_reports",
    "document_chunks",
    "wiki_page_revisions",
    "wiki_file_observations",
    "vault_change_events",
    "vault_write_intents",
    "knowledge_projection_jobs",
    "audit_logs",
]

TABLE_PRIMARY_KEYS = {
    "sources": "id",
    "wiki_pages": "path",
    "review_items": "id",
    "wiki_page_revisions": "id",
    "vault_write_intents": "id",
    "wiki_file_observations": "id",
    "vault_change_events": "id",
    "knowledge_projection_jobs": "id",
    "query_logs": "id",
    "feedback": "id",
    "knowledge_gaps": "id",
    "eval_questions": "id",
    "accounts": "user_id",
    "auth_sessions": "token",
    "entity_aliases": "id",
    "ingest_reports": "id",
    "document_chunks": "id",
    "audit_logs": "id",
}

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  domain TEXT NOT NULL,
  owner TEXT,
  title TEXT NOT NULL,
  source_type TEXT NOT NULL,
  original_path TEXT NOT NULL,
  raw_path TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_compiled_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_hash_path
ON sources(content_hash, original_path);

CREATE INDEX IF NOT EXISTS idx_sources_domain
ON sources(domain);

CREATE TABLE IF NOT EXISTS wiki_pages (
  path TEXT PRIMARY KEY,
  page_id TEXT,
  domain TEXT NOT NULL,
  page_type TEXT NOT NULL,
  title TEXT NOT NULL,
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  review_status TEXT NOT NULL DEFAULT 'draft',
  owner TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  current_revision_id TEXT,
  generated_revision_id TEXT,
  accepted_generated_revision_id TEXT,
  revision_number INTEGER NOT NULL DEFAULT 0,
  file_hash TEXT,
  semantic_hash TEXT,
  last_write_token TEXT,
  rag_visible_revision_id TEXT,
  projection_epoch INTEGER NOT NULL DEFAULT 0,
  rag_visible_epoch INTEGER,
  lifecycle_status TEXT NOT NULL DEFAULT 'active',
  deleted_at TEXT,
  sync_error TEXT,
  observed_file_hash TEXT,
  pending_write_intent_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_wiki_pages_domain
ON wiki_pages(domain);

CREATE TABLE IF NOT EXISTS review_items (
  id TEXT PRIMARY KEY,
  page_path TEXT NOT NULL,
  page_id TEXT,
  issue_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  owner TEXT,
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  base_revision_id TEXT,
  candidate_revision_id TEXT,
  resolution_revision_id TEXT,
  expected_state_json TEXT NOT NULL DEFAULT '{}',
  resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_items_status
ON review_items(status);

CREATE TABLE IF NOT EXISTS wiki_page_revisions (
  id TEXT PRIMARY KEY, page_id TEXT NOT NULL, page_path TEXT NOT NULL,
  revision_number INTEGER NOT NULL, file_hash TEXT NOT NULL, semantic_hash TEXT NOT NULL,
  content TEXT NOT NULL, origin TEXT NOT NULL, base_revision_id TEXT,
  source_ids_json TEXT NOT NULL DEFAULT '[]', actor TEXT, note TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}', idempotency_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL, UNIQUE(page_id, revision_number)
);
CREATE INDEX IF NOT EXISTS idx_wiki_revisions_page_created
ON wiki_page_revisions(page_id, created_at);
CREATE INDEX IF NOT EXISTS idx_wiki_revisions_semantic
ON wiki_page_revisions(semantic_hash);

CREATE TABLE IF NOT EXISTS vault_write_intents (
  id TEXT PRIMARY KEY, page_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  expected_revision_id TEXT, expected_file_hash TEXT, target_path TEXT NOT NULL,
  write_token TEXT NOT NULL, backup_path TEXT, captured_file_hash TEXT,
  backup_last_observed_hash TEXT, backup_retention_status TEXT NOT NULL DEFAULT 'none',
  status TEXT NOT NULL DEFAULT 'pending', executor_owner TEXT, lease_expires_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vault_intents_status_available
ON vault_write_intents(status, lease_expires_at);

CREATE TABLE IF NOT EXISTS wiki_file_observations (
  id TEXT PRIMARY KEY, page_id TEXT, page_path TEXT NOT NULL, file_hash TEXT NOT NULL,
  size_bytes INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, content_bytes BLOB,
  content_prefix BLOB, content_truncated INTEGER NOT NULL DEFAULT 0,
  parse_status TEXT NOT NULL, error_code TEXT, error_message TEXT, observed_at TEXT NOT NULL,
  UNIQUE(page_path, file_hash)
);

CREATE TABLE IF NOT EXISTS vault_change_events (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, page_path TEXT NOT NULL, old_page_path TEXT,
  observation_id TEXT, expected_state_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
  result_revision_id TEXT, detected_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_projection_jobs (
  id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, target TEXT NOT NULL,
  operation TEXT NOT NULL, page_id TEXT, revision_id TEXT, projection_epoch INTEGER NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL, lease_owner TEXT,
  lease_expires_at TEXT, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projection_jobs_claim
ON knowledge_projection_jobs(target, status, available_at, lease_expires_at);

CREATE TABLE IF NOT EXISTS query_logs (
  id TEXT PRIMARY KEY,
  question TEXT NOT NULL,
  domain TEXT,
  answer TEXT NOT NULL,
  citations_json TEXT NOT NULL DEFAULT '[]',
  confidence TEXT NOT NULL,
  missing_info_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY,
  query_id TEXT NOT NULL,
  rating TEXT NOT NULL,
  comment TEXT,
  gap_created INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_gaps (
  id TEXT PRIMARY KEY,
  query_id TEXT NOT NULL,
  feedback_id TEXT,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  comment TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  priority TEXT NOT NULL DEFAULT 'medium',
  owner TEXT,
  linked_page_path TEXT,
  gap_path TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_knowledge_gaps_status
ON knowledge_gaps(status);

CREATE TABLE IF NOT EXISTS eval_questions (
  id TEXT PRIMARY KEY,
  question TEXT NOT NULL,
  domain TEXT NOT NULL,
  expected_sources_json TEXT NOT NULL DEFAULT '[]',
  expected_answer_points_json TEXT NOT NULL DEFAULT '[]',
  risk_level TEXT NOT NULL DEFAULT 'low',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
  user_id TEXT PRIMARY KEY,
  username TEXT,
  role TEXT NOT NULL DEFAULT 'viewer',
  acl_tags_json TEXT NOT NULL DEFAULT '[]',
  password_hash TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  auth_provider TEXT NOT NULL DEFAULT 'local',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_login_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_accounts_status
ON accounts(status);

CREATE TABLE IF NOT EXISTS auth_sessions (
  token TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
ON auth_sessions(user_id);

CREATE TABLE IF NOT EXISTS entity_aliases (
  id TEXT PRIMARY KEY,
  domain TEXT,
  canonical_name TEXT NOT NULL,
  canonical_key TEXT NOT NULL,
  alias TEXT NOT NULL,
  alias_key TEXT NOT NULL,
  entity_type TEXT NOT NULL DEFAULT 'entity',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

DROP INDEX IF EXISTS idx_entity_aliases_domain_alias;

CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_aliases_unique
ON entity_aliases(COALESCE(domain, ''), entity_type, canonical_key, alias_key);

CREATE INDEX IF NOT EXISTS idx_entity_aliases_domain
ON entity_aliases(domain);

CREATE TABLE IF NOT EXISTS audit_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_reports (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  parser TEXT NOT NULL,
  normalized_path TEXT NOT NULL,
  jsonl_path TEXT NOT NULL,
  chunk_count INTEGER NOT NULL,
  char_count INTEGER NOT NULL,
  warnings_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ingest_reports_source
ON ingest_reports(source_id);

CREATE TABLE IF NOT EXISTS document_chunks (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  domain TEXT NOT NULL,
  title TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  text TEXT NOT NULL,
  token_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_document_chunks_source_index
ON document_chunks(source_id, chunk_index);

CREATE INDEX IF NOT EXISTS idx_document_chunks_domain
ON document_chunks(domain);

CREATE INDEX IF NOT EXISTS idx_document_chunks_source
ON document_chunks(source_id);
"""

PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  domain TEXT NOT NULL,
  owner TEXT,
  title TEXT NOT NULL,
  source_type TEXT NOT NULL,
  original_path TEXT NOT NULL,
  raw_path TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_compiled_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_hash_path
ON sources(content_hash, original_path);

CREATE INDEX IF NOT EXISTS idx_sources_domain
ON sources(domain);

CREATE TABLE IF NOT EXISTS wiki_pages (
  path TEXT PRIMARY KEY,
  page_id TEXT,
  domain TEXT NOT NULL,
  page_type TEXT NOT NULL,
  title TEXT NOT NULL,
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  review_status TEXT NOT NULL DEFAULT 'draft',
  owner TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  current_revision_id TEXT,
  generated_revision_id TEXT,
  accepted_generated_revision_id TEXT,
  revision_number INTEGER NOT NULL DEFAULT 0,
  file_hash TEXT,
  semantic_hash TEXT,
  last_write_token TEXT,
  rag_visible_revision_id TEXT,
  projection_epoch INTEGER NOT NULL DEFAULT 0,
  rag_visible_epoch INTEGER,
  lifecycle_status TEXT NOT NULL DEFAULT 'active',
  deleted_at TEXT,
  sync_error TEXT,
  observed_file_hash TEXT,
  pending_write_intent_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_wiki_pages_domain
ON wiki_pages(domain);

CREATE TABLE IF NOT EXISTS review_items (
  id TEXT PRIMARY KEY,
  page_path TEXT NOT NULL,
  page_id TEXT,
  issue_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  owner TEXT,
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  base_revision_id TEXT,
  candidate_revision_id TEXT,
  resolution_revision_id TEXT,
  expected_state_json TEXT NOT NULL DEFAULT '{}',
  resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_items_status
ON review_items(status);

CREATE TABLE IF NOT EXISTS wiki_page_revisions (
  id TEXT PRIMARY KEY, page_id TEXT NOT NULL, page_path TEXT NOT NULL,
  revision_number INTEGER NOT NULL, file_hash TEXT NOT NULL, semantic_hash TEXT NOT NULL,
  content TEXT NOT NULL, origin TEXT NOT NULL, base_revision_id TEXT,
  source_ids_json TEXT NOT NULL DEFAULT '[]', actor TEXT, note TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}', idempotency_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL, UNIQUE(page_id, revision_number)
);
CREATE INDEX IF NOT EXISTS idx_wiki_revisions_page_created
ON wiki_page_revisions(page_id, created_at);
CREATE INDEX IF NOT EXISTS idx_wiki_revisions_semantic
ON wiki_page_revisions(semantic_hash);

CREATE TABLE IF NOT EXISTS vault_write_intents (
  id TEXT PRIMARY KEY, page_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  expected_revision_id TEXT, expected_file_hash TEXT, target_path TEXT NOT NULL,
  write_token TEXT NOT NULL, backup_path TEXT, captured_file_hash TEXT,
  backup_last_observed_hash TEXT, backup_retention_status TEXT NOT NULL DEFAULT 'none',
  status TEXT NOT NULL DEFAULT 'pending', executor_owner TEXT, lease_expires_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vault_intents_status_available
ON vault_write_intents(status, lease_expires_at);

CREATE TABLE IF NOT EXISTS wiki_file_observations (
  id TEXT PRIMARY KEY, page_id TEXT, page_path TEXT NOT NULL, file_hash TEXT NOT NULL,
  size_bytes BIGINT NOT NULL, mtime_ns BIGINT NOT NULL, content_bytes BYTEA,
  content_prefix BYTEA, content_truncated BOOLEAN NOT NULL DEFAULT FALSE,
  parse_status TEXT NOT NULL, error_code TEXT, error_message TEXT, observed_at TEXT NOT NULL,
  UNIQUE(page_path, file_hash)
);

CREATE TABLE IF NOT EXISTS vault_change_events (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, page_path TEXT NOT NULL, old_page_path TEXT,
  observation_id TEXT, expected_state_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
  result_revision_id TEXT, detected_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_projection_jobs (
  id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, target TEXT NOT NULL,
  operation TEXT NOT NULL, page_id TEXT, revision_id TEXT, projection_epoch INTEGER NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL, lease_owner TEXT,
  lease_expires_at TEXT, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projection_jobs_claim
ON knowledge_projection_jobs(target, status, available_at, lease_expires_at);

CREATE TABLE IF NOT EXISTS query_logs (
  id TEXT PRIMARY KEY,
  question TEXT NOT NULL,
  domain TEXT,
  answer TEXT NOT NULL,
  citations_json TEXT NOT NULL DEFAULT '[]',
  confidence TEXT NOT NULL,
  missing_info_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY,
  query_id TEXT NOT NULL,
  rating TEXT NOT NULL,
  comment TEXT,
  gap_created INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_gaps (
  id TEXT PRIMARY KEY,
  query_id TEXT NOT NULL,
  feedback_id TEXT,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  comment TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  priority TEXT NOT NULL DEFAULT 'medium',
  owner TEXT,
  linked_page_path TEXT,
  gap_path TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_knowledge_gaps_status
ON knowledge_gaps(status);

CREATE TABLE IF NOT EXISTS eval_questions (
  id TEXT PRIMARY KEY,
  question TEXT NOT NULL,
  domain TEXT NOT NULL,
  expected_sources_json TEXT NOT NULL DEFAULT '[]',
  expected_answer_points_json TEXT NOT NULL DEFAULT '[]',
  risk_level TEXT NOT NULL DEFAULT 'low',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
  user_id TEXT PRIMARY KEY,
  username TEXT,
  role TEXT NOT NULL DEFAULT 'viewer',
  acl_tags_json TEXT NOT NULL DEFAULT '[]',
  password_hash TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  auth_provider TEXT NOT NULL DEFAULT 'local',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_login_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_accounts_status
ON accounts(status);

CREATE TABLE IF NOT EXISTS auth_sessions (
  token TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
ON auth_sessions(user_id);

CREATE TABLE IF NOT EXISTS entity_aliases (
  id TEXT PRIMARY KEY,
  domain TEXT,
  canonical_name TEXT NOT NULL,
  canonical_key TEXT NOT NULL,
  alias TEXT NOT NULL,
  alias_key TEXT NOT NULL,
  entity_type TEXT NOT NULL DEFAULT 'entity',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

DROP INDEX IF EXISTS idx_entity_aliases_domain_alias;

CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_aliases_unique
ON entity_aliases(COALESCE(domain, ''), entity_type, canonical_key, alias_key);

CREATE INDEX IF NOT EXISTS idx_entity_aliases_domain
ON entity_aliases(domain);

CREATE TABLE IF NOT EXISTS audit_logs (
  id BIGSERIAL PRIMARY KEY,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_reports (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  parser TEXT NOT NULL,
  normalized_path TEXT NOT NULL,
  jsonl_path TEXT NOT NULL,
  chunk_count INTEGER NOT NULL,
  char_count INTEGER NOT NULL,
  warnings_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ingest_reports_source
ON ingest_reports(source_id);

CREATE TABLE IF NOT EXISTS document_chunks (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  domain TEXT NOT NULL,
  title TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  text TEXT NOT NULL,
  token_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_document_chunks_source_index
ON document_chunks(source_id, chunk_index);

CREATE INDEX IF NOT EXISTS idx_document_chunks_domain
ON document_chunks(domain);

CREATE INDEX IF NOT EXISTS idx_document_chunks_source
ON document_chunks(source_id);

ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS page_id TEXT;
ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS current_revision_id TEXT;
ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS generated_revision_id TEXT;
ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS accepted_generated_revision_id TEXT;
ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS revision_number INTEGER DEFAULT 0;
ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS file_hash TEXT;
ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS semantic_hash TEXT;
ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS last_write_token TEXT;
ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS rag_visible_revision_id TEXT;
ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS projection_epoch INTEGER DEFAULT 0;
ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS rag_visible_epoch INTEGER;
ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS lifecycle_status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS deleted_at TEXT;
ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS sync_error TEXT;
ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS observed_file_hash TEXT;
ALTER TABLE wiki_pages ADD COLUMN IF NOT EXISTS pending_write_intent_id TEXT;
UPDATE wiki_pages SET revision_number = 0 WHERE revision_number IS NULL;
ALTER TABLE wiki_pages ALTER COLUMN revision_number SET DEFAULT 0;
ALTER TABLE wiki_pages ALTER COLUMN revision_number SET NOT NULL;
UPDATE wiki_pages SET projection_epoch = 0 WHERE projection_epoch IS NULL;
ALTER TABLE wiki_pages ALTER COLUMN projection_epoch SET DEFAULT 0;
ALTER TABLE wiki_pages ALTER COLUMN projection_epoch SET NOT NULL;

ALTER TABLE review_items ADD COLUMN IF NOT EXISTS page_id TEXT;
ALTER TABLE review_items ADD COLUMN IF NOT EXISTS base_revision_id TEXT;
ALTER TABLE review_items ADD COLUMN IF NOT EXISTS candidate_revision_id TEXT;
ALTER TABLE review_items ADD COLUMN IF NOT EXISTS resolution_revision_id TEXT;
ALTER TABLE review_items ADD COLUMN IF NOT EXISTS expected_state_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE review_items ADD COLUMN IF NOT EXISTS resolved_at TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_pages_page_id
ON wiki_pages(page_id) WHERE page_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_pending_content_conflict
ON review_items(page_id) WHERE status = 'pending' AND issue_type = 'content_conflict';
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_pending_concurrent_conflict
ON review_items(page_id) WHERE status = 'pending' AND issue_type = 'concurrent_write_conflict';
"""


WIKI_PAGE_ADDITIONS = {
    "page_id": "TEXT",
    "current_revision_id": "TEXT",
    "generated_revision_id": "TEXT",
    "accepted_generated_revision_id": "TEXT",
    "revision_number": "INTEGER NOT NULL DEFAULT 0",
    "file_hash": "TEXT",
    "semantic_hash": "TEXT",
    "last_write_token": "TEXT",
    "rag_visible_revision_id": "TEXT",
    "projection_epoch": "INTEGER NOT NULL DEFAULT 0",
    "rag_visible_epoch": "INTEGER",
    "lifecycle_status": "TEXT NOT NULL DEFAULT 'active'",
    "deleted_at": "TEXT",
    "sync_error": "TEXT",
    "observed_file_hash": "TEXT",
    "pending_write_intent_id": "TEXT",
}

REVIEW_ITEM_ADDITIONS = {
    "page_id": "TEXT",
    "base_revision_id": "TEXT",
    "candidate_revision_id": "TEXT",
    "resolution_revision_id": "TEXT",
    "expected_state_json": "TEXT NOT NULL DEFAULT '{}'",
    "resolved_at": "TEXT",
}

WIKI_PAGE_COLUMNS = [
    "path",
    "page_id",
    "domain",
    "page_type",
    "title",
    "source_ids_json",
    "review_status",
    "owner",
    "created_at",
    "updated_at",
    "current_revision_id",
    "generated_revision_id",
    "accepted_generated_revision_id",
    "revision_number",
    "file_hash",
    "semantic_hash",
    "last_write_token",
    "rag_visible_revision_id",
    "projection_epoch",
    "rag_visible_epoch",
    "lifecycle_status",
    "deleted_at",
    "sync_error",
    "observed_file_hash",
    "pending_write_intent_id",
]

WIKI_PAGE_REBUILD_SQL = """
CREATE TABLE wiki_pages__new (
  path TEXT PRIMARY KEY,
  page_id TEXT,
  domain TEXT NOT NULL,
  page_type TEXT NOT NULL,
  title TEXT NOT NULL,
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  review_status TEXT NOT NULL DEFAULT 'draft',
  owner TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  current_revision_id TEXT,
  generated_revision_id TEXT,
  accepted_generated_revision_id TEXT,
  revision_number INTEGER NOT NULL DEFAULT 0,
  file_hash TEXT,
  semantic_hash TEXT,
  last_write_token TEXT,
  rag_visible_revision_id TEXT,
  projection_epoch INTEGER NOT NULL DEFAULT 0,
  rag_visible_epoch INTEGER,
  lifecycle_status TEXT NOT NULL DEFAULT 'active',
  deleted_at TEXT,
  sync_error TEXT,
  observed_file_hash TEXT,
  pending_write_intent_id TEXT
)
"""


def _sqlite_table_info(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.row_factory = previous_factory


def _sqlite_default_is_zero(value: Any) -> bool:
    if value is None:
        return False
    return str(value).strip().strip("()'") == "0"


def _sqlite_rebuild_wiki_pages(
    conn: sqlite3.Connection,
    existing: dict[str, sqlite3.Row],
) -> None:
    schema_sql = [
        row[0]
        for row in conn.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type IN ('index', 'trigger') AND tbl_name='wiki_pages' AND sql IS NOT NULL
            ORDER BY CASE type WHEN 'index' THEN 0 ELSE 1 END, name
            """
        ).fetchall()
    ]
    conn.execute("DROP TABLE IF EXISTS wiki_pages__new")
    conn.execute(WIKI_PAGE_REBUILD_SQL)

    defaults = {
        "source_ids_json": "'[]'",
        "review_status": "'draft'",
        "revision_number": "0",
        "projection_epoch": "0",
        "lifecycle_status": "'active'",
    }
    expressions: list[str] = []
    for column in WIKI_PAGE_COLUMNS:
        quoted = f'"{column}"'
        if column in existing:
            if column in {"revision_number", "projection_epoch"}:
                expressions.append(f"COALESCE({quoted}, 0)")
            else:
                expressions.append(quoted)
        else:
            expressions.append(defaults.get(column, "NULL"))

    columns_sql = ", ".join(f'"{column}"' for column in WIKI_PAGE_COLUMNS)
    before_count = conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0]
    conn.execute(
        f"INSERT INTO wiki_pages__new ({columns_sql}) "
        f"SELECT {', '.join(expressions)} FROM wiki_pages"
    )
    after_count = conn.execute("SELECT COUNT(*) FROM wiki_pages__new").fetchone()[0]
    if before_count != after_count:
        raise RuntimeError("SQLite Wiki migration row count mismatch")

    conn.execute("DROP TABLE wiki_pages")
    conn.execute("ALTER TABLE wiki_pages__new RENAME TO wiki_pages")
    for statement in schema_sql:
        conn.execute(statement)


def _ensure_sqlite_revision_schema(conn: sqlite3.Connection) -> None:
    columns = _sqlite_table_info(conn, "wiki_pages")
    rebuild = any(
        name in columns
        and (
            int(columns[name]["notnull"]) != 1
            or not _sqlite_default_is_zero(columns[name]["dflt_value"])
        )
        for name in ("revision_number", "projection_epoch")
    )
    if rebuild:
        _sqlite_rebuild_wiki_pages(conn, columns)
        columns = _sqlite_table_info(conn, "wiki_pages")

    for name, definition in WIKI_PAGE_ADDITIONS.items():
        if name not in columns:
            conn.execute(f'ALTER TABLE wiki_pages ADD COLUMN "{name}" {definition}')

    review_columns = _sqlite_table_info(conn, "review_items")
    for name, definition in REVIEW_ITEM_ADDITIONS.items():
        if name not in review_columns:
            conn.execute(f'ALTER TABLE review_items ADD COLUMN "{name}" {definition}')

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_pages_page_id "
        "ON wiki_pages(page_id) WHERE page_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_review_pending_content_conflict "
        "ON review_items(page_id) WHERE status = 'pending' AND issue_type = 'content_conflict'"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_review_pending_concurrent_conflict "
        "ON review_items(page_id) WHERE status = 'pending' AND issue_type = 'concurrent_write_conflict'"
    )


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_sqlite_revision_schema(conn)
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("SQLite foreign_key_check failed after Wiki migration")
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init_app_db(settings: Settings) -> None:
    if settings.database_backend == "postgres":
        init_postgres_schema(settings)
        return

    init_db(settings.database_path)


def init_postgres_schema(settings: Settings) -> None:
    ensure_postgres_database(settings)
    with connect_postgres(settings) as conn:
        with conn.cursor() as cur:
            for statement in _split_sql_statements(PG_SCHEMA):
                cur.execute(statement)


@contextmanager
def connect(path: Path) -> Iterable[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextmanager
def connect_app(settings: Settings) -> Iterable[Any]:
    if settings.database_backend == "postgres":
        with connect_postgres(settings) as conn:
            yield PgCompatConnection(conn)
        return

    with connect(settings.database_path) as conn:
        yield conn


@contextmanager
def connect_app_write(settings: Settings) -> Iterable[Any]:
    if settings.database_backend == "postgres":
        with connect_postgres(settings) as conn:
            yield PgCompatConnection(conn)
        return

    path = settings.database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for key in list(data):
        if key.endswith("_json"):
            out_key = key[:-5]
            data[out_key] = json.loads(data.pop(key) or "{}")
    return data


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [row_to_dict(row) or {} for row in rows]


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def audit(conn: Any, event_type: str, payload: dict[str, Any], created_at: str) -> None:
    conn.execute(
        "INSERT INTO audit_logs(event_type, payload_json, created_at) VALUES (?, ?, ?)",
        (event_type, json_dump(payload), created_at),
    )


def connect_postgres(settings: Settings, database: str | None = None, *, autocommit: bool = False):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("缺少 psycopg 依赖，请先运行 pip install -e .") from exc

    conn = psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password or "",
        dbname=database or settings.postgres_database,
    )
    conn.autocommit = autocommit
    return conn


def ensure_postgres_database(settings: Settings) -> None:
    try:
        with connect_postgres(settings):
            return
    except Exception:
        pass

    with connect_postgres(settings, database="postgres", autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (settings.postgres_database,))
            if cur.fetchone() is None:
                from psycopg import sql

                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(settings.postgres_database)))


class PgCompatConnection:
    def __init__(self, conn: Any):
        self.conn = conn

    def execute(self, query: str, params: Iterable[Any] | None = None):
        cur = self.conn.cursor()
        cur.execute(_sqlite_to_pg_sql(query), tuple(params or ()))
        return PgCompatCursor(cur)

    def commit(self) -> None:
        self.conn.commit()


class PgCompatCursor:
    def __init__(self, cursor: Any):
        self.cursor = cursor
        self.rowcount = cursor.rowcount

    def fetchone(self):
        row = self.cursor.fetchone()
        return _pg_row_to_dict(self.cursor, row)

    def fetchall(self):
        rows = self.cursor.fetchall()
        return [_pg_row_to_dict(self.cursor, row) for row in rows]


def _pg_row_to_dict(cursor: Any, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    columns = [desc.name for desc in cursor.description]
    return PgCompatRow(columns, row)


class PgCompatRow(dict):
    def __init__(self, columns: list[str], values: Any):
        super().__init__(zip(columns, values))
        self._columns = columns
        self._values = tuple(values)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def _sqlite_to_pg_sql(query: str) -> str:
    return query.replace("?", "%s")


def _split_sql_statements(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";") if statement.strip()]
