from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.auth import UserContext
from app.citations import (
    all_sources_readable,
    dedupe_citations,
    map_gbrain_hit,
    map_local_hit,
)
from app.config import Settings
from app.db import connect_app, connect_app_write, init_app_db
from app.gbrain import GBrainHit
from app.models import AskRequest, Citation
from app.search import build_ask_assembly


NOW = "2099-01-01T12:00:00+00:00"
PAGE_ID = "page-1"
REVISION_ID = "wrev-1"
PAGE_PATH = "wiki/product/faq/demo.md"
GBRAIN_SOURCE = "lgdo-managed"
SLUG = "product/faq/demo"


@pytest.fixture
def settings(tmp_path) -> Settings:
    configured = Settings(
        database_backend="sqlite",
        rag_store_backend="sqlite",
        database_path=tmp_path / "citation-contract.db",
        vault_path=tmp_path / "vault",
        upload_path=tmp_path / "uploads",
        gbrain_enabled=True,
        projection_worker_enabled=False,
        _env_file=None,
    )
    init_app_db(configured)
    _seed_current_projection(configured)
    return configured


@pytest.fixture
def reader() -> UserContext:
    return UserContext(user_id="reader", role="viewer", acl_tags=("team",))


def _insert_source(
    conn,
    source_id: str,
    *,
    status: str = "active",
    acl_tags: tuple[str, ...] = ("team",),
) -> None:
    conn.execute(
        """
        INSERT INTO sources(
          id,domain,owner,title,source_type,original_path,raw_path,content_hash,
          size_bytes,status,metadata_json,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            source_id,
            "product",
            "owner",
            source_id,
            "markdown",
            f"{source_id}.md",
            f"raw/{source_id}.md",
            f"hash-{source_id}",
            100,
            status,
            json.dumps({"acl_tags": list(acl_tags)}),
            NOW,
            NOW,
        ),
    )


def _seed_current_projection(settings: Settings) -> None:
    with connect_app_write(settings) as conn:
        _insert_source(conn, "src-a")
        _insert_source(conn, "src-b")
        conn.execute(
            """
            INSERT INTO wiki_page_revisions(
              id,page_id,page_path,revision_number,file_hash,semantic_hash,content,
              origin,base_revision_id,source_ids_json,actor,note,metadata_json,
              idempotency_key,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                REVISION_ID,
                PAGE_ID,
                PAGE_PATH,
                1,
                "file-hash",
                "semantic-hash",
                "# Demo\n\nCurrent immutable revision.",
                "manual",
                None,
                json.dumps(["src-a", "src-b"]),
                "tester",
                None,
                "{}",
                "citation-contract:wrev-1",
                NOW,
            ),
        )
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,page_id,domain,page_type,title,source_ids_json,review_status,
              created_at,updated_at,current_revision_id,revision_number,file_hash,
              semantic_hash,rag_visible_revision_id,projection_epoch,
              rag_visible_epoch,lifecycle_status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                PAGE_PATH,
                PAGE_ID,
                "product",
                "faq",
                "Demo",
                json.dumps(["namespace-must-not-be-used"]),
                "approved",
                NOW,
                NOW,
                REVISION_ID,
                1,
                "file-hash",
                "semantic-hash",
                REVISION_ID,
                7,
                7,
                "active",
            ),
        )
        conn.execute(
            """
            INSERT INTO gbrain_page_projections(
              id,page_id,revision_id,projection_epoch,page_path,file_hash,
              semantic_hash,gbrain_source_id,slug,source_path,gbrain_content_hash,
              gbrain_page_generation,status,imported_at,invalidated_at,last_job_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "gproj-1",
                PAGE_ID,
                REVISION_ID,
                7,
                PAGE_PATH,
                "file-hash",
                "semantic-hash",
                GBRAIN_SOURCE,
                SLUG,
                "product/faq/demo.md",
                "gbrain-content-hash",
                11,
                "current",
                NOW,
                None,
                "pjob-1",
            ),
        )


def _valid_gbrain_hit() -> GBrainHit:
    return GBrainHit(
        slug=SLUG,
        title="Demo",
        snippet="Authorized projected answer.",
        score=3.5,
        source_id=GBRAIN_SOURCE,
        gbrain_source_id=GBRAIN_SOURCE,
        source_path="product/faq/demo.md",
        content_hash="gbrain-content-hash",
        page_generation=11,
        page_type="faq",
        chunk_id=42,
    )


def test_exact_current_gbrain_mapping_emits_one_citation_per_lgdo_source(
    settings, reader
):
    with connect_app(settings) as conn:
        evidence = map_gbrain_hit(conn, _valid_gbrain_hit(), reader)

    assert evidence.diagnostic == "mapped"
    assert evidence.context
    assert evidence.answer_part
    assert [citation.source_id for citation in evidence.citations] == ["src-a", "src-b"]
    assert GBRAIN_SOURCE not in {citation.source_id for citation in evidence.citations}
    assert {
        (
            citation.wiki_page,
            citation.page_id,
            citation.revision_id,
            citation.chunk_id,
            citation.origin,
        )
        for citation in evidence.citations
    } == {(PAGE_PATH, PAGE_ID, REVISION_ID, "42", "gbrain")}


@pytest.mark.parametrize(
    ("case", "expected_diagnostic"),
    [
        ("unmapped_slug", "unmapped"),
        ("wrong_namespace", "unmapped"),
        ("stale_mapping", "stale"),
        ("content_hash_mismatch", "stale"),
        ("generation_mismatch", "stale"),
        ("source_path_mismatch", "stale"),
        ("old_revision", "stale"),
        ("old_epoch", "stale"),
        ("invalid_page", "stale"),
        ("deleted_page", "stale"),
        ("missing_source", "unauthorized"),
        ("inactive_source", "unauthorized"),
        ("one_denied_source", "unauthorized"),
        ("empty_snippet", "unmapped"),
        ("ambiguous_mapping", "unmapped"),
    ],
)
def test_invalid_gbrain_mapping_never_yields_context(
    settings,
    reader,
    case,
    expected_diagnostic,
):
    hit = _valid_gbrain_hit()
    with connect_app_write(settings) as conn:
        if case == "unmapped_slug":
            hit = replace(hit, slug="product/faq/not-mapped")
        elif case == "wrong_namespace":
            hit = replace(hit, source_id="other", gbrain_source_id="other")
        elif case == "stale_mapping":
            conn.execute(
                "UPDATE gbrain_page_projections SET status='stale' WHERE id='gproj-1'"
            )
        elif case == "content_hash_mismatch":
            hit = replace(hit, content_hash="old-content-hash")
        elif case == "generation_mismatch":
            hit = replace(hit, page_generation=10)
        elif case == "source_path_mismatch":
            hit = replace(hit, source_path="product/faq/other.md")
        elif case == "old_revision":
            conn.execute(
                "UPDATE wiki_pages SET current_revision_id='wrev-new' WHERE page_id=?",
                (PAGE_ID,),
            )
        elif case == "old_epoch":
            conn.execute(
                "UPDATE wiki_pages SET projection_epoch=8 WHERE page_id=?",
                (PAGE_ID,),
            )
        elif case in {"invalid_page", "deleted_page"}:
            conn.execute(
                "UPDATE wiki_pages SET lifecycle_status=? WHERE page_id=?",
                (case.removesuffix("_page"), PAGE_ID),
            )
        elif case == "missing_source":
            conn.execute(
                "UPDATE wiki_page_revisions SET source_ids_json=? WHERE id=?",
                (json.dumps(["src-a", "src-missing"]), REVISION_ID),
            )
        elif case == "inactive_source":
            conn.execute("UPDATE sources SET status='inactive' WHERE id='src-b'")
        elif case == "one_denied_source":
            conn.execute(
                "UPDATE sources SET metadata_json=? WHERE id='src-b'",
                (json.dumps({"acl_tags": ["secret"]}),),
            )
        elif case == "empty_snippet":
            hit = replace(hit, snippet="   ")
        elif case == "ambiguous_mapping":
            conn.execute("DROP INDEX idx_gbrain_projection_slug_unique")
            conn.execute(
                """
                INSERT INTO gbrain_page_projections(
                  id,page_id,revision_id,projection_epoch,page_path,file_hash,
                  semantic_hash,gbrain_source_id,slug,source_path,
                  gbrain_content_hash,gbrain_page_generation,status,imported_at,
                  invalidated_at,last_job_id
                )
                SELECT 'gproj-ambiguous',page_id,revision_id,projection_epoch,
                  page_path,file_hash,semantic_hash,gbrain_source_id,slug,
                  source_path,gbrain_content_hash,gbrain_page_generation,'stale',
                  imported_at,invalidated_at,last_job_id
                FROM gbrain_page_projections WHERE id='gproj-1'
                """
            )

        evidence = map_gbrain_hit(conn, hit, reader)

    assert evidence.diagnostic == expected_diagnostic
    assert tuple(evidence.citations) == ()
    assert evidence.context == ""
    assert evidence.answer_part == ""


def test_valid_local_wiki_hit_emits_every_revision_source(settings, reader):
    hit = {
        "origin": "wiki",
        "id": "wiki-chunk-1",
        "page_id": PAGE_ID,
        "revision_id": REVISION_ID,
        "projection_epoch": 7,
        "page_path": PAGE_PATH,
        "title": "Demo",
        "snippet": "Current local Wiki evidence.",
        "text": "Current local Wiki evidence.",
    }

    with connect_app(settings) as conn:
        evidence = map_local_hit(conn, hit, reader)

    assert evidence.diagnostic == "mapped"
    assert [citation.source_id for citation in evidence.citations] == ["src-a", "src-b"]
    assert {citation.origin for citation in evidence.citations} == {"wiki"}
    assert {citation.chunk_id for citation in evidence.citations} == {"wiki-chunk-1"}


def test_local_wiki_hit_fails_closed_when_one_source_is_denied(settings, reader):
    with connect_app_write(settings) as conn:
        conn.execute(
            "UPDATE sources SET metadata_json=? WHERE id='src-b'",
            (json.dumps({"acl_tags": ["secret"]}),),
        )
        evidence = map_local_hit(
            conn,
            {
                "origin": "wiki",
                "id": "wiki-chunk-1",
                "page_id": PAGE_ID,
                "revision_id": REVISION_ID,
                "projection_epoch": 7,
                "page_path": PAGE_PATH,
                "title": "Demo",
                "snippet": "Must not leak.",
                "text": "Must not leak.",
            },
            reader,
        )

    assert evidence.diagnostic == "unauthorized"
    assert evidence.context == ""
    assert tuple(evidence.citations) == ()


def test_document_hit_maps_only_its_single_active_source(settings, reader):
    with connect_app(settings) as conn:
        evidence = map_local_hit(
            conn,
            {
                "origin": "document",
                "id": "document-chunk-1",
                "source_id": "src-a",
                "title": "Raw document",
                "snippet": "Raw source evidence.",
                "text": "Raw source evidence.",
            },
            reader,
        )

    assert evidence.diagnostic == "mapped"
    assert [citation.source_id for citation in evidence.citations] == ["src-a"]
    assert evidence.citations[0].origin == "document"
    assert evidence.citations[0].chunk_id == "document-chunk-1"


def test_all_sources_readable_requires_nonempty_active_authorized_sources(
    settings, reader
):
    with connect_app(settings) as conn:
        assert all_sources_readable(conn, ["src-a", "src-a"], reader) is True
        assert all_sources_readable(conn, [], reader) is False
        assert all_sources_readable(conn, ["src-a", "missing"], reader) is False


def test_citations_deduplicate_only_by_full_identity():
    first = Citation(
        source_id="src-a",
        wiki_page=PAGE_PATH,
        page_id=PAGE_ID,
        revision_id=REVISION_ID,
        chunk_id="chunk-1",
        origin="wiki",
        snippet="same snippet",
    )
    same = first.model_copy()
    other_source = first.model_copy(update={"source_id": "src-b"})
    other_snippet = first.model_copy(update={"snippet": "other snippet"})

    deduped = dedupe_citations([first, same, other_source, other_snippet])

    assert deduped == [first, other_source, other_snippet]


def test_ask_assembly_uses_only_mapped_gbrain_evidence(
    settings, reader, monkeypatch
):
    monkeypatch.setattr("app.search.search_chunks", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "app.search.query_gbrain",
        lambda *args, **kwargs: [_valid_gbrain_hit()],
    )

    assembly = build_ask_assembly(
        settings,
        AskRequest(question="What is the projected answer?", domain="product"),
        reader,
    )

    assert [citation.source_id for citation in assembly.citations] == ["src-a", "src-b"]
    assert len(assembly.gbrain_hits) == 1
    assert len(assembly.gbrain_context_blocks) == 1
    assert "Authorized projected answer" in assembly.gbrain_context_blocks[0]


def test_ask_assembly_maps_local_wiki_hit_without_treating_page_id_as_source(
    settings, reader, monkeypatch
):
    monkeypatch.setattr(
        "app.search.search_chunks",
        lambda *args, **kwargs: [
            {
                "origin": "wiki",
                "id": "wiki-chunk-1",
                "source_id": PAGE_ID,
                "source_ids": ["src-a", "src-b"],
                "page_id": PAGE_ID,
                "revision_id": REVISION_ID,
                "projection_epoch": 7,
                "page_path": PAGE_PATH,
                "title": "Demo",
                "snippet": "Current local Wiki evidence.",
                "text": "Current local Wiki evidence.",
                "score": 4.0,
            }
        ],
    )
    monkeypatch.setattr("app.search.query_gbrain", lambda *args, **kwargs: [])

    assembly = build_ask_assembly(
        settings,
        AskRequest(question="Current local Wiki evidence", domain="product"),
        reader,
    )

    assert [citation.source_id for citation in assembly.citations] == ["src-a", "src-b"]
    assert PAGE_ID not in {citation.source_id for citation in assembly.citations}
