from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.db import (
    connect_app,
    connect_app_write,
    init_app_db,
    json_dump,
    row_to_dict,
    rows_to_dicts,
)
from app.normalization import chunk_markdown
from app.projection_jobs import ProjectionJob, ProjectionOutbox
from app.rag import embed_text_with_model, tokenize
from app.timeutil import now_iso


class ProjectionLeaseLost(RuntimeError):
    def __init__(self, job_id: str):
        self.job_id = job_id
        super().__init__(f"projection lease lost: {job_id}")


class ProjectionSuperseded(RuntimeError):
    def __init__(self, job_id: str):
        self.job_id = job_id
        super().__init__(f"projection job superseded: {job_id}")


@dataclass(frozen=True)
class RagProjectionResult:
    job_id: str
    page_id: str
    revision_id: str
    projection_epoch: int
    chunks: int


@dataclass(frozen=True)
class _WikiRevisionSnapshot:
    page_id: str
    revision_id: str
    projection_epoch: int
    page_path: str
    domain: str
    title: str
    content: str
    source_ids: tuple[str, ...]


class WikiRagProjector:
    def __init__(self, settings: Settings, outbox: ProjectionOutbox):
        self.settings = settings
        self.outbox = outbox

    def project(self, job: ProjectionJob, worker_id: str) -> RagProjectionResult:
        try:
            rows = self.write_physical_rows(job, worker_id)
        except ProjectionSuperseded:
            self._finish_superseded(job, worker_id)
            raise

        superseded = False
        with connect_app_write(self.settings) as conn:
            page = row_to_dict(
                conn.execute(
                    """
                    SELECT current_revision_id, projection_epoch, lifecycle_status
                    FROM wiki_pages WHERE page_id=?
                    """,
                    (job.page_id,),
                ).fetchone()
            )
            if not self._page_matches_job(page, job):
                if not self.outbox.finish_claimed(
                    conn,
                    job_id=job.id,
                    worker_id=worker_id,
                    status="superseded",
                    last_error="desired revision or epoch changed",
                ):
                    raise ProjectionLeaseLost(job.id)
                superseded = True
            else:
                updated = conn.execute(
                    """
                    UPDATE wiki_pages
                    SET rag_visible_revision_id=?, rag_visible_epoch=?
                    WHERE page_id=? AND current_revision_id=?
                      AND projection_epoch=? AND lifecycle_status='active'
                    """,
                    (
                        job.revision_id,
                        job.projection_epoch,
                        job.page_id,
                        job.revision_id,
                        job.projection_epoch,
                    ),
                )
                if updated.rowcount != 1 or not self.outbox.finish_claimed(
                    conn,
                    job_id=job.id,
                    worker_id=worker_id,
                    status="succeeded",
                ):
                    raise ProjectionLeaseLost(job.id)

        if superseded:
            raise ProjectionSuperseded(job.id)
        return RagProjectionResult(
            job_id=job.id,
            page_id=str(job.page_id),
            revision_id=str(job.revision_id),
            projection_epoch=job.projection_epoch,
            chunks=len(rows),
        )

    def write_physical_rows(
        self,
        job: ProjectionJob,
        worker_id: str,
    ) -> list[dict[str, Any]]:
        _ = worker_id
        init_app_db(self.settings)
        snapshot = self._load_snapshot(job)
        chunks = chunk_markdown(
            source_id=snapshot.page_id,
            markdown=snapshot.content,
            metadata={
                "page_id": snapshot.page_id,
                "revision_id": snapshot.revision_id,
                "domain": snapshot.domain,
                "title": snapshot.title,
                "source_ids": list(snapshot.source_ids),
            },
            max_chars=self.settings.rag_chunk_size,
            overlap_chars=self.settings.rag_chunk_overlap,
        )
        rows = [
            self._build_row(snapshot, job.projection_epoch, index, chunk)
            for index, chunk in enumerate(chunks)
        ]
        self._write_physical_rows(rows)
        return rows

    def cleanup_epoch(self, page_id: str, projection_epoch: int) -> int:
        init_app_db(self.settings)
        with connect_app_write(self.settings) as conn:
            suffix = " FOR UPDATE" if self.settings.database_backend == "postgres" else ""
            running = conn.execute(
                """
                SELECT id FROM knowledge_projection_jobs
                WHERE page_id=? AND projection_epoch=? AND status='running'
                LIMIT 1
                """
                + suffix,
                (page_id, projection_epoch),
            ).fetchone()
            if running is not None:
                return 0
            deleted = conn.execute(
                "DELETE FROM wiki_chunks WHERE page_id=? AND projection_epoch=?",
                (page_id, projection_epoch),
            )
            return int(deleted.rowcount or 0)

    def _load_snapshot(self, job: ProjectionJob) -> _WikiRevisionSnapshot:
        with connect_app(self.settings) as conn:
            row = row_to_dict(
                conn.execute(
                    """
                    SELECT
                      wp.page_id,
                      wr.id AS revision_id,
                      wp.projection_epoch,
                      wp.path AS page_path,
                      wp.domain,
                      wp.title,
                      wp.lifecycle_status,
                      wr.content,
                      wr.source_ids_json
                    FROM wiki_pages wp
                    JOIN wiki_page_revisions wr
                      ON wr.id=wp.current_revision_id AND wr.page_id=wp.page_id
                    WHERE wp.page_id=?
                    """,
                    (job.page_id,),
                ).fetchone()
            )
        if not self._page_matches_job(row, job):
            raise ProjectionSuperseded(job.id)

        source_ids = tuple(str(value) for value in row.get("source_ids") or [] if value)
        return _WikiRevisionSnapshot(
            page_id=str(row["page_id"]),
            revision_id=str(row["revision_id"]),
            projection_epoch=int(row["projection_epoch"]),
            page_path=str(row["page_path"]),
            domain=str(row["domain"]),
            title=str(row["title"]),
            content=str(row["content"]),
            source_ids=source_ids,
        )

    def _build_row(
        self,
        snapshot: _WikiRevisionSnapshot,
        projection_epoch: int,
        index: int,
        chunk: dict[str, Any],
    ) -> dict[str, Any]:
        text = str(chunk.get("text") or "")
        searchable_text = f"{snapshot.title}\n{text}"
        embedding, embedding_model = embed_text_with_model(
            searchable_text,
            settings=self.settings,
        )
        timestamp = now_iso()
        chunk_id = hashlib.sha256(
            f"{snapshot.page_id}:{snapshot.revision_id}:{projection_epoch}:{index}".encode(
                "utf-8"
            )
        ).hexdigest()
        return {
            "id": chunk_id,
            "page_id": snapshot.page_id,
            "revision_id": snapshot.revision_id,
            "projection_epoch": projection_epoch,
            "chunk_index": index,
            "page_path": snapshot.page_path,
            "domain": snapshot.domain,
            "title": snapshot.title,
            "text": text,
            "token_json": json_dump(tokenize(searchable_text)),
            "embedding_json": json_dump(embedding),
            "embedding_model": embedding_model,
            "source_ids_json": json_dump(list(snapshot.source_ids)),
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def _write_physical_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        with connect_app_write(self.settings) as conn:
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO wiki_chunks(
                      id,page_id,revision_id,projection_epoch,chunk_index,page_path,
                      domain,title,text,token_json,embedding_json,embedding_model,
                      source_ids_json,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(page_id,revision_id,projection_epoch,chunk_index)
                    DO UPDATE SET
                      id=excluded.id,
                      page_path=excluded.page_path,
                      domain=excluded.domain,
                      title=excluded.title,
                      text=excluded.text,
                      token_json=excluded.token_json,
                      embedding_json=excluded.embedding_json,
                      embedding_model=excluded.embedding_model,
                      source_ids_json=excluded.source_ids_json,
                      updated_at=excluded.updated_at
                    """,
                    (
                        row["id"],
                        row["page_id"],
                        row["revision_id"],
                        row["projection_epoch"],
                        row["chunk_index"],
                        row["page_path"],
                        row["domain"],
                        row["title"],
                        row["text"],
                        row["token_json"],
                        row["embedding_json"],
                        row["embedding_model"],
                        row["source_ids_json"],
                        row["created_at"],
                        row["updated_at"],
                    ),
                )

    def _finish_superseded(self, job: ProjectionJob, worker_id: str) -> None:
        with connect_app_write(self.settings) as conn:
            if not self.outbox.finish_claimed(
                conn,
                job_id=job.id,
                worker_id=worker_id,
                status="superseded",
                last_error="desired revision or epoch changed",
            ):
                raise ProjectionLeaseLost(job.id)

    @staticmethod
    def _page_matches_job(page: dict[str, Any] | None, job: ProjectionJob) -> bool:
        return bool(
            page is not None
            and job.page_id is not None
            and job.revision_id is not None
            and page.get("page_id", job.page_id) == job.page_id
            and page.get("revision_id", page.get("current_revision_id")) == job.revision_id
            and int(page.get("projection_epoch") or 0) == job.projection_epoch
            and page.get("lifecycle_status") == "active"
        )


def load_visible_wiki_rows(conn: Any, domain: str | None) -> list[dict[str, Any]]:
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT wc.*
            FROM wiki_chunks wc
            JOIN wiki_pages wp ON wp.page_id = wc.page_id
            WHERE wp.lifecycle_status = 'active'
              AND wp.current_revision_id = wc.revision_id
              AND wp.rag_visible_revision_id = wc.revision_id
              AND wp.rag_visible_epoch = wc.projection_epoch
              AND wp.projection_epoch = wc.projection_epoch
              AND (CAST(? AS TEXT) IS NULL OR wc.domain = ?)
            """,
            (domain, domain),
        ).fetchall()
    )
    normalized: list[dict[str, Any]] = []
    for row in rows:
        source_ids = [str(value) for value in row.get("source_ids") or [] if value]
        tokens = row.pop("token", [])
        row.update(
            origin="wiki",
            source_id=row["page_id"],
            source_ids=source_ids,
            tokens=tokens if isinstance(tokens, list) else [],
            projection_epoch=int(row["projection_epoch"]),
            metadata={
                "origin": "wiki",
                "page_id": row["page_id"],
                "revision_id": row["revision_id"],
                "projection_epoch": int(row["projection_epoch"]),
                "page_path": row["page_path"],
                "source_ids": source_ids,
                "embedding_model": row.get("embedding_model"),
            },
        )
        normalized.append(row)
    return normalized
