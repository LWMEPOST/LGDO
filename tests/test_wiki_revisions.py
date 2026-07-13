from pathlib import Path

from app.config import get_settings
from app.db import connect_app, init_app_db
from app.wiki_revisions import WikiRevisionService


def make_settings(tmp_path: Path):
    settings = get_settings().model_copy()
    settings.database_backend = "sqlite"
    settings.database_path = tmp_path / "wiki.db"
    settings.vault_path = tmp_path / "vault"
    (settings.vault_path / "wiki/product/faq").mkdir(parents=True)
    init_app_db(settings)
    return settings


def seed_legacy_page(settings, content: bytes) -> str:
    page_path = "wiki/product/faq/demo.md"
    (settings.vault_path / page_path).write_bytes(content)
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(path,domain,page_type,title,source_ids_json,review_status,owner,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (page_path,"product","faq","Demo",'["src_1"]',"reviewed","alice","t0","t0"),
        )
    return page_path


def test_first_read_creates_one_legacy_revision_without_generated_baseline(tmp_path):
    settings = make_settings(tmp_path)
    page_path = seed_legacy_page(
        settings,
        b"---\ntitle: Demo\nsource_ids: [src_1]\nreview_status: reviewed\n---\n# Human legacy\n",
    )
    service = WikiRevisionService(settings)

    first = service.get_page(page_path)
    replay = service.get_page(page_path)

    assert first.current_revision_id == replay.current_revision_id
    with connect_app(settings) as conn:
        page = conn.execute("SELECT * FROM wiki_pages WHERE path=?", (page_path,)).fetchone()
        revisions = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE page_id=?", (page["page_id"],)
        ).fetchall()
    assert len(revisions) == 1
    assert revisions[0]["origin"] == "legacy"
    assert page["current_revision_id"] == revisions[0]["id"]
    assert page["generated_revision_id"] is None
    assert page["file_hash"] == revisions[0]["file_hash"]
    assert first.content == replay.content


def test_revision_event_replay_reuses_only_same_idempotency_key(tmp_path):
    settings = make_settings(tmp_path)
    page_path = seed_legacy_page(
        settings,
        b"---\ntitle: Demo\nsource_ids: [src_1]\n---\n# A\n",
    )
    service = WikiRevisionService(settings)
    page = service.get_page(page_path)
    with service.coordinator.lock_page(page_path) as locked:
        one = service._create_revision_locked(
            locked.conn, locked.page, content=page.raw_bytes, origin="external",
            base_revision_id=page.current_revision_id, source_ids=["src_1"], actor="test",
            note=None, idempotency_key="event:one", metadata={},
        )
        replay = service._create_revision_locked(
            locked.conn, locked.page, content=page.raw_bytes, origin="external",
            base_revision_id=page.current_revision_id, source_ids=["src_1"], actor="test",
            note=None, idempotency_key="event:one", metadata={},
        )
        two = service._create_revision_locked(
            locked.conn, locked.page, content=page.raw_bytes, origin="external",
            base_revision_id=page.current_revision_id, source_ids=["src_1"], actor="test",
            note=None, idempotency_key="event:two", metadata={},
        )
    assert one.id == replay.id
    assert two.id != one.id
    assert two.revision_number == one.revision_number + 1
