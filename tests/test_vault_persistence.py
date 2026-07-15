from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import inspect

import pytest

from app.config import get_settings
from app.db import connect_app, connect_app_write, init_app_db
from app.vault_events import VaultEventStore, VaultOccurrenceConflict
from app.wiki_revisions import _upsert_sync_issue_locked


@pytest.fixture
def sqlite_settings(tmp_path):
    settings = get_settings().model_copy(
        deep=True,
        update={
            "database_backend": "sqlite",
            "database_path": tmp_path / "vault-persistence.db",
            "vault_path": tmp_path / "vault",
            "vault_watch_enabled": False,
            "projection_worker_enabled": False,
        },
    )
    init_app_db(settings)
    return settings


def test_begin_and_finish_occurrence_stays_out_of_revision_event_log(
    sqlite_settings,
):
    store = VaultEventStore(sqlite_settings)
    detected_at = datetime(2026, 7, 15, 1, 2, 3, tzinfo=timezone.utc)

    occurrence = store.begin_occurrence(
        "add",
        "wiki/product/new.md",
        detected_at=detected_at,
        occurrence_id="vocc_begin_finish",
    )
    finished = store.finish_occurrence(occurrence.id, "ignored")

    assert occurrence.id == "vocc_begin_finish"
    assert occurrence.status == "pending"
    assert occurrence.detected_at == detected_at
    assert finished is True
    with connect_app(sqlite_settings) as conn:
        stored = conn.execute(
            "SELECT status FROM vault_watch_occurrences WHERE id=?",
            (occurrence.id,),
        ).fetchone()
        revision_event_count = conn.execute(
            "SELECT COUNT(*) FROM vault_change_events"
        ).fetchone()[0]
    assert stored["status"] == "ignored"
    assert revision_event_count == 0


def test_occurrence_id_is_generated_only_when_omitted(sqlite_settings):
    store = VaultEventStore(sqlite_settings)
    detected_at = datetime(2026, 7, 15, 1, 30, tzinfo=timezone.utc)

    explicit = store.begin_occurrence(
        "add",
        "wiki/product/explicit-empty-id.md",
        detected_at=detected_at,
        occurrence_id="",
    )
    generated = store.begin_occurrence(
        "add",
        "wiki/product/generated-id.md",
        detected_at=detected_at,
    )

    assert explicit.id == ""
    assert generated.id.startswith("vocc_")


def test_occurrence_replay_uses_only_canonical_identity_payload(sqlite_settings):
    store = VaultEventStore(sqlite_settings)
    occurrence_id = "vocc_replay"
    first_detected_at = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)
    replay_detected_at = datetime(2026, 7, 15, 3, 0, tzinfo=timezone.utc)
    original = store.begin_occurrence(
        "rename",
        "wiki/product/new-name.md",
        detected_at=first_detected_at,
        old_page_path="wiki/product/old-name.md",
        occurrence_id=occurrence_id,
    )
    with connect_app(sqlite_settings) as conn:
        before = dict(
            conn.execute(
                "SELECT * FROM vault_watch_occurrences WHERE id=?",
                (occurrence_id,),
            ).fetchone()
        )

    replayed = store.begin_occurrence(
        "rename",
        "wiki/product/new-name.md",
        detected_at=replay_detected_at,
        old_page_path="wiki/product/old-name.md",
        occurrence_id=occurrence_id,
    )
    with connect_app(sqlite_settings) as conn:
        after = dict(
            conn.execute(
                "SELECT * FROM vault_watch_occurrences WHERE id=?",
                (occurrence_id,),
            ).fetchone()
        )

    assert replayed == original
    assert replayed.detected_at == first_detected_at
    assert after == before

    changed_payloads = (
        ("change", "wiki/product/new-name.md", "wiki/product/old-name.md"),
        ("rename", "wiki/product/other.md", "wiki/product/old-name.md"),
        ("rename", "wiki/product/new-name.md", "wiki/product/other-old.md"),
    )
    for kind, page_path, old_page_path in changed_payloads:
        with pytest.raises(
            VaultOccurrenceConflict,
            match="^watch occurrence payload changed$",
        ):
            store.begin_occurrence(
                kind,
                page_path,
                detected_at=replay_detected_at,
                old_page_path=old_page_path,
                occurrence_id=occurrence_id,
            )


