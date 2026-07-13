# GBrain Projection and Citation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Wiki RAG and GBrain projection out of request handling, then admit local and GBrain evidence into answers only when it maps to the current revision, current projection epoch, and every authorized LGDO source.

**Architecture:** `raw/` and immutable `wiki_page_revisions` remain evidence; `vault/wiki` remains the editable source of truth. `ProjectionOutbox` leases immutable revision/epoch jobs to a lifespan worker: RAG writes epoch-isolated `wiki_chunks`, while GBrain batches call one authenticated in-process `lgdo_vault_sync` tool on the already-running PGLite/Postgres server. Citation assembly is a fail-closed projection consumer: no visible watermark or exact GBrain mapping means no factual context.

**Tech Stack:** Python 3.10+, FastAPI lifespan, Pydantic 2, SQLite/PostgreSQL compatibility layer, `httpx.AsyncClient`, pytest; Bun 1.3+, TypeScript 5.6, GBrain `BrainEngine`, MCP Streamable HTTP, PGLite, Bun test.

---

## Fixed Upstream Contract

The preceding revision plan owns `wiki_pages`, `wiki_page_revisions`, and `knowledge_projection_jobs`. Do not recreate those tables. Construct the repository as `ProjectionOutbox(settings: Settings)` and consume this exact public API from `app/projection_jobs.py`:

- `enqueue(conn, *, target: str, operation: str, page_id: str | None, revision_id: str | None, projection_epoch: int, payload: dict) -> str`
- `enqueue_pair_for_state(conn, page: dict, operation: str, payload: dict) -> list[str]`
- `claim(*, target: str, worker_id: str, limit: int, lease_seconds: int, now: datetime | None = None) -> list[ProjectionJob]`
- `renew_lease(job_id: str, worker_id: str, lease_seconds: int, now: datetime | None = None) -> bool`
- `finish_claimed(conn, *, job_id: str, worker_id: str, status: Literal["succeeded", "failed", "superseded"], last_error: str | None = None, available_at: str | None = None) -> bool`
- `mark_succeeded(job_id: str, worker_id: str) -> bool`
- `mark_failed(job_id: str, worker_id: str, last_error: str, now: datetime | None = None) -> bool`
- `supersede_stale(conn, page_id: str, projection_epoch: int) -> int`

`claim`, `renew_lease`, `mark_succeeded`, and `mark_failed` use the `Settings` captured by the constructor; callers never pass settings again. Every `pending`, `failed`, and expired-`running` branch in the claim SQL includes `attempts < 5`. `mark_failed` locks and reads the still-claimed row, derives the delay from its incremented `attempts`, and calls `finish_claimed` in the same `connect_app_write` transaction: attempts 1-4 use 5, 30, 120, and 600 seconds, while attempt 5 receives no next availability and remains terminally failed until the explicit retry API resets `attempts` to zero.

`finish_claimed` is the transaction boundary used below: mapping/watermark changes and job terminal state must commit together. A `False` return means lease ownership was lost; callers discard every late result.

## File Structure

- Create `app/wiki_rag_projection.py`: epoch-isolated Wiki chunk writer, visible-watermark CAS, and epoch-scoped garbage collection.
- Create `app/gbrain_projection.py`: trusted manifest models, async MCP projection client, batch/segment repository, result attribution, GBrain mapping/protection state, and persistent LGDO cache generation.
- Create `app/projection_worker.py`: process lease owner, RAG/GBrain loops, lease renewal, retry schedule, lifecycle, backlog/status queries.
- Create `app/citations.py`: all-source ACL gate, local/GBrain evidence mapping, citation deduplication, and post-generation revalidation.
- Modify `app/db.py`: add only projection-owned tables and indexes; register them in migration metadata.
- Modify `app/config.py`: separate query/projection credentials, source/root, lease, timeout, polling, and worker switches.
- Modify `app/models.py`: add Citation provenance fields and projection status response models.
- Modify `app/rag.py`: merge original `document_chunks` with current-visible `wiki_chunks` without weakening existing ranking.
- Modify `app/gbrain.py`: enrich hits, use the query token, include persistent projection generation in cache keys, and add no CLI fallback.
- Modify `app/search.py`: replace direct Vault fallback, map evidence before context creation, restrict memory, and enforce citations before and after LLM generation.
- Modify `app/api.py`: expose filtered projection job listing and failed-job retry.
- Modify `app/main.py`: own the projection worker in lifespan and report query/projection health separately.
- Modify `app/wiki.py`: remove the synchronous GBrain import call; return queued projection IDs supplied by the upstream revision transaction.
- Modify `pyproject.toml`: make `httpx` a runtime dependency and register the `gbrain_e2e` marker.
- Create `tests/conftest.py`: disable background projection by default for hermetic tests.
- Create `tests/test_projection_schema.py`, `tests/test_wiki_rag_projection.py`, `tests/test_gbrain_projection_client.py`, `tests/test_gbrain_projection_batches.py`, `tests/test_projection_worker.py`, `tests/test_citation_contract.py`, and `tests/test_projection_api.py`.
- Create `tests/integration/conftest.py` and `tests/integration/test_gbrain_pglite_projection.py`: shared real PGLite HTTP fixture and opt-in acceptance tests.
- Create `gbrain/src/core/lgdo-vault-sync.ts`: validated trusted-manifest sync, per-page CAS, rename compensation, deletion reconciliation, idempotency, and cooperative yielding.
- Modify `gbrain/src/core/import-file.ts`: include-deleted restore inside the existing import transaction and return content hash/generation.
- Modify `gbrain/src/core/engine.ts`, `gbrain/src/core/pglite-engine.ts`, and `gbrain/src/core/postgres-engine.ts`: affected-row rename contract and explicit generation bump.
- Modify `gbrain/src/core/operations.ts`: register `lgdo_vault_sync` and stamp query/search results with projection identity.
- Modify `gbrain/src/commands/serve-http.ts`: 1 MiB MCP body cap and source-bound projection authorization.
- Modify `gbrain/src/core/migrate.ts`, `gbrain/src/schema.sql`, `gbrain/src/core/schema-embedded.ts`, and `gbrain/src/core/pglite-schema.ts`: GBrain sync idempotency table and generation-trigger parity.
- Create `gbrain/test/lgdo-vault-sync.test.ts`, `gbrain/test/lgdo-vault-sync-operation.test.ts`, and `gbrain/test/e2e/lgdo-vault-sync-pglite.test.ts`.

### Task 1: Add Projection Schema, Settings, and Public Models

**Files:**
- Modify: `app/db.py`
- Modify: `app/config.py`
- Modify: `app/models.py`
- Modify: `pyproject.toml`
- Create: `tests/conftest.py`
- Create: `tests/test_projection_schema.py`

- [ ] **Step 1: Write failing schema and configuration tests**

```python
from app.config import Settings, get_settings
from app.db import connect_app, init_app_db
from app.models import Citation


def test_projection_schema_has_epoch_and_mapping_constraints(tmp_path):
    settings = Settings(database_path=tmp_path / "projection.db", database_backend="sqlite")
    init_app_db(settings)
    with connect_app(settings) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {
            "wiki_chunks",
            "gbrain_page_projections",
            "gbrain_projection_batches",
            "gbrain_projection_batch_jobs",
            "gbrain_projection_segments",
            "gbrain_projection_protections",
            "projection_state",
        } <= tables
        indexes = {row[1] for row in conn.execute("PRAGMA index_list('wiki_chunks')")}
        assert "idx_wiki_chunks_epoch_unique" in indexes


def test_projection_and_query_tokens_are_not_aliased():
    settings = Settings(gbrain_api_key="legacy-query", gbrain_projection_api_key=None)
    assert settings.gbrain_query_token == "legacy-query"
    assert settings.gbrain_projection_api_key is None


def test_citation_projection_identity_round_trips():
    citation = Citation(
        source_id="src_hr",
        wiki_page="wiki/product/faq/demo.md",
        snippet="Refunds are available for 30 days.",
        page_id="page_demo",
        revision_id="wrev_demo_3",
        chunk_id="wchunk_demo_0",
        origin="wiki",
    )
    restored = Citation.model_validate(citation.model_dump())
    assert restored == citation


def test_background_projection_is_disabled_by_default_in_tests():
    assert get_settings().projection_worker_enabled is False
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python -m pytest tests/test_projection_schema.py -q`

Expected: FAIL with `no such table: wiki_chunks` and `Settings` rejecting `gbrain_projection_api_key`.

- [ ] **Step 3: Add exact settings and model interfaces**

Add these settings; keep `gbrain_api_key` only as the query compatibility alias:

```python
class Settings(BaseSettings):
    gbrain_query_api_key: str | None = None
    gbrain_projection_api_key: str | None = None
    gbrain_managed_source_id: str | None = None
    gbrain_import_allowed_root: Path | None = None
    gbrain_incremental_timeout_seconds: int = 120
    gbrain_reconcile_timeout_seconds: int = 600
    projection_worker_enabled: bool = True
    projection_poll_seconds: float = 1.0
    projection_lease_seconds: int = 180
    projection_claim_limit: int = 500

    @property
    def gbrain_query_token(self) -> str | None:
        return self.gbrain_query_api_key or self.gbrain_api_key
```

Extend the model without removing compatible fields:

