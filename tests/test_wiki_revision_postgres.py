import socket
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.config import get_settings
from app.db import (
    PgCompatConnection,
    connect_app,
    connect_postgres,
    init_postgres_schema,
)
from app.ingest import scan_sources
from app.models import CompileRequest, ScanRequest
from app.wiki import compile_wiki
from app.wiki_revisions import RevisionConflict


def pg_available() -> bool:
    try:
        with socket.create_connection(("localhost", 5432), timeout=1):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not pg_available(), reason="PostgreSQL 5432 is not available")
def test_postgres_revision_schema_uses_bigint_and_enforces_pending_conflict_uniqueness(monkeypatch):
    settings = get_settings().model_copy()
    settings.postgres_database = f"lgdo_revision_{uuid.uuid4().hex[:10]}"
    init_postgres_schema(settings)

    with connect_postgres(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_schema='public' AND table_name='wiki_file_observations'
              AND column_name IN ('size_bytes','mtime_ns')
            """
        )
        types = dict(cur.fetchall())
        cur.execute("SELECT indexname FROM pg_indexes WHERE tablename='review_items'")
        indexes = {row[0] for row in cur.fetchall()}
    assert types == {"size_bytes": "bigint", "mtime_ns": "bigint"}
    assert "idx_review_pending_content_conflict" in indexes
    assert "idx_review_pending_concurrent_conflict" in indexes


@pytest.mark.skipif(not pg_available(), reason="PostgreSQL 5432 is not available")
def test_concurrent_first_compile_replays_the_winning_postgres_page(
    tmp_path,
    monkeypatch,
):
    settings = get_settings().model_copy()
    settings.database_backend = "postgres"
    settings.postgres_database = f"lgdo_compile_{uuid.uuid4().hex[:10]}"
    settings.vault_path = tmp_path / "vault"
    settings.upload_path = tmp_path / "uploads"
    settings.gbrain_import_on_compile = False
    init_postgres_schema(settings)

    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "concurrent.md").write_text(
        "# Concurrent\n\nOne generated body.\n",
        encoding="utf-8",
    )
    scan_sources(
        settings,
        ScanRequest(
            root_path=str(samples),
            domain="product",
            owner="postgres-test",
        ),
    )
    with connect_app(settings) as conn:
        source_id = conn.execute(
            "SELECT id FROM sources WHERE title='concurrent'"
        ).fetchone()["id"]

    barrier = threading.Barrier(2)
    thread_state = threading.local()
    original_execute = PgCompatConnection.execute

    def synchronized_execute(self, query, params=None):
        cursor = original_execute(self, query, params)
        normalized = " ".join(query.split())
        if (
            normalized
            == "SELECT * FROM wiki_pages WHERE path=? FOR UPDATE"
            and not getattr(thread_state, "synchronized", False)
        ):
            thread_state.synchronized = True
            barrier.wait(timeout=10)
        return cursor

    monkeypatch.setattr(PgCompatConnection, "execute", synchronized_execute)

    def compile_once():
        try:
            return compile_wiki(
                settings,
                CompileRequest(
                    domain="product",
                    source_ids=[source_id],
                    compile_job_id="compile-concurrent-first",
                ),
            )
        except RevisionConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: compile_once(), range(2)))

    with connect_app(settings) as conn:
        pages = conn.execute(
            """
            SELECT page_id,current_revision_id,generated_revision_id
            FROM wiki_pages
            """
        ).fetchall()
        generated_count = conn.execute(
            """
            SELECT COUNT(*) AS count FROM wiki_page_revisions
            WHERE origin='generated'
            """
        ).fetchone()["count"]

    responses = [
        result for result in results if not isinstance(result, RevisionConflict)
    ]
    conflicts = [
        result for result in results if isinstance(result, RevisionConflict)
    ]
    assert responses
    assert len(conflicts) <= 1
    assert sum(result.created_pages for result in responses) <= 1
    assert sum(result.updated_pages for result in responses) == 0
    assert sum(result.conflicted_pages for result in responses) == 0
    assert len(pages) == 1
    assert generated_count == 1
    assert pages[0]["current_revision_id"] == pages[0]["generated_revision_id"]
