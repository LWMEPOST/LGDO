# Vault Watcher And Obsidian Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a persistent, restart-safe Vault watcher and Obsidian workflow so external Markdown edits flow through the existing revision/outbox contracts without overwriting user bytes or exposing stale projections.

**Architecture:** Treat `app/wiki_markdown.py`, `WikiRevisionService`, `IntentExecutor`, `ProjectionOutbox`, and `ProjectionWorker` as prerequisite boundaries. The watcher layer only observes and stabilizes filesystem events, persists occurrences and delete grace, delegates mutations to `WikiRevisionService`, reconciles drift on startup, and exposes status/deep-link APIs. Obsidian resources are version-controlled outside the ignored runtime Vault and are installed through a merge-and-backup operation.

**Tech Stack:** Python 3.10+, FastAPI lifespan, SQLite/PostgreSQL, `watchfiles`, shared `ruamel.yaml` round-trip parsing, pytest/pytest-asyncio, React 18, TypeScript, Vitest, Testing Library, PowerShell.

---

## Prerequisites And Execution Order

1. Complete and pass `docs/superpowers/plans/2026-07-13-wiki-revision-conflict.md`. This supplies `app/wiki_markdown.py`, `app/wiki_revisions.py`, `app/vault_writer.py`, `app/projection_jobs.py`, and the revision schema.
2. Complete and pass `docs/superpowers/plans/2026-07-13-gbrain-projection-citation.md`. This supplies `app/projection_worker.py`, projection status APIs/helpers, Wiki RAG projection, and the real GBrain test server fixtures.
3. Execute this plan. Do not create alternate frontmatter, revision, writer, outbox, or projection-worker implementations in watcher files.

The prerequisite contract is locked before this plan starts:

- `FrontmatterLimits` includes `max_file_bytes` and `max_prefix_bytes` in addition to the YAML/frontmatter limits. File capture still uses the keyword-only API `capture_file_observation(path, *, max_content_bytes, prefix_bytes)`; never pass a `FrontmatterLimits` instance positionally.
- `MutationResult.revision_id` is the canonical revision reference for occurrence/audit recording. For an applied external ingest it is the committed current revision ID; for a rename it is the rename audit revision ID; for an ignored mutation it is the already-recorded revision ID or `None`. Watcher code may persist it in `vault_change_events.result_revision_id`, but must not use it for CAS, projection visibility, or current-state decisions.

Verify the prerequisite imports before Task 1:

```powershell
python -c "import inspect; from app.wiki_markdown import FrontmatterLimits, FileObservationInput, capture_file_observation, parse_wiki_bytes; from app.wiki_revisions import MutationResult, WikiRevisionService; from app.vault_writer import IntentExecutor; from app.projection_worker import ProjectionWorker; limits=FrontmatterLimits(); assert hasattr(limits, 'max_file_bytes') and hasattr(limits, 'max_prefix_bytes'); params=inspect.signature(capture_file_observation).parameters; assert params['max_content_bytes'].kind is inspect.Parameter.KEYWORD_ONLY and params['prefix_bytes'].kind is inspect.Parameter.KEYWORD_ONLY; assert 'revision_id' in MutationResult.__dataclass_fields__"
```

Expected: exit code 0 with no output. If it fails, finish the prerequisite plan rather than adding compatibility shims here.

## File Structure

- Modify `pyproject.toml`: add `watchfiles` and async-test dependencies; retain the prerequisite `ruamel.yaml` dependency.
- Modify `app/config.py`: add validated watcher, stability, rename-grace, worker, and Obsidian settings.
- Modify `.env.example`: document all new settings with development defaults.
- Modify `tests/conftest.py`: extend the prerequisite background-integration fixture so ordinary tests never launch watcher/GBrain workers.
- Consume `app/wiki_markdown.py`: use its bounded observation and round-trip parser APIs; do not duplicate them.
- Create `app/vault_events.py`: persist watcher event occurrences, pending deletes, sync issues, and status counters.
- Create `app/vault_watcher.py`: canonical path filtering, stable observation, `watchfiles.awatch` adaptation, event coalescing, and task shutdown.
- Create `app/vault_sync.py`: event orchestration, page-level serialization, rename pairing, delete grace, startup reconcile, and status snapshots.
- Create `app/obsidian.py`: resource materialization, drift detection, refresh merge/backup, and `obsidian://` link construction.
- Modify `app/vault.py`: create `indexes/` and invoke the Obsidian resource installer without overwriting existing user configuration.
- Modify `app/main.py`: order intent recovery, startup reconcile, projection worker, watcher startup, and shutdown.
- Modify `app/models.py`: add Vault status/reconcile and Obsidian-link response models.
- Modify `app/api.py`: add `/vault/status`, `/vault/reconcile`, and `/wiki/pages/{path}/obsidian-link`.
- Create `resources/obsidian-vault/.obsidian/app.json`: stable managed Obsidian application settings.
- Create `resources/obsidian-vault/.obsidian/core-plugins.json`: core plugin set only.
- Create `resources/obsidian-vault/.obsidian/templates.json`: template folder configuration.
- Create `resources/obsidian-vault/templates/Wiki Page.md`: user template without managed identity/revision fields.
- Create `resources/obsidian-vault/indexes/Home.md`: default MOC.
- Create `resources/obsidian-vault/README.md`: editable/read-only boundaries and recovery behavior.
- Create `scripts/install-obsidian-vault.ps1`: explicit install/refresh command.
- Create `scripts/open-obsidian.ps1`: encoded deep-link launcher with testable print-only mode.
- Modify `.gitignore`: keep runtime workspace/plugin state out of version control while retaining stable resources.
- Modify `frontend/package.json`: add `lucide-react`, Vitest, jsdom, and Testing Library.
- Create `frontend/src/test/setup.ts`: frontend test setup.
- Modify `frontend/vite.config.ts`: add a jsdom Vitest configuration.
- Modify `frontend/src/types.ts`: add Vault/Obsidian status types.
- Modify `frontend/src/App.tsx`: load status and request/open deep links.
- Modify `frontend/src/features/WikiTask.tsx`: show sync state and an icon-based Obsidian command.
- Create `frontend/src/features/WikiTask.test.tsx`: verify the deep-link command and degraded state.
- Create `tests/test_vault_config.py`: grace-window validation and test defaults.
- Create `tests/test_vault_watcher.py`: bounded stability, path filtering, debounce/coalescing, and fake watch stream tests.
- Create `tests/test_vault_persistence.py`: SQLite event/delete/issue persistence and migration-list tests.
- Create `tests/test_vault_sync.py`: add/modify/rename/delete-grace and startup-reconcile state-machine tests.
- Create `tests/test_obsidian_assets.py`: resource, installer, merge, backup, and link tests.
- Create `tests/test_vault_api.py`: lifespan/status/reconcile/link endpoint tests.
- Create `tests/test_vault_obsidian_e2e.py`: deterministic local five-second workflow test.
- Modify `tests/integration/test_gbrain_pglite_projection.py`: add real incremental/reconcile SLA coverage using shared fixtures.
- Modify `README.md` and `README.zh-CN.md`: operator setup, status, refresh, and test commands.

---

### Task 1: Watcher Dependencies And Validated Settings

**Files:**
- Modify: `pyproject.toml`
- Modify: `app/config.py`
- Modify: `.env.example`
- Modify: `tests/conftest.py`
- Create: `tests/test_vault_config.py`

- [ ] **Step 1: Write failing configuration tests**

Create `tests/test_vault_config.py`:

```python
import pytest
from pydantic import ValidationError

from app.config import Settings


def test_vault_watcher_defaults_cover_debounce_and_stability():
    settings = Settings(_env_file=None)

    required = max(
        5000,
        settings.vault_watch_debounce_ms
        + int(settings.vault_watch_stability_timeout_seconds * 1000)
        + settings.vault_rename_safety_margin_ms,
    )

    assert settings.vault_watch_enabled is True
    assert settings.vault_rename_grace_ms >= required
    assert settings.vault_watch_max_file_bytes == 5 * 1024 * 1024
    assert settings.obsidian_vault_name is None


def test_vault_watcher_rejects_unsafe_rename_grace():
    with pytest.raises(ValidationError, match="vault_rename_grace_ms"):
        Settings(
            _env_file=None,
            vault_watch_debounce_ms=750,
            vault_watch_stability_timeout_seconds=3,
            vault_rename_safety_margin_ms=1500,
            vault_rename_grace_ms=5000,
        )
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m pytest tests/test_vault_config.py -q
```

Expected: FAIL because the watcher settings and cross-field validator do not exist.

- [ ] **Step 3: Add dependencies and the minimal settings implementation**

Add these dependencies to `pyproject.toml` without removing `ruamel.yaml` from the prerequisite plan:

```toml
  "watchfiles>=0.24.0",
```

Add this dev dependency:

```toml
  "pytest-asyncio>=0.23.0",
```

In `app/config.py`, import `model_validator` and add these fields/method:

```python
from pydantic import model_validator


class Settings(BaseSettings):
    vault_watch_enabled: bool = True
    vault_watch_debounce_ms: int = 750
    vault_watch_stability_timeout_seconds: float = 3.0
    vault_watch_max_file_bytes: int = 5 * 1024 * 1024
    vault_rename_grace_ms: int = 5000
    vault_rename_safety_margin_ms: int = 1000
    vault_watch_concurrency: int = 4
    projection_worker_enabled: bool = True
    obsidian_vault_name: str | None = None

    @model_validator(mode="after")
    def validate_vault_rename_grace(self) -> "Settings":
        required = max(
            5000,
            self.vault_watch_debounce_ms
            + int(self.vault_watch_stability_timeout_seconds * 1000)
            + self.vault_rename_safety_margin_ms,
        )
        if self.vault_rename_grace_ms < required:
            raise ValueError(
                "vault_rename_grace_ms must be at least "
                f"max(5000, debounce + stability + margin)={required}"
            )
        if self.vault_watch_concurrency < 1:
            raise ValueError("vault_watch_concurrency must be at least 1")
        return self

    @property
    def effective_obsidian_vault_name(self) -> str:
        return self.obsidian_vault_name or self.vault_path.resolve().name
```

Append matching uppercase keys to `.env.example`:

```dotenv
VAULT_WATCH_ENABLED=true
VAULT_WATCH_DEBOUNCE_MS=750
VAULT_WATCH_STABILITY_TIMEOUT_SECONDS=3
VAULT_WATCH_MAX_FILE_BYTES=5242880
VAULT_RENAME_GRACE_MS=5000
VAULT_RENAME_SAFETY_MARGIN_MS=1000
VAULT_WATCH_CONCURRENCY=4
PROJECTION_WORKER_ENABLED=true
OBSIDIAN_VAULT_NAME=
```

Extend `tests/conftest.py::disable_background_integrations`, created by the GBrain prerequisite plan:

```python
@pytest.fixture(autouse=True)
def disable_background_integrations(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "projection_worker_enabled", False)
    monkeypatch.setattr(settings, "gbrain_enabled", False)
    monkeypatch.setattr(settings, "vault_watch_enabled", False)
```

- [ ] **Step 4: Install and run the focused tests**

Run:

```powershell
python -m pip install -e ".[dev]"
python -m pytest tests/test_vault_config.py -q
```

Expected: PASS, 2 tests.

- [ ] **Step 5: Commit the configuration boundary**

```powershell
git add pyproject.toml app/config.py .env.example tests/conftest.py tests/test_vault_config.py
git commit -m "feat: configure vault watcher lifecycle"
```

---

### Task 2: Stable And Bounded Wiki Observation Adapter

**Files:**
- Create: `app/vault_watcher.py`
- Create: `tests/test_vault_watcher.py`
- Consume: `app/wiki_markdown.py`

- [ ] **Step 1: Write failing stable-observation integration tests**

Create `tests/test_vault_watcher.py` with the shared parser integration tests:

```python
import asyncio

import pytest

from app.vault_watcher import UnstableVaultFile, wait_for_stable_observation
from app.wiki_markdown import FrontmatterLimits, parse_wiki_bytes, render_managed_frontmatter


def test_stable_observation_uses_round_trip_parser_without_losing_comments(tmp_path):
    page = tmp_path / "page.md"
    page.write_text(
        "---\n"
        "# owner comment\n"
        "title: Refund policy\n"
        "source_ids: [src_1]\n"
        "domain: product\n"
        "page_type: policy\n"
        "review_status: draft\n"
        "---\n"
        "# Refund policy\n",
        encoding="utf-8",
    )
    limits = FrontmatterLimits(max_file_bytes=1024 * 1024)

    observation = asyncio.run(
        wait_for_stable_observation(page, limits, timeout_seconds=0.2, poll_interval=0)
    )
    document = parse_wiki_bytes(observation.content_bytes or b"", limits)
    rendered = render_managed_frontmatter(
        document,
        page_id="page_1",
        revision_id="wrev_1",
        write_token="write_1",
    )

    assert observation.content_truncated is False
    assert b"# owner comment" in rendered
    assert rendered.index(b"title:") < rendered.index(b"source_ids:")


def test_sparse_oversized_file_observation_is_bounded(tmp_path):
    page = tmp_path / "large.md"
    with page.open("wb") as stream:
        stream.write(b"---\ntitle: large\n---\n")
        stream.truncate(8 * 1024 * 1024)
    limits = FrontmatterLimits(max_file_bytes=1024 * 1024, max_prefix_bytes=64 * 1024)

    observation = asyncio.run(
        wait_for_stable_observation(page, limits, timeout_seconds=0.2, poll_interval=0)
    )

    assert observation.content_bytes is None
    assert observation.content_truncated is True
    assert len(observation.content_prefix or b"") == 64 * 1024
    assert len(observation.file_hash) == 64


def test_unstable_file_times_out_without_parsing(monkeypatch, tmp_path):
    page = tmp_path / "moving.md"
    page.write_text("first", encoding="utf-8")
    signatures = iter([(5, 1), (6, 2), (7, 3), (8, 4)])

    async def changing_signature(_path):
        return next(signatures)

    monkeypatch.setattr("app.vault_watcher._stat_signature", changing_signature)

    with pytest.raises(UnstableVaultFile):
        asyncio.run(
            wait_for_stable_observation(page, FrontmatterLimits(), timeout_seconds=0, poll_interval=0)
        )
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m pytest tests/test_vault_watcher.py -q
```

Expected: FAIL because `app.vault_watcher` does not exist.

- [ ] **Step 3: Implement only the stability/observation boundary**

Create `app/vault_watcher.py` with this initial implementation:

```python
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from app.wiki_markdown import FileObservationInput, FrontmatterLimits, capture_file_observation


class UnstableVaultFile(RuntimeError):
    pass


async def _stat_signature(path: Path) -> tuple[int, int]:
    stat = await asyncio.to_thread(path.stat)
    return stat.st_size, stat.st_mtime_ns


async def wait_for_stable_observation(
    path: Path,
    limits: FrontmatterLimits,
    *,
    timeout_seconds: float,
    poll_interval: float = 0.05,
) -> FileObservationInput:
    deadline = time.monotonic() + timeout_seconds
    previous: tuple[int, int] | None = None
    while True:
        try:
            current = await _stat_signature(path)
        except (FileNotFoundError, OSError) as exc:
            raise UnstableVaultFile(f"file disappeared while stabilizing: {path}") from exc
        if current == previous:
            return await asyncio.to_thread(
                capture_file_observation,
                path,
                max_content_bytes=limits.max_file_bytes,
                prefix_bytes=limits.max_prefix_bytes,
            )
        previous = current
        if time.monotonic() >= deadline:
            raise UnstableVaultFile(f"file did not stabilize before timeout: {path}")
        await asyncio.sleep(poll_interval)
```

- [ ] **Step 4: Run the focused tests and shared parser tests**

Run:

```powershell
python -m pytest tests/test_vault_watcher.py tests/test_wiki_markdown.py -q
```

Expected: PASS. The exact count is the sum of 3 watcher tests and the prerequisite parser suite.

- [ ] **Step 5: Commit the bounded observation adapter**

```powershell
git add app/vault_watcher.py tests/test_vault_watcher.py
git commit -m "feat: observe stable vault files safely"
```

---

### Task 3: Persistent Events, Delete Grace, And Sync Issues

**Files:**
- Modify: `app/db.py`
- Modify: `app/migration.py`
- Create: `app/vault_events.py`
- Create: `tests/test_vault_persistence.py`

- [ ] **Step 1: Write failing persistence tests**

Create `tests/test_vault_persistence.py`:

```python
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.db import MAIN_TABLES, TABLE_PRIMARY_KEYS, connect_app, init_app_db
from app.vault_events import VaultEventStore


def make_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_backend="sqlite",
        database_path=tmp_path / "events.db",
        vault_path=tmp_path / "vault",
        vault_watch_enabled=False,
        projection_worker_enabled=False,
    )


def test_event_store_persists_unclassified_add_across_restart(tmp_path):
    settings = make_settings(tmp_path)
    init_app_db(settings)
    first = VaultEventStore(settings)
    detected = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)

    event = first.begin_event("add", "wiki/product/new.md", detected_at=detected)
    second = VaultEventStore(settings)

    assert second.has_unclassified_add_before(detected + timedelta(seconds=5)) is True
    second.finish_event(event.id, "applied", result_revision_id="wrev_1")
    assert second.has_unclassified_add_before(detected + timedelta(seconds=5)) is False


def test_pending_delete_replay_is_idempotent_and_later_cycle_gets_new_id(tmp_path):
    settings = make_settings(tmp_path)
    init_app_db(settings)
    store = VaultEventStore(settings)
    detected = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)

    first = store.create_pending_delete(
        occurrence_event_id="vevt_delete_1",
        page_id="page_1",
        old_page_path="wiki/product/old.md",
        file_hash="a" * 64,
        semantic_hash="b" * 64,
        detected_at=detected,
        expires_at=detected + timedelta(seconds=5),
    )
    second = store.create_pending_delete(
        occurrence_event_id="vevt_delete_1",
        page_id="page_1",
        old_page_path="wiki/product/old.md",
        file_hash="a" * 64,
        semantic_hash="b" * 64,
        detected_at=detected,
        expires_at=detected + timedelta(seconds=5),
    )
    store.complete_delete(first.id)
    third = store.create_pending_delete(
        occurrence_event_id="vevt_delete_2",
        page_id="page_1",
        old_page_path="wiki/product/new.md",
        file_hash="c" * 64,
        semantic_hash="d" * 64,
        detected_at=detected + timedelta(seconds=10),
        expires_at=detected + timedelta(seconds=15),
    )
    issue_a = store.upsert_issue("wiki/product/new.md", "a" * 64, "page_1", "identity_conflict", "ID mismatch")
    issue_b = store.upsert_issue("wiki/product/new.md", "a" * 64, "page_1", "identity_conflict", "ID mismatch")

    assert first.id == second.id
    assert third.id != first.id
    assert third.occurrence_event_id == "vevt_delete_2"
    assert issue_a == issue_b
    assert store.list_due_deletes(detected + timedelta(seconds=16))[0].page_id == "page_1"
    assert {"vault_change_events", "pending_vault_deletes", "vault_sync_issues"} <= set(MAIN_TABLES)
    assert TABLE_PRIMARY_KEYS["pending_vault_deletes"] == "id"


def test_sqlite_schema_has_pending_delete_lookup_indexes(tmp_path):
    settings = make_settings(tmp_path)
    init_app_db(settings)

    with connect_app(settings) as conn:
        indexes = {
            row[1]: bool(row[2])
            for row in conn.execute("PRAGMA index_list('pending_vault_deletes')").fetchall()
        }

    assert "idx_pending_vault_deletes_due" in indexes
    assert "idx_pending_vault_deletes_page_pending" in indexes
    assert indexes["idx_pending_vault_deletes_occurrence_event"] is True
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m pytest tests/test_vault_persistence.py -q
```

Expected: FAIL because the watcher tables and `VaultEventStore` are absent.

- [ ] **Step 3: Add exact SQLite/PostgreSQL schema and migration metadata**

Add equivalent DDL to `SCHEMA` and `PG_SCHEMA` in `app/db.py`; use `BIGINT` for PostgreSQL time/size values inherited from the revision observation schema:

```sql
CREATE TABLE IF NOT EXISTS pending_vault_deletes (
  id TEXT PRIMARY KEY,
  occurrence_event_id TEXT NOT NULL,
  page_id TEXT NOT NULL,
  old_page_path TEXT NOT NULL,
  file_hash TEXT,
  semantic_hash TEXT,
  detected_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  matched_event_id TEXT,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_vault_deletes_due
ON pending_vault_deletes(status, expires_at);

CREATE INDEX IF NOT EXISTS idx_pending_vault_deletes_page_pending
ON pending_vault_deletes(page_id, status);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_vault_deletes_occurrence_event
ON pending_vault_deletes(occurrence_event_id);

CREATE TABLE IF NOT EXISTS vault_sync_issues (
  id TEXT PRIMARY KEY,
  page_path TEXT NOT NULL,
  file_hash TEXT NOT NULL,
  page_id TEXT,
  issue_type TEXT NOT NULL,
  error_summary TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_vault_sync_issues_status
ON vault_sync_issues(status, issue_type);
```

Add both tables to `MAIN_TABLES` and map both IDs in `TABLE_PRIMARY_KEYS`; the prerequisite plan already lists `vault_change_events`. This automatically makes `app/migration.py` copy the two new tables through its existing metadata-driven loop.

- [ ] **Step 4: Implement the event store public surface**

Create `app/vault_events.py`:

```python
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import Settings
from app.db import connect_app


def iso(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class VaultEvent:
    id: str
    kind: str
    page_path: str
    old_page_path: str | None
    detected_at: datetime


@dataclass(frozen=True)
class PendingVaultDelete:
    id: str
    occurrence_event_id: str
    page_id: str
    old_page_path: str
    file_hash: str | None
    semantic_hash: str | None
    detected_at: datetime
    expires_at: datetime


class VaultEventStore:
    def __init__(self, settings: Settings):
        self.settings = settings

    def begin_event(
        self,
        kind: str,
        page_path: str,
        *,
        detected_at: datetime,
        old_page_path: str | None = None,
        event_id: str | None = None,
    ) -> VaultEvent:
        event_id = event_id or f"vevt_{uuid.uuid4().hex}"
        with connect_app(self.settings) as conn:
            conn.execute(
                """
                INSERT INTO vault_change_events(
                  id, kind, page_path, old_page_path, expected_state_json,
                  status, detected_at, updated_at
                ) VALUES (?, ?, ?, ?, '{}', 'pending', ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (event_id, kind, page_path, old_page_path, iso(detected_at), iso(detected_at)),
            )
            row = conn.execute("SELECT * FROM vault_change_events WHERE id = ?", (event_id,)).fetchone()
        return VaultEvent(
            id=row["id"],
            kind=row["kind"],
            page_path=row["page_path"],
            old_page_path=row["old_page_path"],
            detected_at=datetime.fromisoformat(row["detected_at"]),
        )

    def finish_event(self, event_id: str, status: str, *, result_revision_id: str | None = None) -> None:
        with connect_app(self.settings) as conn:
            conn.execute(
                """
                UPDATE vault_change_events
                SET status = ?, result_revision_id = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (status, result_revision_id, iso(), event_id),
            )

    def has_unclassified_add_before(self, cutoff: datetime) -> bool:
        with connect_app(self.settings) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM vault_change_events
                WHERE kind = 'add' AND status = 'pending' AND detected_at <= ?
                LIMIT 1
                """,
                (iso(cutoff),),
            ).fetchone()
        return row is not None

    def create_pending_delete(
        self,
        *,
        occurrence_event_id: str,
        page_id: str,
        old_page_path: str,
        file_hash: str | None,
        semantic_hash: str | None,
        detected_at: datetime,
        expires_at: datetime,
    ) -> PendingVaultDelete:
        delete_id = f"vdel_{uuid.uuid4().hex}"
        with connect_app(self.settings) as conn:
            conn.execute(
                """
                INSERT INTO pending_vault_deletes(
                  id, occurrence_event_id, page_id, old_page_path, file_hash, semantic_hash,
                  detected_at, expires_at, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                ON CONFLICT(occurrence_event_id) DO NOTHING
                """,
                (
                    delete_id,
                    occurrence_event_id,
                    page_id,
                    old_page_path,
                    file_hash,
                    semantic_hash,
                    iso(detected_at),
                    iso(expires_at),
                    iso(detected_at),
                ),
            )
            row = conn.execute(
                "SELECT * FROM pending_vault_deletes WHERE occurrence_event_id = ?",
                (occurrence_event_id,),
            ).fetchone()
        return PendingVaultDelete(
            row["id"],
            row["occurrence_event_id"],
            row["page_id"],
            row["old_page_path"],
            row["file_hash"],
            row["semantic_hash"],
            datetime.fromisoformat(row["detected_at"]),
            datetime.fromisoformat(row["expires_at"]),
        )

    def list_due_deletes(self, now: datetime) -> list[PendingVaultDelete]:
        with connect_app(self.settings) as conn:
            rows = conn.execute(
                """
                SELECT * FROM pending_vault_deletes
                WHERE status = 'pending' AND expires_at <= ?
                ORDER BY expires_at, id
                """,
                (iso(now),),
            ).fetchall()
        return [
            PendingVaultDelete(
                row["id"],
                row["occurrence_event_id"],
                row["page_id"],
                row["old_page_path"],
                row["file_hash"],
                row["semantic_hash"],
                datetime.fromisoformat(row["detected_at"]),
                datetime.fromisoformat(row["expires_at"]),
            )
            for row in rows
        ]

    def cancel_delete(self, delete_id: str, event_id: str) -> None:
        with connect_app(self.settings) as conn:
            conn.execute(
                """
                UPDATE pending_vault_deletes
                SET status = 'cancelled', matched_event_id = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (event_id, iso(), delete_id),
            )

    def protect_delete(self, delete_id: str, event_id: str) -> None:
        with connect_app(self.settings) as conn:
            conn.execute(
                """
                UPDATE pending_vault_deletes
                SET status = 'protected', matched_event_id = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (event_id, iso(), delete_id),
            )

    def complete_delete(self, delete_id: str) -> None:
        with connect_app(self.settings) as conn:
            conn.execute(
                """
                UPDATE pending_vault_deletes
                SET status = 'applied', updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (iso(), delete_id),
            )

    def upsert_issue(
        self,
        page_path: str,
        file_hash: str,
        page_id: str | None,
        issue_type: str,
        error_summary: str,
    ) -> str:
        material = f"{page_path}\0{file_hash}\0{issue_type}".encode("utf-8")
        issue_id = f"visi_{hashlib.sha256(material).hexdigest()[:24]}"
        with connect_app(self.settings) as conn:
            conn.execute(
                """
                INSERT INTO vault_sync_issues(
                  id, page_path, file_hash, page_id, issue_type, error_summary,
                  status, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  page_id = excluded.page_id,
                  error_summary = excluded.error_summary,
                  last_seen_at = excluded.last_seen_at,
                  status = 'open',
                  resolved_at = NULL
                """,
                (issue_id, page_path, file_hash, page_id, issue_type, error_summary[:1000], iso(), iso()),
            )
        return issue_id

    def snapshot(self) -> dict[str, int]:
        with connect_app(self.settings) as conn:
            pending_events = conn.execute(
                "SELECT COUNT(*) FROM vault_change_events WHERE status = 'pending'"
            ).fetchone()[0]
            pending_deletes = conn.execute(
                "SELECT COUNT(*) FROM pending_vault_deletes WHERE status = 'pending'"
            ).fetchone()[0]
            open_issues = conn.execute(
                "SELECT COUNT(*) FROM vault_sync_issues WHERE status = 'open'"
            ).fetchone()[0]
            invalid_pages = conn.execute(
                "SELECT COUNT(*) FROM wiki_pages WHERE lifecycle_status = 'invalid'"
            ).fetchone()[0]
        return {
            "pending_events": pending_events,
            "pending_deletes": pending_deletes,
            "open_issues": open_issues,
            "invalid_pages": invalid_pages,
        }
```