def test_finish_occurrence_is_terminal_compare_and_set(sqlite_settings):
    store = VaultEventStore(sqlite_settings)
    occurrence_id = "vocc_finish_cas"
    detected_at = datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc)
    store.begin_occurrence(
        "change",
        "wiki/product/cas.md",
        detected_at=detected_at,
        occurrence_id=occurrence_id,
    )

    with pytest.raises(
        VaultOccurrenceConflict,
        match="watch occurrence requires a terminal status",
    ):
        store.finish_occurrence(occurrence_id, "pending")

    def finish(_worker: int):
        return store.finish_occurrence(
            occurrence_id,
            "applied",
            page_id="page_cas",
            revision_id="wrev_cas",
            sync_issue_id=None,
            error_summary=None,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(finish, (1, 2)))

    assert sorted(outcomes) == [False, True]
    assert finish(3) is False
    with pytest.raises(VaultOccurrenceConflict, match="terminal result changed"):
        store.finish_occurrence(
            occurrence_id,
            "failed",
            page_id="page_other",
            error_summary="different winner",
        )
    with pytest.raises(VaultOccurrenceConflict, match="occurrence does not exist"):
        store.finish_occurrence("vocc_missing", "ignored")

    replayed = store.begin_occurrence(
        "change",
        "wiki/product/cas.md",
        detected_at=datetime(2026, 7, 15, 5, 0, tzinfo=timezone.utc),
        occurrence_id=occurrence_id,
    )
    assert replayed.status == "applied"
    assert replayed.detected_at == detected_at


def test_pending_delete_deduplicates_only_the_active_absence_cycle(
    sqlite_settings,
):
    store = VaultEventStore(sqlite_settings)
    detected_at = datetime(2026, 7, 15, 6, 0, tzinfo=timezone.utc)
    expires_at = datetime(2026, 7, 15, 6, 0, 5, tzinfo=timezone.utc)

    first = store.get_or_create_pending_delete(
        occurrence_id="vocc_delete_a",
        page_id="page_delete",
        old_page_path="wiki/product/deleted.md",
        file_hash="a" * 64,
        semantic_hash="b" * 64,
        detected_at=detected_at,
        expires_at=expires_at,
    )
    duplicate = store.get_or_create_pending_delete(
        occurrence_id="vocc_delete_b",
        page_id="page_delete",
        old_page_path="wiki/product/deleted.md",
        file_hash="c" * 64,
        semantic_hash="d" * 64,
        detected_at=datetime(2026, 7, 15, 6, 0, 1, tzinfo=timezone.utc),
        expires_at=datetime(2026, 7, 15, 6, 0, 6, tzinfo=timezone.utc),
    )

    assert duplicate == first
    assert store.get_pending_delete(first.id) == first
    assert store.find_pending_delete_for_path(first.old_page_path) == first
    assert store.find_pending_deletes(page_id=first.page_id) == [first]
    assert store.list_due_deletes(
        datetime(2026, 7, 15, 6, 0, 4, tzinfo=timezone.utc)
    ) == []
    assert store.list_due_deletes(expires_at) == [first]
    assert store.complete_delete(first.id) is True
    assert store.complete_delete(first.id) is False
    assert store.find_pending_delete_for_path(first.old_page_path) is None

    later = store.get_or_create_pending_delete(
        occurrence_id="vocc_delete_c",
        page_id="page_delete",
        old_page_path="wiki/product/deleted.md",
        file_hash=None,
        semantic_hash=None,
        detected_at=datetime(2026, 7, 15, 7, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 7, 15, 7, 0, 5, tzinfo=timezone.utc),
    )
    assert later.id != first.id


def test_pending_delete_cancel_records_matching_occurrence(sqlite_settings):
    store = VaultEventStore(sqlite_settings)
    detected_at = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
    pending = store.get_or_create_pending_delete(
        occurrence_id="vocc_delete_cancel",
        page_id="page_cancel",
        old_page_path="wiki/product/cancelled-delete.md",
        file_hash=None,
        semantic_hash=None,
        detected_at=detected_at,
        expires_at=datetime(2026, 7, 15, 8, 0, 5, tzinfo=timezone.utc),
    )

    assert store.cancel_delete(pending.id, "vocc_matching_add") is True
    assert store.cancel_delete(pending.id, "vocc_other_add") is False
    with connect_app(sqlite_settings) as conn:
        row = conn.execute(
            "SELECT status,matched_occurrence_id FROM pending_vault_deletes WHERE id=?",
            (pending.id,),
        ).fetchone()
    assert tuple(row) == ("cancelled", "vocc_matching_add")


def test_concurrent_reconcile_requests_share_one_active_job(sqlite_settings):
    store = VaultEventStore(sqlite_settings)

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(store.request_reconcile, ("manual", "startup")))

    assert len({job.id for job in jobs}) == 1
    assert {job.status for job in jobs} == {"queued"}
    assert store.active_reconcile() == jobs[0]
    assert store.latest_reconcile() == jobs[0]