```python
class Citation(BaseModel):
    source_id: str
    wiki_page: str | None = None
    snippet: str
    page_id: str | None = None
    revision_id: str | None = None
    chunk_id: str | None = None
    origin: Literal["document", "wiki", "gbrain"] | None = None


class ProjectionJobResponse(BaseModel):
    id: str
    target: Literal["rag", "gbrain"]
    operation: str
    page_id: str | None
    revision_id: str | None
    projection_epoch: int
    status: str
    attempts: int
    available_at: str
    last_error: str | None


class CompileResponse(BaseModel):
    job_id: str
    created_pages: int
    updated_pages: int
    review_items: int
    projection_job_ids: list[str] = Field(default_factory=list)
    projection_status: Literal["queued"] = "queued"
```

- [ ] **Step 4: Add projection-owned SQL**

Add equivalent SQLite and PostgreSQL DDL. Use `TEXT` JSON for both through the compatibility layer and use `INTEGER` for epochs/generations:

```sql
CREATE TABLE IF NOT EXISTS wiki_chunks (
  id TEXT PRIMARY KEY,
  page_id TEXT NOT NULL,
  revision_id TEXT NOT NULL,
  projection_epoch INTEGER NOT NULL,
  chunk_index INTEGER NOT NULL,
  page_path TEXT NOT NULL,
  domain TEXT NOT NULL,
  title TEXT NOT NULL,
  text TEXT NOT NULL,
  token_json TEXT NOT NULL DEFAULT '[]',
  embedding_json TEXT NOT NULL DEFAULT '[]',
  embedding_model TEXT NOT NULL,
  source_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wiki_chunks_epoch_unique
ON wiki_chunks(page_id, revision_id, projection_epoch, chunk_index);
CREATE INDEX IF NOT EXISTS idx_wiki_chunks_domain_epoch
ON wiki_chunks(domain, page_id, projection_epoch);

CREATE TABLE IF NOT EXISTS gbrain_page_projections (
  id TEXT PRIMARY KEY,
  page_id TEXT NOT NULL,
  revision_id TEXT NOT NULL,
  projection_epoch INTEGER NOT NULL,
  page_path TEXT NOT NULL,
  file_hash TEXT NOT NULL,
  semantic_hash TEXT NOT NULL,
  gbrain_source_id TEXT NOT NULL,
  slug TEXT NOT NULL,
  source_path TEXT NOT NULL,
  gbrain_content_hash TEXT NOT NULL,
  gbrain_page_generation INTEGER NOT NULL,
  status TEXT NOT NULL,
  imported_at TEXT,
  invalidated_at TEXT,
  last_job_id TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gbrain_projection_slug_unique
ON gbrain_page_projections(gbrain_source_id, slug);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gbrain_projection_current_page
ON gbrain_page_projections(gbrain_source_id, page_id) WHERE status = 'current';

CREATE TABLE IF NOT EXISTS gbrain_projection_batches (
  id TEXT PRIMARY KEY,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  batch_watermark_json TEXT NOT NULL,
  included_snapshot_json TEXT NOT NULL,
  lease_owner TEXT NOT NULL,
  lease_expires_at TEXT NOT NULL,
  result_json TEXT NOT NULL DEFAULT '{}',
  last_error TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gbrain_projection_batch_jobs (
  batch_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  page_id TEXT,
  revision_id TEXT,
  projection_epoch INTEGER NOT NULL,
  operation TEXT NOT NULL,
  PRIMARY KEY(batch_id, job_id)
);
CREATE TABLE IF NOT EXISTS gbrain_projection_segments (
  id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL,
  segment_index INTEGER NOT NULL,
  mode TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  expected_pages_json TEXT NOT NULL,
  protected_mappings_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL,
  lease_owner TEXT NOT NULL,
  lease_expires_at TEXT NOT NULL,
  result_json TEXT NOT NULL DEFAULT '{}',
  last_error TEXT,
  started_at TEXT,
  finished_at TEXT,
  UNIQUE(batch_id, segment_index)
);
CREATE TABLE IF NOT EXISTS gbrain_projection_protections (
  id TEXT PRIMARY KEY,
  gbrain_source_id TEXT NOT NULL,
  page_id TEXT,
  slug TEXT,
  source_path TEXT,
  reason TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_gbrain_active_protections
ON gbrain_projection_protections(gbrain_source_id, active);
CREATE TABLE IF NOT EXISTS projection_state (
  key TEXT PRIMARY KEY,
  value INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
```

Seed `projection_state('gbrain_projection_generation', 0, now)` idempotently. Add all seven tables to `MAIN_TABLES` and `TABLE_PRIMARY_KEYS`.

- [ ] **Step 5: Add hermetic pytest defaults**

Create `tests/conftest.py` with an autouse fixture named `disable_background_integrations`. It monkeypatches the cached global settings to `projection_worker_enabled=False` and `gbrain_enabled=False`; later Vault work extends this same fixture with `vault_watch_enabled=False`.

- [ ] **Step 6: Run GREEN and both database schema checks**

Run: `python -m pytest tests/test_projection_schema.py tests/test_postgres_metadata_backend.py -q`

Expected: PASS; SQLite introspection sees every table/index and PostgreSQL compatibility flow exits without DDL conversion errors.

- [ ] **Step 7: Commit**

```bash
git add app/db.py app/config.py app/models.py pyproject.toml tests/conftest.py tests/test_projection_schema.py
git commit -m "feat: add projection persistence contracts"
```

### Task 2: Implement Epoch-Isolated Wiki RAG Projection

**Files:**
- Create: `app/wiki_rag_projection.py`
- Modify: `app/rag.py`
- Create: `tests/test_wiki_rag_projection.py`
- Modify: `tests/test_pg_rag_store.py`

- [ ] **Step 1: Write RED tests for physical epoch isolation**

Create fixtures with one `wiki_pages` row and two `wiki_page_revisions` rows. Test these cases explicitly:

```python
def test_lost_old_epoch_cannot_flip_or_delete_new_epoch(projector, seeded_page):
    old = seeded_page.job(revision_id="wrev_old", projection_epoch=4)
    new = seeded_page.job(revision_id="wrev_new", projection_epoch=5)
    projector.write_physical_rows(old, worker_id="old-owner")
    projector.project(new, worker_id="new-owner")
    projector.cleanup_epoch(old.page_id, old.projection_epoch)
    assert seeded_page.visible() == ("wrev_new", 5)
    assert seeded_page.chunk_epochs() == [5]


def test_same_revision_new_epoch_requires_new_rows(projector, seeded_page):
    projector.project(seeded_page.job(revision_id="wrev_same", projection_epoch=7), "owner-a")
    projector.project(seeded_page.job(revision_id="wrev_same", projection_epoch=8), "owner-b")
    assert seeded_page.visible() == ("wrev_same", 8)
    assert seeded_page.chunk_epochs() == [7, 8]
```

Add tests for a failed embedding leaving both visible fields `NULL`, source IDs copied from the immutable revision, and `finish_claimed=False` preventing a late watermark update.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_wiki_rag_projection.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'app.wiki_rag_projection'`.

- [ ] **Step 3: Implement the projector public surface**

```python
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


