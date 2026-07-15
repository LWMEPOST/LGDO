import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.aliases import DEFAULT_ENTITY_ALIASES
from app.db import connect_postgres, init_app_db, init_postgres_schema
from app.main import app
from app.migration import migrate_sqlite_to_postgres


TEST_DATABASE_PREFIX = "lgdo_migration_"


def postgres_capability() -> tuple[bool, str]:
    admin = get_settings().model_copy(deep=True)
    try:
        with connect_postgres(
            admin,
            database="postgres",
            autocommit=True,
        ) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(rolsuper OR rolcreatedb, FALSE)
                FROM pg_roles WHERE rolname=current_user
                """
            )
            role = cur.fetchone()
    except Exception as exc:
        return (
            False,
            f"PostgreSQL maintenance connection failed ({type(exc).__name__})",
        )
    if role is None or not role[0]:
        return False, "PostgreSQL maintenance role lacks CREATEDB"
    return True, ""


def _create_owned_database(admin, database_name: str, mark_owned) -> None:
    from psycopg import sql

    with connect_postgres(
        admin,
        database="postgres",
        autocommit=True,
    ) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s",
            (database_name,),
        )
        if cur.fetchone() is not None:
            raise AssertionError(
                f"refusing to reuse migration database {database_name}"
            )
        mark_owned()
        cur.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )


def _drop_owned_database(admin, database_name: str) -> None:
    from psycopg import sql

    identifier = sql.Identifier(database_name)
    stages = (
        (
            "alter",
            sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS false").format(
                identifier
            ),
            None,
        ),
        (
            "terminate",
            """
            SELECT pg_terminate_backend(pid) FROM pg_stat_activity
            WHERE datname=%s AND pid<>pg_backend_pid()
            """,
            (database_name,),
        ),
        (
            "drop",
            sql.SQL("DROP DATABASE IF EXISTS {}").format(identifier),
            None,
        ),
    )
    errors: list[tuple[str, Exception]] = []
    for stage, statement, params in stages:
        try:
            with connect_postgres(
                admin,
                database="postgres",
                autocommit=True,
            ) as conn, conn.cursor() as cur:
                if params is None:
                    cur.execute(statement)
                else:
                    cur.execute(statement, params)
        except Exception as exc:
            errors.append((stage, exc))
    database_exists = None
    try:
        with connect_postgres(
            admin,
            database="postgres",
            autocommit=True,
        ) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname=%s",
                (database_name,),
            )
            database_exists = cur.fetchone() is not None
    except Exception as exc:
        errors.append(("verify", exc))
    summary = ", ".join(
        f"{stage}={type(error).__name__}" for stage, error in errors
    )
    if database_exists:
        raise AssertionError(
            f"migration database leaked: {database_name}; {summary}"
        ) from (errors[0][1] if errors else None)
    if errors:
        raise RuntimeError(
            f"migration database cleanup failed: {summary}"
        ) from errors[0][1]


@pytest.fixture
def postgres_database():
    available, reason = postgres_capability()
    if not available:
        pytest.skip(reason)
    admin = get_settings().model_copy(deep=True)
    database_name = f"{TEST_DATABASE_PREFIX}{uuid.uuid4().hex[:24]}"
    owned = False

    def mark_owned() -> None:
        nonlocal owned
        owned = True

    try:
        _create_owned_database(admin, database_name, mark_owned)
        yield database_name
    finally:
        if owned:
            _drop_owned_database(admin, database_name)


def empty_migration_pair(tmp_path, monkeypatch, postgres_database):
    settings = get_settings()
    sqlite_path = tmp_path / "data/source.db"
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", sqlite_path)
    monkeypatch.setattr(settings, "postgres_database", postgres_database)
    monkeypatch.setattr(settings, "vault_path", tmp_path / "vault")
    monkeypatch.setattr(settings, "upload_path", tmp_path / "uploads")
    init_app_db(settings)
    init_postgres_schema(settings)
    return settings, sqlite_path


def test_migrates_populated_vault_sync_issue(
    tmp_path,
    monkeypatch,
    postgres_database,
):
    settings, sqlite_path = empty_migration_pair(
        tmp_path,
        monkeypatch,
        postgres_database,
    )
    expected = (
        "visi_migration",
        "wiki/product/broken.md",
        "f" * 64,
        None,
        "invalid_frontmatter",
        "invalid yaml",
        "open",
        3,
        "2026-07-15T00:00:00+00:00",
        "2026-07-15T00:01:00+00:00",
        None,
    )
    with sqlite3.connect(sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO vault_sync_issues(
              id,page_path,file_hash,page_id,issue_type,error_summary,status,
              generation,first_seen_at,last_seen_at,resolved_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            expected,
        )

    result = migrate_sqlite_to_postgres(settings, sqlite_path)

    assert result["tables"]["vault_sync_issues"] == 1
    with connect_postgres(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id,page_path,file_hash,page_id,issue_type,error_summary,status,
                   generation,first_seen_at,last_seen_at,resolved_at
            FROM vault_sync_issues WHERE id=%s
            """,
            (expected[0],),
        )
        assert cur.fetchone() == expected


