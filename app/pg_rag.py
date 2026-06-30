from __future__ import annotations

from typing import Any

from app.auth import UserContext, filter_rows_by_acl
from app.config import Settings
from app.db import connect, rows_to_dicts
from app.rag import diversify_ranked_rows, embed_text_with_model, rank_search_rows, tokenize


PGVECTOR_DIMENSION = 96

PG_RAG_SCHEMA = """
CREATE TABLE IF NOT EXISTS rag_document_chunks (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  domain TEXT NOT NULL,
  title TEXT NOT NULL,
  chunk_index INTEGER NOT NULL,
  text TEXT NOT NULL,
  tokens JSONB NOT NULL DEFAULT '[]'::jsonb,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  embedding JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_rag_document_chunks_source_index
ON rag_document_chunks(source_id, chunk_index);

CREATE INDEX IF NOT EXISTS idx_rag_document_chunks_domain
ON rag_document_chunks(domain);

CREATE INDEX IF NOT EXISTS idx_rag_document_chunks_source
ON rag_document_chunks(source_id);
"""

PGVECTOR_SCHEMA = f"""
ALTER TABLE rag_document_chunks
ADD COLUMN IF NOT EXISTS embedding_vector vector({PGVECTOR_DIMENSION});

CREATE INDEX IF NOT EXISTS idx_rag_document_chunks_embedding_vector
ON rag_document_chunks
USING ivfflat (embedding_vector vector_cosine_ops)
WITH (lists = 32);
"""


def connect_postgres(settings: Settings, database: str | None = None, *, autocommit: bool = False):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("缺少 psycopg 依赖，请先运行 pip install -e .") from exc

    conn = psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password or "",
        dbname=database or settings.postgres_database,
    )
    conn.autocommit = autocommit
    return conn


def ensure_pg_database(settings: Settings) -> None:
    try:
        with connect_postgres(settings):
            return
    except Exception:
        pass

    with connect_postgres(settings, database="postgres", autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (settings.postgres_database,))
            if cur.fetchone() is None:
                from psycopg import sql

                cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(settings.postgres_database)))