- [ ] **Step 5: Run persistence and migration tests**

Run:

```powershell
python -m pytest tests/test_vault_persistence.py tests/test_sqlite_to_postgres_migration.py -q
```

Expected: PASS. PostgreSQL-dependent cases may be skipped only when PostgreSQL is unavailable.

- [ ] **Step 6: Commit the persistent event layer**

```powershell
git add app/db.py app/migration.py app/vault_events.py tests/test_vault_persistence.py
git commit -m "feat: persist vault events and delete grace"
```

---

### Task 4: Watchfiles Filtering, Debounce, And Lifecycle Adapter

**Files:**
- Modify: `app/vault_watcher.py`
- Modify: `tests/test_vault_watcher.py`

- [ ] **Step 1: Add failing path/coalescing/fake-stream tests**

Append to `tests/test_vault_watcher.py`:

```python
from pathlib import Path

from watchfiles import Change

from app.config import Settings
from app.vault_watcher import VaultFsEvent, VaultWatchAdapter, normalize_watchfiles_batch


def test_normalize_batch_accepts_only_canonical_wiki_markdown(tmp_path):
    vault = tmp_path / "vault"
    wiki = vault / "wiki"
    wiki.mkdir(parents=True)
    valid = wiki / "product" / "page.md"
    valid.parent.mkdir()
    valid.write_text("# page", encoding="utf-8")
    ignored = vault / "logs" / "page.md"
    ignored.parent.mkdir()
    ignored.write_text("# log", encoding="utf-8")

    events = normalize_watchfiles_batch(
        {(Change.added, str(valid)), (Change.added, str(ignored))},
        vault,
    )

    assert events == [VaultFsEvent("add", "wiki/product/page.md")]


def test_normalize_batch_uses_final_filesystem_state(tmp_path):
    vault = tmp_path / "vault"
    page = vault / "wiki" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("final", encoding="utf-8")

    events = normalize_watchfiles_batch(
        {(Change.deleted, str(page)), (Change.added, str(page)), (Change.modified, str(page))},
        vault,
    )

    assert events == [VaultFsEvent("add", "wiki/page.md")]


def test_watch_adapter_passes_configured_debounce_to_awatch(tmp_path):
    calls = []
    batches = []

    async def fake_awatch(path, **kwargs):
        calls.append((Path(path), kwargs))
        yield {(Change.added, str(Path(path) / "page.md"))}

    async def handler(events):
        batches.append(events)

    settings = Settings(
        _env_file=None,
        vault_path=tmp_path / "vault",
        vault_watch_debounce_ms=875,
        vault_rename_grace_ms=5000,
    )
    (settings.vault_path / "wiki").mkdir(parents=True)
    (settings.vault_path / "wiki" / "page.md").write_text("# page", encoding="utf-8")
    adapter = VaultWatchAdapter(settings, handler, watch_factory=fake_awatch)

    asyncio.run(adapter.run_until_first_batch())

    assert calls[0][1]["debounce"] == 875
    assert calls[0][1]["recursive"] is True
    assert batches == [[VaultFsEvent("add", "wiki/page.md")]]
```

- [ ] **Step 2: Run the three new tests and verify RED**

Run:

```powershell
python -m pytest tests/test_vault_watcher.py::test_normalize_batch_accepts_only_canonical_wiki_markdown tests/test_vault_watcher.py::test_normalize_batch_uses_final_filesystem_state tests/test_vault_watcher.py::test_watch_adapter_passes_configured_debounce_to_awatch -q
```

Expected: FAIL because event types, normalization, and the adapter are not implemented.

- [ ] **Step 3: Implement canonical filtering and the watch adapter**

Append this public surface to `app/vault_watcher.py`:

```python
from dataclasses import dataclass
from typing import Awaitable, Callable

from watchfiles import Change, awatch

from app.config import Settings


@dataclass(frozen=True, order=True)
class VaultFsEvent:
    kind: str
    page_path: str


def _canonical_relative_wiki_path(vault_path: Path, raw_path: str) -> str | None:
    vault_root = vault_path.resolve()
    wiki_root = (vault_root / "wiki").resolve()
    candidate = Path(raw_path)
    if candidate.exists() and candidate.is_symlink():
        return None
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(wiki_root)
    except ValueError:
        return None
    if resolved.suffix.lower() != ".md":
        return None
    if any(part.startswith(".") for part in relative.parts):
        return None
    return (Path("wiki") / relative).as_posix()


def normalize_watchfiles_batch(changes: set[tuple[Change, str]], vault_path: Path) -> list[VaultFsEvent]:
    by_path: dict[str, set[Change]] = {}
    absolute_by_rel: dict[str, Path] = {}
    for change, raw_path in changes:
        relative = _canonical_relative_wiki_path(vault_path, raw_path)
        if relative is None:
            continue
        by_path.setdefault(relative, set()).add(change)
        absolute_by_rel[relative] = Path(raw_path)

    events: list[VaultFsEvent] = []
    for relative in sorted(by_path):
        absolute = absolute_by_rel[relative]
        seen = by_path[relative]
        if not absolute.exists():
            kind = "delete"
        elif Change.added in seen:
            kind = "add"
        else:
            kind = "modify"
        events.append(VaultFsEvent(kind, relative))
    return events


class VaultWatchAdapter:
    def __init__(
        self,
        settings: Settings,
        handler: Callable[[list[VaultFsEvent]], Awaitable[None]],
        *,
        watch_factory=awatch,
    ):
        self.settings = settings
        self.handler = handler
        self.watch_factory = watch_factory
        self._task: asyncio.Task | None = None
        self._started = asyncio.Event()

    async def _batches(self):
        wiki_root = self.settings.vault_path / "wiki"
        async for changes in self.watch_factory(
            wiki_root,
            debounce=self.settings.vault_watch_debounce_ms,
            step=min(250, self.settings.vault_watch_debounce_ms),
            recursive=True,
        ):
            events = normalize_watchfiles_batch(changes, self.settings.vault_path)
            if events:
                yield events

    async def run(self) -> None:
        self._started.set()
        async for events in self._batches():
            await self.handler(events)

    async def run_until_first_batch(self) -> None:
        async for events in self._batches():
            await self.handler(events)
            return

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._started.clear()
            self._task = asyncio.create_task(self.run(), name="vault-watchfiles")
            await self._started.wait()
            # Let run() advance into awatch before start() exposes readiness.
            await asyncio.sleep(0)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._started.clear()
```

- [ ] **Step 4: Run all watcher unit tests**

Run:

```powershell
python -m pytest tests/test_vault_watcher.py -q
```

Expected: PASS, 6 tests.

- [ ] **Step 5: Commit the watchfiles adapter**

```powershell
git add app/vault_watcher.py tests/test_vault_watcher.py
git commit -m "feat: adapt watchfiles events for vault sync"
```

---

### Task 5: Add, Modify, And Rename Event Orchestration

**Files:**
- Create: `app/vault_sync.py`
- Modify: `app/vault_events.py`
- Create: `tests/test_vault_sync.py`

- [ ] **Step 1: Write failing delegation and rename tests**

Create `tests/test_vault_sync.py`:

```python
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import Settings
from app.db import connect_app, init_app_db, json_dump
from app.vault_sync import VaultSyncService
from app.vault_watcher import VaultFsEvent


@dataclass
class FakeResult:
    status: str
    page_id: str
    revision_id: str | None = None


class FakeRevisionService:
    def __init__(self):
        self.calls = []

    def ingest_external_change(self, event_id, page_path, observation):
        self.calls.append(("ingest", event_id, page_path, observation.file_hash))
        return FakeResult("applied", "page_new", "wrev_new")

    def rename_page(self, event_id, old_page_path, page_path):
        self.calls.append(("rename", event_id, old_page_path, page_path))
        return FakeResult("renamed", "page_1", "wrev_audit")

    def delete_page(self, event_id, page_path):
        self.calls.append(("delete", event_id, page_path))
        return FakeResult("deleted", "page_1")

    def ensure_projection_jobs(self, page_id=None):
        self.calls.append(("ensure_projection", page_id))
        return []


def settings_for(tmp_path):
    return Settings(
        _env_file=None,
        database_backend="sqlite",
        database_path=tmp_path / "vault.db",
        vault_path=tmp_path / "vault",
        vault_watch_enabled=False,
        projection_worker_enabled=False,
    )


def seed_page(settings, *, page_id="page_1", path="wiki/product/old.md"):
    init_app_db(settings)
    now = "2026-07-13T08:00:00+00:00"
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path, domain, page_type, title, source_ids_json, review_status,
              created_at, updated_at, page_id, current_revision_id,
              revision_number, file_hash, semantic_hash, projection_epoch,
              lifecycle_status
            ) VALUES (?, 'product', 'policy', 'Old', ?, 'draft', ?, ?, ?, 'wrev_1', 1, ?, ?, 1, 'active')
            """,
            (path, json_dump(["src_1"]), now, now, page_id, "a" * 64, "b" * 64),
        )


def managed_page(page_id="page_1") -> bytes:
    return (
        "---\n"
        f"id: {page_id}\n"
        f"lgdo_page_id: {page_id}\n"
        "lgdo_revision_id: wrev_1\n"
        "title: Old\n"
        "source_ids: [src_1]\n"
        "domain: product\n"
        "page_type: policy\n"
        "review_status: draft\n"
        "---\n# Old\n"
    ).encode("utf-8")


def test_modify_delegates_bounded_observation_to_revision_service(tmp_path):
    settings = settings_for(tmp_path)
    seed_page(settings, path="wiki/product/page.md")
    target = settings.vault_path / "wiki/product/page.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(managed_page())
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)

    asyncio.run(
        service.handle_batch(
            [VaultFsEvent("modify", "wiki/product/page.md")],
            detected_at=datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc),
            stability_poll_interval=0,
        )
    )

    assert revisions.calls[0][0] == "ingest"
    assert revisions.calls[0][2] == "wiki/product/page.md"


def test_windows_delete_add_batch_is_renamed_by_page_id(tmp_path):
    settings = settings_for(tmp_path)
    seed_page(settings)
    new_path = settings.vault_path / "wiki/product/new.md"
    new_path.parent.mkdir(parents=True)
    new_path.write_bytes(managed_page())
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)

    asyncio.run(
        service.handle_batch(
            [
                VaultFsEvent("add", "wiki/product/new.md"),
                VaultFsEvent("delete", "wiki/product/old.md"),
            ],
            detected_at=datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc),
            stability_poll_interval=0,
        )
    )

    assert [call[0] for call in revisions.calls] == ["rename"]
    assert revisions.calls[0][2:] == ("wiki/product/old.md", "wiki/product/new.md")
    assert service.snapshot()["pending_deletes"] == 0
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m pytest tests/test_vault_sync.py::test_modify_delegates_bounded_observation_to_revision_service tests/test_vault_sync.py::test_windows_delete_add_batch_is_renamed_by_page_id -q
```

