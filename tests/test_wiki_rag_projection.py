from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.config import Settings
from app.db import connect_app, init_app_db
from app.projection_jobs import ProjectionJob
from app.wiki_rag_projection import (
    ProjectionLeaseLost,
    ProjectionSuperseded,
    WikiRagProjector,
    load_visible_wiki_rows,
)


@dataclass
class FakeOutbox:
    finish_result: bool = True
    calls: list[dict[str, Any]] = field(default_factory=list)

    def finish_claimed(self, _conn, **kwargs) -> bool:
        self.calls.append(kwargs)
        return self.finish_result


@dataclass(frozen=True)
class SeededPage:
    settings: Settings
    page_id: str = "page_demo"
    page_path: str = "wiki/product/faq/demo.md"

    def set_desired(self, revision_id: str, projection_epoch: int) -> None:
        with connect_app(self.settings) as conn:
            conn.execute(
                """
                UPDATE wiki_pages
                SET current_revision_id=?, projection_epoch=?,
                    rag_visible_revision_id=NULL, rag_visible_epoch=NULL
                WHERE page_id=?
                """,
                (revision_id, projection_epoch, self.page_id),
            )

    def job(self, revision_id: str, projection_epoch: int) -> ProjectionJob:
        job_id = f"pjob_{revision_id}_{projection_epoch}"
        return ProjectionJob(
            id=job_id,
            idempotency_key=f"rag:upsert:{self.page_id}:{revision_id}:{projection_epoch}",
            target="rag",
            operation="upsert",
            page_id=self.page_id,
            revision_id=revision_id,
            projection_epoch=projection_epoch,
            payload={"path": self.page_path},
            status="running",
            attempts=1,
            available_at="t0",
            lease_owner=None,
            lease_expires_at=None,
            last_error=None,
            created_at="t0",
            updated_at="t0",
        )

    def visible(self) -> tuple[str | None, int | None]:
        with connect_app(self.settings) as conn:
            row = conn.execute(
                """
                SELECT rag_visible_revision_id, rag_visible_epoch
                FROM wiki_pages WHERE page_id=?
                """,
                (self.page_id,),
            ).fetchone()
        return row[0], row[1]

    def chunk_epochs(self) -> list[int]:
        with connect_app(self.settings) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT projection_epoch FROM wiki_chunks
                WHERE page_id=? ORDER BY projection_epoch
                """,
                (self.page_id,),
            ).fetchall()
        return [int(row[0]) for row in rows]


@pytest.fixture
def projection_settings(tmp_path) -> Settings:
    settings = Settings(
        database_backend="sqlite",
        database_path=tmp_path / "projection.db",
        rag_store_backend="sqlite",
        rag_chunk_size=300,
        rag_chunk_overlap=0,
        _env_file=None,
    )
    init_app_db(settings)
    return settings


@pytest.fixture
def seeded_page(projection_settings) -> SeededPage:
    page = SeededPage(projection_settings)
    with connect_app(projection_settings) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,page_id,domain,page_type,title,source_ids_json,review_status,
              created_at,updated_at,projection_epoch,lifecycle_status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                page.page_path,
                page.page_id,
                "product",
                "faq",
                "Demo",
                json.dumps(["src_page"]),
                "draft",
                "t0",
                "t0",
                0,
                "active",
            ),
        )
        for revision_number, (revision_id, content, source_ids) in enumerate(
            [
                ("wrev_old", "# Demo\n\nOld refund policy.", ["src_old", "src_shared"]),
                ("wrev_new", "# Demo\n\nNew refund policy.", ["src_new", "src_shared"]),
            ],
            start=1,
        ):
            conn.execute(
                """
                INSERT INTO wiki_page_revisions(
                  id,page_id,page_path,revision_number,file_hash,semantic_hash,
                  content,origin,base_revision_id,source_ids_json,actor,note,
                  metadata_json,idempotency_key,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    revision_id,
                    page.page_id,
                    page.page_path,
                    revision_number,
                    f"hash-{revision_id}",
                    f"semantic-{revision_id}",
                    content,
                    "manual",
                    None,
                    json.dumps(source_ids),
                    "tester",
                    None,
                    "{}",
                    f"test:{revision_id}",
                    "t0",
                ),
            )
    return page


@pytest.fixture
def outbox() -> FakeOutbox:
    return FakeOutbox()


@pytest.fixture
def projector(projection_settings, outbox) -> WikiRagProjector:
    return WikiRagProjector(projection_settings, outbox)


def test_lost_old_epoch_cannot_flip_or_delete_new_epoch(projector, seeded_page):
    old = seeded_page.job(revision_id="wrev_old", projection_epoch=4)
    new = seeded_page.job(revision_id="wrev_new", projection_epoch=5)

    seeded_page.set_desired("wrev_old", 4)
    projector.write_physical_rows(old, worker_id="old-owner")
    seeded_page.set_desired("wrev_new", 5)
    projector.project(new, worker_id="new-owner")

    with pytest.raises(ProjectionSuperseded):
        projector.project(old, worker_id="old-owner")
    projector.cleanup_epoch(old.page_id, old.projection_epoch)

    assert seeded_page.visible() == ("wrev_new", 5)
    assert seeded_page.chunk_epochs() == [5]


