import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import connect_postgres
from app.main import app


TEST_DATABASE_PREFIX = "lgdo_meta_"


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
                f"refusing to reuse metadata test database {database_name}"
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
            f"metadata test database leaked: {database_name}; {summary}"
        ) from (errors[0][1] if errors else None)
    if errors:
        raise RuntimeError(
            f"metadata test database cleanup failed: {summary}"
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


def test_create_owned_database_marks_ownership_before_create_connection_closes(
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
        _create_owned_database(object(), "lgdo_meta_owned", mark_owned)

    assert events == ["checked", "owned", ("create", True)]


def test_create_owned_database_refuses_preexisting_name(monkeypatch):
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
        _create_owned_database(
            object(),
            "lgdo_meta_existing",
            lambda: events.append("owned"),
        )

    assert events == ["checked"]


def test_drop_owned_database_runs_all_stages_before_reporting_leak(monkeypatch):
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

    with pytest.raises(AssertionError, match="database leaked") as exc_info:
        _drop_owned_database(object(), "lgdo_meta_leaked")

    assert stages == list(labels)
    assert all(label in str(exc_info.value) for label in labels[:-1])
    assert isinstance(exc_info.value.__cause__, CleanupFailure)
    assert str(exc_info.value.__cause__) == "alter"


def test_postgres_metadata_backend_runs_internal_flow(
    tmp_path, monkeypatch, postgres_database
):
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "refund_policy.md").write_text(
        "# 退款政策\n\n用户在 7 天内可以申请退款，客服需要核验订单号。",
        encoding="utf-8",
    )
    (sample_dir / "permission_export.md").write_text(
        "# 权限导出\n\n管理员可以在权限中心导出成员权限清单，导出前需要二次确认。",
        encoding="utf-8",
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "postgres")
    monkeypatch.setattr(settings, "postgres_database", postgres_database)
    monkeypatch.setattr(settings, "rag_store_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "fallback.db")
    monkeypatch.setattr(settings, "vault_path", tmp_path / "vault")
    monkeypatch.setattr(settings, "upload_path", tmp_path / "uploads")
    monkeypatch.setattr(settings, "deepseek_api_key", None)
    monkeypatch.setattr(settings, "deepseek_model", None)

    from app.db import init_app_db

    init_app_db(settings)

    client = TestClient(app)
    scan = client.post(
        "/api/internal/sources/scan",
        json={
            "root_path": str(sample_dir),
            "domain": "product",
            "owner": "tester",
            "acl_tags": ["internal", "product"],
            "metadata_defaults": {"source_system": "pg_metadata_test"},
        },
    )
    assert scan.status_code == 200
    assert scan.json()["new_files"] == 2

    status = client.get("/api/internal/rag/status")
    assert status.status_code == 200
    assert status.json()["database_backend"] == "postgres"
    assert status.json()["chunk_count"] >= 2
    assert status.json()["source_count"] == 2

    compile_result = client.post("/api/internal/wiki/compile", json={"domain": "product"})
    assert compile_result.status_code == 200
    assert compile_result.json()["created_pages"] >= 2

    sources = client.get("/api/internal/sources?domain=product")
    assert sources.status_code == 200
    permission_source = next(source for source in sources.json() if source["title"] == "permission_export")

    answer = client.post(
        "/api/internal/ask",
        json={"question": "管理员怎么导出成员权限清单？", "domain": "product"},
    )
    assert answer.status_code == 200
    body = answer.json()
    assert body["citations"]
    assert body["citations"][0]["source_id"] == permission_source["id"]

    delete_result = client.delete(f"/api/internal/sources/{permission_source['id']}?note=pg-metadata-test")
    assert delete_result.status_code == 200
    assert delete_result.json()["status"] == "deleted"

    with connect_postgres(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM document_chunks WHERE source_id = %s", (permission_source["id"],))
            assert cur.fetchone()[0] == 0