def test_expired_reconcile_has_one_reclaim_winner(sqlite_settings):
    store = VaultEventStore(sqlite_settings)
    job = store.request_reconcile("manual")
    started_at = datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc)

    assert store.claim_reconcile(
        job.id, "worker-a", now=started_at, lease_seconds=10
    ) is True
    assert store.claim_reconcile(
        job.id,
        "worker-b",
        now=started_at + timedelta(seconds=9),
        lease_seconds=10,
    ) is False

    reclaim_at = started_at + timedelta(seconds=11)

    def reclaim(owner: str):
        return owner, store.claim_reconcile(
            job.id,
            owner,
            now=reclaim_at,
            lease_seconds=10,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reclaim, ("worker-b", "worker-c")))

    winners = [owner for owner, won in outcomes if won]
    assert len(winners) == 1
    winner = winners[0]
    claimed = store.get_reconcile(job.id)
    assert claimed is not None
    assert claimed.attempts == 2
    assert claimed.lease_owner == winner
    for loser in {"worker-a", "worker-b", "worker-c"} - {winner}:
        assert store.finish_reconcile(job.id, loser, {"scanned": 1}) is False
    assert store.finish_reconcile(job.id, winner, {"scanned": 2}) is True
    assert store.finish_reconcile(job.id, winner, {"scanned": 3}) is False
    finished = store.get_reconcile(job.id)
    assert finished is not None
    assert finished.status == "succeeded"
    assert finished.result == {"scanned": 2}
    assert finished.lease_owner is None


def test_restart_reuses_stale_reconcile_job_and_reclaims_it(sqlite_settings):
    store = VaultEventStore(sqlite_settings)
    job = store.request_reconcile("startup")
    started_at = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
    assert store.claim_reconcile(
        job.id, "worker-before-restart", now=started_at, lease_seconds=5
    ) is True

    restarted_store = VaultEventStore(sqlite_settings)
    restarted = restarted_store.request_reconcile("restart")

    assert restarted.id == job.id
    assert restarted.status == "running"
    assert restarted_store.claim_reconcile(
        job.id,
        "worker-after-restart",
        now=started_at + timedelta(seconds=6),
        lease_seconds=5,
    ) is True


