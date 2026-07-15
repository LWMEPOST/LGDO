import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.config import get_settings
from app.db import (
    PgCompatConnection,
    connect_app,
    connect_app_write,
    connect_postgres,
    init_postgres_schema,
    json_dump,
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
from app.vault_writer import IntentExecutor


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
            f"PostgreSQL maintenance connection failed ({type(exc).__name__})",
        )
    if role is None or not role[0]:
        return (
            False,
            "PostgreSQL maintenance role lacks SUPERUSER or CREATEDB",
        )
    return True, ""


def _create_test_database(admin, database_name: str, mark_owned) -> None:
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
        mark_owned()
        cur.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )


def _drop_test_database(admin, database_name: str) -> None:
    from psycopg import sql

    identifier = sql.Identifier(database_name)
    cleanup_stages = (
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
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = %s AND pid <> pg_backend_pid()
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
    for stage, statement, params in cleanup_stages:
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
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (database_name,),
            )
            database_exists = cur.fetchone() is not None
    except Exception as exc:
        errors.append(("verify", exc))

    failure_summary = ", ".join(
        f"{stage}={type(error).__name__}" for stage, error in errors
    )
    if database_exists:
        message = f"test database leaked: {database_name}"
        if failure_summary:
            message = f"{message}; cleanup failures: {failure_summary}"
        leak = AssertionError(message)
        if errors:
            raise leak from errors[0][1]
        raise leak
    if errors:
        failure = RuntimeError(
            f"PostgreSQL test database cleanup failed: {failure_summary}"
        )
        raise failure from errors[0][1]


def test_create_test_database_owns_name_before_create_connection_closes(
    monkeypatch,
):
    events = []
    owned = False

    class FakeCursor:
        created = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            if exc is None and self.created:
                raise RuntimeError("connection close failed after CREATE")
            return False

        def execute(self, query, params=None):
            if params is not None:
                events.append("checked")
                return
            events.append(("create", owned))
            self.created = True

        @staticmethod
        def fetchone():
            return None

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        @staticmethod
        def cursor():
            return FakeCursor()

    def mark_owned():
        nonlocal owned
        owned = True
        events.append("owned")

    monkeypatch.setitem(
        globals(),
        "connect_postgres",
        lambda *args, **kwargs: FakeConnection(),
    )

    with pytest.raises(RuntimeError, match="close failed after CREATE"):
        _create_test_database(object(), "lgdo_t16_owned", mark_owned)

    assert events == ["checked", "owned", ("create", True)]


def test_create_test_database_does_not_own_preexisting_name(monkeypatch):
    events = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, query, params=None):
            events.append("checked" if params is not None else "create")

        @staticmethod
        def fetchone():
            return (1,)

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        @staticmethod
        def cursor():
            return FakeCursor()

    monkeypatch.setitem(
        globals(),
        "connect_postgres",
        lambda *args, **kwargs: FakeConnection(),
    )

    with pytest.raises(AssertionError, match="refusing to reuse"):
        _create_test_database(
            object(),
            "lgdo_t16_existing",
            lambda: events.append("owned"),
        )

    assert events == ["checked"]


def test_drop_test_database_runs_every_stage_before_reporting_leak(monkeypatch):
    stages = []
    connections = []
    labels = ("alter", "terminate", "drop", "verify")

    class CleanupFailure(RuntimeError):
        pass

    class FakeCursor:
        def __init__(self, stage_index):
            self.stage_index = stage_index

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, query, params=None):
            label = labels[self.stage_index]
            stages.append(label)
            if label != "verify":
                raise CleanupFailure(label)

        @staticmethod
        def fetchone():
            return (1,)

    class FakeConnection:
        def __init__(self, stage_index):
            self.stage_index = stage_index

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def cursor(self):
            return FakeCursor(self.stage_index)

    def fake_connect(admin, database, *, autocommit):
        assert database == "postgres"
        assert autocommit is True
        connection = FakeConnection(len(connections))
        connections.append(connection)
        return connection

    monkeypatch.setitem(globals(), "connect_postgres", fake_connect)

    with pytest.raises(AssertionError, match="test database leaked") as exc_info:
        _drop_test_database(object(), "lgdo_t16_leaked")

    assert stages == list(labels)
    assert all(label in str(exc_info.value) for label in labels[:-1])
    assert isinstance(exc_info.value.__cause__, CleanupFailure)
    assert str(exc_info.value.__cause__) == "alter"


