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
from app.wiki_markdown import capture_file_observation
from app.wiki_revisions import (
    ManualSaveCommand,
    MutationResult,
    RevisionConflict,
    WikiRevisionService,
)


TEST_DATABASE_PREFIX = "lgdo_t16_"


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
                FROM pg_roles
                WHERE rolname = current_user
                """
            )
            role = cur.fetchone()
    except Exception as exc:
        return (
            False,
            "PostgreSQL maintenance connection failed for "
            f"{admin.postgres_user}@{admin.postgres_host}:"
            f"{admin.postgres_port}: {type(exc).__name__}: {exc}",
        )
    if role is None or not role[0]:
        return (
            False,
            f"PostgreSQL role {admin.postgres_user!r} lacks SUPERUSER or CREATEDB",
        )
    return True, ""


def _create_test_database(admin, database_name: str) -> None:
    from psycopg import sql

    with connect_postgres(
        admin,
        database="postgres",
        autocommit=True,
    ) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (database_name,),
        )
        if cur.fetchone() is not None:
            raise AssertionError(f"refusing to reuse test database {database_name}")
        cur.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )


def _drop_test_database(admin, database_name: str) -> None:
    from psycopg import sql

    with connect_postgres(
        admin,
        database="postgres",
        autocommit=True,
    ) as conn, conn.cursor() as cur:
        identifier = sql.Identifier(database_name)
        cur.execute(
            sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS false").format(
                identifier
            )
        )
        cur.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = %s AND pid <> pg_backend_pid()
            """,
            (database_name,),
        )
        cur.execute(sql.SQL("DROP DATABASE {}").format(identifier))
        cur.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (database_name,),
        )
        assert cur.fetchone() is None, f"test database leaked: {database_name}"


@pytest.fixture
def postgres_settings(tmp_path):
    available, reason = postgres_capability()
    if not available:
        pytest.skip(reason)

    admin = get_settings().model_copy(deep=True)
    database_name = f"{TEST_DATABASE_PREFIX}{uuid.uuid4().hex[:24]}"
    settings = admin.model_copy(
        deep=True,
        update={
            "database_backend": "postgres",
            "postgres_database": database_name,
            "vault_path": tmp_path / "vault",
            "upload_path": tmp_path / "uploads",
            "gbrain_import_on_compile": False,
        },
    )
    created = False
    try:
        _create_test_database(admin, database_name)
        created = True
        init_postgres_schema(settings)
        yield settings
    finally:
        if created:
            _drop_test_database(admin, database_name)


def test_postgres_revision_schema_uses_bigint_and_enforces_pending_conflict_uniqueness(
    postgres_settings,
):
    settings = postgres_settings

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


def test_concurrent_first_compile_replays_the_winning_postgres_page(
    tmp_path,
    monkeypatch,
    postgres_settings,
):
    settings = postgres_settings

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