def test_reconcile_renewal_and_owner_transitions_are_fenced(sqlite_settings):
    store = VaultEventStore(sqlite_settings)
    job = store.request_reconcile("manual")
    started_at = datetime(2026, 7, 15, 11, 0, tzinfo=timezone.utc)
    assert store.claim_reconcile(
        job.id, "worker-a", now=started_at, lease_seconds=10
    ) is True
    assert store.renew_reconcile(
        job.id,
        "worker-a",
        now=started_at + timedelta(seconds=5),
        lease_seconds=10,
    ) is True
    assert store.renew_reconcile(
        job.id,
        "worker-b",
        now=started_at + timedelta(seconds=6),
        lease_seconds=10,
    ) is False
    assert store.claim_reconcile(
        job.id,
        "worker-b",
        now=started_at + timedelta(seconds=11),
        lease_seconds=10,
    ) is False
    assert store.claim_reconcile(
        job.id,
        "worker-b",
        now=started_at + timedelta(seconds=16),
        lease_seconds=10,
    ) is True
    assert store.finish_reconcile(job.id, "worker-a", {"scanned": 1}) is False
    assert store.requeue_reconcile(job.id, "worker-a") is False
    assert store.requeue_reconcile(job.id, "worker-b") is True

    requeued = store.get_reconcile(job.id)
    assert requeued is not None
    assert requeued.status == "queued"
    assert requeued.attempts == 2
    assert requeued.lease_owner is None
    assert requeued.lease_expires_at is None
    assert store.claim_reconcile(
        job.id,
        "worker-c",
        now=started_at + timedelta(seconds=17),
        lease_seconds=10,
    ) is True
    assert store.fail_reconcile(job.id, "worker-b", "stale failure") is False
    assert store.fail_reconcile(job.id, "worker-c", "reconcile failed") is True

    failed = store.get_reconcile(job.id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error_summary == "reconcile failed"
    assert failed.lease_owner is None
    assert store.active_reconcile() is None
    assert store.latest_reconcile() == failed
    assert store.request_reconcile("retry").id != job.id


def test_pending_occurrence_queries_classify_adds_by_detection_time(
    sqlite_settings,
):
    store = VaultEventStore(sqlite_settings)
    first_at = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    second_at = first_at + timedelta(seconds=1)
    pending_add = store.begin_occurrence(
        "add",
        "wiki/product/pending-add.md",
        detected_at=first_at,
        occurrence_id="vocc_pending_add",
    )
    completed_change = store.begin_occurrence(
        "change",
        "wiki/product/completed-change.md",
        detected_at=second_at,
        occurrence_id="vocc_completed_change",
    )
    assert store.finish_occurrence(completed_change.id, "ignored") is True

    assert store.pending_occurrences() == [pending_add]
    assert store.has_unclassified_add_before(
        first_at - timedelta(seconds=1)
    ) is False
    assert store.has_unclassified_add_before(first_at) is True


def test_vault_event_store_source_has_no_revision_event_sql():
    assert "vault_change_events" not in inspect.getsource(VaultEventStore)


def test_sync_issue_generation_cas_rejects_stale_resolver(sqlite_settings):
    store = VaultEventStore(sqlite_settings)
    page_path = "wiki/product/generation.md"
    with connect_app_write(sqlite_settings) as conn:
        issue_id, generation = _upsert_sync_issue_locked(
            conn,
            page_path=page_path,
            file_hash="f" * 64,
            page_id=None,
            issue_type="invalid_frontmatter",
            error_summary="invalid yaml",
        )
    assert generation == 1
    stale = store.list_open_issue_refs(page_path)[0]

    with connect_app_write(sqlite_settings) as conn:
        replayed_id, replayed_generation = _upsert_sync_issue_locked(
            conn,
            page_path=page_path,
            file_hash="f" * 64,
            page_id=None,
            issue_type="invalid_frontmatter",
            error_summary="invalid yaml seen again",
        )

    assert (replayed_id, replayed_generation) == (issue_id, 2)
    current = store.list_open_issue_refs(page_path)[0]
    assert current.id == stale.id
    assert current.generation == 2
    assert store.resolve_issue(stale) is False
    assert store.list_open_issue_refs(page_path) == (current,)
    assert store.resolve_issue(current) is True
    assert store.resolve_issue(current) is False
    assert store.list_open_issue_refs(page_path) == ()


def test_sqlite_has_partial_unique_vault_coordination_indexes(sqlite_settings):
    with connect_app(sqlite_settings) as conn:
        delete_indexes = {
            row["name"]: (bool(row["unique"]), bool(row["partial"]))
            for row in conn.execute(
                "PRAGMA index_list('pending_vault_deletes')"
            ).fetchall()
        }
        reconcile_indexes = {
            row["name"]: (bool(row["unique"]), bool(row["partial"]))
            for row in conn.execute(
                "PRAGMA index_list('vault_reconcile_jobs')"
            ).fetchall()
        }
    assert delete_indexes["idx_pending_vault_delete_active_absence"] == (
        True,
        True,
    )
    assert reconcile_indexes["idx_vault_reconcile_single_flight"] == (
        True,
        True,
    )


def test_status_snapshot_reads_persisted_vault_health(
    sqlite_settings,
    monkeypatch,
):
    store = VaultEventStore(sqlite_settings)
    detected_at = datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "app.vault_events.now_iso",
        lambda: "2026-07-15T13:05:00+00:00",
    )
    store.begin_occurrence(
        "add",
        "wiki/product/pending-health.md",
        detected_at=detected_at,
        occurrence_id="vocc_health_pending",
    )
    failed = store.begin_occurrence(
        "change",
        "wiki/product/failed-health.md",
        detected_at=detected_at + timedelta(seconds=1),
        occurrence_id="vocc_health_failed",
    )
    assert store.finish_occurrence(
        failed.id,
        "failed",
        error_summary="latest watcher failure",
    ) is True
    store.get_or_create_pending_delete(
        occurrence_id="vocc_health_delete",
        page_id="page_health",
        old_page_path="wiki/product/missing-health.md",
        file_hash=None,
        semantic_hash=None,
        detected_at=detected_at,
        expires_at=detected_at + timedelta(seconds=5),
    )
    with connect_app_write(sqlite_settings) as conn:
        _upsert_sync_issue_locked(
            conn,
            page_path="wiki/product/invalid-health.md",
            file_hash="e" * 64,
            page_id=None,
            issue_type="invalid_frontmatter",
            error_summary="invalid yaml",
        )
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,domain,page_type,title,source_ids_json,review_status,
              created_at,updated_at,lifecycle_status,sync_error
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "wiki/product/invalid-health.md",
                "product",
                "feature",
                "Invalid health",
                "[]",
                "draft",
                "2026-07-15T13:00:00+00:00",
                "2026-07-15T13:00:00+00:00",
                "invalid",
                "invalid_frontmatter",
            ),
        )

    assert store.status_snapshot() == {
        "pending_occurrences": 1,
        "failed_occurrences": 1,
        "pending_deletes": 1,
        "open_issues": 1,
        "invalid_pages": 1,
        "last_event_at": "2026-07-15T13:05:00+00:00",
        "last_error": "latest watcher failure",
    }
