import sqlite3

import pytest

from app.config import Settings
from app.db import MAIN_TABLES, TABLE_PRIMARY_KEYS, connect_app, init_app_db, init_db
from app.wiki_markdown import compute_file_hash, compute_semantic_hash, parse_wiki_bytes
from app.wiki_revisions import RevisionConflict, WikiRevisionService


REVISION_TABLES = {
    "wiki_page_revisions",
    "vault_write_intents",
    "wiki_file_observations",
    "vault_change_events",
    "vault_sync_issues",
    "knowledge_projection_jobs",
}


def schema_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_backend="sqlite",
        database_path=tmp_path / "schema.db",
        vault_path=tmp_path / "vault",
        projection_worker_enabled=False,
    )


def test_external_event_schema_has_payload_and_terminal_result_columns(tmp_path):
    settings = schema_settings(tmp_path)
    init_app_db(settings)
    with connect_app(settings) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info('vault_change_events')")
        }
        issue_columns = {
            row[1] for row in conn.execute("PRAGMA table_info('vault_sync_issues')")
        }
        observation_columns = {
            row[1]: row
            for row in conn.execute("PRAGMA table_info('wiki_file_observations')")
        }
    assert {"payload_digest", "result_payload_json"} <= columns
    assert {"id", "page_id", "file_hash", "generation", "resolved_at"} <= issue_columns
    assert observation_columns["page_id"][3] == 0


def seed_legacy_page_at(settings, page_path: str) -> None:
    page_id = "page_legacy_migration"
    revision_id = "wrev_legacy_migration"
    timestamp = "2026-07-14T00:00:00+00:00"
    content = (
        "---\nid: page_legacy_migration\nlgdo_page_id: page_legacy_migration\n"
        "lgdo_revision_id: wrev_legacy_migration\ntitle: Legacy\nsource_ids: []\n"
        "domain: product\npage_type: feature\nreview_status: draft\nowner:\n"
        "---\n# Legacy\n"
    )
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,page_id,domain,page_type,title,source_ids_json,review_status,
              owner,created_at,updated_at,current_revision_id,revision_number,
              file_hash,semantic_hash,projection_epoch,lifecycle_status
            ) VALUES (?,?,'product','feature','Legacy','[]','draft',NULL,?,?,?,1,?,?,1,'active')
            """,
            (
                page_path,
                page_id,
                timestamp,
                timestamp,
                revision_id,
                compute_file_hash(content.encode("utf-8")),
                compute_semantic_hash(parse_wiki_bytes(content.encode("utf-8"))),
            ),
        )
        conn.execute(
            """
            INSERT INTO wiki_page_revisions(
              id,page_id,page_path,revision_number,file_hash,semantic_hash,content,
              origin,source_ids_json,metadata_json,idempotency_key,created_at
            ) VALUES (?,?,?,1,?,?,?,'legacy','[]','{}',?,?)
            """,
            (
                revision_id,
                page_id,
                page_path,
                compute_file_hash(content.encode("utf-8")),
                compute_semantic_hash(parse_wiki_bytes(content.encode("utf-8"))),
                content,
                "legacy:migration",
                timestamp,
            ),
        )


def test_legacy_vault_events_are_upgraded_but_never_reconstructed_from_page_state(
    tmp_path,
):
    settings = schema_settings(tmp_path)
    with sqlite3.connect(settings.database_path) as conn:
        conn.execute(
            """
            CREATE TABLE vault_change_events (
              id TEXT PRIMARY KEY,kind TEXT NOT NULL,page_path TEXT NOT NULL,
              old_page_path TEXT,observation_id TEXT,expected_state_json TEXT NOT NULL DEFAULT '{}',
              status TEXT NOT NULL DEFAULT 'pending',result_revision_id TEXT,
              detected_at TEXT NOT NULL,updated_at TEXT NOT NULL
            )
            """
        )
        for event_id, status in (
            ("legacy-terminal", "applied"),
            ("legacy-pending", "pending"),
        ):
            conn.execute(
                """
                INSERT INTO vault_change_events(
                  id,kind,page_path,expected_state_json,status,detected_at,updated_at
                ) VALUES (?,'delete','wiki/product/legacy.md','{}',?,'2026-07-14','2026-07-14')
                """,
                (event_id, status),
            )
    init_app_db(settings)
    seed_legacy_page_at(settings, "wiki/product/legacy.md")
    service = WikiRevisionService(settings)
    with connect_app(settings) as conn:
        rows = conn.execute(
            """
            SELECT id,payload_digest,result_payload_json
            FROM vault_change_events ORDER BY id
            """
        ).fetchall()
        before = dict(
            conn.execute(
                "SELECT * FROM wiki_pages WHERE path='wiki/product/legacy.md'"
            ).fetchone()
        )
    assert [
        (row["payload_digest"], row["result_payload_json"]) for row in rows
    ] == [("", None), ("", None)]
    for event_id in ("legacy-terminal", "legacy-pending"):
        with pytest.raises(
            RevisionConflict,
            match="legacy vault event has no authoritative result payload",
        ):
            service.delete_page(event_id, "wiki/product/legacy.md")
    with connect_app(settings) as conn:
        after = dict(
            conn.execute(
                "SELECT * FROM wiki_pages WHERE path='wiki/product/legacy.md'"
            ).fetchone()
        )
    assert after == before


def table_info(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_sqlite_schema_contains_revision_tables_columns_and_partial_indexes(tmp_path):
    database = tmp_path / "fresh.db"
    init_db(database)

    with sqlite3.connect(database) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = table_info(conn, "wiki_pages")
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(review_items)")}

    assert REVISION_TABLES <= tables
    assert columns["revision_number"]["notnull"] == 1
    assert columns["revision_number"]["dflt_value"] == "0"
    assert columns["projection_epoch"]["notnull"] == 1
    assert columns["projection_epoch"]["dflt_value"] == "0"
    assert {"idx_review_pending_content_conflict", "idx_review_pending_concurrent_conflict"} <= indexes
    assert REVISION_TABLES <= set(MAIN_TABLES)
    assert all(table in TABLE_PRIMARY_KEYS for table in REVISION_TABLES)


def test_legacy_sqlite_schema_is_upgraded_without_losing_page(tmp_path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE wiki_pages (
              path TEXT PRIMARY KEY, domain TEXT NOT NULL, page_type TEXT NOT NULL,
              title TEXT NOT NULL, source_ids_json TEXT NOT NULL DEFAULT '[]',
              review_status TEXT NOT NULL DEFAULT 'draft', owner TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO wiki_pages VALUES
              ('wiki/product/faq/demo.md','product','faq','Demo','["src_1"]','reviewed','alice','t0','t0');
            """
        )

    init_db(database)

    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM wiki_pages").fetchone()
    assert row["path"] == "wiki/product/faq/demo.md"
    assert row["page_id"] is None
    assert row["current_revision_id"] is None
    assert row["revision_number"] == 0
    assert row["projection_epoch"] == 0


