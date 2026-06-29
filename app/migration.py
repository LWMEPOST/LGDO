from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import Settings
from app.db import MAIN_TABLES, TABLE_PRIMARY_KEYS, connect, connect_postgres, init_db, init_postgres_schema


def migrate_sqlite_to_postgres(settings: Settings, sqlite_path: Path | None = None) -> dict[str, Any]:
    source_path = sqlite_path or settings.database_path
    init_db(source_path)
    init_postgres_schema(settings)

    migrated: dict[str, int] = {}
    with connect(source_path) as sqlite_conn, connect_postgres(settings) as pg_conn:
        with pg_conn.cursor() as cur:
            for table in MAIN_TABLES:
                rows = [dict(row) for row in sqlite_conn.execute(f"SELECT * FROM {table}").fetchall()]
                if not rows:
                    migrated[table] = 0
                    continue
                for row in rows:
                    _upsert_row(cur, table, row)
                migrated[table] = len(rows)
            _sync_pg_sequences(cur)

    return {
        "source": str(source_path),
        "target": {
            "host": settings.postgres_host,
            "port": settings.postgres_port,
            "database": settings.postgres_database,
            "user": settings.postgres_user,
        },
        "tables": migrated,
        "total_rows": sum(migrated.values()),
    }


def _upsert_row(cur: Any, table: str, row: dict[str, Any]) -> None:
    primary_key = TABLE_PRIMARY_KEYS[table]
    columns = list(row.keys())
    placeholders = ", ".join(["%s"] * len(columns))
    quoted_columns = ", ".join(_quote_identifier(column) for column in columns)
    updates = ", ".join(
        f"{_quote_identifier(column)} = EXCLUDED.{_quote_identifier(column)}"
        for column in columns
        if column != primary_key
    )
    conflict = f"ON CONFLICT ({_quote_identifier(primary_key)}) DO UPDATE SET {updates}" if updates else "ON CONFLICT DO NOTHING"
    cur.execute(
        f"""
        INSERT INTO {_quote_identifier(table)} ({quoted_columns})
        VALUES ({placeholders})
        {conflict}
        """,
        tuple(row[column] for column in columns),
    )


def _quote_identifier(value: str) -> str:
    if value not in MAIN_TABLES and not any(value in table_columns for table_columns in _KNOWN_COLUMNS.values()):
        raise ValueError(f"未知字段或表名: {value}")
    return '"' + value.replace('"', '""') + '"'


def _sync_pg_sequences(cur: Any) -> None:
    cur.execute(
        """
        SELECT setval(
          pg_get_serial_sequence('audit_logs', 'id'),
          COALESCE((SELECT MAX(id) FROM audit_logs), 1),
          (SELECT COUNT(*) > 0 FROM audit_logs)
        )
        """
    )


_KNOWN_COLUMNS = {
    "sources": {
        "id",
        "domain",
        "owner",
        "title",
        "source_type",
        "original_path",
        "raw_path",
        "content_hash",
        "size_bytes",
        "status",
        "metadata_json",
        "created_at",
        "updated_at",
        "last_compiled_at",
    },
    "wiki_pages": {
        "path",
        "domain",
        "page_type",
        "title",
        "source_ids_json",
        "review_status",
        "owner",
        "created_at",
        "updated_at",
    },
    "review_items": {"id", "page_path", "issue_type", "status", "owner", "source_ids_json", "created_at", "updated_at"},
    "query_logs": {"id", "question", "domain", "answer", "citations_json", "confidence", "missing_info_json", "created_at"},
    "feedback": {"id", "query_id", "rating", "comment", "gap_created", "created_at"},
    "knowledge_gaps": {
        "id",
        "query_id",
        "feedback_id",
        "question",
        "answer",
        "comment",
        "status",
        "priority",
        "owner",
        "linked_page_path",
        "gap_path",
        "created_at",
        "updated_at",
        "resolved_at",
    },
    "eval_questions": {
        "id",
        "question",
        "domain",
        "expected_sources_json",
        "expected_answer_points_json",
        "risk_level",
        "status",
        "created_at",
        "updated_at",
    },
    "audit_logs": {"id", "event_type", "payload_json", "created_at"},
    "ingest_reports": {
        "id",
        "source_id",
        "job_id",
        "parser",
        "normalized_path",
        "jsonl_path",
        "chunk_count",
        "char_count",
        "warnings_json",
        "metadata_json",
        "created_at",
    },
    "document_chunks": {"id", "source_id", "domain", "title", "chunk_index", "text", "token_json", "metadata_json", "created_at", "updated_at"},
}
