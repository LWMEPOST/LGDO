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
    primary_keys = (primary_key,) if isinstance(primary_key, str) else primary_key
    columns = list(row.keys())
    placeholders = ", ".join(["%s"] * len(columns))
    quoted_columns = ", ".join(_quote_identifier(column) for column in columns)
    updates = ", ".join(
        f"{_quote_identifier(column)} = EXCLUDED.{_quote_identifier(column)}"
        for column in columns
        if column not in primary_keys
    )
    conflict_target = ", ".join(_quote_identifier(column) for column in primary_keys)
    conflict = (
        f"ON CONFLICT ({conflict_target}) DO UPDATE SET {updates}"
        if updates
        else "ON CONFLICT DO NOTHING"
    )
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
    "accounts": {
        "user_id",
        "username",
        "role",
        "acl_tags_json",
        "password_hash",
        "status",
        "auth_provider",
        "created_at",
        "updated_at",
        "last_login_at",
    },
    "auth_sessions": {"token", "user_id", "created_at", "expires_at", "revoked_at"},
    "entity_aliases": {
        "id",
        "domain",
        "canonical_name",
        "canonical_key",
        "alias",
        "alias_key",
        "entity_type",
        "metadata_json",
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

_KNOWN_COLUMNS["wiki_pages"].update(
    {
        "page_id",
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
    }
)
_KNOWN_COLUMNS["review_items"].update(
    {
        "page_id",
        "base_revision_id",
        "candidate_revision_id",
        "resolution_revision_id",
        "expected_state_json",
        "resolved_at",
    }
)
_KNOWN_COLUMNS.update(
    {
        "wiki_page_revisions": {
            "id",
            "page_id",
            "page_path",
            "revision_number",
            "file_hash",
            "semantic_hash",
            "content",
            "origin",
            "base_revision_id",
            "source_ids_json",
            "actor",
            "note",
            "metadata_json",
            "idempotency_key",
            "created_at",
        },
        "wiki_file_observations": {
            "id",
            "page_id",
            "page_path",
            "file_hash",
            "size_bytes",
            "mtime_ns",
            "content_bytes",
            "content_prefix",
            "content_truncated",
            "parse_status",
            "error_code",
            "error_message",
            "observed_at",
        },
        "vault_change_events": {
            "id",
            "kind",
            "page_path",
            "old_page_path",
            "observation_id",
            "expected_state_json",
            "status",
            "result_revision_id",
            "payload_digest",
            "result_payload_json",
            "detected_at",
            "updated_at",
        },
        "vault_sync_issues": {
            "id",
            "page_path",
            "file_hash",
            "page_id",
            "issue_type",
            "error_summary",
            "status",
            "generation",
            "first_seen_at",
            "last_seen_at",
            "resolved_at",
        },
        "vault_write_intents": {
            "id",
            "page_id",
            "revision_id",
            "expected_revision_id",
            "expected_file_hash",
            "target_path",
            "write_token",
            "backup_path",
            "captured_file_hash",
            "backup_last_observed_hash",
            "backup_retention_status",
            "status",
            "executor_owner",
            "lease_expires_at",
            "attempts",
            "last_error",
            "created_at",
            "updated_at",
        },
        "knowledge_projection_jobs": {
            "id",
            "idempotency_key",
            "target",
            "operation",
            "page_id",
            "revision_id",
            "projection_epoch",
            "payload_json",
            "status",
            "attempts",
            "available_at",
            "lease_owner",
            "lease_expires_at",
            "last_error",
            "created_at",
            "updated_at",
        },
        "wiki_chunks": {
            "id",
            "page_id",
            "revision_id",
            "projection_epoch",
            "chunk_index",
            "page_path",
            "domain",
            "title",
            "text",
            "token_json",
            "embedding_json",
            "embedding_model",
            "source_ids_json",
            "created_at",
            "updated_at",
        },
        "gbrain_page_projections": {
            "id",
            "page_id",
            "revision_id",
            "projection_epoch",
            "page_path",
            "file_hash",
            "semantic_hash",
            "gbrain_source_id",
            "slug",
            "source_path",
            "gbrain_content_hash",
            "gbrain_page_generation",
            "status",
            "imported_at",
            "invalidated_at",
            "last_job_id",
        },
        "gbrain_projection_batches": {
            "id",
            "mode",
            "status",
            "batch_watermark_json",
            "included_snapshot_json",
            "lease_owner",
            "lease_expires_at",
            "result_json",
            "last_error",
            "started_at",
            "finished_at",
            "created_at",
            "updated_at",
        },
        "gbrain_projection_batch_jobs": {
            "batch_id",
            "job_id",
            "page_id",
            "revision_id",
            "projection_epoch",
            "operation",
        },
        "gbrain_projection_segments": {
            "id",
            "batch_id",
            "segment_index",
            "mode",
            "idempotency_key",
            "expected_pages_json",
            "protected_mappings_json",
            "status",
            "lease_owner",
            "lease_expires_at",
            "result_json",
            "last_error",
            "started_at",
            "finished_at",
        },
        "gbrain_projection_protections": {
            "id",
            "gbrain_source_id",
            "page_id",
            "slug",
            "source_path",
            "reason",
            "active",
            "created_at",
            "resolved_at",
        },
        "projection_state": {"key", "value", "updated_at"},
    }
)