def test_half_migrated_nullable_counters_are_rebuilt_atomically(tmp_path):
    database = tmp_path / "half.db"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE wiki_pages (
              path TEXT PRIMARY KEY, domain TEXT NOT NULL, page_type TEXT NOT NULL,
              title TEXT NOT NULL, source_ids_json TEXT NOT NULL DEFAULT '[]',
              review_status TEXT NOT NULL DEFAULT 'draft', owner TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              revision_number INTEGER NULL, projection_epoch INTEGER NULL
            );
            CREATE UNIQUE INDEX idx_half_page_title ON wiki_pages(domain, title);
            CREATE TABLE wiki_page_events(path TEXT NOT NULL);
            CREATE TRIGGER trg_half_page_update AFTER UPDATE ON wiki_pages
            BEGIN
              INSERT INTO wiki_page_events(path) VALUES (NEW.path);
            END;
            INSERT INTO wiki_pages(path,domain,page_type,title,created_at,updated_at,revision_number,projection_epoch)
            VALUES ('wiki/product/faq/demo.md','product','faq','Demo','t0','t0',NULL,NULL);
            """
        )

    init_db(database)

    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        columns = table_info(conn, "wiki_pages")
        row = conn.execute("SELECT revision_number, projection_epoch FROM wiki_pages").fetchone()
        indexes = {item[1] for item in conn.execute("PRAGMA index_list(wiki_pages)")}
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        triggers = {
            item[0] for item in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        conn.execute("UPDATE wiki_pages SET revision_number=1")
        trigger_rows = conn.execute("SELECT path FROM wiki_page_events").fetchall()
    assert columns["revision_number"]["notnull"] == 1
    assert columns["projection_epoch"]["notnull"] == 1
    assert row["revision_number"] == row["projection_epoch"] == 0
    assert "idx_half_page_title" in indexes
    assert "trg_half_page_update" in triggers
    assert [item["path"] for item in trigger_rows] == ["wiki/product/faq/demo.md"]
    assert foreign_key_errors == []