Expected: FAIL because `VaultSyncService` and rename lookup helpers do not exist.

- [ ] **Step 3: Add exact pending-delete lookup helpers**

Add these methods to `VaultEventStore` in `app/vault_events.py`:

```python
    def find_pending_deletes(
        self,
        *,
        page_id: str | None = None,
        file_hash: str | None = None,
    ) -> list[PendingVaultDelete]:
        clauses = ["status = 'pending'"]
        params: list[str] = []
        if page_id:
            clauses.append("page_id = ?")
            params.append(page_id)
        elif file_hash:
            clauses.append("file_hash = ?")
            params.append(file_hash)
        else:
            return []
        with connect_app(self.settings) as conn:
            rows = conn.execute(
                f"SELECT * FROM pending_vault_deletes WHERE {' AND '.join(clauses)} ORDER BY detected_at, id",
                params,
            ).fetchall()
        return [
            PendingVaultDelete(
                row["id"],
                row["occurrence_event_id"],
                row["page_id"],
                row["old_page_path"],
                row["file_hash"],
                row["semantic_hash"],
                datetime.fromisoformat(row["detected_at"]),
                datetime.fromisoformat(row["expires_at"]),
            )
            for row in rows
        ]

    def page_by_path(self, page_path: str):
        with connect_app(self.settings) as conn:
            return conn.execute("SELECT * FROM wiki_pages WHERE path = ?", (page_path,)).fetchone()

    def has_active_intent(self, page_id: str) -> bool:
        with connect_app(self.settings) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM vault_write_intents
                WHERE page_id = ? AND status IN ('pending', 'captured', 'installed', 'recovery_required')
                LIMIT 1
                """,
                (page_id,),
            ).fetchone()
        return row is not None
```

Keep the occurrence-based delete identity from Task 3. Every persisted delete event owns one `vdel_<uuid>` through unique `occurrence_event_id`; replaying that event returns the same row, while a later delete cycle for the same page creates a new row. Never derive a pending-delete primary key from `page_id` or path.

- [ ] **Step 4: Implement live event orchestration with delete-first ordering**

Create `app/vault_sync.py`:

```python
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.vault_events import VaultEvent, VaultEventStore
from app.vault_watcher import UnstableVaultFile, VaultFsEvent, wait_for_stable_observation
from app.wiki_markdown import FrontmatterLimits, parse_wiki_bytes
from app.wiki_revisions import WikiRevisionService
from app.vault_writer import IntentExecutor


class VaultSyncService:
    def __init__(
        self,
        settings: Settings,
        *,
        revisions=None,
        events=None,
        intent_executor: IntentExecutor | None = None,
    ):
        self.settings = settings
        self.revisions = revisions or WikiRevisionService(settings)
        self.events = events or VaultEventStore(settings)
        self.intent_executor = intent_executor or IntentExecutor(settings)
        self.limits = FrontmatterLimits(max_file_bytes=settings.vault_watch_max_file_bytes)
        self._semaphore = asyncio.Semaphore(settings.vault_watch_concurrency)
        self._page_locks: dict[str, asyncio.Lock] = {}
        self.last_event_at: str | None = None
        self.last_error: str | None = None
        self.running = False

    def _lock(self, key: str) -> asyncio.Lock:
        return self._page_locks.setdefault(key, asyncio.Lock())

    def _frontmatter_page_id(self, observation) -> str | None:
        if observation.content_bytes is None:
            return None
        try:
            document = parse_wiki_bytes(observation.content_bytes, self.limits)
        except Exception:
            return None
        value = document.frontmatter.get("lgdo_page_id") or document.frontmatter.get("id")
        return str(value) if value else None

    @staticmethod
    def _event_status(mutation_status: str) -> str:
        if mutation_status == "ignored":
            return "ignored"
        if mutation_status == "failed":
            return "failed"
        return "applied"

    # MutationResult.revision_id is occurrence/audit metadata only. It is never
    # used as the expected current revision or as a projection visibility gate.

    async def _queue_delete(self, event: VaultEvent, detected_at: datetime) -> None:
        page = self.events.page_by_path(event.page_path)
        if page is None:
            self.events.finish_event(event.id, "ignored")
            return
        self.events.create_pending_delete(
            occurrence_event_id=event.id,
            page_id=page["page_id"],
            old_page_path=event.page_path,
            file_hash=page["file_hash"],
            semantic_hash=page["semantic_hash"],
            detected_at=detected_at,
            expires_at=detected_at + timedelta(milliseconds=self.settings.vault_rename_grace_ms),
        )
        self.events.finish_event(event.id, "applied")

    async def _apply_file_event(
        self,
        event: VaultEvent,
        *,
        stability_poll_interval: float,
    ) -> None:
        absolute = self.settings.vault_path / event.page_path
        try:
            observation = await wait_for_stable_observation(
                absolute,
                self.limits,
                timeout_seconds=self.settings.vault_watch_stability_timeout_seconds,
                poll_interval=stability_poll_interval,
            )
            page_id = self._frontmatter_page_id(observation)
            candidates = self.events.find_pending_deletes(
                page_id=page_id,
                file_hash=None if page_id else observation.file_hash,
            )
            if len(candidates) == 1:
                candidate = candidates[0]
                async with self._lock(candidate.page_id):
                    result = await asyncio.to_thread(
                        self.revisions.rename_page,
                        event.id,
                        candidate.old_page_path,
                        event.page_path,
                    )
                    if result.status in {"renamed", "ignored"}:
                        self.events.cancel_delete(candidate.id, event.id)
                    elif result.status == "conflicted":
                        self.events.protect_delete(candidate.id, event.id)
                    self.events.finish_event(
                        event.id,
                        self._event_status(result.status),
                        result_revision_id=result.revision_id,
                    )
                return
            if len(candidates) > 1:
                self.events.upsert_issue(
                    event.page_path,
                    observation.file_hash,
                    page_id,
                    "ambiguous_rename",
                    "multiple pending deletes match this add candidate",
                )
            lock_key = page_id or event.page_path
            async with self._semaphore, self._lock(lock_key):
                result = await asyncio.to_thread(
                    self.revisions.ingest_external_change,
                    event.id,
                    event.page_path,
                    observation,
                )
                self.events.finish_event(
                    event.id,
                    self._event_status(result.status),
                    result_revision_id=result.revision_id,
                )
        except UnstableVaultFile as exc:
            self.last_error = str(exc)
            self.events.upsert_issue(event.page_path, "unstable", None, "unstable_file", str(exc))
            self.events.finish_event(event.id, "failed")

    async def handle_batch(
        self,
        batch: list[VaultFsEvent],
        *,
        detected_at: datetime | None = None,
        stability_poll_interval: float = 0.05,
    ) -> None:
        detected_at = detected_at or datetime.now(timezone.utc)
        persisted = [
            self.events.begin_event(item.kind, item.page_path, detected_at=detected_at)
            for item in batch
        ]
        for event in persisted:
            if event.kind == "delete":
                await self._queue_delete(event, detected_at)
        await asyncio.gather(
            *(
                self._apply_file_event(event, stability_poll_interval=stability_poll_interval)
                for event in persisted
                if event.kind in {"add", "modify"}
            )
        )
        self.last_event_at = detected_at.isoformat()

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "last_event_at": self.last_event_at,
            "last_error": self.last_error,
            **self.events.snapshot(),
        }
```

- [ ] **Step 5: Run live-event tests**

Run:

```powershell
python -m pytest tests/test_vault_sync.py -q
```

Expected: PASS, 2 tests.

- [ ] **Step 6: Commit live event orchestration**

```powershell
git add app/vault_events.py app/vault_sync.py tests/test_vault_sync.py
git commit -m "feat: route vault edits through revision service"
```

---

### Task 6: Delete Expiry And Startup Reconcile

**Files:**
- Modify: `app/vault_events.py`
- Modify: `app/vault_sync.py`
- Modify: `tests/test_vault_sync.py`

- [ ] **Step 1: Add failing slow-add, true-delete, and offline-rename tests**

Append to `tests/test_vault_sync.py`:

```python
from datetime import timedelta


def test_delete_expiry_pauses_for_add_detected_before_deadline(tmp_path):
    settings = settings_for(tmp_path)
    seed_page(settings)
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)
    detected = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
    delete_event = service.events.begin_event("delete", "wiki/product/old.md", detected_at=detected)
    asyncio.run(service._queue_delete(delete_event, detected))
    service.events.begin_event(
        "add",
        "wiki/product/slow.md",
        detected_at=detected + timedelta(seconds=4),
    )

    applied = asyncio.run(service.expire_deletes(detected + timedelta(seconds=6)))

    assert applied == 0
    assert not any(call[0] == "delete" for call in revisions.calls)
    assert service.snapshot()["pending_deletes"] == 1


def test_true_delete_applies_after_grace_and_rechecks_path(tmp_path):
    settings = settings_for(tmp_path)
    seed_page(settings)
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)
    detected = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
    asyncio.run(
        service.handle_batch(
            [VaultFsEvent("delete", "wiki/product/old.md")],
            detected_at=detected,
        )
    )

    applied = asyncio.run(service.expire_deletes(detected + timedelta(seconds=6)))

    assert applied == 1
    assert [call[0] for call in revisions.calls] == ["delete"]
    assert service.snapshot()["pending_deletes"] == 0


class FakeIntentExecutor:
    def __init__(self, calls):
        self.calls = calls

    def reconcile_all(self):
        self.calls.append("intents")


def test_startup_reconcile_pairs_offline_rename_before_missing(tmp_path):
    settings = settings_for(tmp_path)
    seed_page(settings)
    new_path = settings.vault_path / "wiki/product/new.md"
    new_path.parent.mkdir(parents=True)
    new_path.write_bytes(managed_page())
    revisions = FakeRevisionService()
    order = []
    service = VaultSyncService(
        settings,
        revisions=revisions,
        intent_executor=FakeIntentExecutor(order),
    )

    result = asyncio.run(service.reconcile_startup(stability_poll_interval=0))

    assert order[0] == "intents"
    assert result["renamed"] == 1
    assert [call[0] for call in revisions.calls] == ["rename", "ensure_projection"]


def test_startup_reconcile_replays_pending_event_before_inventory(tmp_path):
    settings = settings_for(tmp_path)
    init_app_db(settings)
    page = settings.vault_path / "wiki/product/pending.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntitle: Pending\nsource_ids: []\ndomain: product\n"
        "page_type: note\nreview_status: draft\n---\n# Pending\n",
        encoding="utf-8",
    )
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)
    event = service.events.begin_event(
        "add",
        "wiki/product/pending.md",
        detected_at=datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc),
    )

    result = asyncio.run(service.reconcile_startup(stability_poll_interval=0))

    assert revisions.calls[0][0:3] == ("ingest", event.id, "wiki/product/pending.md")
    assert result["replayed"] == 1
    assert service.snapshot()["pending_events"] == 0


def test_startup_reconcile_ingests_unidentified_and_invalid_files(tmp_path):
    settings = settings_for(tmp_path)
    init_app_db(settings)
    wiki = settings.vault_path / "wiki/product"
    wiki.mkdir(parents=True)
    (wiki / "no-id.md").write_text(
        "---\ntitle: No ID\nsource_ids: []\ndomain: product\n"
        "page_type: note\nreview_status: draft\n---\n# No ID\n",
        encoding="utf-8",
    )
    (wiki / "invalid.md").write_text("---\ntitle: [broken\n---\n", encoding="utf-8")
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)

    asyncio.run(service.reconcile_startup(stability_poll_interval=0))

    ingested_paths = {call[2] for call in revisions.calls if call[0] == "ingest"}
    assert ingested_paths == {"wiki/product/no-id.md", "wiki/product/invalid.md"}


def test_startup_reconcile_isolates_every_duplicate_page_id(tmp_path):
    settings = settings_for(tmp_path)
    seed_page(settings)
    wiki = settings.vault_path / "wiki/product"
    wiki.mkdir(parents=True)
    (wiki / "duplicate-a.md").write_bytes(managed_page())
    (wiki / "duplicate-b.md").write_bytes(managed_page())
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)

    result = asyncio.run(service.reconcile_startup(stability_poll_interval=0))

    assert not any(call[0] in {"rename", "delete", "ingest"} for call in revisions.calls)
    assert result["duplicate_page_ids"] == 1
    assert service.snapshot()["open_issues"] == 2
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
python -m pytest tests/test_vault_sync.py -k "delete_expiry or true_delete or startup_reconcile" -q
```