def test_same_revision_new_epoch_requires_new_rows(projector, seeded_page):
    seeded_page.set_desired("wrev_new", 7)
    projector.project(seeded_page.job("wrev_new", 7), "owner-a")
    seeded_page.set_desired("wrev_new", 8)
    projector.project(seeded_page.job("wrev_new", 8), "owner-b")

    assert seeded_page.visible() == ("wrev_new", 8)
    assert seeded_page.chunk_epochs() == [7, 8]


def test_failed_embedding_does_not_advance_visible_watermark(
    projector, seeded_page, monkeypatch
):
    seeded_page.set_desired("wrev_new", 9)

    def fail_embedding(*_args, **_kwargs):
        raise RuntimeError("embedding failed")

    monkeypatch.setattr("app.wiki_rag_projection.embed_text_with_model", fail_embedding)

    with pytest.raises(RuntimeError, match="embedding failed"):
        projector.project(seeded_page.job("wrev_new", 9), "owner")

    assert seeded_page.visible() == (None, None)
    assert seeded_page.chunk_epochs() == []


def test_projection_copies_source_ids_from_immutable_revision(projector, seeded_page):
    seeded_page.set_desired("wrev_new", 10)
    projector.project(seeded_page.job("wrev_new", 10), "owner")

    with connect_app(seeded_page.settings) as conn:
        source_ids_json = conn.execute(
            "SELECT source_ids_json FROM wiki_chunks WHERE page_id=?",
            (seeded_page.page_id,),
        ).fetchone()[0]

    assert json.loads(source_ids_json) == ["src_new", "src_shared"]


def test_lost_lease_rolls_back_late_watermark_update(
    projection_settings, seeded_page
):
    outbox = FakeOutbox(finish_result=False)
    projector = WikiRagProjector(projection_settings, outbox)
    seeded_page.set_desired("wrev_new", 11)

    with pytest.raises(ProjectionLeaseLost):
        projector.project(seeded_page.job("wrev_new", 11), "old-owner")

    assert seeded_page.visible() == (None, None)
    assert seeded_page.chunk_epochs() == [11]


def test_cleanup_keeps_epoch_referenced_by_running_job(projector, seeded_page):
    seeded_page.set_desired("wrev_old", 12)
    job = seeded_page.job("wrev_old", 12)
    projector.write_physical_rows(job, "owner")
    with connect_app(seeded_page.settings) as conn:
        conn.execute(
            """
            INSERT INTO knowledge_projection_jobs(
              id,idempotency_key,target,operation,page_id,revision_id,projection_epoch,
              payload_json,status,attempts,available_at,lease_owner,lease_expires_at,
              created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job.id,
                job.idempotency_key,
                "rag",
                "upsert",
                job.page_id,
                job.revision_id,
                job.projection_epoch,
                "{}",
                "running",
                1,
                "t0",
                "owner",
                "t1",
                "t0",
                "t0",
            ),
        )

    assert projector.cleanup_epoch(job.page_id, job.projection_epoch) == 0
    assert seeded_page.chunk_epochs() == [12]


def test_visible_loader_requires_current_revision_and_epoch(projector, seeded_page):
    seeded_page.set_desired("wrev_new", 13)
    projector.project(seeded_page.job("wrev_new", 13), "owner")

    with connect_app(seeded_page.settings) as conn:
        rows = load_visible_wiki_rows(conn, "product")
    assert len(rows) == 1
    assert rows[0]["origin"] == "wiki"
    assert rows[0]["source_ids"] == ["src_new", "src_shared"]
    assert rows[0]["revision_id"] == "wrev_new"
    assert rows[0]["projection_epoch"] == 13
    assert rows[0]["page_path"] == seeded_page.page_path

    invalid_states = [
        ("current_revision_id=?", ("wrev_old",)),
        ("rag_visible_revision_id=?", ("wrev_old",)),
        ("rag_visible_epoch=?", (14,)),
        ("projection_epoch=?", (14,)),
        ("lifecycle_status=?", ("deleted",)),
    ]
    for assignment, values in invalid_states:
        with connect_app(seeded_page.settings) as conn:
            conn.execute(
                f"UPDATE wiki_pages SET {assignment} WHERE page_id=?",
                (*values, seeded_page.page_id),
            )
            assert load_visible_wiki_rows(conn, "product") == []
            conn.execute(
                """
                UPDATE wiki_pages
                SET current_revision_id='wrev_new',
                    rag_visible_revision_id='wrev_new',
                    projection_epoch=13,
                    rag_visible_epoch=13,
                    lifecycle_status='active'
                WHERE page_id=?
                """,
                (seeded_page.page_id,),
            )

    with connect_app(seeded_page.settings) as conn:
        assert load_visible_wiki_rows(conn, "people") == []