def test_postgres_capability_redacts_connection_failure(monkeypatch):
    def fail_connection(*args, **kwargs):
        raise RuntimeError("secret-user:secret-password@internal-host")

    monkeypatch.setitem(globals(), "connect_postgres", fail_connection)

    available, reason = postgres_capability()

    assert available is False
    assert reason == "PostgreSQL maintenance connection failed (RuntimeError)"


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
    owns_database = False

    def mark_owned() -> None:
        nonlocal owns_database
        owns_database = True

    try:
        _create_test_database(admin, database_name, mark_owned)
        init_postgres_schema(settings)
        yield settings
    finally:
        if owns_database:
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


def test_postgres_external_schema_has_payload_issue_and_partial_indexes(
    postgres_settings,
):
    with connect_postgres(postgres_settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name='vault_change_events'
            """
        )
        columns = {row[0] for row in cur.fetchall()}
        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename='vault_sync_issues'"
        )
        indexes = {row[0] for row in cur.fetchall()}
        cur.execute(
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_name='wiki_file_observations' AND column_name='page_id'
            """
        )
        observation_page_id_nullable = cur.fetchone()[0]
    assert {"payload_digest", "result_payload_json"} <= columns
    assert "idx_vault_sync_issue_open_identity" in indexes
    assert observation_page_id_nullable == "YES"


def test_concurrent_postgres_external_create_replays_one_event_without_duplicates(
    postgres_settings,
):
    settings = postgres_settings
    page_path = "wiki/product/concurrent-external.md"
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        b"---\ntitle: Concurrent external\nsource_ids: []\n"
        b"domain: product\npage_type: feature\nreview_status: draft\nowner:\n"
        b"---\n# Concurrent external\nBody\n"
    )
    observation = capture_file_observation(
        target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
    )

    def ingest(_worker: int):
        return WikiRevisionService(settings).ingest_external_change(
            "pg-create-shared-event", page_path, observation
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(ingest, (1, 2)))
    with connect_postgres(settings) as conn, conn.cursor() as cur:
        cur.execute("SELECT page_id FROM wiki_pages WHERE path=%s", (page_path,))
        pages = cur.fetchall()
        cur.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_path=%s",
            (page_path,),
        )
        revisions = cur.fetchone()[0]
        cur.execute(
            """
            SELECT COUNT(*) FROM vault_write_intents i
            JOIN wiki_page_revisions r ON r.id=i.revision_id WHERE r.page_path=%s
            """,
            (page_path,),
        )
        intents = cur.fetchone()[0]
    assert len(pages) == 1
    assert revisions == 1
    assert intents == 1
    assert [result.status for result in outcomes] == ["applied", "applied"]
    assert sorted(result.replayed for result in outcomes) == [False, True]
    assert len({result.page_id for result in outcomes}) == 1