Expected: FAIL because expiry and startup reconcile are absent.

- [ ] **Step 3: Add inventory queries to the event store**

Add to `VaultEventStore`:

```python
    def list_pages(self) -> list[dict]:
        with connect_app(self.settings) as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM wiki_pages WHERE lifecycle_status IN ('active', 'invalid', 'deleted') ORDER BY page_id"
                ).fetchall()
            ]

    def pending_events(self) -> list[VaultEvent]:
        with connect_app(self.settings) as conn:
            rows = conn.execute(
                "SELECT * FROM vault_change_events WHERE status = 'pending' ORDER BY detected_at, id"
            ).fetchall()
        return [
            VaultEvent(
                id=row["id"],
                kind=row["kind"],
                page_path=row["page_path"],
                old_page_path=row["old_page_path"],
                detected_at=datetime.fromisoformat(row["detected_at"]),
            )
            for row in rows
        ]
```

- [ ] **Step 4: Implement expiry with the candidate barrier and startup ordering**

`VaultSyncService.__init__` already receives and stores the shared `IntentExecutor` dependency in Task 5. Keep that complete constructor signature; do not add an unbound incremental assignment here.

Add these methods to `VaultSyncService`:

```python
    async def expire_deletes(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        applied = 0
        for pending in self.events.list_due_deletes(now):
            if self.events.has_unclassified_add_before(pending.expires_at):
                continue
            absolute = self.settings.vault_path / pending.old_page_path
            if absolute.exists() or self.events.has_active_intent(pending.page_id):
                continue
            async with self._lock(pending.page_id):
                result = await asyncio.to_thread(
                    self.revisions.delete_page,
                    pending.id,
                    pending.old_page_path,
                )
                if result.status in {"deleted", "ignored"}:
                    self.events.complete_delete(pending.id)
                    applied += 1
        return applied

    async def _replay_pending_events(self, *, stability_poll_interval: float) -> set[str]:
        replayed_paths: set[str] = set()
        for event in self.events.pending_events():
            replayed_paths.add(event.page_path)
            if event.kind == "delete":
                await self._queue_delete(event, event.detected_at)
                continue
            if event.kind in {"add", "modify"}:
                absolute = self.settings.vault_path / event.page_path
                if absolute.exists():
                    await self._apply_file_event(
                        event,
                        stability_poll_interval=stability_poll_interval,
                    )
                else:
                    self.events.finish_event(event.id, "ignored")
                continue
            if event.kind == "rename" and event.old_page_path:
                result = await asyncio.to_thread(
                    self.revisions.rename_page,
                    event.id,
                    event.old_page_path,
                    event.page_path,
                )
                self.events.finish_event(
                    event.id,
                    self._event_status(result.status),
                    result_revision_id=result.revision_id,
                )
                continue
            self.events.upsert_issue(
                event.page_path,
                f"event-{event.id}",
                None,
                "unsupported_pending_event",
                f"cannot replay persisted event kind {event.kind!r}",
            )
            self.events.finish_event(event.id, "failed")
        return replayed_paths

    async def _inventory(
        self,
        *,
        stability_poll_interval: float,
    ) -> list[tuple[str, object, str | None]]:
        inventory: list[tuple[str, object, str | None]] = []
        wiki_root = self.settings.vault_path / "wiki"
        for absolute in sorted(wiki_root.rglob("*.md")):
            if absolute.is_symlink() or not absolute.is_file():
                continue
            relative = absolute.relative_to(self.settings.vault_path).as_posix()
            observation = await wait_for_stable_observation(
                absolute,
                self.limits,
                timeout_seconds=self.settings.vault_watch_stability_timeout_seconds,
                poll_interval=stability_poll_interval,
            )
            page_id = self._frontmatter_page_id(observation)
            inventory.append((relative, observation, page_id))
        return inventory

    def _classify_inventory(self, inventory):
        by_page_id: dict[str, list[tuple[str, object]]] = {}
        for relative, observation, page_id in inventory:
            if page_id:
                by_page_id.setdefault(page_id, []).append((relative, observation))

        duplicate_page_ids = {
            page_id for page_id, entries in by_page_id.items() if len(entries) > 1
        }
        for page_id in sorted(duplicate_page_ids):
            for relative, observation in by_page_id[page_id]:
                self.events.upsert_issue(
                    relative,
                    observation.file_hash,
                    page_id,
                    "duplicate_page_id",
                    "startup inventory contains the same managed page ID more than once",
                )
        unique_by_id = {
            page_id: entries[0]
            for page_id, entries in by_page_id.items()
            if len(entries) == 1
        }
        return unique_by_id, duplicate_page_ids

    async def _ingest_inventory_entry(self, relative: str, observation, now: datetime):
        event = self.events.begin_event("add", relative, detected_at=now)
        result = await asyncio.to_thread(
            self.revisions.ingest_external_change,
            event.id,
            relative,
            observation,
        )
        self.events.finish_event(
            event.id,
            self._event_status(result.status),
            result_revision_id=result.revision_id,
        )
        return result

    async def reconcile_startup(
        self,
        *,
        stability_poll_interval: float = 0.05,
        reconcile_intents: bool = True,
    ) -> dict[str, int]:
        if reconcile_intents:
            await asyncio.to_thread(self.intent_executor.reconcile_all)
        replayed_paths = await self._replay_pending_events(
            stability_poll_interval=stability_poll_interval
        )
        inventory = await self._inventory(stability_poll_interval=stability_poll_interval)
        unique_by_id, duplicate_page_ids = self._classify_inventory(inventory)
        renamed = 0
        ingested = 0
        missing = 0
        now = datetime.now(timezone.utc)
        pages = self.events.list_pages()
        pages_by_id = {page["page_id"]: page for page in pages}

        # Unknown IDs, missing IDs, malformed frontmatter, and oversized files
        # all cross the revision boundary so it can persist a valid/invalid observation.
        for relative, observation, page_id in inventory:
            if page_id in duplicate_page_ids or relative in replayed_paths:
                continue
            if page_id is None or page_id not in pages_by_id:
                result = await self._ingest_inventory_entry(relative, observation, now)
                ingested += int(result.status in {"applied", "invalid"})

        # Only a unique, valid page ID may participate in rename matching.
        for page in pages:
            page_id = page["page_id"]
            if page_id in duplicate_page_ids:
                continue
            disk = unique_by_id.get(page_id)
            current_path = page["path"]
            current_exists = (self.settings.vault_path / current_path).exists()
            if disk and disk[0] != current_path and current_exists:
                self.events.upsert_issue(
                    disk[0],
                    disk[1].file_hash,
                    page_id,
                    "path_occupied",
                    "managed page ID moved while its database path is still occupied",
                )
                continue
            if disk and disk[0] != current_path:
                event = self.events.begin_event("rename", disk[0], old_page_path=current_path, detected_at=now)
                result = await asyncio.to_thread(
                    self.revisions.rename_page,
                    event.id,
                    current_path,
                    disk[0],
                )
                self.events.finish_event(
                    event.id,
                    self._event_status(result.status),
                    result_revision_id=result.revision_id,
                )
                renamed += int(result.status == "renamed")
                if result.status not in {"renamed", "ignored"}:
                    continue
                for pending in self.events.find_pending_deletes(page_id=page_id):
                    self.events.cancel_delete(pending.id, event.id)
                current_path = disk[0]
            if disk and (
                disk[1].file_hash != page.get("observed_file_hash", page.get("file_hash"))
                or page["lifecycle_status"] != "active"
            ):
                event = self.events.begin_event("modify", current_path, detected_at=now)
                result = await asyncio.to_thread(
                    self.revisions.ingest_external_change,
                    event.id,
                    current_path,
                    disk[1],
                )
                self.events.finish_event(
                    event.id,
                    self._event_status(result.status),
                    result_revision_id=result.revision_id,
                )
                ingested += int(result.status == "applied")
            if not disk and not current_exists and page["lifecycle_status"] != "deleted":
                event = self.events.begin_event("delete", current_path, detected_at=now)
                await self._queue_delete(event, now)
                missing += 1
        projection_jobs = await asyncio.to_thread(self.revisions.ensure_projection_jobs, None)
        return {
            "renamed": renamed,
            "ingested": ingested,
            "missing": missing,
            "replayed": len(replayed_paths),
            "duplicate_page_ids": len(duplicate_page_ids),
            "projection_jobs": len(projection_jobs),
        }
```

The add event remains `status='pending'` for the entire stability/parse/classification phase, so `expire_deletes()` cannot run ahead of a pre-deadline candidate. Only `finish_event()` releases that barrier.

- [ ] **Step 5: Run state-machine and revision integration tests**

Run:

```powershell
python -m pytest tests/test_vault_sync.py tests/test_wiki_revisions.py tests/test_vault_writer.py -q
```

Expected: PASS. The exact count includes all prerequisite revision/writer tests.

- [ ] **Step 6: Commit delete grace and startup reconcile**

```powershell
git add app/vault_events.py app/vault_sync.py tests/test_vault_sync.py
git commit -m "feat: reconcile vault rename and delete races"
```

---

### Task 7: Versioned Obsidian Resources And Safe Install Scripts

**Files:**
- Create: `app/obsidian.py`
- Modify: `app/vault.py`
- Create: `resources/obsidian-vault/.obsidian/app.json`
- Create: `resources/obsidian-vault/.obsidian/core-plugins.json`
- Create: `resources/obsidian-vault/.obsidian/templates.json`
- Create: `resources/obsidian-vault/templates/Wiki Page.md`
- Create: `resources/obsidian-vault/indexes/Home.md`
- Create: `resources/obsidian-vault/README.md`
- Create: `scripts/install-obsidian-vault.ps1`
- Create: `scripts/open-obsidian.ps1`
- Modify: `.gitignore`
- Create: `tests/test_obsidian_assets.py`

- [ ] **Step 1: Write failing resource/install/link tests**

Create `tests/test_obsidian_assets.py`:

```python
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.obsidian import build_obsidian_uri, ensure_obsidian_vault


ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ROOT = ROOT / "resources" / "obsidian-vault"


def test_versioned_obsidian_resources_are_stable_and_exclude_workspace_state():
    app_config = json.loads((RESOURCE_ROOT / ".obsidian/app.json").read_text(encoding="utf-8"))
    plugins = json.loads((RESOURCE_ROOT / ".obsidian/core-plugins.json").read_text(encoding="utf-8"))
    template = (RESOURCE_ROOT / "templates/Wiki Page.md").read_text(encoding="utf-8")

    assert app_config["newFileFolderPath"] == "wiki"
    assert {"backlink", "outgoing-link", "tag-pane", "templates"} <= set(plugins)
    assert "lgdo_page_id" not in template
    assert "lgdo_revision_id" not in template
    assert not list(RESOURCE_ROOT.rglob("workspace*.json"))
    assert not (RESOURCE_ROOT / ".obsidian/plugins").exists()


def test_install_preserves_user_config_and_refresh_backs_up_then_merges(tmp_path):
    vault = tmp_path / "My Vault"
    config_dir = vault / ".obsidian"
    config_dir.mkdir(parents=True)
    (config_dir / "app.json").write_text(
        json.dumps({"showLineNumber": True, "newFileFolderPath": "custom"}),
        encoding="utf-8",
    )

    initial = ensure_obsidian_vault(vault, refresh=False)
    unchanged = json.loads((config_dir / "app.json").read_text(encoding="utf-8"))
    refreshed = ensure_obsidian_vault(vault, refresh=True)
    merged = json.loads((config_dir / "app.json").read_text(encoding="utf-8"))

    assert initial.drifted == [".obsidian/app.json"]
    assert unchanged["newFileFolderPath"] == "custom"
    assert merged["showLineNumber"] is True
    assert merged["newFileFolderPath"] == "wiki"
    assert refreshed.backup_dir is not None
    assert (refreshed.backup_dir / ".obsidian/app.json").exists()


def test_obsidian_uri_encodes_vault_chinese_spaces_and_subdirectories():
    uri = build_obsidian_uri("LGDO 知识库", "wiki/产品/退款 政策.md")

    assert uri.startswith("obsidian://open?")
    assert "LGDO%20%E7%9F%A5%E8%AF%86%E5%BA%93" in uri
    assert "wiki%2F%E4%BA%A7%E5%93%81%2F%E9%80%80%E6%AC%BE%20%E6%94%BF%E7%AD%96.md" in uri


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell is unavailable")
def test_open_script_print_only_returns_encoded_uri(tmp_path):
    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(ROOT / "scripts/open-obsidian.ps1"),
            "-VaultName",
            "LGDO 知识库",
            "-PagePath",
            "wiki/产品/退款 政策.md",
            "-PrintOnly",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip().startswith("obsidian://open?")
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
python -m pytest tests/test_obsidian_assets.py -q
```