def init_pg_rag(settings: Settings) -> None:
    ensure_pg_database(settings)
    with connect_postgres(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(PG_RAG_SCHEMA)
        conn.commit()

    if _try_enable_pgvector(settings):
        with connect_postgres(settings) as conn:
            with conn.cursor() as cur:
                cur.execute(PGVECTOR_SCHEMA)


def sync_sqlite_chunks_to_pg(settings: Settings) -> int:
    with connect(settings.database_path) as sqlite_conn:
        chunks = rows_to_dicts(sqlite_conn.execute("SELECT * FROM document_chunks").fetchall())

    if not chunks:
        return 0

    init_pg_rag(settings)
    with connect_postgres(settings) as pg_conn:
        with pg_conn.cursor() as cur:
            has_vector_column = _column_exists(cur, "rag_document_chunks", "embedding_vector")
            for chunk in chunks:
                metadata: dict[str, Any] = chunk.get("metadata") or {}
                tokens = chunk.get("token") or tokenize(f"{chunk['title']}\n{chunk['text']}")
                embedding, configured_model = embed_text_with_model(f"{chunk['title']}\n{chunk['text']}", settings=settings)
                embedding = metadata.get("embedding") or embedding
                metadata = {**metadata, "embedding_model": metadata.get("embedding_model") or configured_model}
                _upsert_pg_chunk(
                    cur,
                    {
                        "id": chunk["id"],
                        "source_id": chunk["source_id"],
                        "domain": chunk["domain"],
                        "title": chunk["title"],
                        "chunk_index": chunk["chunk_index"],
                        "text": chunk["text"],
                        "tokens": tokens,
                        "metadata": metadata,
                        "embedding": embedding,
                    },
                    has_vector_column=has_vector_column,
                )
    return len(chunks)


def upsert_pg_document_chunks(settings: Settings, chunks: list[dict[str, Any]]) -> None:
    if not chunks:
        return

    init_pg_rag(settings)
    with connect_postgres(settings) as pg_conn:
        with pg_conn.cursor() as cur:
            has_vector_column = _column_exists(cur, "rag_document_chunks", "embedding_vector")
            for chunk in chunks:
                metadata: dict[str, Any] = chunk.get("metadata") or {}
                title = metadata.get("title") or ""
                text = chunk.get("text") or ""
                tokens = tokenize(f"{title}\n{text}")
                embedding, embedding_model = embed_text_with_model(f"{title}\n{text}", settings=settings)
                metadata = {**metadata, "embedding_model": embedding_model}
                _upsert_pg_chunk(
                    cur,
                    {
                        "id": chunk["id"],
                        "source_id": chunk["source_id"],
                        "domain": metadata.get("domain") or "",
                        "title": title,
                        "chunk_index": chunk["chunk_index"],
                        "text": text,
                        "tokens": tokens,
                        "metadata": metadata,
                        "embedding": embedding,
                    },
                    has_vector_column=has_vector_column,
                )


def delete_pg_document_chunks(settings: Settings, source_id: str) -> int:
    init_pg_rag(settings)
    with connect_postgres(settings) as pg_conn:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM rag_document_chunks WHERE source_id = %s", (source_id,))
            return cur.rowcount if cur.rowcount is not None else 0


def search_pg_chunks(
    settings: Settings,
    question: str,
    domain: str | None = None,
    limit: int = 5,
    *,
    keyword_weight: float = 1.0,
    vector_weight: float = 12.0,
    user_context: UserContext | None = None,
    alias_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    init_pg_rag(settings)
    query_vector, _ = embed_text_with_model(question, settings=settings)
    with connect_postgres(settings) as conn:
        with conn.cursor() as cur:
            used_pgvector = False
            if query_vector and _can_use_pgvector_search(cur):
                candidate_limit = _pg_candidate_limit(limit)
                try:
                    if domain:
                        cur.execute(
                            """
                            SELECT id, source_id, domain, title, chunk_index, text, tokens, metadata, embedding,
                                   embedding_vector <=> %s::vector AS pgvector_distance
                            FROM rag_document_chunks
                            WHERE domain = %s AND embedding_vector IS NOT NULL
                            ORDER BY embedding_vector <=> %s::vector
                            LIMIT %s
                            """,
                            (_vector(query_vector), domain, _vector(query_vector), candidate_limit),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT id, source_id, domain, title, chunk_index, text, tokens, metadata, embedding,
                                   embedding_vector <=> %s::vector AS pgvector_distance
                            FROM rag_document_chunks
                            WHERE embedding_vector IS NOT NULL
                            ORDER BY embedding_vector <=> %s::vector
                            LIMIT %s
                            """,
                            (_vector(query_vector), _vector(query_vector), candidate_limit),
                        )
                    used_pgvector = True
                except Exception:
                    used_pgvector = False

            if not used_pgvector and domain:
                cur.execute(
                    """
                    SELECT id, source_id, domain, title, chunk_index, text, tokens, metadata, embedding
                    FROM rag_document_chunks
                    WHERE domain = %s
                    """,
                    (domain,),
                )
            elif not used_pgvector:
                cur.execute(
                    """
                    SELECT id, source_id, domain, title, chunk_index, text, tokens, metadata, embedding
                    FROM rag_document_chunks
                    """
                )
            columns = [desc.name for desc in cur.description]
            rows = [dict(zip(columns, row)) for row in cur.fetchall()]

    rows = filter_rows_by_acl(rows, user_context)
    ranked = rank_search_rows(
        rows,
        question,
        keyword_weight=keyword_weight,
        vector_weight=vector_weight,
        alias_context=alias_context,
        settings=settings,
    )
    return diversify_ranked_rows(ranked, limit)


def pg_rag_status(settings: Settings) -> dict[str, Any]:
    init_pg_rag(settings)
    with connect_postgres(settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), COUNT(DISTINCT source_id) FROM rag_document_chunks")
            chunk_count, source_count = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM rag_document_chunks WHERE embedding IS NOT NULL")
            embedding_count = cur.fetchone()[0]
            pgvector_enabled = _pgvector_available(cur)
            if _column_exists(cur, "rag_document_chunks", "embedding_vector"):
                cur.execute("SELECT COUNT(*) FROM rag_document_chunks WHERE embedding_vector IS NOT NULL")
                vector_count = cur.fetchone()[0]
            else:
                vector_count = 0
            cur.execute(
                """
                SELECT metadata->>'embedding_model' AS embedding_model, COUNT(*) AS count
                FROM rag_document_chunks
                WHERE metadata ? 'embedding_model'
                GROUP BY metadata->>'embedding_model'
                ORDER BY count DESC, embedding_model
                LIMIT 1
                """
            )
            model_row = cur.fetchone()
            embedding_model = model_row[0] if model_row else settings.rag_embedding_model
            cur.execute(
                """
                SELECT domain, COUNT(*) AS chunk_count
                FROM rag_document_chunks
                GROUP BY domain
                ORDER BY domain
                """
            )
            domains = [{"domain": row[0], "chunk_count": row[1]} for row in cur.fetchall()]
    return {
        "chunk_count": chunk_count,
        "source_count": source_count,
        "embedding_count": embedding_count,
        "vector_count": vector_count,
        "embedding_model": embedding_model,
        "vector_backend": "pgvector" if pgvector_enabled else "jsonb",
        "pgvector_enabled": pgvector_enabled,
        "domains": domains,
    }


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _upsert_pg_chunk(cur: Any, row: dict[str, Any], *, has_vector_column: bool) -> None:
    if has_vector_column:
        embedding_vector = _vector(row["embedding"]) if len(row["embedding"]) == PGVECTOR_DIMENSION else None
        cur.execute(
            """
            INSERT INTO rag_document_chunks(
              id, source_id, domain, title, chunk_index, text, tokens, metadata,
              embedding, embedding_vector, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, now())
            ON CONFLICT(id) DO UPDATE SET
              source_id = EXCLUDED.source_id,
              domain = EXCLUDED.domain,
              title = EXCLUDED.title,
              chunk_index = EXCLUDED.chunk_index,
              text = EXCLUDED.text,
              tokens = EXCLUDED.tokens,
              metadata = EXCLUDED.metadata,
              embedding = EXCLUDED.embedding,
              embedding_vector = EXCLUDED.embedding_vector,
              updated_at = now()
            """,
            (
                row["id"],
                row["source_id"],
                row["domain"],
                row["title"],
                row["chunk_index"],
                row["text"],
                _json(row["tokens"]),
                _json(row["metadata"]),
                _json(row["embedding"]),
                embedding_vector,
            ),
        )
        return

    cur.execute(
        """
        INSERT INTO rag_document_chunks(
          id, source_id, domain, title, chunk_index, text, tokens, metadata, embedding, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, now())
        ON CONFLICT(id) DO UPDATE SET
          source_id = EXCLUDED.source_id,
          domain = EXCLUDED.domain,
          title = EXCLUDED.title,
          chunk_index = EXCLUDED.chunk_index,
          text = EXCLUDED.text,
          tokens = EXCLUDED.tokens,
          metadata = EXCLUDED.metadata,
          embedding = EXCLUDED.embedding,
          updated_at = now()
        """,
        (
            row["id"],
            row["source_id"],
            row["domain"],
            row["title"],
            row["chunk_index"],
            row["text"],
            _json(row["tokens"]),
            _json(row["metadata"]),
            _json(row["embedding"]),
        ),
    )


def _vector(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.6f}" for value in values) + "]"


def _pg_candidate_limit(limit: int) -> int:
    return max(limit * 8, 40)


def _can_use_pgvector_search(cur: Any) -> bool:
    return _pgvector_available(cur) and _column_exists(cur, "rag_document_chunks", "embedding_vector")


def _try_enable_pgvector(settings: Settings) -> bool:
    try:
        with connect_postgres(settings, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        return True
    except Exception:
        return False


def _pgvector_available(cur: Any) -> bool:
    cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
    return cur.fetchone() is not None


def _column_exists(cur: Any, table_name: str, column_name: str) -> bool:
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = %s AND column_name = %s
        """,
        (table_name, column_name),
    )
    return cur.fetchone() is not None
