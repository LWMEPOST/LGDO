import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.pg_rag import connect_postgres


TEST_DATABASE_PREFIX = "lgdo_rag_"


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
                f"refusing to reuse RAG test database {database_name}"
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
            f"RAG test database leaked: {database_name}; {summary}"
        ) from (errors[0][1] if errors else None)
    if errors:
        raise RuntimeError(
            f"RAG test database cleanup failed: {summary}"
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
        _create_owned_database(object(), "lgdo_rag_owned", mark_owned)

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
            "lgdo_rag_existing",
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
        _drop_owned_database(object(), "lgdo_rag_leaked")

    assert stages == list(labels)
    assert all(label in str(exc_info.value) for label in labels[:-1])
    assert isinstance(exc_info.value.__cause__, CleanupFailure)
    assert str(exc_info.value.__cause__) == "alter"


def test_postgres_rag_store_indexes_and_retrieves_chunks(
    tmp_path, monkeypatch, postgres_database
):
    sample_dir = tmp_path / "samples"
    sample_dir.mkdir()
    (sample_dir / "service_sla.md").write_text(
        "# 服务 SLA\n\n企业客户 P1 故障需要在 30 分钟内响应，并同步值班负责人。",
        encoding="utf-8",
    )
    (sample_dir / "refund_policy.md").write_text(
        "# 退款政策\n\n用户 7 天内可以申请退款，客服需要记录订单号。",
        encoding="utf-8",
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "database_path", tmp_path / "data" / "test.db")
    monkeypatch.setattr(settings, "vault_path", tmp_path / "vault")
    monkeypatch.setattr(settings, "upload_path", tmp_path / "uploads")
    monkeypatch.setattr(settings, "rag_store_backend", "postgres")
    monkeypatch.setattr(settings, "postgres_database", postgres_database)
    monkeypatch.setattr(settings, "deepseek_api_key", None)
    monkeypatch.setattr(settings, "deepseek_model", None)

    from app.pg_rag import init_pg_rag

    init_pg_rag(settings)

    client = TestClient(app)
    scan = client.post(
        "/api/internal/sources/scan",
        json={
            "root_path": str(sample_dir),
            "domain": "product",
            "owner": "tester",
            "acl_tags": ["internal", "product"],
            "metadata_defaults": {"source_system": "pg_rag_test"},
        },
    )
    assert scan.status_code == 200

    with connect_postgres(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM rag_document_chunks WHERE domain = %s", ("product",))
            assert cur.fetchone()[0] >= 2
            cur.execute("SELECT COUNT(*) FROM rag_document_chunks WHERE domain = %s AND embedding IS NOT NULL", ("product",))
            assert cur.fetchone()[0] >= 2

    status = client.get("/api/internal/rag/status")
    assert status.status_code == 200
    assert status.json()["rag_store_backend"] == "postgres"
    assert status.json()["chunk_count"] >= 2
    assert status.json()["embedding_count"] >= 2
    assert status.json()["embedding_model"] == "local-hash-v1"
    assert status.json()["vector_backend"] in {"pgvector", "jsonb"}
    assert isinstance(status.json()["pgvector_enabled"], bool)
    if status.json()["pgvector_enabled"]:
        assert status.json()["vector_count"] >= 2

    compile_result = client.post("/api/internal/wiki/compile", json={"domain": "product"})
    assert compile_result.status_code == 200

    answer = client.post(
        "/api/internal/ask",
        json={"question": "P1 故障多久内响应？", "domain": "product"},
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["citations"]
    service_source = next(source for source in client.get("/api/internal/sources?domain=product").json() if source["title"] == "service_sla")
    assert body["citations"][0]["source_id"] == service_source["id"]
    assert "30 分钟" in body["citations"][0]["snippet"]

    delete_result = client.delete(f"/api/internal/sources/{service_source['id']}?note=pg-test")
    assert delete_result.status_code == 200

    with connect_postgres(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM rag_document_chunks WHERE source_id = %s", (service_source["id"],))
            assert cur.fetchone()[0] == 0

    answer_after_delete = client.post(
        "/api/internal/ask",
        json={"question": "P1 故障多久内响应？", "domain": "product"},
    )
    assert answer_after_delete.status_code == 200
    assert all(
        citation["source_id"] != service_source["id"]
        for citation in answer_after_delete.json()["citations"]
    )


def test_search_pg_chunks_uses_pgvector_preselection_when_available(monkeypatch):
    import app.pg_rag as pg_rag

    executed: list[tuple[str, tuple | None]] = []

    class Cursor:
        description = [
            type("Desc", (), {"name": name})
            for name in [
                "id",
                "source_id",
                "domain",
                "title",
                "chunk_index",
                "text",
                "tokens",
                "metadata",
                "embedding",
                "pgvector_distance",
            ]
        ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            executed.append((query, params))

        def fetchall(self):
            return [
                (
                    "row1",
                    "source1",
                    "customer_service",
                    "退款制度",
                    0,
                    "退款期限是 7 天。",
                    "退款 期限",
                    "{}",
                    "[0.1, 0.2]",
                    0.12,
                )
            ]

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(pg_rag, "init_pg_rag", lambda settings: None)
    monkeypatch.setattr(pg_rag, "connect_postgres", lambda settings: Conn())
    monkeypatch.setattr(pg_rag, "_pgvector_available", lambda cur: True)
    monkeypatch.setattr(pg_rag, "_column_exists", lambda cur, table, column: column == "embedding_vector")
    monkeypatch.setattr(pg_rag, "embed_text_with_model", lambda question, settings=None: ([0.1, 0.2], "test-embedding"))
    monkeypatch.setattr(pg_rag, "rank_search_rows", lambda rows, question, **kwargs: rows)
    monkeypatch.setattr(pg_rag, "diversify_ranked_rows", lambda rows, limit: rows[:limit])

    rows = pg_rag.search_pg_chunks(object(), "退款期限", "customer_service", limit=5)

    assert rows[0]["pgvector_distance"] == 0.12
    vector_queries = [query for query, _params in executed if "<=>" in query]
    assert vector_queries
    assert "LIMIT %s" in vector_queries[0]
    assert executed[-1][1][-1] >= 40


def test_search_pg_chunks_falls_back_without_pgvector(monkeypatch):
    import app.pg_rag as pg_rag

    executed: list[str] = []

    class Cursor:
        description = [
            type("Desc", (), {"name": name})
            for name in ["id", "source_id", "domain", "title", "chunk_index", "text", "tokens", "metadata", "embedding"]
        ]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params=None):
            executed.append(query)

        def fetchall(self):
            return []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(pg_rag, "init_pg_rag", lambda settings: None)
    monkeypatch.setattr(pg_rag, "connect_postgres", lambda settings: Conn())
    monkeypatch.setattr(pg_rag, "_pgvector_available", lambda cur: False)
    monkeypatch.setattr(pg_rag, "_column_exists", lambda cur, table, column: False)
    monkeypatch.setattr(pg_rag, "embed_text_with_model", lambda question, settings=None: ([0.1, 0.2], "test-embedding"))
    monkeypatch.setattr(pg_rag, "rank_search_rows", lambda rows, question, **kwargs: rows)

    pg_rag.search_pg_chunks(object(), "退款期限", "customer_service", limit=5)

    assert executed
    assert all("<=>" not in query for query in executed)


def test_search_chunks_merges_postgres_and_visible_wiki_candidates(tmp_path, monkeypatch):
    import app.pg_rag as pg_rag
    import app.rag as rag
    from app.config import Settings
    from app.db import connect_app, init_app_db

    settings = Settings(
        database_backend="sqlite",
        database_path=tmp_path / "metadata.db",
        rag_store_backend="postgres",
        _env_file=None,
    )
    init_app_db(settings)
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,page_id,domain,page_type,title,source_ids_json,review_status,
              created_at,updated_at,current_revision_id,rag_visible_revision_id,
              projection_epoch,rag_visible_epoch,lifecycle_status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "wiki/product/refund.md",
                "page_refund",
                "product",
                "faq",
                "Refund FAQ",
                '["source_document"]',
                "approved",
                "t0",
                "t0",
                "wrev_refund",
                "wrev_refund",
                3,
                3,
                "active",
            ),
        )
        conn.execute(
            """
            INSERT INTO wiki_chunks(
              id,page_id,revision_id,projection_epoch,chunk_index,page_path,
              domain,title,text,token_json,embedding_json,embedding_model,
              source_ids_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "wiki_chunk_refund",
                "page_refund",
                "wrev_refund",
                3,
                0,
                "wiki/product/refund.md",
                "product",
                "Refund FAQ",
                "Wiki refund terms.",
                '["wiki", "refund"]',
                "[]",
                "local-hash-v1",
                '["source_document"]',
                "t0",
                "t0",
            ),
        )

    pg_limits: list[int] = []
    document_row = {
        "id": "document_chunk_refund",
        "source_id": "source_document",
        "domain": "product",
        "title": "Refund Policy",
        "chunk_index": 0,
        "text": "Document refund terms.",
        "tokens": ["document", "refund"],
        "metadata": {},
        "embedding": [],
    }

    def fake_pg_search(_settings, _question, _domain, limit, **_kwargs):
        pg_limits.append(limit)
        return [document_row]

    ranked_inputs: list[dict] = []

    def capture_ranker(rows, _question, **_kwargs):
        ranked_inputs.extend(rows)
        return rows

    monkeypatch.setattr(pg_rag, "search_pg_chunks", fake_pg_search)
    monkeypatch.setattr(rag, "rank_search_rows", capture_ranker)
    monkeypatch.setattr(rag, "diversify_ranked_rows", lambda rows, limit: rows[:limit])

    result = rag.search_chunks(settings, "refund", domain="product", limit=5)

    assert pg_limits == [40]
    assert [row["origin"] for row in ranked_inputs] == ["wiki", "document"]
    assert [row["id"] for row in ranked_inputs] == [
        "wiki_chunk_refund",
        "document_chunk_refund",
    ]
    assert result == ranked_inputs