Expected: FAIL because resources, installer helpers, and scripts do not exist.

- [ ] **Step 3: Add the exact version-controlled resources**

Create `.obsidian/app.json`:

```json
{
  "alwaysUpdateLinks": true,
  "newFileLocation": "folder",
  "newFileFolderPath": "wiki",
  "showUnsupportedFiles": false,
  "useMarkdownLinks": false
}
```

Create `.obsidian/core-plugins.json`:

```json
[
  "file-explorer",
  "global-search",
  "switcher",
  "backlink",
  "outgoing-link",
  "tag-pane",
  "page-preview",
  "templates",
  "outline",
  "word-count"
]
```

Create `.obsidian/templates.json`:

```json
{
  "folder": "templates",
  "dateFormat": "YYYY-MM-DD",
  "timeFormat": "HH:mm"
}
```

Create `templates/Wiki Page.md`:

```markdown
---
title: ""
source_ids: []
domain: product
page_type: feature
review_status: draft
owner: ""
tags: []
aliases: []
---

# Untitled
```

Create `indexes/Home.md`:

```markdown
# LGDO Knowledge Home

- [[wiki/product|Product knowledge]]
- [[reviews|Pending reviews and conflicts]]
- [[logs|Sync logs]]
```

Create `resources/obsidian-vault/README.md` with this operational contract:

```markdown
# LGDO Obsidian Vault

Edit only `wiki/**/*.md`. The `raw`, `normalized`, `jsonl`, `indexes`, `reviews`, and `logs` directories are generated or read-only views.

Do not edit `id`, `lgdo_page_id`, `lgdo_revision_id`, or `lgdo_write_token`. Use `scripts/install-obsidian-vault.ps1 -Refresh` for an explicit managed-setting refresh; the command backs up existing configuration before merging.
```

- [ ] **Step 4: Implement materialization, drift, refresh backup, and URI building**

Create `app/obsidian.py` with `ObsidianInstallResult`, `build_obsidian_uri()`, and `ensure_obsidian_vault()`:

