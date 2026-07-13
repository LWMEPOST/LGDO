import socket
import uuid

import pytest

from app.config import get_settings
from app.db import connect_postgres, init_postgres_schema


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
