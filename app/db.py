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
    "audit_logs",
]

TABLE_PRIMARY_KEYS = {
    "sources": "id",
    "wiki_pages": "path",
    "review_items": "id",
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
  domain TEXT NOT NULL,
  page_type TEXT NOT NULL,
  title TEXT NOT NULL,
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  review_status TEXT NOT NULL DEFAULT 'draft',
  owner TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wiki_pages_domain
ON wiki_pages(domain);

CREATE TABLE IF NOT EXISTS review_items (
  id TEXT PRIMARY KEY,
  page_path TEXT NOT NULL,
  issue_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  owner TEXT,
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_items_status
ON review_items(status);

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
  domain TEXT NOT NULL,
  page_type TEXT NOT NULL,
  title TEXT NOT NULL,
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  review_status TEXT NOT NULL DEFAULT 'draft',
  owner TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wiki_pages_domain
ON wiki_pages(domain);

CREATE TABLE IF NOT EXISTS review_items (
  id TEXT PRIMARY KEY,
  page_path TEXT NOT NULL,
  issue_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  owner TEXT,
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_items_status
ON review_items(status);

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
"""


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)


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