class WikiRagProjector:
    def __init__(self, settings: Settings, outbox: ProjectionOutbox):
        self.settings = settings
        self.outbox = outbox

    def project(self, job: ProjectionJob, worker_id: str) -> RagProjectionResult:
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
        rows = [self._build_row(snapshot, job.projection_epoch, index, chunk) for index, chunk in enumerate(chunks)]
        self._write_physical_rows(rows)
        superseded = False
        with connect_app_write(self.settings) as conn:
            page = conn.execute(
                "SELECT * FROM wiki_pages WHERE page_id = ?",
                (job.page_id,),
            ).fetchone()
            if (
                page is None
                or page["current_revision_id"] != job.revision_id
                or int(page["projection_epoch"]) != job.projection_epoch
                or page["lifecycle_status"] != "active"
            ):
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
                    SET rag_visible_revision_id = ?, rag_visible_epoch = ?
                    WHERE page_id = ? AND current_revision_id = ?
                      AND projection_epoch = ? AND lifecycle_status = 'active'
                    """,
                    (job.revision_id, job.projection_epoch, job.page_id, job.revision_id, job.projection_epoch),
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
        return RagProjectionResult(job.id, job.page_id, job.revision_id, job.projection_epoch, len(rows))
```

`_load_snapshot` must join `wiki_pages.current_revision_id` to `wiki_page_revisions.id` and reject a page/revision/epoch mismatch before embedding. `_build_row` uses `sha256(f"{page_id}:{revision_id}:{epoch}:{index}")`, stores the revision's `source_ids_json`, and calls `embed_text_with_model`. `_write_physical_rows` upserts only the exact `(page_id, revision_id, projection_epoch, chunk_index)` key. `cleanup_epoch(page_id, projection_epoch)` executes only `DELETE FROM wiki_chunks WHERE page_id=? AND projection_epoch=?` after proving no running job references that epoch.

- [ ] **Step 4: Merge visible Wiki chunks into retrieval**

Add `load_visible_wiki_rows(conn, domain)` with this mandatory join predicate:

```sql
SELECT wc.*
FROM wiki_chunks wc
JOIN wiki_pages wp ON wp.page_id = wc.page_id
WHERE wp.lifecycle_status = 'active'
  AND wp.current_revision_id = wc.revision_id
  AND wp.rag_visible_revision_id = wc.revision_id
  AND wp.rag_visible_epoch = wc.projection_epoch
  AND wp.projection_epoch = wc.projection_epoch
  AND (? IS NULL OR wc.domain = ?)
```

Normalize rows with `origin='wiki'`, `source_ids`, `page_id`, `revision_id`, `projection_epoch`, and `page_path`. Keep original `document_chunks` with `origin='document'`. In `search_chunks`, the PostgreSQL branch must assign `search_pg_chunks(settings, question, domain, max(limit * 8, 40), keyword_weight=keyword_weight, vector_weight=vector_weight, user_context=user_context, alias_context=alias_context)` to `document_candidates` and continue; it must not return early. Load visible Wiki rows through the LGDO metadata connection for both backends, merge them with the SQLite or PostgreSQL document candidates, apply ACL filtering, and only then call `rank_search_rows` and `diversify_ranked_rows`. For equal source/content identity prefer current Wiki rows, but never delete or hide an original source chunk. Add a PostgreSQL regression assertion that one document candidate and one visible Wiki candidate both reach the final ranker.

- [ ] **Step 5: Run GREEN and regression tests**

Run: `python -m pytest tests/test_wiki_rag_projection.py tests/test_rag_retrieval.py tests/test_rag_reranking.py tests/test_pg_rag_store.py -q`

Expected: PASS; the old epoch cleanup test retains epoch 5 and existing document retrieval/reranking assertions stay green.

- [ ] **Step 6: Commit**

```bash
git add app/wiki_rag_projection.py app/rag.py tests/test_wiki_rag_projection.py tests/test_pg_rag_store.py
git commit -m "feat: project wiki chunks by revision epoch"
```

### Task 3: Strengthen GBrain Engine Import and Generation Contracts

**Files:**
- Modify: `gbrain/src/core/engine.ts`
- Modify: `gbrain/src/core/pglite-engine.ts`
- Modify: `gbrain/src/core/postgres-engine.ts`
- Modify: `gbrain/src/core/import-file.ts`
- Modify: `gbrain/src/core/migrate.ts`
- Modify: `gbrain/src/schema.sql`
- Modify: `gbrain/src/core/schema-embedded.ts`
- Modify: `gbrain/src/core/pglite-schema.ts`
- Modify: `gbrain/test/import-file.test.ts`
- Modify: `gbrain/test/source-id-tx-regression.test.ts`
- Create: `gbrain/test/e2e/lgdo-vault-sync-pglite.test.ts`

- [ ] **Step 1: Write RED engine tests**

Add assertions that `updateSlug` returns `true` for exactly one source-scoped row and `false` for zero rows, `bumpPageGeneration` strictly increases the page generation, and `importFromContent(engine, slug, markdown, {sourceId, restoreDeleted:true, forceRechunk:true, forceGenerationBump:true})` restores and refreshes a tombstone atomically. Force `upsertChunks` to throw and assert `deleted_at` remains non-null after rollback.

- [ ] **Step 2: Run tests and verify RED**

Run: `cd gbrain && bun test test/import-file.test.ts test/source-id-tx-regression.test.ts test/e2e/lgdo-vault-sync-pglite.test.ts`

Expected: TypeScript compile failures because `updateSlug` returns `void`, `bumpPageGeneration` is absent, and import options reject `restoreDeleted`.

- [ ] **Step 3: Change the exact engine interface**

```typescript
export interface BrainEngine {
  updateSlug(
    oldSlug: string,
    newSlug: string,
    opts?: { sourceId?: string },
  ): Promise<boolean>;
  bumpPageGeneration(slug: string, opts: { sourceId: string }): Promise<number>;
}
```

PGLite uses `UPDATE pages SET slug = $1, updated_at = now() WHERE slug = $2 AND source_id = $3 RETURNING slug` and `UPDATE pages SET generation = generation + 1 WHERE slug = $1 AND source_id = $2 RETURNING generation`; PostgreSQL uses the same source-qualified predicates through `postgres.js`. Preserve the existing `opts?.sourceId ?? 'default'` behavior for non-LGDO callers. Both implementations return `false` for a zero-row rename; `bumpPageGeneration` throws on zero rows. Every call from `lgdo_vault_sync` must pass `{sourceId: input.source_id}`.

- [ ] **Step 4: Extend import options without adding nested transactions**

Add these options and result fields:

```typescript
export interface ImportResult {
  slug: string;
  status: 'imported' | 'skipped' | 'error';
  chunks: number;
  error?: string;
  parsedPage?: ParsedPage;
  quarantined?: boolean;
  flagged?: boolean;
  flag_reason?: 'markup_heavy' | 'oversized';
  content_hash?: string;
  page_generation?: number;
}

export interface LgdoImportOptions {
  restoreDeleted?: boolean;
  forceGenerationBump?: boolean;
}
```

Merge `LgdoImportOptions` into the existing `importFromContent` options. Load `existing` with `includeDeleted: opts.restoreDeleted === true`. Inside the function's existing `engine.transaction`, before `createVersion` and `putPage`, call `tx.restorePage(slug,{sourceId})` when `existing.deleted_at` is set and require `true`. At the end of that same transaction call `tx.bumpPageGeneration` when requested, assign the returned number, and return `content_hash` plus `page_generation`. Embeddings remain outside the transaction, so restore begins only after embedding preparation succeeds.

- [ ] **Step 5: Make slug/path changes advance per-page generation**

Create migration version 120 and update all three bootstrap schema sources. Extend `bump_page_generation_fn` with `OLD.slug IS DISTINCT FROM NEW.slug` and `OLD.source_path IS DISTINCT FROM NEW.source_path`. Add this idempotency table in the same migration:

```sql
CREATE TABLE IF NOT EXISTS lgdo_vault_sync_runs (
  source_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  result_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY(source_id, idempotency_key)
);
```

- [ ] **Step 6: Run GREEN, engine parity, and typecheck**

Run: `cd gbrain && bun test test/import-file.test.ts test/source-id-tx-regression.test.ts test/e2e/lgdo-vault-sync-pglite.test.ts test/e2e/engine-parity.test.ts`

Expected: PASS, including tombstone rollback and zero-row rename tests.

Run: `cd gbrain && bun run typecheck`

Expected: exit 0 with no `BrainEngine.updateSlug` signature errors.

- [ ] **Step 7: Commit**

```bash
git add gbrain/src/core/engine.ts gbrain/src/core/pglite-engine.ts gbrain/src/core/postgres-engine.ts gbrain/src/core/import-file.ts gbrain/src/core/migrate.ts gbrain/src/schema.sql gbrain/src/core/schema-embedded.ts gbrain/src/core/pglite-schema.ts gbrain/test/import-file.test.ts gbrain/test/source-id-tx-regression.test.ts gbrain/test/e2e/lgdo-vault-sync-pglite.test.ts
git commit -m "feat(gbrain): add verifiable restore and rename contracts"
```

### Task 4: Build the Trusted-Manifest `lgdo_vault_sync` Core

**Files:**
- Create: `gbrain/src/core/lgdo-vault-sync.ts`
- Create: `gbrain/test/lgdo-vault-sync.test.ts`
- Modify: `gbrain/test/e2e/lgdo-vault-sync-pglite.test.ts`

- [ ] **Step 1: Write RED contract tests**

Cover all of these named cases in `lgdo-vault-sync.test.ts`: canonical root mismatch; root outside `GBRAIN_IMPORT_ALLOWED_ROOTS`; non-managed source; source/projection-client mismatch; more than 100 expected pages; duplicate page IDs/paths; absolute/traversal path; symlink file or symlink ancestor; file larger than 5 MiB; mismatched `id`, `lgdo_page_id`, `lgdo_revision_id`, or raw hash; a valid unlisted Markdown file in incremental and reconcile; parent `.gitignore`; raw bytes changing between pre/post hash; empty incremental rejection; empty reconcile deletion; per-page deterministic error; recovery-required mapping protected from deletion.

Add real PGLite cases for delete then `restorePage` then force reimport, rename refresh failure compensation, compensation failure returning `recovery_required`, and both recovery mappings surviving reconcile.

- [ ] **Step 2: Run tests and verify RED**

Run: `cd gbrain && bun test test/lgdo-vault-sync.test.ts test/e2e/lgdo-vault-sync-pglite.test.ts`

Expected: FAIL because `src/core/lgdo-vault-sync.ts` does not exist.

- [ ] **Step 3: Define the exact MCP-domain types**

```typescript
export type LgdoVaultSyncMode = 'incremental' | 'reconcile';
export type LgdoPageStatus = 'imported' | 'skipped' | 'error' | 'superseded' | 'recovery_required';

export interface LgdoExpectedPage {
  page_id: string;
  revision_id: string;
  projection_epoch: number;
  path: string;
  file_hash: string;
}

export interface LgdoProtectedMapping {
  source_id: string;
  slug?: string;
  source_path?: string;
  reason: string;
}

export interface LgdoVaultSyncInput {
  source_id: string;
  root: string;
  mode: LgdoVaultSyncMode;
  expected_pages: LgdoExpectedPage[];
  protected_mappings: LgdoProtectedMapping[];
  no_embed: boolean;
  idempotency_key: string;
}

export interface LgdoPageResult extends LgdoExpectedPage {
  source_id: string;
  slug: string | null;
  source_path: string;
  raw_file_hash_before: string | null;
  raw_file_hash_after: string | null;
  content_hash: string | null;
  page_generation: number | null;
  status: LgdoPageStatus;
  error: string | null;
  protected_mappings: LgdoProtectedMapping[];
}

export interface LgdoVaultSyncResult {
  source_id: string;
  mode: LgdoVaultSyncMode;
  idempotency_key: string;
  pages: LgdoPageResult[];
  deleted: Array<{source_id: string; slug: string}>;
  protected_mappings: LgdoProtectedMapping[];
  imported: number;
  skipped: number;
  errors: number;
  chunks: number;
  duration_ms: number;
}
```

Path ownership is exact: LGDO persists Vault-relative page paths such as `wiki/product/faq/demo.md`; the managed GBrain source root is the canonical `<vault_path>/wiki`; every MCP `expected_pages[].path`, result `path`, and `source_path` is root-relative and therefore equals `product/faq/demo.md`. The Python client strips exactly one leading `wiki/`; the TypeScript tool rejects an empty path, a path still beginning with `wiki/`, backslashes, absolute paths, `.` segments, and `..` segments. It never strips another component or accepts a caller-supplied absolute file path.

- [ ] **Step 4: Implement source/root and file-CAS validation**

`validateManagedSource(engine, ctx, input)` must use `fetchSource`, parse `sources.config`, and require all of these values: `config.lgdo_managed === true`, non-empty `config.lgdo_projection_client_id`, `ctx.auth.clientId === config.lgdo_projection_client_id`, `ctx.sourceId === input.source_id`, `ctx.auth.sourceId === input.source_id`, source not archived, and canonical `input.root === source.local_path`. Parse `GBRAIN_IMPORT_ALLOWED_ROOTS` with `delimiter`, realpath every root, and require exact canonical membership.

For each manifest path, require the root-relative contract above, resolve under root, lstat every path component, require a regular non-symlink file, stream SHA-256 with a 5 MiB cap, decode strict UTF-8, and parse Markdown. Require both frontmatter IDs to equal `page_id`, `lgdo_revision_id` to equal the manifest revision, and the pre-hash to equal `file_hash`. Never use a caller-provided slug; derive it with `slugifyPath` and enforce the existing path-authoritative slug rule. Every returned `path` and `source_path` must equal the validated root-relative manifest path.

- [ ] **Step 5: Implement restore, rename, compensation, and verification**

Use an exact source-scoped external-ID lookup (`frontmatter->>'id'`) that includes deleted rows and rejects multiple matches. The per-page state machine is:

```typescript
const old = await findPageByExternalId(engine, input.source_id, expected.page_id, true);
const newSlug = resolveLgdoSlug(expected.path, markdown);
let renamedFrom: string | null = null;
if (old && old.slug !== newSlug) {
  const moved = await engine.updateSlug(old.slug, newSlug, { sourceId: input.source_id });
  if (!moved) throw new Error(`rename affected zero rows: ${old.slug}`);
  renamedFrom = old.slug;
}
try {
  const imported = await importFromContent(engine, newSlug, markdown, {
    sourceId: input.source_id,
    sourcePath: expected.path,
    filename: basename(expected.path, '.md'),
    noEmbed: input.no_embed,
    forceRechunk: true,
    restoreDeleted: true,
    forceGenerationBump: true,
  });
  const afterHash = await sha256RegularFile(absolutePath, 5_000_000);
  if (afterHash !== expected.file_hash) throw new SupersededFileError(afterHash);
  return await verifyImportedIdentity(engine, expected, input.source_id, newSlug, imported, afterHash);
} catch (error) {
  if (renamedFrom !== null) {
    const compensated = await engine.updateSlug(newSlug, renamedFrom, { sourceId: input.source_id });
    const restored = compensated
      ? await verifyExternalIdentity(engine, input.source_id, expected.page_id, renamedFrom)
      : false;
    if (!restored) return recoveryRequiredResult(expected, input.source_id, renamedFrom, newSlug, error);
  }
  return failedPageResult(expected, input.source_id, newSlug, error);
}
```

`verifyImportedIdentity` must compare unique external ID, source ID, actual slug, `source_path`, GBrain content hash, non-null generation, `deleted_at IS NULL`, and post-hash. A zero-row forward or compensation rename is failure. Restore, force import, and row verification stay transactional through Task 3's import contract; a failure keeps the tombstone with `deleted_at IS NOT NULL`.

- [ ] **Step 6: Implement safe reconcile and idempotency**

The directory walker builds only a presence set of regular `.md` paths and a protected set for present-but-unlisted/invalid paths. It never imports walker discoveries. Reconcile lists existing pages only in `input.source_id`, and deletes a slug only when its `source_path` is absent from presence and neither slug nor source path appears in input, deterministic-error, or persisted recovery protection. Delete in bounded batches and yield with `await Bun.sleep(0)` after each page and delete batch.

Hash the canonical request. Before execution, read `lgdo_vault_sync_runs`; same key/same hash returns stored output, same key/different hash returns `idempotency_conflict`. Store only complete structured results after processing. Protect all old/new mappings for `recovery_required`.

- [ ] **Step 7: Run GREEN and focused PGLite tests**

Run: `cd gbrain && bun test test/lgdo-vault-sync.test.ts test/e2e/lgdo-vault-sync-pglite.test.ts`

Expected: PASS, including restore visibility, rename rollback, unlisted-file non-import, empty reconcile, and recovery protection.

- [ ] **Step 8: Commit**

```bash
git add gbrain/src/core/lgdo-vault-sync.ts gbrain/test/lgdo-vault-sync.test.ts gbrain/test/e2e/lgdo-vault-sync-pglite.test.ts
git commit -m "feat(gbrain): add trusted LGDO vault synchronization"
```

### Task 5: Expose the Authenticated MCP Tool and Projection Metadata

**Files:**
- Modify: `gbrain/src/core/operations.ts`
- Modify: `gbrain/src/commands/serve-http.ts`
- Create: `gbrain/test/lgdo-vault-sync-operation.test.ts`
- Modify: `gbrain/test/e2e/lgdo-vault-sync-pglite.test.ts`

- [ ] **Step 1: Write RED operation/auth/cache tests**

Assert the operation is `mutating: true`, `scope: 'write'`, and absent from local CLI aliases. Exercise authorization with a read-only query token, a write token bound to another source, the dedicated projection client, wrong root, and request body over 1 MiB. Cache a query result containing an old snippet, run a successful import, and assert the next query returns the new snippet and new page generation.

- [ ] **Step 2: Run tests and verify RED**

Run: `cd gbrain && bun test test/lgdo-vault-sync-operation.test.ts test/e2e/lgdo-vault-sync-pglite.test.ts`

Expected: FAIL with `op registered: lgdo_vault_sync` missing and read-token authorization not testable through a shared helper.

- [ ] **Step 3: Register the operation**

```typescript
const lgdo_vault_sync: Operation = {
  name: 'lgdo_vault_sync',
  description: 'Synchronize a source-bound LGDO Vault from a trusted revision manifest.',
  mutating: true,
  scope: 'write',
  params: {
    source_id: {type: 'string', required: true},
    root: {type: 'string', required: true},
    mode: {type: 'string', required: true, enum: ['incremental', 'reconcile']},
    expected_pages: {type: 'array', required: true, items: {type: 'object'}},
    protected_mappings: {type: 'array', required: true, items: {type: 'object'}},
    no_embed: {type: 'boolean', required: true},
    idempotency_key: {type: 'string', required: true},
  },
  handler: async (ctx, params) => runLgdoVaultSync(ctx, params as unknown as LgdoVaultSyncInput),
};
```

Append it to `operations`. Keep the process-wide async mutex in `lgdo-vault-sync.ts`, so all transports serialize this one write operation while search/query continue between cooperative yields.

- [ ] **Step 4: Make HTTP authorization and body limits testable**

Export and use one helper from `serve-http.ts`:

```typescript
export function authorizeMcpOperation(auth: AuthInfo, op: Operation): {ok: true} | {ok: false; message: string} {
  const required = op.scope ?? 'read';
  if (!hasScope(auth.scopes, required)) return {ok: false, message: `requires '${required}'`};
  if (op.name === 'lgdo_vault_sync' && (!auth.sourceId || !auth.clientId)) {
    return {ok: false, message: 'projection token must be source-bound'};
  }
  return {ok: true};
}
```

Apply `express.json({limit:'1mb'})` specifically to `POST /mcp` before bearer auth and return a JSON 413 envelope. Keep request logs redacted; only declared key names and bucketed size may be logged.

- [ ] **Step 5: Stamp search/query hits with current GBrain identity**

Add a batched `stampProjectionIdentity(engine, rows)` helper in `operations.ts`. Group by source, query `pages` for `(source_id, slug, source_path, content_hash, generation, deleted_at)`, and add `source_path`, `content_hash`, and `page_generation` only for active exact matches. Call it on both `search` and `query` result arrays after cache retrieval and before serialization. This metadata is diagnostic identity; it never becomes an LGDO source ID.

- [ ] **Step 6: Run GREEN, cache regression, and typecheck**

Run: `cd gbrain && bun test test/lgdo-vault-sync-operation.test.ts test/e2e/lgdo-vault-sync-pglite.test.ts test/e2e/cache-gate-pglite.test.ts`

Expected: PASS; the query token gets `insufficient_scope`, projection token imports only its bound source, and cached old content is not served.

Run: `cd gbrain && bun run typecheck`

Expected: exit 0.

- [ ] **Step 7: Commit**

```bash
git add gbrain/src/core/operations.ts gbrain/src/commands/serve-http.ts gbrain/test/lgdo-vault-sync-operation.test.ts gbrain/test/e2e/lgdo-vault-sync-pglite.test.ts
git commit -m "feat(gbrain): expose source-bound LGDO MCP sync"
```

### Task 6: Add the Async LGDO Projection Client and Persistent Query Generation

**Files:**
- Create: `app/gbrain_projection.py`
- Modify: `app/gbrain.py`
- Create: `tests/test_gbrain_projection_client.py`

- [ ] **Step 1: Write RED client and cache-generation tests**

Test projection calls use only `gbrain_projection_api_key`; a legacy/query key alone raises `ProjectionConfigurationError`; incremental timeout is 120 seconds; reconcile timeout is 600 seconds; MCP `isError`, network timeout, malformed JSON, missing per-page results, and aggregate success with a page error are distinguishable. Assert `to_gbrain_manifest_path("wiki/product/faq/demo.md") == "product/faq/demo.md"` and reject a missing `wiki/` prefix, an empty suffix, backslashes, absolute paths, traversal, `wiki/product/./demo.md`, and `wiki/product//demo.md`. Create two local cache instances, bump `projection_state` in one database transaction, and assert both produce a new cache key.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_gbrain_projection_client.py -q`

Expected: FAIL because `app.gbrain_projection` is absent and `GBrainHit` lacks projection identity fields.

- [ ] **Step 3: Define Python transport/result interfaces**

```python
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any, Literal


class ProjectionConfigurationError(RuntimeError):
    """Projection endpoint, credential, source, or root configuration is invalid."""


class ProjectionNetworkError(RuntimeError):
    """The HTTP MCP exchange failed before a trustworthy result was decoded."""


class ProjectionProtocolError(RuntimeError):
    """The MCP envelope or lgdo_vault_sync payload violated the locked contract."""


class ProjectionToolError(RuntimeError):
    """The MCP server returned a structured tool error."""


def to_gbrain_manifest_path(page_path: str) -> str:
    prefix = "wiki/"
    if "\\" in page_path or not page_path.startswith(prefix):
        raise ProjectionProtocolError("wiki page path must begin with 'wiki/'")
    relative = page_path[len(prefix):]
    raw_parts = relative.split("/")
    if not relative or relative.startswith("/") or any(part in {"", ".", ".."} for part in raw_parts):
        raise ProjectionProtocolError("wiki page path is not a safe GBrain source path")
    parts = PurePosixPath(relative).parts
    if "/".join(parts) != relative:
        raise ProjectionProtocolError("wiki page path is not a safe GBrain source path")
    return relative


@dataclass(frozen=True)
class ExpectedPage:
    page_id: str
    revision_id: str
    projection_epoch: int
    path: str
    file_hash: str


@dataclass(frozen=True)
class ProtectedMapping:
    source_id: str
    slug: str | None
    source_path: str | None
    reason: str


@dataclass(frozen=True)
class GBrainSyncRequest:
    source_id: str
    root: str
    mode: Literal["incremental", "reconcile"]
    expected_pages: Sequence[ExpectedPage]
    protected_mappings: Sequence[ProtectedMapping]
    no_embed: bool
    idempotency_key: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "root": self.root,
            "mode": self.mode,
            "expected_pages": [asdict(page) for page in self.expected_pages],
            "protected_mappings": [asdict(mapping) for mapping in self.protected_mappings],
            "no_embed": self.no_embed,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True)
class GBrainPageSyncResult:
    page_id: str
    revision_id: str
    projection_epoch: int
    path: str
    source_id: str
    slug: str | None
    source_path: str
    raw_file_hash_before: str | None
    raw_file_hash_after: str | None
    content_hash: str | None
    page_generation: int | None
    status: str
    error: str | None
    protected_mappings: Sequence[ProtectedMapping]


@dataclass(frozen=True)
class GBrainDeletedResult:
    source_id: str
    slug: str


@dataclass(frozen=True)
class GBrainSyncResponse:
    source_id: str
    mode: Literal["incremental", "reconcile"]
    idempotency_key: str
    pages: Sequence[GBrainPageSyncResult]
    deleted: Sequence[GBrainDeletedResult]
    protected_mappings: Sequence[ProtectedMapping]
    imported: int
    skipped: int
    errors: int
    chunks: int
    duration_ms: float

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "GBrainSyncResponse":
        mode = payload["mode"]
        if mode not in {"incremental", "reconcile"}:
            raise ValueError(f"invalid sync mode: {mode}")
        pages: list[GBrainPageSyncResult] = []
        for item in payload["pages"]:
            page = dict(item)
            page["protected_mappings"] = tuple(
                ProtectedMapping(**mapping)
                for mapping in page["protected_mappings"]
            )
            pages.append(GBrainPageSyncResult(**page))
        return cls(
            source_id=str(payload["source_id"]),
            mode=mode,
            idempotency_key=str(payload["idempotency_key"]),
            pages=tuple(pages),
            deleted=tuple(GBrainDeletedResult(**item) for item in payload["deleted"]),
            protected_mappings=tuple(
                ProtectedMapping(**item)
                for item in payload["protected_mappings"]
            ),
            imported=int(payload["imported"]),
            skipped=int(payload["skipped"]),
            errors=int(payload["errors"]),
            chunks=int(payload["chunks"]),
            duration_ms=float(payload["duration_ms"]),
        )


class GBrainProjectionClient:
    def __init__(self, settings: Settings):
        if not settings.gbrain_endpoint or not settings.gbrain_projection_api_key:
            raise ProjectionConfigurationError("GBrain projection endpoint/token is not configured")
        self.settings = settings
        self.root = (settings.vault_path / "wiki").resolve()

    async def sync(self, request: GBrainSyncRequest) -> GBrainSyncResponse:
        timeout = (
            self.settings.gbrain_reconcile_timeout_seconds
            if request.mode == "reconcile"
            else self.settings.gbrain_incremental_timeout_seconds
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            await self._initialize(client)
            payload = await self._call(client, "lgdo_vault_sync", request.to_payload())
        try:
            return GBrainSyncResponse.from_payload(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectionProtocolError(f"invalid lgdo_vault_sync result: {exc}") from exc
```

Build every `ExpectedPage.path` with `to_gbrain_manifest_path(wiki_pages.path)`, set `GBrainSyncRequest.root` to `str(client.root)`, and require returned `path/source_path` to equal the converted value before attribution. `GBrainSyncResponse.from_payload` is the only structured-result parser: required aggregate keys use indexed lookup, page/deletion/protection entries are converted to the exact dataclasses above, and any missing required field, extra per-page field, wrong shape, or invalid numeric conversion becomes `ProjectionProtocolError` at the client boundary. `_initialize` and `_call` send bearer `gbrain_projection_api_key`, parse JSON or SSE MCP envelopes, and raise typed `ProjectionNetworkError`, `ProjectionProtocolError`, or `ProjectionToolError`. There is no subprocess or `import_vault_to_gbrain` fallback.

- [ ] **Step 4: Enrich query hits and split credentials**

Extend `GBrainHit` with `gbrain_source_id`, `source_path`, `content_hash`, and `page_generation`; retain `source_id` as a compatibility alias for the GBrain namespace only. `normalize_gbrain_hits` must never call `infer_gbrain_doc_code` to replace this identity. `call_gbrain_tool` uses `settings.gbrain_query_token`.

Read `projection_state.gbrain_projection_generation` in `_query_cache_key`. Mapping current/stale/delete transactions call:

```python
def bump_gbrain_projection_generation(conn, timestamp: str) -> int:
    conn.execute(
        "UPDATE projection_state SET value = value + 1, updated_at = ? WHERE key = ?",
        (timestamp, "gbrain_projection_generation"),
    )
    row = conn.execute(
        "SELECT value FROM projection_state WHERE key = ?",
        ("gbrain_projection_generation",),
    ).fetchone()
    return int(row["value"])
```

- [ ] **Step 5: Run GREEN and existing GBrain unit tests**

Run: `python -m pytest tests/test_gbrain_projection_client.py tests/test_gbrain_integration.py -q`

Expected: PASS; existing candidate/ranking tests remain green and query-cache keys change after a persisted generation bump.

- [ ] **Step 6: Commit**

```bash
git add app/gbrain_projection.py app/gbrain.py tests/test_gbrain_projection_client.py
git commit -m "feat: add async GBrain projection transport"
```

### Task 7: Persist Batches and Segment Reconcile Safely

**Files:**
- Modify: `app/gbrain_projection.py`
- Create: `tests/test_gbrain_projection_batches.py`

- [ ] **Step 1: Write RED segmentation and attribution tests**

Test 201 sorted pages become segment sizes `[100, 100, 1]` and modes `['incremental','incremental','reconcile']`; 200 pages become `[100,100]` with only the last reconcile; zero retained pages becomes one empty reconcile control segment. Assert no delete-finalization call after a network/timeout/malformed response in an earlier segment. Assert deterministic `error`, `superseded`, rename compensation, and `recovery_required` mappings accumulate into final `protected_mappings`, and persisted recovery protections reappear in later batches. For a returned `(source_id, slug)` deletion, test all attribution branches: one pre-reconcile mapping plus one owning claimed delete job marks the mapping deleted, bumps `gbrain_projection_generation`, and finishes only that job; one mapping with no claimed job performs legal orphan cleanup; no mapping performs legal unmanaged-ghost cleanup; ambiguous mappings become stale and related claimed delete jobs become failed/protected. An empty-Vault reconcile must return all eligible deletions and complete every uniquely attributable claimed delete job.

Also test older revision/epoch jobs are superseded at batch construction, aggregate success with a per-page error fails only that page's job, and loss of batch/segment/job lease prevents mapping attribution.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_gbrain_projection_batches.py -q`

Expected: FAIL because batch repository and `build_segments` are absent.

- [ ] **Step 3: Implement deterministic segmentation**

```python
@dataclass(frozen=True)
class GBrainSegment:
    id: str
    batch_id: str
    index: int
    mode: Literal["incremental", "reconcile"]
    expected_pages: Sequence[ExpectedPage]
    idempotency_key: str


def build_segments(batch_id: str, mode: str, pages: Sequence[ExpectedPage]) -> list[GBrainSegment]:
    ordered = sorted(pages, key=lambda page: page.page_id)
    groups = [ordered[index:index + 100] for index in range(0, len(ordered), 100)]
    if not groups and mode == "reconcile":
        groups = [[]]
    if not groups:
        return []
    segments: list[GBrainSegment] = []
    for index, group in enumerate(groups):
        segment_mode = "reconcile" if mode == "reconcile" and index == len(groups) - 1 else "incremental"
        digest = sha256(json_dump([page.__dict__ for page in group]).encode("utf-8")).hexdigest()
        segments.append(GBrainSegment(
            id=f"gbseg_{uuid.uuid4().hex}",
            batch_id=batch_id,
            index=index,
            mode=segment_mode,
            expected_pages=tuple(group),
            idempotency_key=f"{batch_id}:{index}:{digest}",
        ))
    return segments
```

- [ ] **Step 4: Implement batch snapshot rules**

For incremental, retain only claimed upserts matching `wiki_pages.current_revision_id` and desired epoch. For a claimed rename/delete/reconcile, snapshot every active, valid current page under the managed root so reconcile has a trusted retained manifest; include all claimed pending upserts whose `created_at` is at or before the batch watermark. Link each claimed job to its immutable revision/epoch snapshot. Mark same-page older pending/running claims superseded using `finish_claimed` and ensure the latest desired state remains pending.

- [ ] **Step 5: Implement lease-safe execution and attribution**

Persist every segment before its call. Before and after each await, renew batch, segment, and member-job leases; a failed CAS raises `ProjectionLeaseLost` and discards the response. Network, timeout, MCP error, or malformed response in any non-final segment marks the batch retryable and never calls the final reconcile segment. Deterministic page outcomes continue to final reconcile with protection.

For each page result, open `connect_app_write(settings)`, verify raw hashes, exact page/revision/epoch, current desired state, source/slug uniqueness, and lease ownership, then mark the old mapping stale, upsert the current mapping, bump persistent generation, and call `finish_claimed(conn, job_id=job.id, worker_id=worker_id, status="succeeded")` before committing. If desired state changed, finish it as superseded and enqueue the current state. If a result is absent or reports an error, fail/retry only the owning job. Store active recovery protections before any later reconcile.

For every returned `deleted(source_id, slug)`, resolve candidates only from the persisted pre-reconcile mapping snapshot. With one mapping, open `connect_app_write(settings)`, set it to `status='deleted'` with `invalidated_at`, and increment `gbrain_projection_generation` before considering job ownership. If exactly one claimed delete job owns that mapping, call `finish_claimed(conn, job_id=delete_job.id, worker_id=worker_id, status="succeeded")` in the same transaction; if no claimed job owns it, commit the mapping update as a legal orphan cleanup. A result with no LGDO mapping is also a successful unmanaged-ghost cleanup and changes no LGDO job. If multiple mapping candidates or multiple owning jobs make attribution ambiguous, set every candidate mapping to `status='stale'` so an actually deleted GBrain row is never left current, persist protections, bump generation once, and finish only the related claimed delete jobs as failed with the ambiguity reason. Apply the same rules to an empty-Vault reconcile; claimed delete success comes from each explicit uniquely attributed result, while unrelated ghost deletion remains legal.

- [ ] **Step 6: Run GREEN**

Run: `python -m pytest tests/test_gbrain_projection_batches.py -q`

Expected: PASS; call traces contain exactly one reconcile finalization, none after ambiguous failure, and lost-owner rows remain unchanged.

- [ ] **Step 7: Commit**

```bash
git add app/gbrain_projection.py tests/test_gbrain_projection_batches.py
git commit -m "feat: batch GBrain projection with safe reconcile"
```

### Task 8: Run RAG and GBrain Through One Lease Worker

**Files:**
- Create: `app/projection_worker.py`
- Modify: `app/projection_jobs.py`
- Modify: `app/main.py`
- Create: `tests/test_projection_worker.py`
- Modify: `tests/test_projection_outbox.py`

- [ ] **Step 1: Write RED worker tests with a fake clock**

Test start/stop idempotency, one GBrain batch at a time, independent RAG progress, 180-second lease with renewal before expiry, crash recovery of expired running jobs, late result rejection, new jobs remaining pending during an active batch, and retry delays `5, 30, 120, 600` seconds with terminal failed status on attempt 5. Inject a fake `now` callable, assert the worker passes that instant unchanged to `mark_failed`, and assert only `ProjectionOutbox.mark_failed` derives `available_at` from the claimed attempt. Seed failed jobs at attempts 4 and 5: the attempt-4 row becomes claimable at `available_at`, while `ProjectionOutbox.claim` never returns the attempt-5 row. Assert `gbrain_endpoint=None` produces degraded/failed state and never invokes `_run_gbrain_cli`.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_projection_worker.py tests/test_projection_outbox.py -q`

Expected: FAIL because `ProjectionWorker` does not exist.

- [ ] **Step 3: Implement the shared lifecycle interface**

```python
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal


@dataclass(frozen=True)
class WorkerRunResult:
    target: Literal["rag", "gbrain", "combined"]
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    superseded: int = 0

    @classmethod
    def combine(cls, rag: "WorkerRunResult", gbrain: "WorkerRunResult") -> "WorkerRunResult":
        return cls(
            target="combined",
            claimed=rag.claimed + gbrain.claimed,
            succeeded=rag.succeeded + gbrain.succeeded,
            failed=rag.failed + gbrain.failed,
            superseded=rag.superseded + gbrain.superseded,
        )


@dataclass(frozen=True)
class ProjectionWorkerStatus:
    worker_id: str
    running: bool
    rag: dict[str, int]
    gbrain: dict[str, int | bool]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProjectionWorker:
    def __init__(
        self,
        settings: Settings,
        outbox: ProjectionOutbox | None = None,
        now: Callable[[], datetime] = utc_now,
    ):
        self.settings = settings
        self.outbox = outbox or ProjectionOutbox(settings)
        self._now = now
        self.worker_id = f"projection-{uuid.uuid4().hex}"
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._gbrain_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._tasks or not self.settings.projection_worker_enabled:
            return
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._loop("rag"), name="projection-rag"),
            asyncio.create_task(self._loop("gbrain"), name="projection-gbrain"),
        ]

    async def stop(self) -> None:
        self._stop.set()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run_once(self, target: str | None = None) -> WorkerRunResult:
        if target == "rag":
            return await self._run_rag_once()
        if target == "gbrain":
            async with self._gbrain_lock:
                return await self._run_gbrain_once()
        rag = await self._run_rag_once()
        async with self._gbrain_lock:
            gbrain = await self._run_gbrain_once()
        return WorkerRunResult.combine(rag, gbrain)

    def snapshot(self) -> ProjectionWorkerStatus:
        return projection_worker_status(self.settings, self.worker_id, bool(self._tasks))
```

Implement the outbox-owned retry transition exactly in `app/projection_jobs.py`:

```python
from datetime import datetime, timedelta, timezone


OUTBOX_RETRY_DELAYS_SECONDS = (5, 30, 120, 600)


def mark_failed(
    self,
    job_id: str,
    worker_id: str,
    last_error: str,
    now: datetime | None = None,
) -> bool:
    failure_time = now or datetime.now(timezone.utc)
    with connect_app_write(self.settings) as conn:
        row = conn.execute(
            """
            SELECT attempts FROM knowledge_projection_jobs
            WHERE id = ? AND status = 'running' AND lease_owner = ?
            """,
            (job_id, worker_id),
        ).fetchone()
        if row is None:
            return False
        attempts = int(row["attempts"])
        available_at = None
        if 1 <= attempts <= len(OUTBOX_RETRY_DELAYS_SECONDS):
            available_at = (
                failure_time + timedelta(seconds=OUTBOX_RETRY_DELAYS_SECONDS[attempts - 1])
            ).isoformat()
        return self.finish_claimed(
            conn,
            job_id=job_id,
            worker_id=worker_id,
            status="failed",
            last_error=last_error,
            available_at=available_at,
        )
```

In `ProjectionOutbox.claim`, add `attempts < 5` independently to the `pending`, `failed`, and expired-`running` SQL branches before `FOR UPDATE SKIP LOCKED`; keep the predicate inside `connect_app_write(self.settings)` for both backends. Attempt 5 therefore receives no retry timestamp and can never be reclaimed automatically. Worker failure paths call `self.outbox.mark_failed(job.id, self.worker_id, error_text, now=self._now())` and never calculate or pass `available_at`. Each loop waits with `asyncio.wait_for(self._stop.wait(), projection_poll_seconds)`, not blocking sleep. A renewal task wakes every 60 seconds for a 180-second lease.

- [ ] **Step 4: Own the worker in FastAPI lifespan**

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_vault(settings.vault_path)
    init_app_db(settings)
    worker = ProjectionWorker(settings)
    app.state.projection_worker = worker
    await worker.start()
    try:
        yield
    finally:
        await worker.stop()
```

- [ ] **Step 5: Run GREEN and prove request code has no 600-second path**

Run: `python -m pytest tests/test_projection_worker.py tests/test_projection_outbox.py tests/test_rag_retrieval.py -q`

Expected: PASS; retry timestamps match the fixed schedule and no test touches the network.

Run: `rg -n "import_vault_to_gbrain|_run_gbrain_cli" app/wiki.py app/api.py app/projection_worker.py`

Expected: no matches in `app/wiki.py`, `app/api.py`, or `app/projection_worker.py`.

- [ ] **Step 6: Commit**

```bash
git add app/projection_worker.py app/projection_jobs.py app/main.py tests/test_projection_worker.py tests/test_projection_outbox.py
git commit -m "feat: run projection jobs under renewable leases"
```

### Task 9: Map Citations Through Current Projection and All-Source ACL

**Files:**
- Create: `app/citations.py`
- Modify: `app/search.py`
- Create: `tests/test_citation_contract.py`

- [ ] **Step 1: Write RED mapping tests**

Create current and stale GBrain mappings and test: exact valid mapping yields one Citation per LGDO source; unmapped slug, wrong namespace, stale status, content-hash mismatch, generation mismatch, old revision, old epoch, invalid/deleted page, missing source, inactive source, one denied source in a multi-source page, empty snippet, and ambiguous mapping all yield no context. Test valid local Wiki chunks also emit one citation per source. Assert GBrain namespace `source_id` never replaces LGDO source IDs.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_citation_contract.py -q`

Expected: FAIL because `app.citations` does not exist.

- [ ] **Step 3: Implement the evidence interface and all-source gate**

```python
@dataclass(frozen=True)
class MappedEvidence:
    citations: Sequence[Citation]
    context: str
    answer_part: str
    diagnostic: Literal["mapped", "stale", "unauthorized", "unmapped"]


def all_sources_readable(conn, source_ids: Sequence[str], user: UserContext | None) -> bool:
    unique = tuple(dict.fromkeys(source_ids))
    if not unique:
        return False
    for source_id in unique:
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None or row["status"] != "active":
            return False
        metadata = json.loads(row["metadata_json"] or "{}")
        if not can_read_metadata(metadata, user, row["owner"]):
            return False
    return True
```

`map_gbrain_hit` queries current mapping by exact `(gbrain_source_id, slug)`, joins `wiki_pages`, verifies mapping revision/epoch against page current/desired state, compares hit content hash/generation, loads source IDs from the immutable mapped revision, applies `all_sources_readable`, requires non-empty snippet, then emits one `Citation(origin='gbrain', page_id, revision_id, chunk_id=str(hit.chunk_id))` per source. `map_local_hit` applies the same all-source gate to `origin='wiki'`; document rows map their single source.

- [ ] **Step 4: Deduplicate only by full citation identity**

Use key `(source_id, wiki_page, page_id, revision_id, chunk_id, origin, snippet)`. Do not collapse two source citations from one multi-source page.

- [ ] **Step 5: Run GREEN plus existing ACL tests**

Run: `python -m pytest tests/test_citation_contract.py tests/test_acl_and_aliases.py -q`

Expected: PASS; the partial multi-source authorization case returns zero evidence and existing single-source ACL behavior remains green.

- [ ] **Step 6: Commit**

```bash
git add app/citations.py app/search.py tests/test_citation_contract.py
git commit -m "feat: enforce current projection citation mapping"
```

### Task 10: Enforce `require_citations`, Memory, and Database-Only Fallback

**Files:**
- Modify: `app/search.py`
- Modify: `tests/test_citation_contract.py`
- Modify: `tests/test_gbrain_integration.py`
- Modify: `tests/test_rag_retrieval.py`

- [ ] **Step 1: Write RED answer-assembly tests**

Assert that only invalid GBrain hits, only memory hits, and current Wiki revision without a visible epoch all avoid `generate_answer` and return the refusal. Assert valid GBrain-only evidence calls the LLM with citations. Revoke ACL between assembly and finalize and assert the generated answer is discarded. For streaming, revoke before completion and assert no factual `answer_delta` is emitted. With `require_citations=True`, assert memory answer snippets are absent from LLM context and local fallback. With `False`, memory still requires every stored citation to pass current ACL and diagnostics report `memory_origin='authorized_history'`.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_citation_contract.py tests/test_gbrain_integration.py tests/test_rag_retrieval.py -q`

Expected: FAIL at the old `not citations and not gbrain_context_blocks` bypass and direct `load_page_text` fallback.

- [ ] **Step 3: Build context only from mapped evidence**

Replace separate citation/GBrain-context assembly with a list of `MappedEvidence`. Extend `context_blocks` and `answer_parts` only from items whose `diagnostic == 'mapped'` and whose citation tuple is non-empty. Diagnostics must count `mapped`, `stale`, `unauthorized`, and `unmapped` GBrain hits.

When `require_citations` is true and the final mapped citation list is empty, set the refusal before calling any LLM. `_memory_blocks_for_assembly` returns `[]` in that mode and `build_local_answer` receives an empty memory list.

- [ ] **Step 4: Replace direct-file fallback with a visible database query**

`fallback_wiki_search` must not accept `settings.vault_path` or call `load_page_text`. Query `wiki_page_revisions.content` through `wiki_pages.current_revision_id` only when lifecycle is active and both visible fields match desired revision/epoch. Apply `all_sources_readable`, score the database content, and create one citation per source.

- [ ] **Step 5: Revalidate after generation without streaming leaks**

Before finalizing a non-stream answer, call `revalidate_citations` against current page/mapping/source state. If empty under `require_citations`, replace the generated answer with the refusal. For streaming, buffer generator chunks, revalidate citations, then emit either buffered chunks or the refusal; do not emit factual deltas before the second gate.

`query_log_is_authorized` must return false for empty/malformed citations even for admin users. Historical answers with citations are rechecked against current source ACL on every use.

- [ ] **Step 6: Run GREEN**

Run: `python -m pytest tests/test_citation_contract.py tests/test_gbrain_integration.py tests/test_rag_retrieval.py tests/test_acl_and_aliases.py -q`

Expected: PASS; mocks prove `generate_answer` and `stream_generate_answer` are not called for citation-empty requests.

- [ ] **Step 7: Commit**

```bash
git add app/search.py tests/test_citation_contract.py tests/test_gbrain_integration.py tests/test_rag_retrieval.py
git commit -m "fix: require authorized citations for factual answers"
```

### Task 11: Expose Projection Status, Retry, Health, and Queued Compile Responses

**Files:**
- Modify: `app/projection_worker.py`
- Modify: `app/api.py`
- Modify: `app/main.py`
- Modify: `app/wiki.py`
- Create: `tests/test_projection_api.py`

- [ ] **Step 1: Write RED API tests**

Test `GET /api/internal/projection-jobs` filters by target/status/page_id and caps limit at 100; `POST /projection-jobs/{id}/retry` resets only failed jobs and returns 409 for pending/running/succeeded; viewers cannot retry. Assert `/health` separates `gbrain.query.available/circuit_open` from `gbrain.projection.pending/running/failed/degraded`. Compile with `GBRAIN_IMPORT_ON_COMPILE=true`, monkeypatch `_run_gbrain_cli` to raise if called, and assert response returns immediately with `projection_status='queued'` and job IDs.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_projection_api.py -q`

Expected: 404 for projection routes and missing health fields.

- [ ] **Step 3: Implement shared status functions**

Expose these exact functions from `projection_worker.py`:

```python
class ProjectionJobNotFound(LookupError):
    def __init__(self, job_id: str):
        self.job_id = job_id
        super().__init__(f"projection job not found: {job_id}")


class ProjectionJobStateConflict(RuntimeError):
    def __init__(self, job_id: str, status: str):
        self.job_id = job_id
        self.status = status
        super().__init__(f"projection job {job_id} cannot be retried from status {status}")


def list_projection_jobs(
    settings: Settings,
    *,
    target: str | None = None,
    status: str | None = None,
    page_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if target is not None:
        clauses.append("target = ?")
        params.append(target)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if page_id is not None:
        clauses.append("page_id = ?")
        params.append(page_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(limit, 100)))
    with connect_app(settings) as conn:
        rows = conn.execute(
            f"SELECT * FROM knowledge_projection_jobs{where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def retry_projection_job(settings: Settings, job_id: str) -> dict:
    timestamp = now_iso()
    with connect_app_write(settings) as conn:
        updated = conn.execute(
            """
            UPDATE knowledge_projection_jobs
            SET status = 'pending', attempts = 0, available_at = ?, last_error = NULL,
                lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
            WHERE id = ? AND status = 'failed'
            """,
            (timestamp, timestamp, job_id),
        )
        row = conn.execute(
            "SELECT * FROM knowledge_projection_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ProjectionJobNotFound(job_id)
        if updated.rowcount != 1:
            raise ProjectionJobStateConflict(job_id, row["status"])
        return dict(row)


def projection_health(settings: Settings) -> dict:
    with connect_app(settings) as conn:
        rows = conn.execute(
            "SELECT target, status, COUNT(*) AS count FROM knowledge_projection_jobs GROUP BY target, status"
        ).fetchall()
    counts = {target: {state: 0 for state in ("pending", "running", "failed")} for target in ("rag", "gbrain")}
    for row in rows:
        if row["target"] in counts and row["status"] in counts[row["target"]]:
            counts[row["target"]][row["status"]] = int(row["count"])
    counts["gbrain"]["configured"] = bool(
        settings.gbrain_endpoint
        and settings.gbrain_projection_api_key
        and settings.gbrain_managed_source_id
    )
    counts["gbrain"]["degraded"] = bool(
        counts["gbrain"]["failed"] or not counts["gbrain"]["configured"]
    )
    return counts
```

Build SQL predicates with bound parameters. Retry uses the single parameterized `UPDATE knowledge_projection_jobs SET status='pending', attempts=0, available_at=?, last_error=NULL, lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=? AND status='failed'` shown above; rowcount zero distinguishes not-found from invalid state with the follow-up read.

- [ ] **Step 4: Add routes and health payload**

Add `GET /projection-jobs` for authenticated users and `POST /projection-jobs/{job_id}/retry` guarded by `require_editor`. Root health must be database-only and nonblocking; query availability comes from configured endpoint plus circuit state, while projection degradation comes from backlog/failed rows and missing projection credentials.

- [ ] **Step 5: Remove synchronous compile import**

Delete the `import_vault_to_gbrain` call and its audit claim from `compile_wiki`. The upstream revision transaction already calls `enqueue_pair_for_state`; return those IDs in `CompileResponse`. `gbrain_import_on_compile` controls whether the GBrain member is enqueued, never whether a subprocess runs.

- [ ] **Step 6: Run GREEN and timing assertion**

Run: `python -m pytest tests/test_projection_api.py tests/test_rag_retrieval.py -q`

Expected: PASS; compile test duration stays below 1 second and `_run_gbrain_cli` call count is zero.

- [ ] **Step 7: Commit**

```bash
git add app/projection_worker.py app/api.py app/main.py app/wiki.py tests/test_projection_api.py
git commit -m "feat: expose nonblocking projection operations"
```

### Task 12: Add Real Temporary PGLite HTTP Acceptance

**Files:**
- Create: `tests/integration/conftest.py`
- Create: `tests/integration/test_gbrain_pglite_projection.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Create an opt-in real-server fixture**

Define `GBrainTestServer(endpoint, query_token, projection_token, source_id, root, process)`, where `endpoint` is the full `/mcp` URL and `root` is the canonical temporary `<vault_path>/wiki` source root. Fixture `gbrain_pglite_server` creates an isolated `GBRAIN_HOME`, initializes PGLite, inserts an LGDO-managed source whose `local_path` equals that exact `root` and whose config contains its dedicated projection client ID, registers separate read and read/write OAuth clients with `--source`, starts `bun run src/cli.ts serve --http` on a free loopback port, polls `/health`, mints both tokens, and terminates the process in `finally`.

Fixture `gbrain_e2e_settings` returns LGDO `Settings` with `vault_path=server.root.parent`, `gbrain_import_allowed_root=server.root`, endpoint, two tokens, managed source, `database_backend='sqlite'`, and projection worker disabled for explicit `run_once`. Lock the raw helper to `async gbrain_mcp_call(server, *, token: str, tool: str, arguments: dict) -> dict`; it returns the decoded tool payload, not the outer JSON-RPC envelope. Implement it in `tests/integration/conftest.py` as follows:

```python
def decode_mcp_envelope(response: httpx.Response) -> dict:
    response.raise_for_status()
    if "text/event-stream" not in response.headers.get("content-type", ""):
        return response.json()
    events = [
        json.loads(line.removeprefix("data:").strip())
        for line in response.text.splitlines()
        if line.startswith("data:") and line.removeprefix("data:").strip()
    ]
    if not events:
        raise AssertionError("MCP response contained no SSE data event")
    return events[-1]


@pytest.fixture
def gbrain_mcp_call():
    async def call(
        server: GBrainTestServer,
        *,
        token: str,
        tool: str,
        arguments: dict,
    ) -> dict:
        headers = {
            "authorization": f"Bearer {token}",
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            initialized = await client.post(
                server.endpoint,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": "initialize",
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "lgdo-e2e", "version": "1"},
                    },
                },
            )
            decode_mcp_envelope(initialized)
            session_id = initialized.headers.get("mcp-session-id")
            if not session_id:
                raise AssertionError("MCP initialize response omitted mcp-session-id")
            headers["mcp-session-id"] = session_id
            ready = await client.post(
                server.endpoint,
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            ready.raise_for_status()
            response = await client.post(
                server.endpoint,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": "tool-call",
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                },
            )
        envelope = decode_mcp_envelope(response)
        if "error" in envelope:
            raise AssertionError(f"MCP JSON-RPC error: {envelope['error']}")
        result = envelope["result"]
        if result.get("isError"):
            raise AssertionError(f"MCP tool error: {result.get('content')}")
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        for block in result.get("content", []):
            if block.get("type") == "text":
                payload = json.loads(block["text"])
                if isinstance(payload, dict):
                    return payload
        raise AssertionError("MCP tool result contained no object payload")

    return call
```

The root `gbrain_pglite_server` fixture calls `pytest.skip` unless `RUN_GBRAIN_E2E=1` and Bun is available; `gbrain_e2e_settings` and `gbrain_mcp_call` are consumed only by tests that also depend on that server fixture. Every asserted `source_path` uses the manifest-relative value returned by `to_gbrain_manifest_path`, such as `product/faq/demo.md`; tests never compare it with the LGDO database path `wiki/product/faq/demo.md`.

- [ ] **Step 2: Write the real acceptance tests**

Test a valid import while serve remains alive; query returns the new content and projection metadata; rename removes the old `(source,slug)` and returns only the new slug; reconcile delete removes the ghost; delete then restore is queryable; query token cannot call sync; projection token cannot write another source; an unlisted valid Markdown file stays absent; parent `.gitignore` has no effect; symlink is rejected; cached old snippet disappears after update.

Add a 97-page sync that polls `/health` and query during import. Record latencies and assert health P95 below 1 second and query P95 below 5 seconds. Use the 120-second incremental and 600-second reconcile client limits as test deadlines, not sleeps.

- [ ] **Step 3: Run the default-suite exclusion check**

Run: `python -m pytest -m "not gbrain_e2e" -q`

Expected: exit 0 with the ordinary hermetic suite selected; every `gbrain_e2e` test is deselected and no Bun process starts.

- [ ] **Step 4: Run the real acceptance test**

Run: `$env:RUN_GBRAIN_E2E='1'; python -m pytest -m gbrain_e2e tests/integration/test_gbrain_pglite_projection.py -v`

Expected: PASS for import, restore, rename compensation, reconcile deletion, token isolation, cache invalidation, and 97-page responsiveness.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/conftest.py tests/integration/test_gbrain_pglite_projection.py pyproject.toml
git commit -m "test: accept projection against live PGLite MCP"
```

### Task 13: Full Verification and Operational Evidence

**Files:**
- Modify only if a verification failure identifies a defect in files listed by Tasks 1-12.

- [ ] **Step 1: Run all hermetic Python tests with external calls disabled**

Run: `$env:GBRAIN_IMPORT_ON_COMPILE='false'; python -m pytest -m "not gbrain_e2e" -q`

Expected: exit 0; the prior 90 tests plus all new projection/citation tests pass, and no request waits for GBrain or DeepSeek.

- [ ] **Step 2: Run focused GBrain tests and static checks**

Run: `cd gbrain && bun test test/lgdo-vault-sync.test.ts test/lgdo-vault-sync-operation.test.ts test/e2e/lgdo-vault-sync-pglite.test.ts test/import-file.test.ts test/source-id-tx-regression.test.ts test/e2e/cache-gate-pglite.test.ts`

Expected: exit 0 with every named test passing.

Run: `cd gbrain && bun run typecheck`

Expected: exit 0.

Run: `cd gbrain && bun run check:source-id-projection`

Expected: exit 0 with no unscoped write finding.

- [ ] **Step 3: Run database-backend regression tests**

Run: `python -m pytest tests/test_postgres_metadata_backend.py tests/test_sqlite_to_postgres_migration.py tests/test_pg_rag_store.py -q`

Expected: PASS or environment-marked PostgreSQL skips; SQLite/PostgreSQL DDL and compatibility SQL remain valid.

- [ ] **Step 4: Run the real PGLite acceptance once**

Run: `$env:RUN_GBRAIN_E2E='1'; python -m pytest -m gbrain_e2e tests/integration/test_gbrain_pglite_projection.py -v`

Expected: PASS and reported P95 values remain within 1-second health and 5-second query budgets.

- [ ] **Step 5: Inspect diff quality and absence of synchronous fallback**

Run: `git diff --check`

Expected: no output.

Run: `rg -n "subprocess\.run|import_vault_to_gbrain|_run_gbrain_cli" app/projection_worker.py app/gbrain_projection.py app/wiki.py`

Expected: no output.

Run: `rg -n "load_page_text\(|vault_path" app/search.py`

Expected: no direct-file Wiki fallback call; any remaining `vault_path` reference is only the QA audit-log append and must not feed retrieval.

- [ ] **Step 6: Return every verification defect to its owning task**

Do not use a broad verification commit or stage a directory. For each defect found by Steps 1-5, return to the first Task 1-12 whose `Files:` list owns the affected path, add a focused regression test there, rerun that task's RED/GREEN commands, and stage only the exact paths named by that task's existing `git add` command. If Steps 1-5 require no code changes, create no commit.
