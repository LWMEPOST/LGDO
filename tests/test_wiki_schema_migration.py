import sqlite3

from app.db import MAIN_TABLES, TABLE_PRIMARY_KEYS, init_db


REVISION_TABLES = {
    "wiki_page_revisions",
    "vault_write_intents",
    "wiki_file_observations",
    "vault_change_events",
    "knowledge_projection_jobs",
}


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