def test_concurrent_external_events_share_one_postgres_observation(
    tmp_path,
    monkeypatch,
    postgres_settings,
):
    settings = postgres_settings

    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "observed.md").write_text(
        "# Observed\n\nGenerated body.\n",
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
    compile_wiki(settings, CompileRequest(domain="product"))
    with connect_app(settings) as conn:
        page_path = conn.execute(
            "SELECT path FROM wiki_pages WHERE title='observed'"
        ).fetchone()["path"]

    service = WikiRevisionService(settings)
    target = settings.vault_path / page_path
    invalid_bytes = b"---\ntitle: [invalid\n---\n# Broken\n"
    target.write_bytes(invalid_bytes)
    observation = capture_file_observation(
        target,
        max_content_bytes=1024 * 1024,
    )

    barrier = threading.Barrier(2)
    thread_state = threading.local()
    synchronized_threads: set[int] = set()
    synchronized_threads_lock = threading.Lock()
    original_execute = PgCompatConnection.execute

    def synchronized_execute(self, query, params=None):
        normalized = " ".join(query.split())
        if (
            normalized
            == "SELECT * FROM wiki_pages WHERE path=? FOR UPDATE"
            and not getattr(thread_state, "occurrence_lock_synchronized", False)
        ):
            thread_state.occurrence_lock_synchronized = True
            with synchronized_threads_lock:
                synchronized_threads.add(threading.get_ident())
            barrier.wait(timeout=10)
        return original_execute(self, query, params)

    monkeypatch.setattr(PgCompatConnection, "execute", synchronized_execute)

    def ingest(event_id: str):
        try:
            return service.ingest_external_change(
                event_id,
                page_path,
                observation,
            )
        except Exception as exc:
            return exc

    event_ids = ["postgres-observation-a", "postgres-observation-b"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(ingest, event_ids))

    with connect_app(settings) as conn:
        observations = conn.execute(
            """
            SELECT id FROM wiki_file_observations
            WHERE page_path=? AND file_hash=?
            """,
            (page_path, observation.file_hash),
        ).fetchall()
        events = conn.execute(
            """
            SELECT id,observation_id FROM vault_change_events
            WHERE id IN (?,?) ORDER BY id
            """,
            tuple(event_ids),
        ).fetchall()

    assert len(synchronized_threads) == 2
    assert all(
        not isinstance(result, Exception) or isinstance(result, RevisionConflict)
        for result in results
    ), results
    assert any(not isinstance(result, Exception) for result in results)
    assert len(observations) == 1
    assert [row["id"] for row in events] == sorted(event_ids)
    assert {row["observation_id"] for row in events} == {observations[0]["id"]}


def test_concurrent_manual_saves_with_same_revision_have_one_postgres_winner(
    monkeypatch,
    postgres_settings,
):
    settings = postgres_settings
    page_path = "wiki/product/faq/concurrent-manual.md"
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True)
    target.write_text(
        "---\ntitle: Concurrent manual\nsource_ids: [src_manual]\n"
        "review_status: draft\n---\n# Concurrent manual\n",
        encoding="utf-8",
    )
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,domain,page_type,title,source_ids_json,review_status,
              created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                page_path,
                "product",
                "faq",
                "Concurrent manual",
                '["src_manual"]',
                "draft",
                "t0",
                "t0",
            ),
        )
    current = WikiRevisionService(settings).get_page(page_path)

    barrier = threading.Barrier(2)
    synchronized_threads: set[int] = set()
    synchronized_threads_lock = threading.Lock()
    thread_state = threading.local()
    original_execute = PgCompatConnection.execute

    def synchronized_execute(self, query, params=None):
        normalized = " ".join(query.split())
        if (
            normalized == "SELECT * FROM wiki_pages WHERE path=? FOR UPDATE"
            and not getattr(thread_state, "manual_lock_synchronized", False)
        ):
            thread_state.manual_lock_synchronized = True
            with synchronized_threads_lock:
                synchronized_threads.add(threading.get_ident())
            barrier.wait(timeout=10)
        return original_execute(self, query, params)

    monkeypatch.setattr(PgCompatConnection, "execute", synchronized_execute)

    def save_once(request_id: str):
        try:
            return WikiRevisionService(settings).prepare_manual_save(
                ManualSaveCommand(
                    page_path=page_path,
                    content=current.content + f"\nWriter {request_id}.\n",
                    expected_revision_id=current.current_revision_id,
                    request_id=request_id,
                    actor="postgres-test",
                    owner=None,
                    note=None,
                    review_status="draft",
                ),
                execute_intent=False,
            )
        except RevisionConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save_once, ("writer-a", "writer-b")))

    mutations = [result for result in results if isinstance(result, MutationResult)]
    conflicts = [result for result in results if isinstance(result, RevisionConflict)]
    assert len(synchronized_threads) == 2
    assert len(mutations) == 1
    assert len(conflicts) == 1
    assert mutations[0].status == "prepared"
    assert mutations[0].write_intent_id is not None
    assert conflicts[0].current_revision_id == current.current_revision_id
    with connect_app(settings) as conn:
        page = conn.execute(
            "SELECT current_revision_id,pending_write_intent_id FROM wiki_pages"
        ).fetchone()
    assert page["current_revision_id"] == current.current_revision_id
    assert page["pending_write_intent_id"] == mutations[0].write_intent_id
