from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib

import app.models as models
from app.config import Settings, get_settings
from app.db import (
    MAIN_TABLES,
    TABLE_PRIMARY_KEYS,
    connect_app,
    init_app_db,
)
from app.migration import _upsert_row
from app.models import Citation, CompileResponse


PROJECTION_TABLES = {
    "wiki_chunks",
    "gbrain_page_projections",
    "gbrain_projection_batches",
    "gbrain_projection_batch_jobs",
    "gbrain_projection_segments",
    "gbrain_projection_protections",
    "projection_state",
}

PROJECTION_COLUMNS = {
    "wiki_chunks": [
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
    ],
    "gbrain_page_projections": [
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
    ],
    "gbrain_projection_batches": [
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
    ],
    "gbrain_projection_batch_jobs": [
        "batch_id",
        "job_id",
        "page_id",
        "revision_id",
        "projection_epoch",
        "operation",
    ],
    "gbrain_projection_segments": [
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
    ],
    "gbrain_projection_protections": [
        "id",
        "gbrain_source_id",
        "page_id",
        "slug",
        "source_path",
        "reason",
        "active",
        "created_at",
        "resolved_at",
    ],
    "projection_state": ["key", "value", "updated_at"],
}


def test_projection_schema_has_epoch_and_mapping_constraints(tmp_path):
    settings = Settings(
        database_path=tmp_path / "projection.db",
        database_backend="sqlite",
        _env_file=None,
    )
    init_app_db(settings)

    with connect_app(settings) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert PROJECTION_TABLES <= tables
        for table, expected_columns in PROJECTION_COLUMNS.items():
            columns = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
            assert [row[1] for row in columns] == expected_columns
        batch_job_columns = conn.execute(
            "PRAGMA table_info('gbrain_projection_batch_jobs')"
        ).fetchall()
        assert [(row[1], row[5]) for row in batch_job_columns if row[5]] == [
            ("batch_id", 1),
            ("job_id", 2),
        ]
        indexes = {
            row[1] for row in conn.execute("PRAGMA index_list('wiki_chunks')")
        }
        assert "idx_wiki_chunks_epoch_unique" in indexes
        current_index = conn.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type='index' AND name='idx_gbrain_projection_current_page'
            """
        ).fetchone()
        assert current_index is not None
        assert "WHERE status = 'current'" in current_index[0]


def test_projection_generation_seed_is_idempotent(tmp_path):
    settings = Settings(
        database_path=tmp_path / "projection-seed.db",
        database_backend="sqlite",
        _env_file=None,
    )
    init_app_db(settings)
    with connect_app(settings) as conn:
        seeded = conn.execute(
            "SELECT value FROM projection_state WHERE key=?",
            ("gbrain_projection_generation",),
        ).fetchone()
        assert seeded[0] == 0
        conn.execute(
            "UPDATE projection_state SET value=7 WHERE key=?",
            ("gbrain_projection_generation",),
        )

    init_app_db(settings)
    with connect_app(settings) as conn:
        rows = conn.execute(
            "SELECT value FROM projection_state WHERE key=?",
            ("gbrain_projection_generation",),
        ).fetchall()
        assert [row[0] for row in rows] == [7]


def test_projection_tables_are_registered_for_migration():
    assert PROJECTION_TABLES <= set(MAIN_TABLES)
    assert PROJECTION_TABLES <= set(TABLE_PRIMARY_KEYS)
    assert TABLE_PRIMARY_KEYS["gbrain_projection_batch_jobs"] == (
        "batch_id",
        "job_id",
    )


def test_projection_and_query_tokens_are_not_aliased():
    settings = Settings(
        gbrain_api_key="legacy-query",
        gbrain_projection_api_key=None,
        _env_file=None,
    )
    assert settings.gbrain_query_token == "legacy-query"
    assert settings.gbrain_projection_api_key is None

    separate = Settings(
        gbrain_api_key="legacy-query",
        gbrain_query_api_key="query-only",
        gbrain_projection_api_key="projection-only",
        _env_file=None,
    )
    assert separate.gbrain_query_token == "query-only"
    assert separate.gbrain_projection_api_key == "projection-only"


def test_projection_settings_have_locked_defaults():
    settings = Settings(_env_file=None)

    assert settings.gbrain_managed_source_id is None
    assert settings.gbrain_import_allowed_root is None
    assert settings.gbrain_incremental_timeout_seconds == 120
    assert settings.gbrain_reconcile_timeout_seconds == 600
    assert settings.projection_worker_enabled is True
    assert settings.projection_poll_seconds == 1.0
    assert settings.projection_lease_seconds == 180
    assert settings.projection_claim_limit == 500


def test_citation_projection_identity_round_trips():
    citation = Citation(
        source_id="src_hr",
        wiki_page="wiki/product/faq/demo.md",
        snippet="Refunds are available for 30 days.",
        page_id="page_demo",
        revision_id="wrev_demo_3",
        chunk_id="wchunk_demo_0",
        origin="wiki",
    )
    restored = Citation.model_validate(citation.model_dump())

    assert restored == citation
    assert restored.model_dump()["revision_id"] == "wrev_demo_3"
    assert restored.origin == "wiki"


def test_projection_response_models_keep_compile_compatibility():
    response_type = getattr(models, "ProjectionJobResponse", None)
    assert response_type is not None
    response = response_type(
        id="pjob_1",
        target="gbrain",
        operation="upsert",
        page_id="page_1",
        revision_id="wrev_1",
        projection_epoch=2,
        status="pending",
        attempts=0,
        available_at="2026-07-14T00:00:00Z",
        last_error=None,
    )
    assert response.target == "gbrain"

    compiled = CompileResponse(
        job_id="compile_1",
        created_pages=1,
        updated_pages=0,
        review_items=0,
    )
    assert compiled.projection_job_ids == []
    assert compiled.projection_status == "queued"
    assert compiled.projection_jobs == 0


def test_background_projection_is_disabled_by_default_in_tests():
    assert getattr(get_settings(), "projection_worker_enabled", None) is False
    assert get_settings().gbrain_enabled is False


def test_httpx_is_a_runtime_dependency():
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject_path.open("rb") as handle:
        pyproject = tomllib.load(handle)

    runtime_dependencies = pyproject["project"]["dependencies"]
    dev_dependencies = pyproject["project"]["optional-dependencies"]["dev"]
    assert any(item.startswith("httpx") for item in runtime_dependencies)
    assert not any(item.startswith("httpx") for item in dev_dependencies)


class RecordingCursor:
    def __init__(self):
        self.sql = ""
        self.params = ()

    def execute(self, sql, params):
        self.sql = " ".join(sql.split())
        self.params = params


def test_projection_batch_job_migration_uses_composite_conflict_target():
    cursor = RecordingCursor()
    row = {
        "batch_id": "gbatch_1",
        "job_id": "pjob_1",
        "page_id": "page_1",
        "revision_id": "wrev_1",
        "projection_epoch": 3,
        "operation": "upsert",
    }

    _upsert_row(cursor, "gbrain_projection_batch_jobs", row)

    assert 'ON CONFLICT ("batch_id", "job_id") DO UPDATE SET' in cursor.sql
    update_sql = cursor.sql.split("DO UPDATE SET", 1)[1]
    assert '"batch_id" = EXCLUDED."batch_id"' not in update_sql
    assert '"job_id" = EXCLUDED."job_id"' not in update_sql
    assert '"page_id" = EXCLUDED."page_id"' in update_sql
    assert cursor.params == tuple(row.values())
