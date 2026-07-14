from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.db import connect_app, init_app_db
from app.projection_jobs import OUTBOX_RETRY_DELAYS_SECONDS, ProjectionOutbox


def sqlite_settings(tmp_path):
    settings = get_settings().model_copy()
    settings.database_backend = "sqlite"
    settings.database_path = tmp_path / "outbox.db"
    settings.vault_path = tmp_path / "vault"
    init_app_db(settings)
    return settings


def seed_page(conn):
    conn.execute(
        """
        INSERT INTO wiki_pages(path,page_id,domain,page_type,title,source_ids_json,review_status,
          created_at,updated_at,current_revision_id,projection_epoch,lifecycle_status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        ("wiki/product/faq/demo.md","page_1","product","faq","Demo","[]","draft","t0","t0","wrev_1",1,"active"),
    )


def test_enqueue_pair_is_idempotent_per_epoch_but_requeues_restored_revision(tmp_path):
    settings = sqlite_settings(tmp_path)
    outbox = ProjectionOutbox(settings)
    with connect_app(settings) as conn:
        seed_page(conn)
        page = dict(conn.execute("SELECT * FROM wiki_pages WHERE page_id='page_1'").fetchone())
        first = outbox.enqueue_pair_for_state(conn, page, "upsert", {"path": page["path"]})
        replay = outbox.enqueue_pair_for_state(conn, page, "upsert", {"path": page["path"]})
        conn.execute("UPDATE wiki_pages SET projection_epoch=2 WHERE page_id='page_1'")
        restored = dict(conn.execute("SELECT * FROM wiki_pages WHERE page_id='page_1'").fetchone())
        second = outbox.enqueue_pair_for_state(conn, restored, "upsert", {"path": restored["path"]})
    assert first == replay
    assert first != second
    assert len(first) == len(second) == 2


def test_lost_lease_cannot_finish_or_advance_rag_watermark(tmp_path):
    settings = sqlite_settings(tmp_path)
    outbox = ProjectionOutbox(settings)
    now = datetime.now(timezone.utc)
    with connect_app(settings) as conn:
        seed_page(conn)
        page = dict(conn.execute("SELECT * FROM wiki_pages WHERE page_id='page_1'").fetchone())
        outbox.enqueue(conn, target="rag", operation="upsert", page_id="page_1",
                       revision_id="wrev_1", projection_epoch=1, payload={"path": page["path"]})
    claimed = outbox.claim(target="rag", worker_id="worker_a", limit=1, lease_seconds=1, now=now)
    assert len(claimed) == 1
    claimed_again = outbox.claim(
        target="rag", worker_id="worker_b", limit=1, lease_seconds=30,
        now=now + timedelta(seconds=2),
    )
    assert claimed_again[0].id == claimed[0].id
    with connect_app(settings) as conn:
        assert outbox.finish_claimed(
            conn, job_id=claimed[0].id, worker_id="worker_a", status="succeeded"
        ) is False
        row = conn.execute("SELECT rag_visible_revision_id FROM wiki_pages WHERE page_id='page_1'").fetchone()
    assert row[0] is None


def test_mark_succeeded_finishes_job_without_advancing_rag_watermark(tmp_path):
    settings = sqlite_settings(tmp_path)
    outbox = ProjectionOutbox(settings)
    with connect_app(settings) as conn:
        seed_page(conn)
        outbox.enqueue(conn, target="rag", operation="upsert", page_id="page_1",
                       revision_id="wrev_1", projection_epoch=1, payload={"path":"wiki/product/faq/demo.md"})
    job = outbox.claim(target="rag", worker_id="rag_worker", limit=1, lease_seconds=30)[0]
    assert outbox.mark_succeeded(job.id, "rag_worker") is True
    with connect_app(settings) as conn:
        row = conn.execute(
            "SELECT rag_visible_revision_id, rag_visible_epoch FROM wiki_pages WHERE page_id='page_1'"
        ).fetchone()
        job_row = conn.execute(
            "SELECT status FROM knowledge_projection_jobs WHERE id=?", (job.id,)
        ).fetchone()
    assert tuple(row) == (None, None)
    assert job_row[0] == "succeeded"


def test_fifth_attempt_can_renew_its_lease_and_finish(tmp_path):
    settings = sqlite_settings(tmp_path)
    outbox = ProjectionOutbox(settings)
    now = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
    with connect_app(settings) as conn:
        seed_page(conn)
        job_id = outbox.enqueue(
            conn,
            target="rag",
            operation="upsert",
            page_id="page_1",
            revision_id="wrev_1",
            projection_epoch=1,
            payload={"path": "wiki/product/faq/demo.md"},
        )
        conn.execute(
            """
            UPDATE knowledge_projection_jobs
            SET status='failed', attempts=4, available_at=?
            WHERE id=?
            """,
            (now.isoformat(), job_id),
        )

    job = outbox.claim(target="rag", worker_id="worker_a", limit=1, lease_seconds=30, now=now)[0]
    assert job.attempts == 5
    assert outbox.renew_lease(job.id, "worker_a", 30, now=now + timedelta(seconds=10)) is True
    assert outbox.mark_succeeded(job.id, "worker_a") is True


@pytest.mark.parametrize(
    "attempt,delay_seconds",
    [(1, 5), (2, 30), (3, 120), (4, 600), (5, None)],
)
def test_mark_failed_schedules_attempts_one_to_four_and_leaves_five_terminal(
    tmp_path, attempt, delay_seconds
):
    settings = sqlite_settings(tmp_path)
    outbox = ProjectionOutbox(settings)
    now = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
    with connect_app(settings) as conn:
        seed_page(conn)
        job_id = outbox.enqueue(
            conn, target="rag", operation="upsert", page_id="page_1",
            revision_id="wrev_1", projection_epoch=1,
            payload={"path": "wiki/product/faq/demo.md"},
        )
        conn.execute(
            """
            UPDATE knowledge_projection_jobs
            SET status='running', attempts=?, lease_owner='worker_a', lease_expires_at=?
            WHERE id=?
            """,
            (attempt, (now + timedelta(minutes=5)).isoformat(), job_id),
        )

    assert outbox.mark_failed(job_id, "worker_a", "boom", now=now) is True
    with connect_app(settings) as conn:
        row = conn.execute(
            "SELECT status,attempts,available_at FROM knowledge_projection_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    assert row["status"] == "failed"
    assert row["attempts"] == attempt
    if delay_seconds is None:
        assert outbox.claim(
            target="rag", worker_id="worker_b", limit=1, lease_seconds=30,
            now=now + timedelta(days=1),
        ) == []
    else:
        assert datetime.fromisoformat(row["available_at"]) == now + timedelta(seconds=delay_seconds)


def test_retry_delay_contract_is_fixed():
    assert OUTBOX_RETRY_DELAYS_SECONDS == (5, 30, 120, 600)


@pytest.mark.parametrize("status", ["pending", "failed", "running"])
def test_attempt_five_is_never_reclaimed_from_any_claim_branch(tmp_path, status):
    settings = sqlite_settings(tmp_path)
    outbox = ProjectionOutbox(settings)
    now = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
    with connect_app(settings) as conn:
        seed_page(conn)
        job_id = outbox.enqueue(
            conn,
            target="rag",
            operation="upsert",
            page_id="page_1",
            revision_id="wrev_1",
            projection_epoch=1,
            payload={"path": "wiki/product/faq/demo.md"},
        )
        conn.execute(
            """
            UPDATE knowledge_projection_jobs
            SET status=?, attempts=5, available_at=?, lease_owner=?, lease_expires_at=?
            WHERE id=?
            """,
            (
                status,
                (now - timedelta(seconds=1)).isoformat(),
                "dead-worker" if status == "running" else None,
                (now - timedelta(seconds=1)).isoformat(),
                job_id,
            ),
        )

    assert outbox.claim(
        target="rag",
        worker_id="worker_b",
        limit=1,
        lease_seconds=30,
        now=now,
    ) == []


def test_attempt_four_failed_job_is_claimed_only_at_available_at(tmp_path):
    settings = sqlite_settings(tmp_path)
    outbox = ProjectionOutbox(settings)
    now = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
    available_at = now + timedelta(seconds=600)
    with connect_app(settings) as conn:
        seed_page(conn)
        job_id = outbox.enqueue(
            conn,
            target="rag",
            operation="upsert",
            page_id="page_1",
            revision_id="wrev_1",
            projection_epoch=1,
            payload={"path": "wiki/product/faq/demo.md"},
        )
        conn.execute(
            """
            UPDATE knowledge_projection_jobs
            SET status='failed', attempts=4, available_at=?
            WHERE id=?
            """,
            (available_at.isoformat(), job_id),
        )

    assert outbox.claim(
        target="rag",
        worker_id="worker_b",
        limit=1,
        lease_seconds=30,
        now=available_at - timedelta(microseconds=1),
    ) == []
    claimed = outbox.claim(
        target="rag",
        worker_id="worker_b",
        limit=1,
        lease_seconds=30,
        now=available_at,
    )
    assert [job.id for job in claimed] == [job_id]
    assert claimed[0].attempts == 5