def test_migrate_sqlite_metadata_to_postgres_preserves_core_queries(
    tmp_path,
    monkeypatch,
    postgres_database,
):
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "refund_policy.md").write_text(
        "# 退款政策\n\n用户 7 天内可以申请退款，客服需要核验订单号。",
        encoding="utf-8",
    )
    (sample_dir / "permission_export.md").write_text(
        "# 权限导出\n\n管理员可以在权限中心导出成员权限清单，导出前需要二次确认。",
        encoding="utf-8",
    )

    settings = get_settings()
    sqlite_path = tmp_path / "data" / "source.db"
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", sqlite_path)
    monkeypatch.setattr(settings, "vault_path", tmp_path / "vault")
    monkeypatch.setattr(settings, "upload_path", tmp_path / "uploads")
    monkeypatch.setattr(settings, "deepseek_api_key", None)
    monkeypatch.setattr(settings, "deepseek_model", None)

    client = TestClient(app)
    scan = client.post(
        "/api/internal/sources/scan",
        json={
            "root_path": str(sample_dir),
            "domain": "product",
            "owner": "tester",
            "acl_tags": ["internal", "product"],
            "metadata_defaults": {"source_system": "migration_test"},
        },
    )
    assert scan.status_code == 200
    compile_result = client.post("/api/internal/wiki/compile", json={"domain": "product"})
    assert compile_result.status_code == 200

    with sqlite3.connect(sqlite_path) as conn:
        page_path, source_ids_json = conn.execute(
            "SELECT path, source_ids_json FROM wiki_pages ORDER BY path DESC LIMIT 1"
        ).fetchone()
        page_id = "page_migration_fixture"
        revision_id = "wrev_migration_fixture"
        content = (settings.vault_path / page_path).read_text(encoding="utf-8")
        conn.execute(
            """
            UPDATE wiki_pages
            SET page_id=?, current_revision_id=?, revision_number=1,
                file_hash='fixture_file_hash', semantic_hash='fixture_semantic_hash'
            WHERE path=?
            """,
            (page_id, revision_id, page_path),
        )
        conn.execute(
            """
            INSERT INTO wiki_page_revisions(
              id,page_id,page_path,revision_number,file_hash,semantic_hash,content,origin,
              base_revision_id,source_ids_json,actor,note,metadata_json,idempotency_key,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                revision_id, page_id, page_path, 1, "fixture_file_hash", "fixture_semantic_hash",
                content, "legacy", None, source_ids_json, "migration-test", None, "{}",
                "migration:test:revision", "t0",
            ),
        )
        source_revision_count = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions"
        ).fetchone()[0]
        source_revision_ids = {
            row[0] for row in conn.execute("SELECT id FROM wiki_page_revisions").fetchall()
        }
        source_page = conn.execute(
            """
            SELECT page_id, path, current_revision_id, revision_number,
                   projection_epoch, lifecycle_status
            FROM wiki_pages
            ORDER BY path
            LIMIT 1
            """
        ).fetchone()
        source_page_id, source_page_path = source_page[:2]
        source_page_state = source_page[2:]

    answer_before = client.post(
        "/api/internal/ask",
        json={"question": "管理员怎么导出成员权限清单？", "domain": "product"},
    )
    assert answer_before.status_code == 200
    expected_source_id = answer_before.json()["citations"][0]["source_id"]
    alias_response = client.post(
        "/api/internal/aliases",
        json={
            "domain": "product",
            "canonical_name": "权限清单",
            "alias": "成员权限导出",
            "entity_type": "feature",
            "metadata": {"terms": ["管理员"]},
        },
    )
    assert alias_response.status_code == 200

    monkeypatch.setattr(settings, "postgres_database", postgres_database)
    init_postgres_schema(settings)

    result = migrate_sqlite_to_postgres(settings, sqlite_path)
    assert result["tables"]["sources"] == 2
    assert result["tables"]["wiki_pages"] >= 2
    assert result["tables"]["document_chunks"] >= 2
    assert result["tables"]["entity_aliases"] == len(DEFAULT_ENTITY_ALIASES) + 1
    assert result["total_rows"] >= 6

    with connect_postgres(settings) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM wiki_page_revisions")
        target_revision_count = cur.fetchone()[0]
        cur.execute("SELECT id FROM wiki_page_revisions")
        target_revision_ids = {row[0] for row in cur.fetchall()}
        cur.execute(
            """
            SELECT current_revision_id, revision_number, projection_epoch, lifecycle_status
            FROM wiki_pages
            WHERE page_id=%s AND path=%s
            """,
            (source_page_id, source_page_path),
        )
        target_page_state = cur.fetchone()
    assert (
        result["tables"]["wiki_page_revisions"]
        == target_revision_count
        == source_revision_count
    )
    assert target_revision_ids == source_revision_ids
    assert target_page_state == source_page_state
    assert target_page_state[0] in target_revision_ids

    monkeypatch.setattr(settings, "database_backend", "postgres")
    migrated_sources = client.get("/api/internal/sources?domain=product")
    assert migrated_sources.status_code == 200
    assert {source["title"] for source in migrated_sources.json()} == {"refund_policy", "permission_export"}

    status = client.get("/api/internal/rag/status")
    assert status.status_code == 200
    assert status.json()["database_backend"] == "postgres"
    assert status.json()["chunk_count"] >= 2

    answer_after = client.post(
        "/api/internal/ask",
        json={"question": "管理员怎么导出成员权限清单？", "domain": "product"},
    )
    assert answer_after.status_code == 200
    assert answer_after.json()["citations"][0]["source_id"] == expected_source_id


def test_migration_endpoint_returns_table_counts(
    tmp_path,
    monkeypatch,
    postgres_database,
):
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "refund_policy.md").write_text("# 退款政策\n\n用户 7 天内可以申请退款。", encoding="utf-8")

    settings = get_settings()
    sqlite_path = tmp_path / "data" / "source.db"
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", sqlite_path)
    monkeypatch.setattr(settings, "postgres_database", postgres_database)
    monkeypatch.setattr(settings, "vault_path", tmp_path / "vault")
    monkeypatch.setattr(settings, "upload_path", tmp_path / "uploads")
    monkeypatch.setattr(settings, "deepseek_api_key", None)
    monkeypatch.setattr(settings, "deepseek_model", None)

    init_postgres_schema(settings)

    client = TestClient(app)
    scan = client.post(
        "/api/internal/sources/scan",
        json={
            "root_path": str(sample_dir),
            "domain": "product",
            "owner": "tester",
            "acl_tags": ["internal"],
        },
    )
    assert scan.status_code == 200

    response = client.post(f"/api/internal/database/migrate-sqlite-to-postgres?sqlite_path={sqlite_path}")
    assert response.status_code == 200
    body = response.json()
    assert body["tables"]["sources"] == 1
    assert body["tables"]["document_chunks"] >= 1
    assert body["total_rows"] >= 3