@pytest.mark.parametrize("source_mutation", ["deactivate", "delete"])
def test_postgres_source_mutation_wins_against_finalize_without_advancing_state(
    monkeypatch, postgres_settings, source_mutation
):
    settings = postgres_settings
    suffix = source_mutation
    source_id = f"src_finalize_race_{suffix}"
    page_id = f"page_finalize_race_{suffix}"
    page_path = f"wiki/product/finalize-source-race-{suffix}.md"
    event_id = f"pg-finalize-source-race-{suffix}"
    timestamp = "2026-07-15T00:00:00+00:00"
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\ntitle: Source race\n"
        f"source_ids: [{source_id}]\nlgdo_page_id: {page_id}\n"
        "domain: product\npage_type: policy\nreview_status: draft\nowner:\n"
        "---\n# Source race\nBefore finalize.\n",
        encoding="utf-8",
    )
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO sources(
              id,domain,title,source_type,original_path,raw_path,content_hash,
              size_bytes,status,metadata_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source_id,
                "product",
                "Source race",
                "markdown",
                "obsidian",
                f"raw/product/{suffix}.md",
                "a" * 64,
                1,
                "active",
                "{}",
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,page_id,domain,page_type,title,source_ids_json,review_status,
              owner,created_at,updated_at,lifecycle_status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,'active')
            """,
            (
                page_path,
                page_id,
                "product",
                "policy",
                "Source race",
                json_dump([source_id]),
                "draft",
                None,
                timestamp,
                timestamp,
            ),
        )

    service = WikiRevisionService(settings)
    before = service.get_page(page_path)
    target.write_bytes(before.raw_bytes + b"\nPrepared external repair.\n")
    observation = capture_file_observation(
        target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
    )
    installed: dict[str, str] = {}

    def install_without_finalize(executor, intent_id):
        installed["intent_id"] = intent_id
        installed["owner"] = executor.owner
        assert executor.claim(intent_id, lease_seconds=30)
        executor.capture_and_install(intent_id, stop_after="installed")
        raise RuntimeError("stop after install before finalize")

    monkeypatch.setattr(IntentExecutor, "execute", install_without_finalize)
    with pytest.raises(RuntimeError, match="stop after install before finalize"):
        service.ingest_external_change(event_id, page_path, observation)

    with connect_app(settings) as conn:
        page_before_finalize = dict(
            conn.execute(
                """
                SELECT current_revision_id,projection_epoch,rag_visible_revision_id,
                       rag_visible_epoch,pending_write_intent_id
                FROM wiki_pages WHERE page_id=?
                """,
                (page_id,),
            ).fetchone()
        )
        event_before_finalize = dict(
            conn.execute(
                """
                SELECT status,result_revision_id,result_payload_json
                FROM vault_change_events WHERE id=?
                """,
                (event_id,),
            ).fetchone()
        )
        jobs_before_finalize = conn.execute(
            "SELECT COUNT(*) FROM knowledge_projection_jobs WHERE page_id=?",
            (page_id,),
        ).fetchone()[0]
    assert page_before_finalize["current_revision_id"] == before.current_revision_id
    assert page_before_finalize["pending_write_intent_id"] == installed["intent_id"]
    assert event_before_finalize == {
        "status": "prepared",
        "result_revision_id": None,
        "result_payload_json": None,
    }

    source_lock_held = threading.Event()
    release_source_mutation = threading.Event()
    share_lock_attempted = threading.Event()
    original_execute = PgCompatConnection.execute

    def observe_share_lock(self, query, params=None):
        normalized = " ".join(query.split())
        if (
            normalized.startswith("SELECT id,status FROM sources WHERE id IN")
            and normalized.endswith("FOR SHARE")
        ):
            share_lock_attempted.set()
        return original_execute(self, query, params)

    monkeypatch.setattr(PgCompatConnection, "execute", observe_share_lock)

    def mutate_source_first():
        with connect_app_write(settings) as conn:
            if source_mutation == "deactivate":
                changed = conn.execute(
                    "UPDATE sources SET status='inactive' WHERE id=?", (source_id,)
                )
            else:
                changed = conn.execute(
                    "DELETE FROM sources WHERE id=?", (source_id,)
                )
            assert changed.rowcount == 1
            source_lock_held.set()
            if not release_source_mutation.wait(timeout=10):
                raise AssertionError("source mutation was not released")

    with ThreadPoolExecutor(max_workers=2) as pool:
        mutation_future = pool.submit(mutate_source_first)
        assert source_lock_held.wait(timeout=10)
        finalize_future = pool.submit(
            service.finalize_intent,
            installed["intent_id"],
            installed["owner"],
        )
        try:
            assert share_lock_attempted.wait(timeout=10)
            assert not finalize_future.done()
        finally:
            release_source_mutation.set()
        mutation_future.result(timeout=20)
        with pytest.raises(RevisionConflict, match="sources are no longer active"):
            finalize_future.result(timeout=20)

    with connect_app(settings) as conn:
        page_after = dict(
            conn.execute(
                """
                SELECT current_revision_id,projection_epoch,rag_visible_revision_id,
                       rag_visible_epoch,pending_write_intent_id
                FROM wiki_pages WHERE page_id=?
                """,
                (page_id,),
            ).fetchone()
        )
        event_after = dict(
            conn.execute(
                """
                SELECT status,result_revision_id,result_payload_json
                FROM vault_change_events WHERE id=?
                """,
                (event_id,),
            ).fetchone()
        )
        jobs_after = conn.execute(
            "SELECT COUNT(*) FROM knowledge_projection_jobs WHERE page_id=?",
            (page_id,),
        ).fetchone()[0]
    assert page_after == page_before_finalize
    assert event_after == event_before_finalize
    assert jobs_after == jobs_before_finalize


def test_postgres_relocate_pending_intent_fences_manual_save(
    monkeypatch, postgres_settings
):
    settings = postgres_settings
    old_path = "wiki/product/relocate-race.md"
    new_path = "wiki/product/relocate-race-new.md"
    old_target = settings.vault_path / old_path
    new_target = settings.vault_path / new_path
    old_target.parent.mkdir(parents=True, exist_ok=True)
    old_target.write_text(
        "---\ntitle: Relocate race\nsource_ids: []\ndomain: product\n"
        "page_type: feature\nreview_status: draft\nowner:\n"
        "---\n# Relocate race\nBefore.\n",
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
                old_path,
                "product",
                "feature",
                "Relocate race",
                "[]",
                "draft",
                "t0",
                "t0",
            ),
        )
    before = WikiRevisionService(settings).get_page(old_path)
    new_target.parent.mkdir(parents=True, exist_ok=True)
    old_target.rename(new_target)
    new_target.write_bytes(before.raw_bytes + b"\nEdited during relocate.\n")
    observation = capture_file_observation(
        new_target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
    )
    prepared = threading.Event()
    release = threading.Event()
    relocate_service = WikiRevisionService(settings)

    def block_after_prepare(point: str):
        if point != "relocate_after_prepare_commit_before_execute":
            return
        prepared.set()
        if not release.wait(timeout=10):
            raise AssertionError("manual-save race did not release relocate")

    monkeypatch.setattr(relocate_service, "_fault", block_after_prepare)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            relocate_service.relocate_external_change,
            "pg-relocate-race",
            old_path,
            new_path,
            before.page_id,
            observation,
        )
        try:
            assert prepared.wait(timeout=10)
            with connect_app(settings) as conn:
                pending_intent_id = conn.execute(
                    """
                    SELECT pending_write_intent_id FROM wiki_pages WHERE page_id=?
                    """,
                    (before.page_id,),
                ).fetchone()["pending_write_intent_id"]
            with pytest.raises(RevisionConflict) as conflict:
                WikiRevisionService(settings).prepare_manual_save(
                    ManualSaveCommand(
                        page_path=old_path,
                        content=before.content + "\nManual loser.\n",
                        expected_revision_id=before.current_revision_id,
                        request_id="pg-manual-loser",
                        actor="postgres-test",
                        owner=None,
                        note=None,
                        review_status="draft",
                    ),
                    execute_intent=False,
                )
            assert conflict.value.pending_intent_id == pending_intent_id
        finally:
            release.set()
        relocated = future.result(timeout=20)

    assert relocated.status == "applied"
    with connect_app(settings) as conn:
        row = conn.execute(
            """
            SELECT p.path,p.current_revision_id,p.pending_write_intent_id,
                   r.origin,r.page_path
            FROM wiki_pages p
            JOIN wiki_page_revisions r ON r.id=p.current_revision_id
            WHERE p.page_id=?
            """,
            (before.page_id,),
        ).fetchone()
        external_count = conn.execute(
            """
            SELECT COUNT(*) FROM wiki_page_revisions
            WHERE page_id=? AND origin='external'
            """,
            (before.page_id,),
        ).fetchone()[0]
    assert tuple(
        row[key]
        for key in (
            "path",
            "current_revision_id",
            "pending_write_intent_id",
            "origin",
            "page_path",
        )
    ) == (
        new_path,
        relocated.revision_id,
        None,
        "external",
        new_path,
    )
    assert external_count == 1