```python
from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode


RESOURCE_ROOT = Path(__file__).resolve().parents[1] / "resources" / "obsidian-vault"
MANAGED_JSON = [
    Path(".obsidian/app.json"),
    Path(".obsidian/core-plugins.json"),
    Path(".obsidian/templates.json"),
]
COPY_IF_MISSING = [
    Path("templates/Wiki Page.md"),
    Path("indexes/Home.md"),
    Path("README.md"),
]


@dataclass(frozen=True)
class ObsidianInstallResult:
    installed: list[str]
    drifted: list[str]
    backup_dir: Path | None


def build_obsidian_uri(vault_name: str, page_path: str) -> str:
    query = urlencode(
        {"vault": vault_name, "file": page_path.replace("\\", "/")},
        quote_via=quote,
    )
    return f"obsidian://open?{query}"


def _merge_json(relative: Path, source, current):
    if relative.name == "core-plugins.json":
        return sorted(set(current or []) | set(source or []))
    merged = dict(current or {})
    merged.update(source or {})
    return merged


def ensure_obsidian_vault(vault_path: Path, *, refresh: bool = False) -> ObsidianInstallResult:
    vault_path.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    drifted: list[str] = []
    backup_dir: Path | None = None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for relative in MANAGED_JSON:
        source_path = RESOURCE_ROOT / relative
        target_path = vault_path / relative
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if not target_path.exists():
            shutil.copy2(source_path, target_path)
            installed.append(relative.as_posix())
            continue
        source = json.loads(source_path.read_text(encoding="utf-8"))
        current = json.loads(target_path.read_text(encoding="utf-8"))
        merged = _merge_json(relative, source, current)
        if merged == current:
            continue
        drifted.append(relative.as_posix())
        if not refresh:
            continue
        backup_dir = backup_dir or vault_path / ".lgdo" / "obsidian-backups" / stamp
        backup_path = backup_dir / relative
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target_path, backup_path)
        target_path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    for relative in COPY_IF_MISSING:
        target_path = vault_path / relative
        if target_path.exists():
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(RESOURCE_ROOT / relative, target_path)
        installed.append(relative.as_posix())
    return ObsidianInstallResult(installed, drifted, backup_dir)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    result = ensure_obsidian_vault(args.vault, refresh=args.refresh)
    print(json.dumps({
        "installed": result.installed,
        "drifted": result.drifted,
        "backup_dir": str(result.backup_dir) if result.backup_dir else None,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

Change `app/vault.py::VAULT_DIRS` from `index` to `indexes`, add `templates`, and call `ensure_obsidian_vault(vault_path)` at the end of `ensure_vault()`.

- [ ] **Step 5: Add testable PowerShell commands and ignore rules**

Create `scripts/install-obsidian-vault.ps1`:

```powershell
param([string]$VaultPath = "vault", [switch]$Refresh)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$resolved = [IO.Path]::GetFullPath((Join-Path $root $VaultPath))
$args = @("-m", "app.obsidian", "--vault", $resolved)
if ($Refresh) { $args += "--refresh" }
Push-Location $root
try { & python @args } finally { Pop-Location }
```

Create `scripts/open-obsidian.ps1`:

```powershell
param(
  [Parameter(Mandatory=$true)][string]$VaultName,
  [Parameter(Mandatory=$true)][string]$PagePath,
  [switch]$PrintOnly
)
$ErrorActionPreference = "Stop"
$vault = [uri]::EscapeDataString($VaultName).Replace("%2F", "/")
$file = [uri]::EscapeDataString($PagePath.Replace("\", "/"))
$uri = "obsidian://open?vault=$vault&file=$file"
if ($PrintOnly) { Write-Output $uri; exit 0 }
try { Start-Process $uri -ErrorAction Stop | Out-Null }
catch { throw "Obsidian could not be opened. Install Obsidian and register the obsidian:// protocol. URI: $uri" }
```

Append to `.gitignore`:

```gitignore
resources/obsidian-vault/.obsidian/workspace*.json
resources/obsidian-vault/.obsidian/plugins/
resources/obsidian-vault/.trash/
```

- [ ] **Step 6: Run resource and script tests**

Run:

```powershell
python -m pytest tests/test_obsidian_assets.py -q
```

Expected: PASS, 4 tests; the PowerShell test is skipped only where `pwsh` is unavailable.

- [ ] **Step 7: Commit the Obsidian distribution**

```powershell
git add app/obsidian.py app/vault.py resources/obsidian-vault scripts/install-obsidian-vault.ps1 scripts/open-obsidian.ps1 .gitignore tests/test_obsidian_assets.py
git commit -m "feat: distribute managed Obsidian vault resources"
```

---

### Task 8: FastAPI Lifespan, Status, Reconcile, And Deep-Link APIs

**Files:**
- Modify: `app/vault_sync.py`
- Modify: `app/main.py`
- Modify: `app/models.py`
- Modify: `app/api.py`
- Create: `tests/test_vault_api.py`

- [ ] **Step 1: Write failing API and lifecycle-order tests**

Create `tests/test_vault_api.py`:

```python
import asyncio
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


def configure(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", tmp_path / "api.db")
    monkeypatch.setattr(settings, "vault_path", tmp_path / "LGDO Vault")
    monkeypatch.setattr(settings, "vault_watch_enabled", False)
    monkeypatch.setattr(settings, "projection_worker_enabled", False)
    return settings


def test_vault_status_reconcile_and_obsidian_link(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)

    with TestClient(app) as client:
        status = client.get("/api/internal/vault/status")
        reconcile = client.post("/api/internal/vault/reconcile")
        link = client.get("/api/internal/wiki/pages/wiki/%E4%BA%A7%E5%93%81/a%20b.md/obsidian-link")

    assert status.status_code == 200
    assert status.json()["configured"] is True
    assert "pending_deletes" in status.json()
    assert reconcile.status_code == 202
    assert reconcile.json()["job_id"].startswith("vrecon_")
    assert link.status_code == 200
    assert link.json()["url"].startswith("obsidian://open?")


def test_vault_status_is_not_clean_for_backlog_issue_or_obsidian_drift(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.api.projection_health",
        lambda settings: {
            "rag": {"pending": 1, "running": 0, "failed": 0},
            "gbrain": {"pending": 0, "running": 0, "failed": 0, "degraded": False},
        },
    )

    with TestClient(app) as client:
        app.state.obsidian_status["drifted"] = [".obsidian/app.json"]
        app.state.vault_sync.events.begin_event(
            "add",
            "wiki/product/pending.md",
            detected_at=datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc),
        )
        app.state.vault_sync.events.upsert_issue(
            "wiki/product/invalid.md",
            "a" * 64,
            None,
            "invalid_frontmatter",
            "invalid YAML",
        )
        status = client.get("/api/internal/vault/status").json()

    assert status["clean"] is False
    assert status["degraded"] is True
    assert status["projection_backlog"] == 1


def test_lifespan_orders_intent_recovery_before_reconcile_and_workers(monkeypatch):
    order = []

    class FakeIntentExecutor:
        def __init__(self, settings):
            pass
        def reconcile_all(self):
            order.append("intent-reconcile")

    class FakeVaultSync:
        def __init__(self, settings, intent_executor=None):
            pass
        async def reconcile_startup(self, reconcile_intents=True):
            assert reconcile_intents is False
            order.append("vault-reconcile")
        async def start(self):
            order.append("watcher-start")
        async def stop(self):
            order.append("watcher-stop")

    class FakeProjectionWorker:
        def __init__(self, settings):
            pass
        async def start(self):
            order.append("projection-start")
        async def stop(self):
            order.append("projection-stop")

    monkeypatch.setattr("app.main.IntentExecutor", FakeIntentExecutor)
    monkeypatch.setattr("app.main.VaultSyncService", FakeVaultSync)
    monkeypatch.setattr("app.main.ProjectionWorker", FakeProjectionWorker)
    monkeypatch.setattr("app.main.settings.vault_watch_enabled", True)
    monkeypatch.setattr("app.main.settings.projection_worker_enabled", True)

    async def run_lifespan():
        from app.main import lifespan
        async with lifespan(app):
            assert order == ["intent-reconcile", "vault-reconcile", "projection-start", "watcher-start"]

    asyncio.run(run_lifespan())
    assert order[-2:] == ["watcher-stop", "projection-stop"]
```

- [ ] **Step 2: Run API tests and verify RED**

Run:

```powershell
python -m pytest tests/test_vault_api.py -q
```

Expected: FAIL because service lifecycle methods, models, routes, and app state are absent.

- [ ] **Step 3: Add service start/stop and asynchronous reconcile jobs**

Add to `VaultSyncService`:

```python
import uuid
from app.vault_watcher import VaultWatchAdapter

    async def _expiry_loop(self) -> None:
        while self.running:
            await self.expire_deletes()
            await asyncio.sleep(0.25)

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._adapter = VaultWatchAdapter(self.settings, self.handle_batch)
        await self._adapter.start()
        self._expiry_task = asyncio.create_task(self._expiry_loop(), name="vault-delete-expiry")

    async def stop(self) -> None:
        self.running = False
        if getattr(self, "_adapter", None):
            await self._adapter.stop()
        task = getattr(self, "_expiry_task", None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for task in list(getattr(self, "_reconcile_tasks", set())):
            await task

    def request_reconcile(self) -> str:
        job_id = f"vrecon_{uuid.uuid4().hex[:16]}"
        task = asyncio.create_task(self.reconcile_startup(), name=job_id)
        self._reconcile_tasks = getattr(self, "_reconcile_tasks", set())
        self._reconcile_tasks.add(task)
        task.add_done_callback(self._reconcile_tasks.discard)
        return job_id
```

- [ ] **Step 4: Add response models, routes, and ordered lifespan wiring**

Add to `app/models.py`:

```python
class VaultStatusResponse(BaseModel):
    configured: bool
    running: bool
    clean: bool
    degraded: bool
    last_event_at: str | None = None
    last_error: str | None = None
    pending_events: int = 0
    pending_deletes: int = 0
    open_issues: int = 0
    invalid_pages: int = 0
    projection_backlog: int = 0
    projection: dict = Field(default_factory=dict)
    obsidian: dict = Field(default_factory=dict)


class VaultReconcileResponse(BaseModel):
    job_id: str
    status: str = "queued"


class ObsidianLinkResponse(BaseModel):
    url: str
```

Add async routes to `app/api.py`; route order must place `obsidian-link` before the generic page GET route:

```python
from fastapi import Response
from app.models import ObsidianLinkResponse, VaultReconcileResponse, VaultStatusResponse
from app.obsidian import build_obsidian_uri
from app.projection_worker import projection_health


@router.get("/vault/status", response_model=VaultStatusResponse)
async def vault_status_endpoint(request: Request, user: UserContext = Depends(current_user)):
    snapshot = request.app.state.vault_sync.snapshot()
    projection = projection_health(get_settings())
    obsidian = request.app.state.obsidian_status
    projection_targets = [value for value in projection.values() if isinstance(value, dict)]
    projection_backlog = sum(
        int(target.get(state, 0) or 0)
        for target in projection_targets
        for state in ("pending", "running", "failed")
    )
    projection_degraded = any(
        bool(target.get("degraded")) or int(target.get("failed", 0) or 0) > 0
        for target in projection_targets
    )
    obsidian_drifted = bool(obsidian.get("drifted"))
    clean = not any(
        (
            snapshot["pending_events"],
            snapshot["pending_deletes"],
            snapshot["open_issues"],
            snapshot["invalid_pages"],
            projection_backlog,
            projection_degraded,
            obsidian_drifted,
            snapshot["last_error"],
        )
    )
    snapshot.update({
        "configured": True,
        "clean": clean,
        "degraded": bool(
            snapshot["last_error"]
            or snapshot["open_issues"]
            or snapshot["invalid_pages"]
            or obsidian_drifted
            or projection_degraded
        ),
        "projection_backlog": projection_backlog,
        "projection": projection,
        "obsidian": obsidian,
    })
    return snapshot


@router.post("/vault/reconcile", response_model=VaultReconcileResponse, status_code=202)
async def vault_reconcile_endpoint(request: Request, user: UserContext = Depends(current_user)):
    require_account_admin(user)
    return {"job_id": request.app.state.vault_sync.request_reconcile(), "status": "queued"}


@router.get("/wiki/pages/{page_path:path}/obsidian-link", response_model=ObsidianLinkResponse)
def obsidian_link_endpoint(page_path: str, user: UserContext = Depends(current_user)):
    settings = get_settings()
    return {"url": build_obsidian_uri(settings.effective_obsidian_vault_name, page_path)}
```

Replace `app/main.py::lifespan()` with the ordered implementation:

```python
from app.obsidian import ensure_obsidian_vault
from app.projection_worker import ProjectionWorker
from app.vault_sync import VaultSyncService
from app.vault_writer import IntentExecutor


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_vault(settings.vault_path)
    init_app_db(settings)
    obsidian = ensure_obsidian_vault(settings.vault_path)
    intent_executor = IntentExecutor(settings)
    vault_sync = VaultSyncService(settings, intent_executor=intent_executor)
    projection_worker = ProjectionWorker(settings)
    app.state.vault_sync = vault_sync
    app.state.projection_worker = projection_worker
    app.state.obsidian_status = {
        "installed": obsidian.installed,
        "drifted": obsidian.drifted,
        "vault_name": settings.effective_obsidian_vault_name,
    }
    await asyncio.to_thread(intent_executor.reconcile_all)
    await vault_sync.reconcile_startup(reconcile_intents=False)
    if settings.projection_worker_enabled:
        await projection_worker.start()
    if settings.vault_watch_enabled:
        await vault_sync.start()
    try:
        yield
    finally:
        await vault_sync.stop()
        await projection_worker.stop()
```

- [ ] **Step 5: Run lifecycle and API tests**

Run:

```powershell
python -m pytest tests/test_vault_api.py tests/test_internal_flow.py -q
```

Expected: PASS. The existing internal flow remains green with background integrations disabled by the autouse fixture.

- [ ] **Step 6: Commit the runtime/API integration**

```powershell
git add app/vault_sync.py app/main.py app/models.py app/api.py tests/test_vault_api.py
git commit -m "feat: expose vault sync lifecycle and status"
```

---

### Task 9: Web Sync State And Obsidian Deep Link

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/vite.config.ts`
- Create: `frontend/src/test/setup.ts`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/features/WikiTask.tsx`
- Create: `frontend/src/features/WikiTask.test.tsx`

- [ ] **Step 1: Add failing component tests**

Create `frontend/src/test/setup.ts`:

```typescript
import "@testing-library/jest-dom/vitest";
```

Create `frontend/src/features/WikiTask.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";

import { WikiTask } from "./WikiTask";


const page = {
  path: "wiki/产品/退款 政策.md",
  domain: "product",
  page_type: "policy",
  title: "退款政策",
  review_status: "draft",
  lifecycle_status: "active",
  sync_error: null,
};


function renderTask(openInObsidian = vi.fn(async () => undefined)) {
  render(
    <WikiTask
      pages={[page]}
      activeSpaceFilter={{ id: "all", label: "全部", desc: "全部", kind: "all", count: 1 }}
      clearSpaceFilter={vi.fn()}
      editor={{ path: page.path, content: "# 退款政策", review_status: "draft", owner: "" }}
      setEditor={vi.fn()}
      loadPage={vi.fn(async () => undefined)}
      savePage={vi.fn(async () => undefined)}
      markPageStale={vi.fn(async () => undefined)}
      openInObsidian={openInObsidian}
        vaultStatus={{ configured: true, running: true, clean: false, degraded: true, pending_events: 1, pending_deletes: 0, open_issues: 1, invalid_pages: 1, projection_backlog: 0 }}
      showToast={vi.fn()}
    />,
  );
  return openInObsidian;
}


test("opens the selected page through the backend Obsidian link", async () => {
  const open = renderTask();
  fireEvent.click(screen.getByRole("button", { name: "在 Obsidian 中打开" }));
  expect(open).toHaveBeenCalledWith(page.path);
});


test("shows degraded sync state without explanatory feature copy", () => {
  renderTask();
  expect(screen.getByText("同步异常")).toBeInTheDocument();
  expect(screen.getByText("1 个待处理事件")).toBeInTheDocument();
});
```

- [ ] **Step 2: Install test packages and verify RED**

Add scripts/dependencies to `frontend/package.json`:

```json
"test": "vitest run"
```

```json
"lucide-react": "^0.468.0"
```

```json
"@testing-library/jest-dom": "^6.6.3",
"@testing-library/react": "^16.1.0",
"@types/react": "^18.3.12",
"@types/react-dom": "^18.3.1",
"jsdom": "^25.0.1",
"vitest": "^2.1.8"
```

Run:

```powershell
npm --prefix frontend install
npm --prefix frontend test -- WikiTask.test.tsx
```

Expected: FAIL because `WikiTask` lacks `openInObsidian` and `vaultStatus` props.

- [ ] **Step 3: Add types, API state, and the icon command**

Add to `frontend/src/types.ts`:

```typescript
export interface VaultStatus {
  configured: boolean;
  running: boolean;
  clean: boolean;
  degraded: boolean;
  last_event_at?: string | null;
  last_error?: string | null;
  pending_events: number;
  pending_deletes: number;
  open_issues: number;
  invalid_pages: number;
  projection_backlog: number;
}
```

Extend `WikiPage` with `lifecycle_status?: string` and `sync_error?: string | null`. In `App.tsx`, add `VaultStatus` to the type imports, add `vaultStatus` state, fetch `/api/internal/vault/status` in `refresh()`, and add:

```typescript
async function openInObsidian(path: string) {
  const result = await api<{ url: string }>(
    `/api/internal/wiki/pages/${encodePath(path)}/obsidian-link`,
  );
  window.location.assign(result.url);
}
```

Pass `vaultStatus={vaultStatus}` and `openInObsidian={openInObsidian}` to `WikiTask`.

In `WikiTask.tsx`, import `ExternalLink` from `lucide-react`, add both props to the exact function type, and render this command for list items and the selected editor path:

```tsx
<button
  aria-label="在 Obsidian 中打开"
  title="在 Obsidian 中打开"
  type="button"
  onClick={() => openInObsidian(page.path).catch((error) => showToast(error.message))}
>
  <ExternalLink aria-hidden="true" size={16} />
</button>
```

Render a compact status line above the list:

```tsx
<div className={`sync-status ${vaultStatus?.degraded ? "warn" : "ok"}`}>
  <strong>{vaultStatus?.degraded ? "同步异常" : vaultStatus?.running ? "同步运行中" : "同步已停止"}</strong>
  <span>{vaultStatus?.pending_events || 0} 个待处理事件</span>
  <span>{vaultStatus?.open_issues || 0} 个同步问题</span>
</div>
```

Update `frontend/vite.config.ts`:

```typescript
export default defineConfig({
  base: "/console-static/",
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
  },
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
});
```

- [ ] **Step 4: Run component tests and production build**

Run:

```powershell
npm --prefix frontend test -- WikiTask.test.tsx
npm --prefix frontend run build
```

Expected: PASS, 2 component tests; TypeScript and Vite build exit 0.

- [ ] **Step 5: Commit the Web workflow**

```powershell
git add frontend/package.json frontend/package-lock.json frontend/vite.config.ts frontend/src/test/setup.ts frontend/src/types.ts frontend/src/App.tsx frontend/src/features/WikiTask.tsx frontend/src/features/WikiTask.test.tsx
git commit -m "feat: open managed wiki pages in Obsidian"
```

---

### Task 10: Deterministic Five-Second Local Workflow

**Files:**
- Create: `tests/test_vault_obsidian_e2e.py`
- Modify: `tests/test_internal_flow.py`

- [ ] **Step 1: Write the failing local E2E and loop-suppression tests**

Create `tests/test_vault_obsidian_e2e.py`:

```python
import asyncio
import time

import pytest

from app.auth import UserContext
from app.config import Settings
from app.db import connect_app, init_app_db, json_dump
from app.models import AskRequest
from app.projection_worker import ProjectionWorker
from app.search import ask
from app.vault_sync import VaultSyncService


@pytest.mark.asyncio
async def test_obsidian_edit_reaches_current_local_rag_citation_within_five_seconds(tmp_path):
    settings = Settings(
        _env_file=None,
        database_backend="sqlite",
        database_path=tmp_path / "e2e.db",
        rag_store_backend="sqlite",
        vault_path=tmp_path / "vault",
        vault_watch_enabled=True,
        vault_watch_debounce_ms=50,
        vault_watch_stability_timeout_seconds=0.25,
        vault_rename_grace_ms=5000,
        projection_worker_enabled=False,
        gbrain_enabled=False,
        deepseek_api_key=None,
        deepseek_model=None,
    )
    init_app_db(settings)
    now = "2026-07-13T08:00:00+00:00"
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO sources(
              id, domain, title, source_type, original_path, raw_path,
              content_hash, size_bytes, status, metadata_json, created_at, updated_at
            ) VALUES ('src_refund', 'product', 'Refund source', 'markdown',
              'obsidian', 'raw/product/refund.md', ?, 100, 'active', ?, ?, ?)
            """,
            ("a" * 64, json_dump({"acl_tags": ["internal"]}), now, now),
        )
    page = settings.vault_path / "wiki/product/refund.md"
    page.parent.mkdir(parents=True)
    service = VaultSyncService(settings)
    worker = ProjectionWorker(settings)
    response = None

    await service.start()
    try:
        await asyncio.sleep(0)
        started = time.monotonic()
        page.write_text(
            "---\n"
            "title: Refund policy\n"
            "source_ids: [src_refund]\n"
            "domain: product\n"
            "page_type: policy\n"
            "review_status: draft\n"
            "---\n"
            "# Refund policy\nEnterprise refunds require finance approval.\n",
            encoding="utf-8",
        )
        while time.monotonic() - started < 5:
            await worker.run_once()
            response = ask(
                settings,
                AskRequest(question="Enterprise refunds need whose approval?", domain="product"),
                UserContext(user_id="admin", role="admin", acl_tags=("*",)),
            )
            if response.citations and response.citations[0].revision_id:
                break
            await asyncio.sleep(0.01)
    finally:
        await service.stop()

    assert response is not None
    assert response.citations
    assert response.citations[0].page_id
    assert response.citations[0].revision_id
    assert response.citations[0].wiki_page == "wiki/product/refund.md"
    assert time.monotonic() - started < 5


@pytest.mark.asyncio
async def test_managed_writeback_event_is_ignored_without_second_revision(tmp_path):
    settings = Settings(
        _env_file=None,
        database_backend="sqlite",
        database_path=tmp_path / "loop.db",
        vault_path=tmp_path / "vault",
        vault_watch_enabled=True,
        vault_watch_debounce_ms=50,
        vault_watch_stability_timeout_seconds=0.25,
        vault_rename_grace_ms=5000,
        projection_worker_enabled=False,
    )
    init_app_db(settings)
    service = VaultSyncService(settings)
    page = settings.vault_path / "wiki/product/loop.md"
    page.parent.mkdir(parents=True)

    def revision_count() -> int:
        with connect_app(settings) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id = (SELECT page_id FROM wiki_pages WHERE path = ?)",
                ("wiki/product/loop.md",),
            ).fetchone()[0]

    await service.start()
    try:
        await asyncio.sleep(0)
        page.write_text(
            "---\ntitle: Loop\nsource_ids: []\ndomain: product\npage_type: feature\nreview_status: draft\n---\n# Loop\n",
            encoding="utf-8",
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if revision_count() == 1 and "lgdo_revision_id" in page.read_text(encoding="utf-8"):
                break
            await asyncio.sleep(0.01)
        assert revision_count() == 1

        previous_event_at = service.last_event_at
        deadline = time.monotonic() + 5
        page.write_bytes(page.read_bytes())
        while time.monotonic() < deadline and service.last_event_at == previous_event_at:
            await asyncio.sleep(0.01)
        assert service.last_event_at != previous_event_at
    finally:
        await service.stop()

    assert revision_count() == 1
```

- [ ] **Step 2: Run both tests and verify RED**

Run:

```powershell
python -m pytest tests/test_vault_obsidian_e2e.py -q
```

Expected: FAIL until the watcher, revision service, local Wiki RAG projection, and Citation mapping are integrated.

- [ ] **Step 3: Make only integration corrections exposed by the E2E**

Keep the ownership rule while correcting failures:

```python
# app/vault_sync.py owns observation/event ordering only.
result = self.revisions.ingest_external_change(event.id, event.page_path, observation)

# app/wiki_revisions.py owns token/revision/hash loop suppression and outbox creation.
# app/projection_worker.py owns wiki_chunks and visible revision/epoch switching.
# app/search.py owns the active/current-visible/ACL Citation gate.
```

Do not add direct `wiki_page_revisions`, `knowledge_projection_jobs`, or `wiki_chunks` writes to `app/vault_sync.py` to make the test pass. Update `tests/test_internal_flow.py` save requests to include the prerequisite `expected_revision_id`, preserving its existing end-to-end assertions.

- [ ] **Step 4: Run local acceptance within the real five-second budget**

Run:

```powershell
python -m pytest tests/test_vault_obsidian_e2e.py tests/test_internal_flow.py -q
```

Expected: PASS; the local E2E starts the production `VaultWatchAdapter` backed by real `watchfiles.awatch`, observes actual filesystem writes, stays below 5 seconds, and performs no DeepSeek or real GBrain request. Neither test may call `handle_batch()` or construct `VaultFsEvent` directly.

- [ ] **Step 5: Commit local workflow acceptance**

```powershell
git add tests/test_vault_obsidian_e2e.py tests/test_internal_flow.py app/vault_sync.py app/wiki_revisions.py app/projection_worker.py app/search.py
git commit -m "test: prove local Obsidian edit convergence"
```

---

### Task 11: Real GBrain Incremental And Reconcile SLA

**Files:**
- Modify: `tests/integration/test_gbrain_pglite_projection.py`

- [ ] **Step 1: Add the real watcher-driven SLA test**

Append the test in the next step using the locked async fixture signature `gbrain_mcp_call(server, *, token, tool, arguments) -> dict`. The helper returns the unpacked tool payload, not the outer MCP envelope. The test must use `gbrain_pglite_server`, `gbrain_e2e_settings`, and `gbrain_mcp_call`; it must start the real `VaultSyncService` watcher runtime and must not call `lgdo_vault_sync` or `handle_batch()` directly.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
python -m pytest -m gbrain_e2e tests/integration/test_gbrain_pglite_projection.py::test_obsidian_incremental_and_reconcile_meet_sla -v
```

Expected: FAIL because the watcher-driven test function has not been added yet.

- [ ] **Step 3: Add the concrete condition-polling test**

Add this test using the fixture call signature defined by the GBrain prerequisite plan:

```python
import asyncio
import time

import pytest

from app.gbrain_projection import to_gbrain_manifest_path
from app.projection_worker import ProjectionWorker
from app.vault_sync import VaultSyncService


@pytest.mark.gbrain_e2e
@pytest.mark.asyncio
async def test_obsidian_incremental_and_reconcile_meet_sla(
    gbrain_pglite_server,
    gbrain_e2e_settings,
    gbrain_mcp_call,
):
    settings = gbrain_e2e_settings.model_copy(
        update={
            "vault_watch_enabled": True,
            "vault_watch_debounce_ms": 50,
            "vault_watch_stability_timeout_seconds": 0.25,
            "vault_rename_grace_ms": 5000,
        }
    )
    service = VaultSyncService(settings)
    worker = ProjectionWorker(settings)
    old_rel = "wiki/product/obsidian-sla.md"
    new_rel = "wiki/product/obsidian-sla-renamed.md"
    old_manifest = to_gbrain_manifest_path(old_rel)
    new_manifest = to_gbrain_manifest_path(new_rel)
    old_path = settings.vault_path / old_rel
    new_path = settings.vault_path / new_rel
    old_path.parent.mkdir(parents=True, exist_ok=True)
    await service.start()
    try:
        await asyncio.sleep(0)
        incremental_started = time.monotonic()
        old_path.write_text(
            managed_wiki_fixture("Obsidian SLA", gbrain_pglite_server.source_id),
            encoding="utf-8",
        )
        while time.monotonic() - incremental_started < 120:
            await worker.run_once()
            hits = await gbrain_mcp_call(
                gbrain_pglite_server,
                token=gbrain_pglite_server.query_token,
                tool="search",
                arguments={"query": "Obsidian SLA", "source_id": gbrain_pglite_server.source_id},
            )
            if any(hit.get("source_path") == old_manifest for hit in hits.get("results", [])):
                break
            await asyncio.sleep(0.25)
        assert time.monotonic() - incremental_started < 120

        rename_started = time.monotonic()
        old_path.rename(new_path)
        while time.monotonic() - rename_started < 600:
            await worker.run_once()
            hits = await gbrain_mcp_call(
                gbrain_pglite_server,
                token=gbrain_pglite_server.query_token,
                tool="search",
                arguments={"query": "Obsidian SLA", "source_id": gbrain_pglite_server.source_id},
            )
            paths = {hit.get("source_path") for hit in hits.get("results", [])}
            if new_manifest in paths and old_manifest not in paths:
                break
            await asyncio.sleep(0.25)
        assert time.monotonic() - rename_started < 600

        delete_started = time.monotonic()
        new_path.unlink()
        while time.monotonic() - delete_started < 600:
            await worker.run_once()
            hits = await gbrain_mcp_call(
                gbrain_pglite_server,
                token=gbrain_pglite_server.query_token,
                tool="search",
                arguments={"query": "Obsidian SLA", "source_id": gbrain_pglite_server.source_id},
            )
            paths = {hit.get("source_path") for hit in hits.get("results", [])}
            if old_manifest not in paths and new_manifest not in paths:
                break
            await asyncio.sleep(0.25)
        assert time.monotonic() - delete_started < 600
    finally:
        await service.stop()
```

Keep `managed_wiki_fixture()` in the same integration file and pass `gbrain_pglite_server.source_id` so its frontmatter references the source created by the shared fixture. Every `source_path` assertion must compare the manifest-relative value from `to_gbrain_manifest_path`, never the LGDO path `wiki/product/obsidian-sla.md`. Do not place the 120/600-second SLA phases in the default CI selection.

- [ ] **Step 4: Run default and opt-in selections separately**

Run:

```powershell
python -m pytest -m "not gbrain_e2e" tests/integration/test_gbrain_pglite_projection.py -q
python -m pytest -m gbrain_e2e tests/integration/test_gbrain_pglite_projection.py::test_obsidian_incremental_and_reconcile_meet_sla -v
```

Expected: the default command exits quickly with the real test deselected; the opt-in test PASSes within its incremental 120-second and reconcile 600-second condition deadlines.

- [ ] **Step 5: Commit the real integration gate**

```powershell
git add tests/integration/test_gbrain_pglite_projection.py
git commit -m "test: enforce Obsidian GBrain projection SLA"
```

---

### Task 12: Operator Documentation And Full Regression Gate

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`

- [ ] **Step 1: Add the operator workflow to both READMEs**

Document these exact commands and meanings in English and Chinese:

```powershell
scripts/install-obsidian-vault.ps1 -VaultPath vault
scripts/install-obsidian-vault.ps1 -VaultPath vault -Refresh
scripts/open-obsidian.ps1 -VaultName "LGDO" -PagePath "wiki/product/example.md"
Invoke-RestMethod http://127.0.0.1:8000/api/internal/vault/status
```

State that only `vault/wiki/**/*.md` is editable, refresh backs up before merging, runtime `vault/` remains ignored, real GBrain SLA tests are opt-in, and sync issues are visible through `/vault/status` and the Web Wiki workspace.

- [ ] **Step 2: Run backend default CI**

Run:

```powershell
python -m pytest -m "not gbrain_e2e" -q
```

Expected: PASS with zero failures and no real DeepSeek/GBrain calls.

- [ ] **Step 3: Run frontend tests and build**

Run:

```powershell
npm --prefix frontend test
npm --prefix frontend run build
```

Expected: all Vitest tests PASS; TypeScript/Vite build exits 0.

- [ ] **Step 4: Validate resources, scripts, and configuration**

Run:

```powershell
python -m app.obsidian --vault "$env:TEMP\lgdo-obsidian-plan-check"
scripts/open-obsidian.ps1 -VaultName "LGDO 知识库" -PagePath "wiki/产品/退款 政策.md" -PrintOnly
python -c "from app.config import Settings; s=Settings(_env_file=None); assert s.vault_rename_grace_ms >= max(5000, s.vault_watch_debounce_ms + int(s.vault_watch_stability_timeout_seconds*1000) + s.vault_rename_safety_margin_ms)"
```

Expected: installer prints structured JSON, link prints one encoded `obsidian://open` URI, and config assertion exits 0.

- [ ] **Step 5: Run final repository checks**

Run:

```powershell
git diff --check
git status --short
```

Expected: `git diff --check` exits 0. Status contains only intentional files from this implementation and preserves unrelated user changes.

- [ ] **Step 6: Commit documentation and final verification metadata**

```powershell
git add README.md README.zh-CN.md
git commit -m "docs: document Obsidian vault operations"
```

---

## Completion Checklist

- [ ] Every filesystem mutation reaches `WikiRevisionService`; watcher files contain no direct revision, outbox, or Wiki-chunk writes.
- [ ] `ruamel.yaml` round-trip parsing and bounded file observation come only from `app/wiki_markdown.py`.
- [ ] Delete expiry is at least `max(5000 ms, debounce + stability + safety margin)` and a pre-deadline pending add blocks expiry until classification.
- [ ] Restart replays intent recovery before offline rename/missing-file reconcile, then starts projection, then watcher.
- [ ] Add/modify/rename/delete, invalid restore, duplicate identity, write-loop suppression, and event replay are covered by deterministic tests.
- [ ] Stable Obsidian resources are version-controlled; workspace and third-party plugin state are not.
- [ ] Web deep links encode spaces, Chinese text, and nested paths and use the backend-generated URI.
- [ ] Local E2E finishes within 5 seconds without external services.
- [ ] Real GBrain incremental/reconcile tests use condition polling and enforce 120/600-second deadlines only under `gbrain_e2e`.
- [ ] SQLite, PostgreSQL-when-available, frontend tests/build, and `git diff --check` pass.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-13-vault-watcher-obsidian.md`. Two execution options:

1. **Subagent-Driven (recommended):** use `superpowers:subagent-driven-development`, one fresh subagent per task with review between tasks.
2. **Inline Execution:** use `superpowers:executing-plans`, execute in batches with checkpoints.
