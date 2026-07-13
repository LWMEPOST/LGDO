# Wiki Revision And Conflict Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `vault/wiki` the human-editable source of truth by introducing immutable Wiki revisions, compare-and-swap mutations, conflict review, crash-recoverable Vault writes, and epoch-scoped non-blocking projection jobs.

**Architecture:** `WikiRevisionService` is the only public mutation boundary and serializes every page transition through `PageMutationCoordinator`; it uses `AtomicVaultWriter`/`IntentExecutor` for capture-before-replace file installation and `ProjectionOutbox` for RAG/GBrain work committed with page state. Shared Markdown parsing and hashing live in `app/wiki_markdown.py`, while SQLite and PostgreSQL keep identical logical tables through backend-specific idempotent migrations.

**Tech Stack:** Python 3.10+, FastAPI, Pydantic v2, SQLite WAL/`BEGIN IMMEDIATE`, PostgreSQL/psycopg row locks, `ruamel.yaml`, `portalocker`, pytest, React 18/TypeScript.

---

## Execution Order And Ownership

This plan runs before `docs/superpowers/plans/2026-07-13-gbrain-projection-citation.md` and `docs/superpowers/plans/2026-07-13-vault-watcher-obsidian.md`. It owns the shared revision schema, `app/wiki_markdown.py`, `WikiRevisionService`, Vault write intents, and `ProjectionOutbox`; later plans consume those APIs and must not create parallel parser, revision, or outbox implementations.

All commands below run from the repository root in PowerShell. Keep GBrain synchronous import disabled for deterministic tests:

```powershell
$env:GBRAIN_IMPORT_ON_COMPILE = 'false'
```

## Locked Public Contracts

`app/wiki_markdown.py` owns these types and functions:

```python
@dataclass(frozen=True)
class FrontmatterLimits:
    max_frontmatter_bytes: int = 65_536
    max_depth: int = 12
    max_nodes: int = 2_000
    max_aliases: int = 32
    max_file_bytes: int = 5_242_880
    max_prefix_bytes: int = 65_536

@dataclass(frozen=True)
class FileObservationInput:
    file_hash: str
    size_bytes: int
    mtime_ns: int
    content_bytes: bytes | None
    content_prefix: bytes | None
    content_truncated: bool

def parse_wiki_bytes(raw_bytes: bytes, limits: FrontmatterLimits = FrontmatterLimits()) -> ParsedWikiDocument: ...
def render_managed_frontmatter(document: ParsedWikiDocument, *, page_id: str, revision_id: str, write_token: str, review_status: str | None = None) -> bytes: ...
def compute_file_hash(raw_bytes: bytes) -> str: ...
def compute_semantic_hash(document: ParsedWikiDocument) -> str: ...
def capture_file_observation(path: Path, *, max_content_bytes: int, prefix_bytes: int = 65_536) -> FileObservationInput: ...
```

The ellipses above denote signatures only; Tasks 1 and 2 provide the executable bodies. Public mutation callers use these methods:

```python
class WikiRevisionService:
    def get_page(self, page_path: str) -> PageReadResult: ...
    def prepare_manual_save(self, command: ManualSaveCommand, *, execute_intent: bool = True) -> MutationResult: ...
    def update_status(self, command: StatusUpdateCommand) -> MutationResult: ...
    def update_metadata(self, command: MetadataUpdateCommand) -> MutationResult: ...
    def apply_generated_candidate(self, command: CompileCandidateCommand) -> MutationResult: ...
    def ingest_external_change(self, event_id: str, page_path: str, observation: FileObservationInput) -> MutationResult: ...
    def rename_page(self, event_id: str, old_page_path: str, page_path: str) -> MutationResult: ...
    def delete_page(self, event_id: str, page_path: str) -> MutationResult: ...
    def resolve_conflict(self, command: ResolveConflictCommand) -> MutationResult: ...
    def ensure_projection_jobs(self, page_id: str | None = None) -> list[str]: ...
```

Every `MutationResult` carries `revision_id`: manual/status/external/merge/accept use the written or selected revision; compile uses the generated candidate; rename uses its audit-only rename revision; delete/restore use the associated current revision; keep-current uses the acknowledged candidate; ignored/replayed events reuse the revision recorded by the original event. Consumers never infer this value from current/generated pointers.

`ProjectionOutbox` uses the following complete public contract. All claim predicates, including pending, failed, and expired-running recovery, include `attempts < 5`:

```python
class ProjectionOutbox:
    def __init__(self, settings: Settings): ...
    def enqueue(self, conn: Any, *, target: str, operation: str, page_id: str | None,
                revision_id: str | None, projection_epoch: int, payload: dict) -> str: ...
    def enqueue_pair_for_state(self, conn: Any, page: dict, operation: str, payload: dict) -> list[str]: ...
    def claim(self, *, target: str, worker_id: str, limit: int, lease_seconds: int,
              now: datetime | None = None) -> list[ProjectionJob]: ...
    def renew_lease(self, job_id: str, worker_id: str, lease_seconds: int,
                    now: datetime | None = None) -> bool: ...
    def finish_claimed(self, conn: Any, *, job_id: str, worker_id: str,
                       status: Literal["succeeded", "failed", "superseded"],
                       last_error: str | None = None,
                       available_at: str | None = None) -> bool: ...
    def mark_succeeded(self, job_id: str, worker_id: str) -> bool: ...
    def mark_failed(self, job_id: str, worker_id: str, last_error: str,
                    now: datetime | None = None) -> bool: ...
    def supersede_stale(self, conn: Any, page_id: str, projection_epoch: int) -> int: ...
```

`mark_failed` reads the already-incremented attempt count and computes retry delays internally: attempts 1-4 become available after 5, 30, 120, and 600 seconds; attempt 5 remains terminal `failed` until an explicit retry operation resets attempts to zero. GBrain/RAG workers never calculate or pass `available_at`.

## File Structure

- Modify `pyproject.toml`: add round-trip YAML and cross-process lock dependencies.
- Create `app/wiki_markdown.py`: bounded observation capture, UTF-8/YAML parsing, managed frontmatter rendering, and file/semantic hashes.
- Modify `app/db.py`: final SQLite/PostgreSQL schema, idempotent migration, write-transaction helper, and table registries.
- Modify `app/migration.py`: new-table/column allowlists and old-source default handling.
- Create `app/projection_jobs.py`: epoch-keyed durable outbox and lease/CAS repository.
- Create `app/wiki_revisions.py`: immutable revision service, page coordinator, state machines, observations, conflicts, rename/delete, and projection orchestration.
- Create `app/vault_writer.py`: no-replace writer, intent lease executor, recovery matrix, and retained-backup release.
- Modify `app/wiki.py`: generate candidates and delegate all page mutation to `WikiRevisionService`; remove synchronous GBrain import.
- Modify `app/catalog.py`: route reads, saves, status changes, revision queries, and conflict resolution through the service.
- Modify `app/models.py`: precondition-bearing requests and revision/conflict responses.
- Modify `app/api.py`: revision/conflict APIs plus precise 428/409/404 error mapping.
- Modify `frontend/src/types.ts`: current revision and conflict types.
- Modify `frontend/src/App.tsx`: send expected revision/request IDs and refresh after 409.
- Modify `tests/test_internal_flow.py`: preserve the existing end-to-end flow under the new precondition contract.
- Modify `tests/test_sqlite_to_postgres_migration.py`: migrate all revision tables and old sparse rows.
- Create `tests/test_wiki_markdown.py`: bounded parser/hash tests.
- Create `tests/test_wiki_schema_migration.py`: SQLite fresh/legacy/half-migrated tests.
- Create `tests/test_projection_outbox.py`: idempotency, epoch, lease, and stale-finish tests.
- Create `tests/test_wiki_revisions.py`: legacy snapshot, compile/manual/status, idempotency, and conflict state machines.
- Create `tests/test_vault_writer.py`: capture/no-replace, crash phase, recovery matrix, and late-writer tests.
- Create `tests/test_wiki_revision_api.py`: API contracts, pagination, conflict resolution, rename/delete, and status-frontmatter tests.
- Create `tests/test_wiki_revision_postgres.py`: PostgreSQL row-lock and migration parity tests.

---

### Task 1: Round-Trip Markdown, Managed Frontmatter, And Hashes

**Files:**
- Modify: `pyproject.toml:6-18`
- Create: `app/wiki_markdown.py`
- Create: `tests/test_wiki_markdown.py`

- [ ] **Step 1: Add failing parser and hash tests**

Create `tests/test_wiki_markdown.py`:

```python
import os
from pathlib import Path

import pytest

from app.wiki_markdown import (
    FrontmatterLimits,
    MarkdownParseError,
    ObservationChanged,
    capture_file_observation,
    compute_file_hash,
    compute_semantic_hash,
    parse_wiki_bytes,
    render_managed_frontmatter,
)


def test_round_trip_preserves_comments_order_and_body_while_managed_fields_change():
    raw = (
        b"---\r\n"
        b"title: Demo # keep title comment\r\n"
        b"source_ids: [src_1]\r\n"
        b"custom: 'quoted'\r\n"
        b"lgdo_revision_id: wrev_old\r\n"
        b"---\r\n\r\n# Demo\r\n\r\nHuman body.\r\n"
    )
    document = parse_wiki_bytes(raw)

    rendered = render_managed_frontmatter(
        document,
        page_id="page_1",
        revision_id="wrev_2",
        write_token="write_2",
        review_status="reviewed",
    )

    assert b"# keep title comment" in rendered
    assert rendered.index(b"title:") < rendered.index(b"source_ids:") < rendered.index(b"custom:")
    assert b"custom: 'quoted'" in rendered
    assert b"# Demo\r\n\r\nHuman body.\r\n" in rendered
    assert b"lgdo_page_id: page_1" in rendered
    assert b"lgdo_revision_id: wrev_2" in rendered
    assert b"lgdo_write_token: write_2" in rendered
    assert b"review_status: reviewed" in rendered


def test_semantic_hash_ignores_managed_fields_comments_and_newline_style():
    first = parse_wiki_bytes(
        b"---\ntitle: Demo # one\nsource_ids: [src_1]\nlgdo_revision_id: old\n---\nBody\n"
    )
    second = parse_wiki_bytes(
        b"---\r\nsource_ids:\r\n  - src_1\r\ntitle: Demo # two\r\nlgdo_revision_id: new\r\n---\r\nBody\r\n"
    )

    assert compute_file_hash(first.raw_bytes) != compute_file_hash(second.raw_bytes)
    assert compute_semantic_hash(first) == compute_semantic_hash(second)


@pytest.mark.parametrize(
    "raw,error_code",
    [
        (b"\xff\xfe", "invalid_utf8"),
        (b"---\ntitle: [broken\n---\nBody", "invalid_yaml"),
        (b"---\na: &x [1]\nb: *x\n---\nBody", "yaml_alias_limit"),
        (b"---\na: !python/object value\n---\nBody", "yaml_tag_forbidden"),
    ],
)
def test_parser_fails_closed_with_stable_error_codes(raw: bytes, error_code: str):
    with pytest.raises(MarkdownParseError) as exc_info:
        parse_wiki_bytes(raw, FrontmatterLimits(max_aliases=0))
    assert exc_info.value.code == error_code


def test_capture_file_observation_streams_large_file_and_keeps_only_prefix(tmp_path: Path):
    path = tmp_path / "large.md"
    path.write_bytes(b"a" * 200_000)

    observed = capture_file_observation(path, max_content_bytes=1_024, prefix_bytes=64)

    assert observed.size_bytes == 200_000
    assert observed.content_bytes is None
    assert observed.content_prefix == b"a" * 64
    assert observed.content_truncated is True
    assert observed.file_hash == compute_file_hash(b"a" * 200_000)


def test_capture_file_observation_rejects_file_that_grows_while_reading(tmp_path: Path, monkeypatch):
    path = tmp_path / "growing.md"
    path.write_bytes(b"a" * 128)
    real_open = Path.open

    class GrowingReader:
        def __init__(self, handle):
            self.handle = handle
            self.grown = False

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self.handle.__exit__(exc_type, exc, tb)

        def read(self, size: int) -> bytes:
            chunk = self.handle.read(size)
            if chunk and not self.grown:
                self.grown = True
                descriptor = os.open(path, os.O_WRONLY | os.O_APPEND)
                try:
                    os.write(descriptor, b"b" * 200_000)
                finally:
                    os.close(descriptor)
            return chunk

    def growing_open(self: Path, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        return GrowingReader(handle) if self == path and "b" in args[0] else handle

    monkeypatch.setattr(Path, "open", growing_open)

    with pytest.raises(ObservationChanged):
        capture_file_observation(path, max_content_bytes=1_024, prefix_bytes=64)
```

- [ ] **Step 2: Run the focused tests to verify RED**

Run:

```powershell
python -m pytest tests/test_wiki_markdown.py -q
```

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'app.wiki_markdown'`.

- [ ] **Step 3: Add runtime dependencies**

Add these entries to `pyproject.toml` under `[project].dependencies`:

```toml
  "ruamel.yaml>=0.18.6",
  "portalocker>=3.1.1",
```

Install the editable project:

```powershell
python -m pip install -e ".[dev]"
```

Expected: exit code 0 and both `ruamel.yaml` and `portalocker` reported as installed.

- [ ] **Step 4: Implement bounded parsing, rendering, and hashing**

Create `app/wiki_markdown.py` with these concrete definitions:

```python
from __future__ import annotations

import hashlib
import io
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError

MANAGED_KEYS = {
    "id",
    "lgdo_page_id",
    "lgdo_revision_id",
    "lgdo_write_token",
    "lgdo_updated_at",
}
ALIAS_RE = re.compile(r"(?:^|\s)[&*][A-Za-z0-9_-]+", re.MULTILINE)
TAG_RE = re.compile(r"(?:^|\s)![!A-Za-z0-9_:./-]+", re.MULTILINE)


class MarkdownParseError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message[:500])
        self.code = code


class ObservationChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class FrontmatterLimits:
    max_frontmatter_bytes: int = 65_536
    max_depth: int = 12
    max_nodes: int = 2_000
    max_aliases: int = 32
    max_file_bytes: int = 5_242_880
    max_prefix_bytes: int = 65_536


@dataclass(frozen=True)
class ParsedWikiDocument:
    frontmatter: CommentedMap
    body: str
    raw_bytes: bytes
    newline: str


@dataclass(frozen=True)
class FileObservationInput:
    file_hash: str
    size_bytes: int
    mtime_ns: int
    content_bytes: bytes | None
    content_prefix: bytes | None
    content_truncated: bool


def compute_file_hash(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _count_nodes(value: Any, depth: int = 0) -> tuple[int, int]:
    if isinstance(value, dict):
        children = [_count_nodes(item, depth + 1) for item in value.values()]
    elif isinstance(value, (list, tuple)):
        children = [_count_nodes(item, depth + 1) for item in value]
    else:
        children = []
    return 1 + sum(count for count, _ in children), max([depth, *(level for _, level in children)])


def parse_wiki_bytes(
    raw_bytes: bytes,
    limits: FrontmatterLimits = FrontmatterLimits(),
) -> ParsedWikiDocument:
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MarkdownParseError("invalid_utf8", "Wiki 文件必须是 UTF-8") from exc
    newline = "\r\n" if "\r\n" in text else "\n"
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        raise MarkdownParseError("missing_frontmatter", "Wiki 文件缺少 YAML frontmatter")
    marker = normalized.find("\n---\n", 4)
    if marker < 0:
        raise MarkdownParseError("unterminated_frontmatter", "Wiki frontmatter 未闭合")
    yaml_text = normalized[4:marker]
    if len(yaml_text.encode("utf-8")) > limits.max_frontmatter_bytes:
        raise MarkdownParseError("frontmatter_too_large", "Wiki frontmatter 超过限制")
    if len(ALIAS_RE.findall(yaml_text)) > limits.max_aliases:
        raise MarkdownParseError("yaml_alias_limit", "Wiki frontmatter alias 超过限制")
    if TAG_RE.search(yaml_text):
        raise MarkdownParseError("yaml_tag_forbidden", "Wiki frontmatter 不允许 YAML tag")
    yaml = YAML(typ="rt")
    yaml.allow_duplicate_keys = False
    yaml.preserve_quotes = True
    try:
        loaded = yaml.load(yaml_text) or CommentedMap()
    except YAMLError as exc:
        raise MarkdownParseError("invalid_yaml", "Wiki frontmatter YAML 无效") from exc
    if not isinstance(loaded, CommentedMap):
        raise MarkdownParseError("frontmatter_not_mapping", "Wiki frontmatter 必须是 mapping")
    nodes, depth = _count_nodes(loaded)
    if nodes > limits.max_nodes or depth > limits.max_depth:
        raise MarkdownParseError("yaml_structure_limit", "Wiki frontmatter 结构超过限制")
    body = normalized[marker + 5 :].replace("\n", newline)
    return ParsedWikiDocument(loaded, body, raw_bytes, newline)


def compute_semantic_hash(document: ParsedWikiDocument) -> str:
    metadata = {
        str(key): _plain(value)
        for key, value in document.frontmatter.items()
        if str(key) not in MANAGED_KEYS and not str(key).startswith("lgdo_")
    }
    canonical = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    body = document.body.replace("\r\n", "\n")
    return hashlib.sha256((canonical + "\n" + body).encode("utf-8")).hexdigest()


def render_managed_frontmatter(
    document: ParsedWikiDocument,
    *,
    page_id: str,
    revision_id: str,
    write_token: str,
    review_status: str | None = None,
) -> bytes:
    metadata = deepcopy(document.frontmatter)
    existing_page_id = metadata.get("lgdo_page_id") or metadata.get("id")
    if existing_page_id and str(existing_page_id) != page_id:
        raise MarkdownParseError("page_identity_conflict", "frontmatter 页面身份与目标页面不一致")
    metadata["lgdo_page_id"] = page_id
    metadata["lgdo_revision_id"] = revision_id
    metadata["lgdo_write_token"] = write_token
    if review_status is not None:
        metadata["review_status"] = review_status
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4_096
    stream = io.StringIO()
    yaml.dump(metadata, stream)
    header = "---\n" + stream.getvalue() + "---\n"
    rendered = (header + document.body.replace("\r\n", "\n")).replace("\n", document.newline)
    return rendered.encode("utf-8")


def capture_file_observation(
    path: Path,
    *,
    max_content_bytes: int,
    prefix_bytes: int = 65_536,
) -> FileObservationInput:
    before = path.stat()
    digest = hashlib.sha256()
    captured = bytearray()
    prefix = bytearray()
    with path.open("rb", buffering=64 * 1024) as handle:
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
            if len(prefix) < prefix_bytes:
                prefix.extend(chunk[: prefix_bytes - len(prefix)])
            if before.st_size <= max_content_bytes and len(captured) < max_content_bytes:
                remaining = max_content_bytes - len(captured)
                captured.extend(chunk[:remaining])
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ObservationChanged(f"file changed while observing: {path}")
    truncated = after.st_size > max_content_bytes
    return FileObservationInput(
        file_hash=digest.hexdigest(),
        size_bytes=after.st_size,
        mtime_ns=after.st_mtime_ns,
        content_bytes=None if truncated else bytes(captured),
        content_prefix=bytes(prefix) if truncated else None,
        content_truncated=truncated,
    )
```

- [ ] **Step 5: Run the parser tests to verify GREEN**

Run:

```powershell
python -m pytest tests/test_wiki_markdown.py -q
```

Expected: PASS with `7 passed`.

- [ ] **Step 6: Commit the shared Markdown boundary**

```powershell
git add pyproject.toml app/wiki_markdown.py tests/test_wiki_markdown.py
git commit -m "feat: add bounded wiki markdown parsing"
```

---

### Task 2: SQLite Revision Schema And Half-Migration Repair

**Files:**
- Modify: `app/db.py:13-459`
- Create: `tests/test_wiki_schema_migration.py`

- [ ] **Step 1: Write failing fresh, legacy, and half-migrated SQLite tests**

Create `tests/test_wiki_schema_migration.py`:

```python
import sqlite3

from app.db import MAIN_TABLES, TABLE_PRIMARY_KEYS, init_db


REVISION_TABLES = {
    "wiki_page_revisions",
    "vault_write_intents",
    "wiki_file_observations",
    "vault_change_events",
    "knowledge_projection_jobs",
}


def table_info(conn: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_sqlite_schema_contains_revision_tables_columns_and_partial_indexes(tmp_path):
    database = tmp_path / "fresh.db"
    init_db(database)

    with sqlite3.connect(database) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = table_info(conn, "wiki_pages")
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(review_items)")}

    assert REVISION_TABLES <= tables
    assert columns["revision_number"]["notnull"] == 1
    assert columns["revision_number"]["dflt_value"] == "0"
    assert columns["projection_epoch"]["notnull"] == 1
    assert columns["projection_epoch"]["dflt_value"] == "0"
    assert {"idx_review_pending_content_conflict", "idx_review_pending_concurrent_conflict"} <= indexes
    assert REVISION_TABLES <= set(MAIN_TABLES)
    assert all(table in TABLE_PRIMARY_KEYS for table in REVISION_TABLES)


def test_legacy_sqlite_schema_is_upgraded_without_losing_page(tmp_path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE wiki_pages (
              path TEXT PRIMARY KEY, domain TEXT NOT NULL, page_type TEXT NOT NULL,
              title TEXT NOT NULL, source_ids_json TEXT NOT NULL DEFAULT '[]',
              review_status TEXT NOT NULL DEFAULT 'draft', owner TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            INSERT INTO wiki_pages VALUES
              ('wiki/product/faq/demo.md','product','faq','Demo','["src_1"]','reviewed','alice','t0','t0');
            """
        )

    init_db(database)

    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM wiki_pages").fetchone()
    assert row["path"] == "wiki/product/faq/demo.md"
    assert row["page_id"] is None
    assert row["current_revision_id"] is None
    assert row["revision_number"] == 0
    assert row["projection_epoch"] == 0


def test_half_migrated_nullable_counters_are_rebuilt_atomically(tmp_path):
    database = tmp_path / "half.db"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE wiki_pages (
              path TEXT PRIMARY KEY, domain TEXT NOT NULL, page_type TEXT NOT NULL,
              title TEXT NOT NULL, source_ids_json TEXT NOT NULL DEFAULT '[]',
              review_status TEXT NOT NULL DEFAULT 'draft', owner TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              revision_number INTEGER NULL, projection_epoch INTEGER NULL
            );
            CREATE UNIQUE INDEX idx_half_page_title ON wiki_pages(domain, title);
            INSERT INTO wiki_pages(path,domain,page_type,title,created_at,updated_at,revision_number,projection_epoch)
            VALUES ('wiki/product/faq/demo.md','product','faq','Demo','t0','t0',NULL,NULL);
            """
        )

    init_db(database)

    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        columns = table_info(conn, "wiki_pages")
        row = conn.execute("SELECT revision_number, projection_epoch FROM wiki_pages").fetchone()
        indexes = {item[1] for item in conn.execute("PRAGMA index_list(wiki_pages)")}
        foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert columns["revision_number"]["notnull"] == 1
    assert columns["projection_epoch"]["notnull"] == 1
    assert row["revision_number"] == row["projection_epoch"] == 0
    assert "idx_half_page_title" in indexes
    assert foreign_key_errors == []
```

- [ ] **Step 2: Run the migration tests to verify RED**

Run:

```powershell
python -m pytest tests/test_wiki_schema_migration.py -q
```

Expected: FAIL because the revision tables/columns and migration registry entries do not exist.

- [ ] **Step 3: Add the complete logical schema**

In both `SCHEMA` and `PG_SCHEMA`, replace the `wiki_pages` and `review_items` definitions with the columns from the approved spec. Use `INTEGER`/`BLOB` in SQLite, `BIGINT` for observation `size_bytes`/`mtime_ns`, and `BYTEA`/`BOOLEAN` in PostgreSQL. Append concrete `CREATE TABLE IF NOT EXISTS` statements for:

```sql
CREATE TABLE IF NOT EXISTS wiki_page_revisions (
  id TEXT PRIMARY KEY, page_id TEXT NOT NULL, page_path TEXT NOT NULL,
  revision_number INTEGER NOT NULL, file_hash TEXT NOT NULL, semantic_hash TEXT NOT NULL,
  content TEXT NOT NULL, origin TEXT NOT NULL, base_revision_id TEXT,
  source_ids_json TEXT NOT NULL DEFAULT '[]', actor TEXT, note TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}', idempotency_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL, UNIQUE(page_id, revision_number)
);
CREATE INDEX IF NOT EXISTS idx_wiki_revisions_page_created ON wiki_page_revisions(page_id, created_at);
CREATE INDEX IF NOT EXISTS idx_wiki_revisions_semantic ON wiki_page_revisions(semantic_hash);

CREATE TABLE IF NOT EXISTS vault_write_intents (
  id TEXT PRIMARY KEY, page_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  expected_revision_id TEXT, expected_file_hash TEXT, target_path TEXT NOT NULL,
  write_token TEXT NOT NULL, backup_path TEXT, captured_file_hash TEXT,
  backup_last_observed_hash TEXT, backup_retention_status TEXT NOT NULL DEFAULT 'none',
  status TEXT NOT NULL DEFAULT 'pending', executor_owner TEXT, lease_expires_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vault_intents_status_available ON vault_write_intents(status, lease_expires_at);

CREATE TABLE IF NOT EXISTS wiki_file_observations (
  id TEXT PRIMARY KEY, page_id TEXT, page_path TEXT NOT NULL, file_hash TEXT NOT NULL,
  size_bytes INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, content_bytes BLOB,
  content_prefix BLOB, content_truncated INTEGER NOT NULL DEFAULT 0,
  parse_status TEXT NOT NULL, error_code TEXT, error_message TEXT, observed_at TEXT NOT NULL,
  UNIQUE(page_path, file_hash)
);

CREATE TABLE IF NOT EXISTS vault_change_events (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, page_path TEXT NOT NULL, old_page_path TEXT,
  observation_id TEXT, expected_state_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
  result_revision_id TEXT, detected_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_projection_jobs (
  id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, target TEXT NOT NULL,
  operation TEXT NOT NULL, page_id TEXT, revision_id TEXT, projection_epoch INTEGER NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL, lease_owner TEXT,
  lease_expires_at TEXT, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projection_jobs_claim ON knowledge_projection_jobs(target, status, available_at, lease_expires_at);
```

For PostgreSQL, use the same names and constraints while changing observation storage to:

```sql
size_bytes BIGINT NOT NULL,
mtime_ns BIGINT NOT NULL,
content_bytes BYTEA,
content_prefix BYTEA,
content_truncated BOOLEAN NOT NULL DEFAULT FALSE
```

Create the two portable partial unique indexes only after `review_items.page_id` has been added. PostgreSQL may create them at the end of `PG_SCHEMA` after its `ALTER TABLE` statements. SQLite must create them inside `_ensure_sqlite_revision_schema()` after all review columns are present; putting them in the initial `SCHEMA` script would break an old database before migration can run:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_pending_content_conflict
ON review_items(page_id) WHERE status = 'pending' AND issue_type = 'content_conflict';
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_pending_concurrent_conflict
ON review_items(page_id) WHERE status = 'pending' AND issue_type = 'concurrent_write_conflict';
```

- [ ] **Step 4: Implement the idempotent SQLite upgrader**

Add `WIKI_PAGE_ADDITIONS`, `_sqlite_table_info`, `_sqlite_rebuild_wiki_pages`, and `_ensure_sqlite_revision_schema` to `app/db.py`. The rebuild must create the full target table, copy every old column plus `COALESCE(revision_number, 0)` and `COALESCE(projection_epoch, 0)`, compare row counts, atomically rename, replay saved non-auto indexes, and run `PRAGMA foreign_key_check` before commit.

Use this exact transaction wrapper from `init_db`:

```python
def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        try:
            _ensure_sqlite_revision_schema(conn)
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("SQLite foreign_key_check failed after Wiki migration")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
```

The additive migration list is fixed to these columns:

```python
WIKI_PAGE_ADDITIONS = {
    "page_id": "TEXT",
    "current_revision_id": "TEXT",
    "generated_revision_id": "TEXT",
    "accepted_generated_revision_id": "TEXT",
    "revision_number": "INTEGER NOT NULL DEFAULT 0",
    "file_hash": "TEXT",
    "semantic_hash": "TEXT",
    "last_write_token": "TEXT",
    "rag_visible_revision_id": "TEXT",
    "projection_epoch": "INTEGER NOT NULL DEFAULT 0",
    "rag_visible_epoch": "INTEGER",
    "lifecycle_status": "TEXT NOT NULL DEFAULT 'active'",
    "deleted_at": "TEXT",
    "sync_error": "TEXT",
    "observed_file_hash": "TEXT",
    "pending_write_intent_id": "TEXT",
}
```

Run rebuild when either counter exists with `notnull != 1` or a default other than `0`; otherwise add missing fields with `ALTER TABLE wiki_pages ADD COLUMN` and add review fields the same way. Add all five tables to `MAIN_TABLES` and their `id` primary keys to `TABLE_PRIMARY_KEYS`.

- [ ] **Step 5: Run the SQLite schema tests to verify GREEN**

Run:

```powershell
python -m pytest tests/test_wiki_schema_migration.py -q
```

Expected: PASS with `3 passed`.

- [ ] **Step 6: Commit the SQLite schema migration**

```powershell
git add app/db.py tests/test_wiki_schema_migration.py
git commit -m "feat: migrate sqlite wiki revision schema"
```

---

### Task 3: PostgreSQL Schema Parity And SQLite-To-PostgreSQL Copy

**Files:**
- Modify: `app/db.py:247-468`
- Modify: `app/migration.py:7-177`
- Modify: `tests/test_sqlite_to_postgres_migration.py`
- Create: `tests/test_wiki_revision_postgres.py`

- [ ] **Step 1: Add failing PostgreSQL schema and sparse-source migration tests**

Append to `tests/test_wiki_revision_postgres.py`:

```python
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
```

Append a sparse source assertion to `tests/test_sqlite_to_postgres_migration.py` after its existing migration setup:

```python
    with connect_postgres(settings) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT revision_number, projection_epoch, lifecycle_status FROM wiki_pages ORDER BY path LIMIT 1"
        )
        revision_number, projection_epoch, lifecycle_status = cur.fetchone()
    assert revision_number == 0
    assert projection_epoch == 0
    assert lifecycle_status == "active"
    assert result["tables"]["wiki_page_revisions"] >= 1
```

- [ ] **Step 2: Run the PostgreSQL migration tests to verify RED**

Run:

```powershell
python -m pytest tests/test_wiki_revision_postgres.py::test_postgres_revision_schema_uses_bigint_and_enforces_pending_conflict_uniqueness tests/test_sqlite_to_postgres_migration.py::test_migrate_sqlite_metadata_to_postgres_preserves_core_queries -q
```

Expected: FAIL because PostgreSQL lacks the revision columns/tables and migration allowlists reject them.

- [ ] **Step 3: Add idempotent PostgreSQL `ALTER` statements**

After the final table definitions in `PG_SCHEMA`, add one `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statement for every `wiki_pages` and `review_items` incremental field. Then normalize the two counters:

```sql
UPDATE wiki_pages SET revision_number = 0 WHERE revision_number IS NULL;
ALTER TABLE wiki_pages ALTER COLUMN revision_number SET DEFAULT 0;
ALTER TABLE wiki_pages ALTER COLUMN revision_number SET NOT NULL;
UPDATE wiki_pages SET projection_epoch = 0 WHERE projection_epoch IS NULL;
ALTER TABLE wiki_pages ALTER COLUMN projection_epoch SET DEFAULT 0;
ALTER TABLE wiki_pages ALTER COLUMN projection_epoch SET NOT NULL;
```

Keep `init_postgres_schema()` executing the split statements inside one psycopg transaction so a failed constraint or index creation rolls back the complete upgrade.

- [ ] **Step 4: Extend migration allowlists and preserve target defaults**

In `app/migration.py`, add every incremental Wiki/review column and every new table column to `_KNOWN_COLUMNS`. Keep `_upsert_row` driven by columns present in the source row; do not synthesize `None` for source-missing target columns because PostgreSQL defaults must apply.

Add these tables in dependency-safe `MAIN_TABLES` order before `audit_logs`:

```python
"wiki_page_revisions",
"wiki_file_observations",
"vault_change_events",
"vault_write_intents",
"knowledge_projection_jobs",
```

Update the PostgreSQL test cleanup table list in `tests/test_sqlite_to_postgres_migration.py` with those same names before `wiki_pages`.

- [ ] **Step 5: Run migration tests to verify GREEN**

Run:

```powershell
python -m pytest tests/test_wiki_schema_migration.py tests/test_wiki_revision_postgres.py tests/test_sqlite_to_postgres_migration.py -q
```

Expected: PASS; PostgreSQL tests execute because the project PostgreSQL service is running.

- [ ] **Step 6: Commit backend parity**

```powershell
git add app/db.py app/migration.py tests/test_sqlite_to_postgres_migration.py tests/test_wiki_revision_postgres.py
git commit -m "feat: add postgres wiki revision schema"
```

---

### Task 4: Portable Write Transactions And Projection Outbox

**Files:**
- Modify: `app/db.py:470-490`
- Create: `app/projection_jobs.py`
- Create: `tests/test_projection_outbox.py`

- [ ] **Step 1: Write failing outbox idempotency, epoch, and lease-CAS tests**

Create `tests/test_projection_outbox.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.db import connect_app, init_app_db
from app.projection_jobs import ProjectionOutbox


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
```

- [ ] **Step 2: Run the outbox tests to verify RED**

Run:

```powershell
python -m pytest tests/test_projection_outbox.py -q
```

Expected: FAIL because `connect_app_write` and `ProjectionOutbox` do not exist.

- [ ] **Step 3: Add a portable write transaction context**

Add to `app/db.py`:

```python
@contextmanager
def connect_app_write(settings: Settings) -> Iterable[Any]:
    if settings.database_backend == "postgres":
        with connect_postgres(settings) as conn:
            yield PgCompatConnection(conn)
        return
    path = settings.database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

`PageMutationCoordinator` will additionally append `FOR UPDATE` to its page query on PostgreSQL; SQLite serialization comes from this context.

- [ ] **Step 4: Implement the outbox repository and CAS completion**

Create `app/projection_jobs.py` with `ProjectionJob` and `ProjectionOutbox`. Implement `enqueue()` using `INSERT ... ON CONFLICT(idempotency_key) DO NOTHING`, then select and return the stable job ID. Generate the key exactly as:

```python
key = f"{target}:{operation}:{page_id or '-'}:{revision_id or '-'}:{projection_epoch}"
```

Implement `claim()` inside `connect_app_write()`. Use this predicate for both backends, append `FOR UPDATE SKIP LOCKED` on PostgreSQL, and bind `now_iso`, target, and limit rather than interpolating values:

```sql
SELECT * FROM knowledge_projection_jobs
WHERE target = ?
  AND attempts < 5
  AND (
    (status IN ('pending','failed') AND available_at <= ?)
    OR (status = 'running' AND lease_expires_at < ?)
  )
ORDER BY available_at, created_at
LIMIT ?
```

For each locked row, run this owner CAS and include only rows whose update count is one:

```python
claimed = conn.execute(
    """
    UPDATE knowledge_projection_jobs
    SET status='running', attempts=attempts+1, lease_owner=?, lease_expires_at=?, updated_at=?
    WHERE id=? AND attempts < 5
      AND ((status IN ('pending','failed') AND available_at <= ?)
           OR (status='running' AND lease_expires_at < ?))
    """,
    (worker_id, lease_expires_at, now_iso, row["id"], now_iso, now_iso),
)
if claimed.rowcount == 1:
    jobs.append(ProjectionJob.from_row({**dict(row), "attempts": row["attempts"] + 1}))
```

Implement the terminal CAS exactly:

```python
def finish_claimed(
    self,
    conn,
    *,
    job_id: str,
    worker_id: str,
    status: Literal["succeeded", "failed", "superseded"],
    last_error: str | None = None,
    available_at: str | None = None,
) -> bool:
    cursor = conn.execute(
        """
        UPDATE knowledge_projection_jobs
        SET status=?, last_error=?, available_at=COALESCE(?, available_at),
            lease_owner=NULL, lease_expires_at=NULL, updated_at=?
        WHERE id=? AND status='running' AND lease_owner=?
        """,
        (status, (last_error or "")[:500] or None, available_at, now_iso(), job_id, worker_id),
    )
    return cursor.rowcount == 1
```

Add the retry wrapper exactly as follows; `mark_succeeded()` is the analogous own-transaction call with status `succeeded` and no watermark write:

```python
RETRY_DELAYS = {1: 5, 2: 30, 3: 120, 4: 600}


def mark_failed(
    self,
    job_id: str,
    worker_id: str,
    last_error: str,
    now: datetime | None = None,
) -> bool:
    failed_at = now or datetime.now(timezone.utc)
    with connect_app_write(self.settings) as conn:
        suffix = " FOR UPDATE" if self.settings.database_backend == "postgres" else ""
        job = conn.execute(
            "SELECT * FROM knowledge_projection_jobs WHERE id=?" + suffix,
            (job_id,),
        ).fetchone()
        if job is None or job["status"] != "running" or job["lease_owner"] != worker_id:
            return False
        delay = RETRY_DELAYS.get(job["attempts"])
        available_at = (
            (failed_at + timedelta(seconds=delay)).isoformat()
            if delay is not None else None
        )
        return self.finish_claimed(
            conn,
            job_id=job_id,
            worker_id=worker_id,
            status="failed",
            last_error=last_error,
            available_at=available_at,
        )
```

`renew_lease()` performs `UPDATE ... WHERE id=? AND status='running' AND lease_owner=? AND attempts<5` and returns `rowcount == 1`. `supersede_stale()` updates only pending/failed/running rows with the matching page and an older epoch. `mark_succeeded()` never updates a projection watermark; the later `WikiRagProjector` owns the single transaction that verifies chunks, updates `rag_visible_revision_id/rag_visible_epoch`, and calls `finish_claimed`. A stale desired state finishes as `superseded`.

- [ ] **Step 5: Run outbox tests to verify GREEN**

Run:

```powershell
python -m pytest tests/test_projection_outbox.py -q
```

Expected: PASS with `8 passed`.

- [ ] **Step 6: Commit transaction and outbox primitives**

```powershell
git add app/db.py app/projection_jobs.py tests/test_projection_outbox.py
git commit -m "feat: add epoch scoped projection outbox"
```

---

### Task 5: Immutable Revisions, Page Locking, Idempotency, And Legacy Snapshot

**Files:**
- Create: `app/wiki_revisions.py`
- Create: `tests/test_wiki_revisions.py`

- [ ] **Step 1: Write failing immutable revision and legacy snapshot tests**

Create `tests/test_wiki_revisions.py`:

```python
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
```

- [ ] **Step 2: Run the focused tests to verify RED**

Run:

```powershell
python -m pytest tests/test_wiki_revisions.py -q
```

Expected: FAIL because `WikiRevisionService` does not exist.

- [ ] **Step 3: Define stable commands, results, and domain errors**

At the top of `app/wiki_revisions.py`, define these immutable records and commands so every later task uses one field vocabulary:

```python
MutationStatus = Literal[
    "prepared", "applied", "conflicted", "invalid", "deleted",
    "renamed", "resolved", "ignored", "failed",
]


@dataclass(frozen=True)
class RevisionRecord:
    id: str
    page_id: str
    page_path: str
    revision_number: int
    file_hash: str
    semantic_hash: str
    content: str
    origin: str
    base_revision_id: str | None
    source_ids: list[str]
    actor: str | None
    note: str | None
    metadata: dict[str, Any]
    idempotency_key: str
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> "RevisionRecord":
        data = dict(row)
        return cls(
            id=data["id"], page_id=data["page_id"], page_path=data["page_path"],
            revision_number=data["revision_number"], file_hash=data["file_hash"],
            semantic_hash=data["semantic_hash"], content=data["content"],
            origin=data["origin"], base_revision_id=data["base_revision_id"],
            source_ids=json.loads(data["source_ids_json"] or "[]"), actor=data["actor"],
            note=data["note"], metadata=json.loads(data["metadata_json"] or "{}"),
            idempotency_key=data["idempotency_key"], created_at=data["created_at"],
        )


@dataclass(frozen=True)
class PageReadResult:
    page_id: str
    page_path: str
    content: str
    raw_bytes: bytes
    current_revision_id: str
    generated_revision_id: str | None
    accepted_generated_revision_id: str | None
    lifecycle_status: str
    projection_epoch: int
    sync_error: str | None
    write_in_progress: bool
    write_intent_id: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class MutationResult:
    page_id: str
    page_path: str
    status: MutationStatus
    revision_id: str | None = None
    current_revision_id: str | None = None
    generated_revision_id: str | None = None
    candidate_revision_id: str | None = None
    write_intent_id: str | None = None
    conflict_review_id: str | None = None
    observation_id: str | None = None
    audit_revision_id: str | None = None
    projection_job_ids: tuple[str, ...] = ()
    replayed: bool = False


@dataclass(frozen=True)
class BackupReleaseResult:
    intent_id: str
    page_id: str
    backup_hash: str
    status: Literal["released"] = "released"


@dataclass(frozen=True)
class ManualSaveCommand:
    page_path: str
    content: str
    expected_revision_id: str
    request_id: str
    actor: str
    owner: str | None
    note: str | None
    review_status: str


@dataclass(frozen=True)
class StatusUpdateCommand:
    page_path: str
    review_status: str
    expected_revision_id: str
    request_id: str
    actor: str
    owner: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class MetadataUpdateCommand:
    page_path: str
    changes: dict[str, Any]
    expected_revision_id: str
    request_id: str
    actor: str
    note: str | None = None


@dataclass(frozen=True)
class CompileCandidateCommand:
    page_path: str
    content: str
    domain: str
    page_type: str
    title: str
    source_ids: list[str]
    owner: str | None
    source_hash: str
    compiler_version: str
    compile_job_id: str
    actor: str = "compiler"


@dataclass(frozen=True)
class ResolveConflictCommand:
    review_id: str
    resolution: Literal["keep_current", "accept_candidate", "merged_content"]
    merged_content: str | None
    expected_current_revision_id: str
    expected_generated_revision_id: str | None
    request_id: str
    actor: str
    note: str | None
```

Define these public errors with structured properties:

```python
class WikiRevisionError(RuntimeError):
    pass


class PreconditionRequired(WikiRevisionError):
    pass


class RevisionConflict(WikiRevisionError):
    def __init__(self, message: str, *, current_revision_id: str | None, pending_intent_id: str | None = None):
        super().__init__(message)
        self.current_revision_id = current_revision_id
        self.pending_intent_id = pending_intent_id


class PageNotFound(WikiRevisionError):
    pass


class InvalidWikiDocument(WikiRevisionError):
    def __init__(self, observation_id: str, error_code: str):
        super().__init__(error_code)
        self.observation_id = observation_id
        self.error_code = error_code
```

When `_create_revision_locked()` inserts a new row, update the in-memory `LockedPage.page["revision_number"]` before a second revision is allocated in the same lock scope; otherwise an A/replay/B sequence could reuse a number.

- [ ] **Step 4: Implement `PageMutationCoordinator` and immutable revision insertion**

`PageMutationCoordinator.lock_page(page_path=None, page_id=None)` must open `connect_app_write`; issue `SELECT * FROM wiki_pages ... FOR UPDATE` on PostgreSQL and the same query without suffix on SQLite; reject a missing page; and yield a `LockedPage(conn, page)` object. New-page creation must happen inside this same transaction and convert path/page ID unique violations to `RevisionConflict`.

Use this concrete coordinator skeleton:

```python
@dataclass
class LockedPage:
    conn: Any
    page: dict[str, Any]


class PageMutationCoordinator:
    def __init__(self, settings: Settings):
        self.settings = settings

    @contextmanager
    def lock_page(
        self,
        page_path: str | None = None,
        page_id: str | None = None,
    ) -> Iterator[LockedPage]:
        if (page_path is None) == (page_id is None):
            raise ValueError("exactly one page selector is required")
        column, value = ("path", page_path) if page_path is not None else ("page_id", page_id)
        suffix = " FOR UPDATE" if self.settings.database_backend == "postgres" else ""
        with connect_app_write(self.settings) as conn:
            row = conn.execute(
                f"SELECT * FROM wiki_pages WHERE {column}=?" + suffix,
                (value,),
            ).fetchone()
            if row is None:
                raise PageNotFound(str(value))
            yield LockedPage(conn=conn, page=dict(row))
```

Implement `_create_revision_locked()` so it:

1. Selects by `idempotency_key` first and returns that exact row on replay.
2. Computes the next number from non-null `wiki_pages.revision_number + 1` while the page lock is held.
3. Parses content with `parse_wiki_bytes`, computes file/semantic hashes, inserts the full immutable row, and updates only `wiki_pages.revision_number` with expected old counter CAS.
4. Raises if the CAS rowcount is not one; never updates an existing revision.

Use IDs `page_<uuid hex>`, `wrev_<uuid hex>`, and stable JSON from `json_dump`.

Implement the insertion/CAS body with this signature and ordering:

```python
def _create_revision_locked(
    self,
    conn: Any,
    page: dict[str, Any],
    *,
    content: bytes,
    origin: str,
    base_revision_id: str | None,
    source_ids: list[str],
    actor: str | None,
    note: str | None,
    idempotency_key: str,
    metadata: dict[str, Any],
    revision_id: str | None = None,
) -> RevisionRecord:
    existing = conn.execute(
        "SELECT * FROM wiki_page_revisions WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        return RevisionRecord.from_row(existing)
    document = parse_wiki_bytes(content)
    old_number = int(page["revision_number"] or 0)
    next_number = old_number + 1
    record = RevisionRecord(
        id=revision_id or f"wrev_{uuid.uuid4().hex}",
        page_id=page["page_id"], page_path=page["path"],
        revision_number=next_number, file_hash=compute_file_hash(content),
        semantic_hash=compute_semantic_hash(document), content=content.decode("utf-8"),
        origin=origin, base_revision_id=base_revision_id, source_ids=source_ids,
        actor=actor, note=note, metadata=metadata,
        idempotency_key=idempotency_key, created_at=now_iso(),
    )
    conn.execute(
        """
        INSERT INTO wiki_page_revisions(
          id,page_id,page_path,revision_number,file_hash,semantic_hash,content,origin,
          base_revision_id,source_ids_json,actor,note,metadata_json,idempotency_key,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            record.id,record.page_id,record.page_path,record.revision_number,
            record.file_hash,record.semantic_hash,record.content,record.origin,
            record.base_revision_id,json_dump(record.source_ids),record.actor,record.note,
            json_dump(record.metadata),record.idempotency_key,record.created_at,
        ),
    )
    advanced = conn.execute(
        "UPDATE wiki_pages SET revision_number=? WHERE page_id=? AND revision_number=?",
        (next_number, record.page_id, old_number),
    )
    if advanced.rowcount != 1:
        raise RevisionConflict(
            "revision number changed while locked",
            current_revision_id=page.get("current_revision_id"),
        )
    page["revision_number"] = next_number
    return record
```

- [ ] **Step 5: Implement conservative first-read legacy snapshot**

`get_page()` must validate the Vault path and capture the original raw bytes before locking the page. If `page_id/current_revision_id` are absent, allocate `page_id`, `revision_id`, and `write_token` first; parse the original document; render those managed fields once; and insert exactly one `origin='legacy'` revision whose `content/file_hash/semantic_hash` describe those final rendered bytes. In the same transaction create one write intent with `expected_revision_id=NULL`, `expected_file_hash=<original raw hash>`, and `revision_id=<the single legacy revision>`, then set only `pending_write_intent_id`. Leave current/generated/accepted-generated null until Task 7 installs and finalizes the intent.

After the prepare transaction commits, `get_page()` invokes the real `IntentExecutor.execute()` synchronously. Task 7's finalize transaction advances current to that one legacy revision; no successor legacy revision and no temporary direct-pointer branch exist. The focused test remains RED until Task 7 is complete, then observes exactly one revision and one applied intent.

- [ ] **Step 6: Run the revision core tests to verify GREEN**

Run after completing Task 7's executor/finalize path:

```powershell
python -m pytest tests/test_wiki_revisions.py::test_first_read_creates_one_legacy_revision_without_generated_baseline tests/test_wiki_revisions.py::test_revision_event_replay_reuses_only_same_idempotency_key -q
```

Expected: PASS with `2 passed`.

- [ ] **Step 7: Commit the revision core together with the Task 6 writer hook**

Do not commit at this checkpoint alone. Complete Tasks 6 and 7, then use Task 7's combined commit command so no commit exposes an unsafe legacy bootstrap.

---

### Task 6: Capture-Before-Replace Atomic Vault Writer

**Files:**
- Create: `app/vault_writer.py`
- Modify: `app/wiki_revisions.py`
- Create: `tests/test_vault_writer.py`

- [ ] **Step 1: Write failing capture/no-replace and retained-backup tests**

Create `tests/test_vault_writer.py`:

```python
from pathlib import Path

import pytest

from app.vault_writer import AtomicVaultWriter, TargetChanged
from app.wiki_markdown import compute_file_hash


def test_writer_captures_old_inode_installs_without_replace_and_retains_backup(tmp_path: Path):
    vault = tmp_path / "vault"
    target = vault / "wiki/product/faq/demo.md"
    target.parent.mkdir(parents=True)
    old = b"old human bytes"
    new = b"new managed bytes"
    target.write_bytes(old)
    writer = AtomicVaultWriter(vault)

    captured = writer.capture(
        intent_id="wint_1", target_path="wiki/product/faq/demo.md",
        expected_file_hash=compute_file_hash(old),
    )
    installed = writer.install(
        intent_id="wint_1", target_path="wiki/product/faq/demo.md", content=new,
    )

    assert captured.backup_path.read_bytes() == old
    assert installed.target_hash == compute_file_hash(new)
    assert target.read_bytes() == new
    assert captured.backup_path.exists()


def test_writer_never_overwrites_target_created_after_capture(tmp_path: Path):
    vault = tmp_path / "vault"
    target = vault / "wiki/product/faq/demo.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    writer = AtomicVaultWriter(vault)
    writer.capture("wint_2", "wiki/product/faq/demo.md", compute_file_hash(b"old"))
    target.write_bytes(b"obsidian wins")

    with pytest.raises(TargetChanged):
        writer.install("wint_2", "wiki/product/faq/demo.md", b"managed")
    assert target.read_bytes() == b"obsidian wins"
    assert writer.backup_path("wint_2").read_bytes() == b"old"


def test_capture_hash_mismatch_keeps_unknown_bytes_in_backup(tmp_path: Path):
    vault = tmp_path / "vault"
    target = vault / "wiki/product/faq/demo.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"changed before capture")
    writer = AtomicVaultWriter(vault)

    with pytest.raises(TargetChanged) as exc_info:
        writer.capture("wint_3", "wiki/product/faq/demo.md", compute_file_hash(b"expected"))
    assert exc_info.value.observed_hash == compute_file_hash(b"changed before capture")
    assert writer.backup_path("wint_3").read_bytes() == b"changed before capture"
    assert not target.exists()
```

- [ ] **Step 2: Run writer tests to verify RED**

Run:

```powershell
python -m pytest tests/test_vault_writer.py -q
```

Expected: FAIL because `app.vault_writer` does not exist.

- [ ] **Step 3: Implement same-filesystem staging, capture, and no-replace install**

Create `AtomicVaultWriter` with paths under `.lgdo/pending/<intent_id>/`: `new.md`, `backup.md`, and `executor.lock`. Its methods must:

```python
def _fsync_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def capture(self, intent_id: str, target_path: str, expected_file_hash: str | None) -> CapturedFile:
    target = self._safe_target(target_path)
    backup = self.backup_path(intent_id)
    if backup.exists():
        observed = compute_file_hash(backup.read_bytes())
        return CapturedFile(backup, observed)
    if not target.exists():
        if expected_file_hash is not None:
            raise TargetMissing(target_path)
        return CapturedFile(backup, None)
    backup.parent.mkdir(parents=True, exist_ok=True)
    os.rename(target, backup)
    observed = self._stream_hash(backup)
    if observed != expected_file_hash:
        raise TargetChanged(target_path, observed)
    return CapturedFile(backup, observed)


def install(self, intent_id: str, target_path: str, content: bytes) -> InstalledFile:
    target = self._safe_target(target_path)
    staged = self.staged_path(intent_id)
    if not staged.exists():
        _fsync_file(staged, content)
    elif self._stream_hash(staged) != compute_file_hash(content):
        raise TargetChanged(str(staged), self._stream_hash(staged))
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(staged, target)
    except FileExistsError as exc:
        raise TargetChanged(target_path, self._stream_hash(target)) from exc
    self._fsync_directory(target.parent)
    observed = self._stream_hash(target)
    if observed != compute_file_hash(content):
        raise TargetChanged(target_path, observed)
    return InstalledFile(target, observed)
```

Use `Path.resolve()` containment checks for every target/hidden path. Never call `os.replace` for target installation and never unlink `backup.md` during successful finalization.

- [ ] **Step 4: Add write-intent preparation to the revision service**

Implement `prepare_write_intent_locked()` in `WikiRevisionService`. In the page transaction it must verify expected current/file hash, require `pending_write_intent_id IS NULL`, create `wint_<uuid>`, insert status `pending`, and set the page pending pointer with a CAS on expected current/generated/pending state. The revision content stored for the intent must already contain final managed page/revision/write-token fields and its `file_hash` must match those final bytes.

Change the Task 5 legacy snapshot path to create and synchronously execute this intent before returning; current advances only in the finalize transaction introduced in Task 7.

- [ ] **Step 5: Verify writer primitives while the revision finalize test remains RED**

Run:

```powershell
python -m pytest tests/test_vault_writer.py -q
python -m pytest tests/test_wiki_revisions.py::test_first_read_creates_one_legacy_revision_without_generated_baseline -q
```

Expected: writer tests PASS; the legacy revision test still FAILS because Task 7 intentionally owns lease-safe finalize.

- [ ] **Step 6: Keep Tasks 5-6 uncommitted until finalize is present**

Proceed directly to Task 7; its commit is the first commit containing `app/wiki_revisions.py` and `app/vault_writer.py`.

---

### Task 7: Intent Lease Executor, Finalize CAS, And Startup Reconcile

**Files:**
- Modify: `app/vault_writer.py`
- Modify: `app/wiki_revisions.py`
- Modify: `tests/test_vault_writer.py`
- Modify: `tests/test_wiki_revisions.py`

- [ ] **Step 1: Add failing lease, phase-owner, and installed-intent recovery tests**

Append to `tests/test_vault_writer.py`:

```python
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.db import connect_app, init_app_db
from app.vault_writer import IntentExecutor
from app.wiki_revisions import ManualSaveCommand, WikiRevisionService


@pytest.fixture
def wiki_intent_fixture(tmp_path):
    settings = get_settings().model_copy()
    settings.database_backend = "sqlite"
    settings.database_path = tmp_path / "intent.db"
    settings.vault_path = tmp_path / "vault"
    page_path = "wiki/product/faq/intent.md"
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True)
    target.write_text(
        "---\ntitle: Intent\nsource_ids: [src_intent]\nreview_status: draft\n---\n# Intent\n",
        encoding="utf-8",
    )
    init_app_db(settings)
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,domain,page_type,title,source_ids_json,review_status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (page_path,"product","faq","Intent",'["src_intent"]',"draft","t0","t0"),
        )
    service = WikiRevisionService(settings)
    current = service.get_page(page_path)
    prepared = service.prepare_manual_save(
        ManualSaveCommand(
            page_path=page_path,
            content=current.content + "\nPrepared manual change.\n",
            expected_revision_id=current.current_revision_id,
            request_id="fixture-pending-intent",
            actor="test",
            owner=None,
            note=None,
            review_status="draft",
        ),
        execute_intent=False,
    )
    assert prepared.status == "prepared"
    assert prepared.write_intent_id is not None
    return settings, prepared.write_intent_id


def test_only_lease_and_os_lock_owner_can_advance_intent(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    now = datetime.now(timezone.utc)
    first = IntentExecutor(settings, owner="executor_a")
    second = IntentExecutor(settings, owner="executor_b")

    assert first.claim(intent_id, now=now, lease_seconds=30) is True
    assert second.claim(intent_id, now=now, lease_seconds=30) is False
    assert second.advance_phase(intent_id, expected_status="pending", status="captured") is False
    assert first.advance_phase(intent_id, expected_status="pending", status="captured") is True


def test_reconcile_finalizes_installed_intent_after_process_crash(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="crashed")
    assert executor.claim(intent_id, lease_seconds=1)
    executor.capture_and_install(intent_id, stop_after="installed")

    with connect_app(settings) as conn:
        before = conn.execute("SELECT current_revision_id,pending_write_intent_id FROM wiki_pages").fetchone()
    assert before[1] == intent_id

    recovered = IntentExecutor(settings, owner="recovery").reconcile_all(
        now=datetime.now(timezone.utc) + timedelta(seconds=2)
    )

    with connect_app(settings) as conn:
        page = conn.execute("SELECT current_revision_id,pending_write_intent_id FROM wiki_pages").fetchone()
        intent = conn.execute("SELECT status,backup_retention_status FROM vault_write_intents WHERE id=?", (intent_id,)).fetchone()
    assert recovered == [intent_id]
    assert page[0] is not None and page[1] is None
    assert tuple(intent) == ("applied", "retained")


def test_terminal_clear_requires_matching_pending_intent(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    executor = IntentExecutor(settings, owner="executor_a")
    assert executor.claim(intent_id, lease_seconds=30)
    with connect_app(settings) as conn:
        conn.execute("UPDATE wiki_pages SET pending_write_intent_id='wint_successor'")
    assert executor.clear_terminal(intent_id, "failed", "simulated") is False
    with connect_app(settings) as conn:
        page = conn.execute("SELECT pending_write_intent_id FROM wiki_pages").fetchone()
    assert page[0] == "wint_successor"
```

- [ ] **Step 2: Run the three executor tests to verify RED**

Run:

```powershell
python -m pytest tests/test_vault_writer.py -k "lease or reconcile or terminal" -q
```

Expected: FAIL because `IntentExecutor` and finalize/reconcile methods are absent.

- [ ] **Step 3: Implement DB lease plus OS lock ownership**

Add this result type and method contract to `app/vault_writer.py` before `IntentExecutor`:

```python
@dataclass(frozen=True)
class IntentReconcileResult:
    intent_id: str
    intent_status: Literal[
        "pending", "captured", "installed", "recovery_required",
        "applied", "superseded", "aborted", "failed",
    ]
    current_revision_id: str | None
    current_kind: Literal["intended", "external_target", "external_backup", "unchanged"]
    successor_intent_id: str | None = None
    observation_ids: tuple[str, ...] = ()


class IntentExecutor:
    def execute(
        self,
        intent_id: str,
        *,
        stop_after: Literal["intent_created", "captured", "installed"] | None = None,
        lease_seconds: int = 30,
    ) -> IntentReconcileResult | None: ...

    def reconcile_one(self, intent_id: str, *, now: datetime | None = None) -> IntentReconcileResult: ...
    def reconcile_all(self, *, now: datetime | None = None) -> list[str]: ...
```

`claim()` must run in `connect_app_write()` and update exactly one nonterminal intent when the lease is empty, expired, or already owned by this executor. Use this exact owner/lease CAS:

```python
claimed = conn.execute(
    """
    UPDATE vault_write_intents
    SET executor_owner=?, lease_expires_at=?, attempts=attempts+1, updated_at=?
    WHERE id=?
      AND status IN ('pending','captured','installed','recovery_required')
      AND (executor_owner IS NULL OR executor_owner=? OR lease_expires_at<?)
    """,
    (self.owner, lease_expires_at, now_iso, intent_id, self.owner, now_iso),
)
return claimed.rowcount == 1
```

`advance_phase()` must use:

```sql
UPDATE vault_write_intents
SET status=?, updated_at=?
WHERE id=? AND status=? AND executor_owner=?
```

and return `cursor.rowcount == 1`. While executing, hold:

```python
with portalocker.Lock(str(writer.lock_path(intent_id)), mode="a+", timeout=0):
    self.renew_lease(intent_id)
    self.capture_and_install(intent_id)
```

If the OS lock cannot be acquired, return without mutating the intent. Renew the lease before/after capture and install. A process without both owner and OS lock may observe only.

- [ ] **Step 4: Implement finalize under page lock and epoch outbox**

Add `WikiRevisionService.finalize_intent(intent_id, executor_owner)` and call it only for `installed` intents. In one `PageMutationCoordinator` transaction:

1. Lock page and intent; verify intent owner, expected current, pending pointer, target hash, and intended revision hash.
2. Update current/file/semantic/last token, review metadata, `projection_epoch = COALESCE(projection_epoch,0)+1`, clear RAG watermarks, and clear the pending pointer with all expected values in the `WHERE` clause.
3. Enqueue RAG and GBrain upserts for the new epoch through `ProjectionOutbox`.
4. Mark intent `applied`, clear lease fields, set backup retention to `retained`, and write an `audit_logs` event.
5. Check every state-changing cursor has rowcount one; otherwise leave/reclassify as `recovery_required` for reconcile.

Use this transaction skeleton; `_stream_hash` reads without rewriting and `_reconcile_pending_reviews_locked` is implemented in Task 10:

```python
def finalize_intent(self, intent_id: str, executor_owner: str) -> MutationResult:
    with connect_app(self.settings) as read_conn:
        locator = read_conn.execute(
            "SELECT page_id,target_path FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()
    if locator is None:
        raise WikiRevisionError(f"write intent not found: {intent_id}")
    target = self.settings.vault_path / locator["target_path"]
    installed_hash = self.writer._stream_hash(target)
    with self.coordinator.lock_page(page_id=locator["page_id"]) as locked:
        suffix = " FOR UPDATE" if self.settings.database_backend == "postgres" else ""
        intent = locked.conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?" + suffix, (intent_id,)
        ).fetchone()
        revision = locked.conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?", (intent["revision_id"],)
        ).fetchone()
        if (
            intent["status"] != "installed"
            or intent["executor_owner"] != executor_owner
            or locked.page["pending_write_intent_id"] != intent_id
            or installed_hash != revision["file_hash"]
        ):
            raise RevisionConflict(
                "intent finalize precondition changed",
                current_revision_id=locked.page["current_revision_id"],
                pending_intent_id=locked.page["pending_write_intent_id"],
            )
        expected_sql = (
            "current_revision_id IS NULL"
            if intent["expected_revision_id"] is None
            else "current_revision_id=?"
        )
        expected_params = () if intent["expected_revision_id"] is None else (intent["expected_revision_id"],)
        next_epoch = int(locked.page["projection_epoch"] or 0) + 1
        advanced = locked.conn.execute(
            f"""
            UPDATE wiki_pages
            SET current_revision_id=?,file_hash=?,semantic_hash=?,last_write_token=?,
                projection_epoch=?,rag_visible_revision_id=NULL,rag_visible_epoch=NULL,
                lifecycle_status='active',sync_error=NULL,pending_write_intent_id=NULL,updated_at=?
            WHERE page_id=? AND pending_write_intent_id=? AND {expected_sql}
            """,
            (
                revision["id"],revision["file_hash"],revision["semantic_hash"],
                intent["write_token"],next_epoch,now_iso(),intent["page_id"],intent_id,
                *expected_params,
            ),
        )
        if advanced.rowcount != 1:
            raise RevisionConflict(
                "current revision changed before finalize",
                current_revision_id=locked.page["current_revision_id"],
                pending_intent_id=intent_id,
            )
        jobs = self.outbox.enqueue_pair_for_state(
            locked.conn,
            {**locked.page,"current_revision_id": revision["id"],"projection_epoch": next_epoch},
            "upsert",
            {"path": locked.page["path"]},
        )
        applied = locked.conn.execute(
            """
            UPDATE vault_write_intents
            SET status='applied',backup_retention_status='retained',executor_owner=NULL,
                lease_expires_at=NULL,updated_at=?
            WHERE id=? AND status='installed' AND executor_owner=?
            """,
            (now_iso(),intent_id,executor_owner),
        )
        if applied.rowcount != 1:
            raise WikiRevisionError("intent owner/status CAS failed during finalize")
        self._reconcile_pending_reviews_locked(locked.conn, intent["page_id"])
        audit(locked.conn,"wiki_revision_applied",{"intent_id":intent_id,"revision_id":revision["id"]},now_iso())
        return MutationResult(
            page_id=intent["page_id"],page_path=locked.page["path"],status="applied",
            revision_id=revision["id"],current_revision_id=revision["id"],
            generated_revision_id=locked.page["generated_revision_id"],
            write_intent_id=intent_id,projection_job_ids=tuple(jobs),
        )
```

For `aborted`/`failed`, clear the pending pointer only while holding the page lock and only when it still equals this intent ID. Never delete the backup/staged directory.

- [ ] **Step 5: Implement deterministic startup reconciliation**

`reconcile_all(now=None)` must claim expired/non-owned `pending`, `captured`, `installed`, and `recovery_required` intents in creation order. Classify target and backup hashes against expected/intended hashes. Continue safe phases, finalize installed content, or call the Task 13 human-wins handoff for unknown bytes. Until Task 13 lands, unknown classifications remain `recovery_required` and pending, never `failed` with a cleared pointer.

Startup ordering for the later watcher plan is fixed as `IntentExecutor.reconcile_all()` first, ordinary Vault reconcile second, watcher start last.

- [ ] **Step 6: Run writer/revision tests to verify GREEN**

Run:

```powershell
python -m pytest tests/test_vault_writer.py tests/test_wiki_revisions.py -q
```

Expected: PASS, including the Task 5 legacy snapshot and all Task 6 writer tests.

- [ ] **Step 7: Commit Tasks 5-7 as one safe unit**

```powershell
git add app/wiki_revisions.py app/vault_writer.py tests/test_wiki_revisions.py tests/test_vault_writer.py
git commit -m "feat: add recoverable wiki revision writes"
```

---

### Task 8: Manual Save, Status Frontmatter, And Concurrent Human Candidates

**Files:**
- Modify: `app/wiki_revisions.py`
- Modify: `app/domain_reclassify.py:145-153`
- Modify: `tests/test_wiki_revisions.py`
- Modify: `tests/test_domain_reclassify.py`

- [ ] **Step 1: Add failing manual/status/CAS tests**

Append to `tests/test_wiki_revisions.py`:

```python
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.wiki_revisions import ManualSaveCommand, RevisionConflict, StatusUpdateCommand


@pytest.fixture
def legacy_page_fixture(tmp_path):
    settings = make_settings(tmp_path)
    page_path = seed_legacy_page(
        settings,
        b"---\ntitle: Demo\nsource_ids: [src_1]\nreview_status: reviewed\n---\n# Demo\n",
    )
    service = WikiRevisionService(settings)
    return service, service.get_page(page_path)


def test_manual_save_requires_current_revision_and_keeps_generated_pointer(legacy_page_fixture):
    service, page = legacy_page_fixture
    before_generated = page.generated_revision_id
    content = page.content.replace("# Demo", "# Demo\n\nHuman addition")

    result = service.prepare_manual_save(
        ManualSaveCommand(
            page_path=page.page_path, content=content,
            expected_revision_id=page.current_revision_id, request_id="save_1",
            actor="alice", owner="alice", note="human edit", review_status="reviewed",
        )
    )
    saved = service.get_page(page.page_path)

    assert result.status == "applied"
    assert saved.current_revision_id != page.current_revision_id
    assert saved.generated_revision_id == before_generated
    assert "Human addition" in saved.content
    assert saved.metadata["review_status"] == "reviewed"
    assert len(result.projection_job_ids) == 2


def test_status_update_is_a_manual_revision_and_changes_frontmatter(legacy_page_fixture):
    service, page = legacy_page_fixture
    result = service.update_status(
        StatusUpdateCommand(
            page_path=page.page_path, review_status="stale",
            expected_revision_id=page.current_revision_id, request_id="status_1",
            actor="alice", owner=None, note="expired",
        )
    )
    changed = service.get_page(page.page_path)
    assert result.current_revision_id == changed.current_revision_id
    assert changed.generated_revision_id == page.generated_revision_id
    assert "review_status: stale" in changed.content


def test_stale_manual_save_fails_before_file_change(legacy_page_fixture):
    service, page = legacy_page_fixture
    path = service.settings.vault_path / page.page_path
    before = path.read_bytes()
    with pytest.raises(RevisionConflict) as exc_info:
        service.prepare_manual_save(
            ManualSaveCommand(
                page_path=page.page_path, content=page.content + "\nstale",
                expected_revision_id="wrev_stale", request_id="save_stale",
                actor="bob", owner=None, note=None, review_status="draft",
            )
        )
    assert exc_info.value.current_revision_id == page.current_revision_id
    assert path.read_bytes() == before


def test_two_sqlite_writers_prepare_only_one_intent(legacy_page_fixture):
    service, page = legacy_page_fixture
    def save(request_id: str):
        try:
            return service.prepare_manual_save(
                ManualSaveCommand(
                    page_path=page.page_path, content=page.content + request_id,
                    expected_revision_id=page.current_revision_id, request_id=request_id,
                    actor=request_id, owner=None, note=None, review_status="draft",
                ),
                execute_intent=False,
            )
        except RevisionConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save, ["a", "b"]))
    assert sum(isinstance(item, RevisionConflict) for item in outcomes) == 1
    assert sum(getattr(item, "write_intent_id", None) is not None for item in outcomes) == 1
    with connect_app(service.settings) as conn:
        prepared = conn.execute(
            "SELECT COUNT(*) FROM vault_write_intents WHERE page_id=? AND expected_revision_id=?",
            (page.page_id, page.current_revision_id),
        ).fetchone()[0]
    assert prepared == 1
```

- [ ] **Step 2: Run the manual/status tests to verify RED**

Run:

```powershell
python -m pytest tests/test_wiki_revisions.py -k "manual or status or sqlite_writers" -q
```

Expected: FAIL because manual/status commands and state transitions are absent.

- [ ] **Step 3: Implement command/state idempotency and manual writes**

`prepare_manual_save()` and `update_status()` must build a transition key from `request_id` plus canonical expected state:

```python
import hashlib
import json


def canonical_state_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


state = {
    "current": page["current_revision_id"],
    "generated": page["generated_revision_id"],
    "accepted_generated": page["accepted_generated_revision_id"],
    "lifecycle": page["lifecycle_status"],
    "path": page["path"],
    "file_hash": page["file_hash"],
    "pending_conflict": pending_conflict_ids,
}
state_digest = hashlib.sha256(canonical_state_json(state).encode("utf-8")).hexdigest()
transition_key = f"manual:{command.request_id}:{state_digest}"
```

Require expected current equality and no pending intent under the page lock. Parse and validate source IDs/status, replace only managed fields, create an immutable `manual` revision, create the intent, and commit. With the public default `execute_intent=True`, synchronously call `IntentExecutor.execute()` and return the applied result; `execute_intent=False` returns the durable prepared result for a separately scheduled executor. Replaying the same request/state returns the original revision/result; a new expected state executes a new transition.

If an external/backup write appears after prepare, retain the manual revision as the candidate and let Task 13 create `concurrent_write_conflict`; never modify generated/accepted-generated pointers for this conflict type.

- [ ] **Step 4: Route domain reclassification through the same service**

Replace `_update_wiki_frontmatter()` string replacement/direct `write_text` with a `WikiRevisionService.update_metadata()` command carrying the current expected revision, request ID `domain-reclassify:{page_id}:{domain}:{source_command_id}`, actor `domain-reclassifier`, and a metadata transform that sets `domain`. Extend `tests/test_domain_reclassify.py` to assert one `manual` revision, a changed current pointer, unchanged generated pointer, and two projection jobs.

- [ ] **Step 5: Run focused tests to verify GREEN**

Run:

```powershell
python -m pytest tests/test_wiki_revisions.py -k "manual or status or sqlite_writers" tests/test_domain_reclassify.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit human mutation paths**

```powershell
git add app/wiki_revisions.py app/domain_reclassify.py tests/test_wiki_revisions.py tests/test_domain_reclassify.py
git commit -m "feat: serialize manual wiki mutations"
```

---

### Task 9: Generated Compile State Machine And Non-Blocking Result

**Files:**
- Modify: `app/wiki.py:58-235`
- Modify: `app/models.py:44-55`
- Modify: `app/wiki_revisions.py`
- Modify: `tests/test_wiki_revisions.py`

- [ ] **Step 1: Add failing compile safety and artifact-reuse tests**

Append to `tests/test_wiki_revisions.py`:

```python
from app.ingest import scan_sources
from app.models import CompileRequest, ScanRequest
from app.wiki import compile_wiki


@pytest.fixture
def compiled_source_fixture(tmp_path):
    settings = make_settings(tmp_path)
    settings.upload_path = tmp_path / "uploads"
    settings.gbrain_import_on_compile = False
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "demo.md").write_text("# Demo\n\nGenerated source body.\n", encoding="utf-8")
    scan_sources(
        settings,
        ScanRequest(root_path=str(samples), domain="product", owner="compiler-test"),
    )
    with connect_app(settings) as conn:
        source_id = conn.execute("SELECT id FROM sources WHERE title='demo'").fetchone()[0]
    compile_wiki(
        settings,
        CompileRequest(domain="product", source_ids=[source_id], compile_job_id="compile-initial"),
    )
    with connect_app(settings) as conn:
        page_path = conn.execute("SELECT path FROM wiki_pages WHERE title='demo'").fetchone()[0]
    return settings, source_id, page_path


@pytest.fixture
def page_with_generated(compiled_source_fixture):
    settings, _, page_path = compiled_source_fixture
    service = WikiRevisionService(settings)
    return service, service.get_page(page_path)


def test_recompile_after_manual_edit_preserves_human_file_and_creates_one_conflict(compiled_source_fixture):
    settings, source_id, page_path = compiled_source_fixture
    service = WikiRevisionService(settings)
    page = service.get_page(page_path)
    service.prepare_manual_save(
        ManualSaveCommand(
            page_path=page_path, content=page.content + "\nHuman text\n",
            expected_revision_id=page.current_revision_id, request_id="manual-before-compile",
            actor="alice", owner=None, note=None, review_status="reviewed",
        )
    )
    human_bytes = (settings.vault_path / page_path).read_bytes()

    first = compile_wiki(settings, CompileRequest(domain="product", source_ids=[source_id]))
    second = compile_wiki(settings, CompileRequest(domain="product", source_ids=[source_id], compile_job_id=first.job_id))

    assert (settings.vault_path / page_path).read_bytes() == human_bytes
    assert first.conflicted_pages == 1
    assert second.conflicted_pages == 1
    with connect_app(settings) as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM review_items WHERE page_path=? AND issue_type='content_conflict' AND status='pending'",
            (page_path,),
        ).fetchone()[0]
    assert pending == 1


def test_recompile_auto_advances_only_when_current_equals_previous_generated(compiled_source_fixture):
    settings, source_id, page_path = compiled_source_fixture
    service = WikiRevisionService(settings)
    before = service.get_page(page_path)
    assert before.current_revision_id == before.generated_revision_id
    with connect_app(settings) as conn:
        conn.execute("UPDATE sources SET content_hash='source-v2', last_compiled_at=NULL WHERE id=?", (source_id,))
    result = compile_wiki(settings, CompileRequest(domain="product", source_ids=[source_id], compile_job_id="compile-v2"))
    after = service.get_page(page_path)
    assert result.updated_pages == 1
    assert result.conflicted_pages == 0
    assert after.current_revision_id == after.generated_revision_id
    assert after.current_revision_id != before.current_revision_id


def test_unchanged_generated_artifact_still_reconciles_new_manual_divergence(compiled_source_fixture):
    settings, source_id, page_path = compiled_source_fixture
    service = WikiRevisionService(settings)
    before = service.get_page(page_path)
    service.prepare_manual_save(
        ManualSaveCommand(
            page_path=page_path, content=before.content + "\nmanual\n",
            expected_revision_id=before.current_revision_id, request_id="manual-diverge",
            actor="alice", owner=None, note=None, review_status="reviewed",
        )
    )
    with connect_app(settings) as conn:
        generated_count = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=? AND origin='generated'", (before.page_id,)
        ).fetchone()[0]
    result = compile_wiki(settings, CompileRequest(domain="product", source_ids=[source_id], compile_job_id="same-artifact"))
    with connect_app(settings) as conn:
        generated_after = conn.execute(
            "SELECT COUNT(*) FROM wiki_page_revisions WHERE page_id=? AND origin='generated'", (before.page_id,)
        ).fetchone()[0]
    assert generated_after == generated_count
    assert result.conflicted_pages == 1
```

- [ ] **Step 2: Run compile state tests to verify RED**

Run:

```powershell
python -m pytest tests/test_wiki_revisions.py -k "recompile or generated_artifact" -q
```

Expected: FAIL because compile still writes the file directly and response lacks conflict/projection counts.

- [ ] **Step 3: Add compile command identity and response fields**

Add optional `compile_job_id` to `CompileRequest` with a UUID default. Extend `CompileResponse` while retaining `review_items` for client compatibility:

```python
class CompileResponse(BaseModel):
    job_id: str
    created_pages: int
    updated_pages: int
    review_items: int
    conflicted_pages: int = 0
    projection_jobs: int = 0
```

- [ ] **Step 4: Implement generated candidate transition**

`apply_generated_candidate()` must first compare the observed disk file hash with the current revision. Unknown valid bytes go through Task 11 external ingest; invalid bytes stop this page compile. Under the page lock:

1. Capture old current/generated pointers and state vector.
2. Reuse the latest generated revision only when semantic hash, source content hash, and `COMPILER_VERSION = "wiki-revision-v1"` all match; otherwise create `origin='generated'` with key `compile:{job_id}:{page_id}:{source_hash}:{compiler_version}:{state_hash}`.
3. Before changing generated, supersede pending content conflicts whose candidate differs.
4. Set `generated_revision_id` to the candidate even when current remains human content.
5. Auto-prepare a write only for a new page, or when old current equals old generated and disk hash equals current revision hash.
6. Otherwise call `_reconcile_pending_reviews_locked()` from Task 10; no file write occurs.
7. Only a finalized current change increments projection epoch and enqueues jobs.

The candidate revision stores final managed page/revision/write token bytes, source IDs, source hash, and compiler version. A reused artifact still executes steps 3-6 against the latest state; it is not a whole-command no-op.

- [ ] **Step 5: Refactor `compile_wiki()` to delegate without waiting for GBrain**

Keep source selection/page rendering in `app/wiki.py`, but remove `Path.write_text`, Wiki table upsert, ad hoc review insertion, and `import_vault_to_gbrain()`. Call the service once per generated page, update `sources.last_compiled_at` only after its transition succeeds, preserve index generation, and aggregate created/updated/conflicted/projection counts. Compile returns after durable outbox creation.

- [ ] **Step 6: Run compile tests to verify GREEN**

Run:

```powershell
python -m pytest tests/test_wiki_revisions.py -k "recompile or generated_artifact" -q
```

Expected: PASS with no GBrain CLI invocation.

- [ ] **Step 7: Commit safe compilation**

```powershell
git add app/wiki.py app/models.py app/wiki_revisions.py tests/test_wiki_revisions.py
git commit -m "feat: compile wiki through revision state machine"
```

---

### Task 10: Generated And Concurrent Conflict Resolution

**Files:**
- Modify: `app/wiki_revisions.py`
- Modify: `tests/test_wiki_revisions.py`

- [ ] **Step 1: Add failing conflict transition-table tests**

Append these parameterized tests:

```python
from dataclasses import dataclass
import uuid

from app.db import connect_app_write, json_dump
from app.timeutil import now_iso


@dataclass(frozen=True)
class ContentConflictScenario:
    service: WikiRevisionService
    conflict: dict
    current: PageReadResult
    generated_content: str

    def generated_job(self, content: str, source_hash: str, job_id: str) -> CompileCandidateCommand:
        metadata = self.current.metadata
        return CompileCandidateCommand(
            page_path=self.current.page_path,
            content=content,
            domain=metadata["domain"],
            page_type=metadata["page_type"],
            title=metadata["title"],
            source_ids=metadata["source_ids"],
            owner=metadata.get("owner"),
            source_hash=source_hash,
            compiler_version="wiki-revision-v1",
            compile_job_id=job_id,
        )

    def same_semantic_new_job(self) -> CompileCandidateCommand:
        return self.generated_job(self.generated_content, "same-semantic-v2", "compile-same-semantic")

    def new_candidate_job(self) -> CompileCandidateCommand:
        return self.generated_job(
            self.generated_content + "\nNew generated fact.\n",
            "different-semantic-v3",
            "compile-different-semantic",
        )


@pytest.fixture
def content_conflict_fixture(page_with_generated):
    service, generated_page = page_with_generated
    service.prepare_manual_save(
        ManualSaveCommand(
            page_path=generated_page.page_path,
            content=generated_page.content + "\nHuman divergence.\n",
            expected_revision_id=generated_page.current_revision_id,
            request_id="fixture-manual-divergence",
            actor="alice", owner=None, note=None, review_status="reviewed",
        )
    )
    current = service.get_page(generated_page.page_path)
    service.apply_generated_candidate(
        CompileCandidateCommand(
            page_path=current.page_path,
            content=generated_page.content,
            domain=current.metadata["domain"], page_type=current.metadata["page_type"],
            title=current.metadata["title"], source_ids=current.metadata["source_ids"],
            owner=current.metadata.get("owner"), source_hash="fixture-generated-v1",
            compiler_version="wiki-revision-v1", compile_job_id="fixture-conflict",
        )
    )
    conflict = service.list_conflicts(current.page_path, status="pending")[0]
    return ContentConflictScenario(service, conflict, current, generated_page.content)


@pytest.fixture
def concurrent_conflict_fixture(page_with_generated):
    service, before = page_with_generated
    prepared = service.prepare_manual_save(
        ManualSaveCommand(
            page_path=before.page_path,
            content=before.content + "\nConcurrent manual candidate.\n",
            expected_revision_id=before.current_revision_id,
            request_id="fixture-concurrent-candidate",
            actor="bob", owner=None, note=None, review_status="reviewed",
        ),
        execute_intent=False,
    )
    review_id = f"review_{uuid.uuid4().hex}"
    timestamp = now_iso()
    with connect_app_write(service.settings) as conn:
        page = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?", (before.page_id,)
        ).fetchone()
        expected_state = json_dump({
            "current": page["current_revision_id"],
            "generated": page["generated_revision_id"],
            "accepted_generated": page["accepted_generated_revision_id"],
            "lifecycle": page["lifecycle_status"],
            "path": page["path"],
            "file_hash": page["file_hash"],
        })
        conn.execute(
            """
            UPDATE vault_write_intents
            SET status='superseded', updated_at=?
            WHERE id=? AND status='pending'
            """,
            (timestamp, prepared.write_intent_id),
        )
        cleared = conn.execute(
            "UPDATE wiki_pages SET pending_write_intent_id=NULL WHERE page_id=? AND pending_write_intent_id=?",
            (before.page_id, prepared.write_intent_id),
        )
        assert cleared.rowcount == 1
        conn.execute(
            """
            INSERT INTO review_items(
              id,page_path,issue_type,status,source_ids_json,created_at,updated_at,
              page_id,base_revision_id,candidate_revision_id,expected_state_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                review_id,before.page_path,"concurrent_write_conflict","pending",
                page["source_ids_json"],timestamp,timestamp,before.page_id,
                before.current_revision_id,prepared.revision_id,expected_state,
            ),
        )
    conflict = service.list_conflicts(before.page_path, status="pending")[0]
    return service, conflict, before


@pytest.mark.parametrize(
    "resolution,merged_suffix,current_is_generated",
    [("keep_current", None, False), ("accept_candidate", None, True), ("merged_content", "\nMerged human rule\n", False)],
)
def test_content_conflict_resolution_and_next_compile_transition(
    content_conflict_fixture, resolution, merged_suffix, current_is_generated
):
    scenario = content_conflict_fixture
    service, conflict, current = scenario.service, scenario.conflict, scenario.current
    merged = current.content + merged_suffix if merged_suffix else None
    result = service.resolve_conflict(
        ResolveConflictCommand(
            review_id=conflict["id"], resolution=resolution, merged_content=merged,
            expected_current_revision_id=conflict["base_revision_id"],
            expected_generated_revision_id=conflict["candidate_revision_id"],
            request_id=f"resolve-{resolution}", actor="alice", note="reviewed",
        )
    )
    page = service.get_page(current.page_path)
    assert (page.current_revision_id == page.generated_revision_id) is current_is_generated
    assert page.accepted_generated_revision_id == conflict["candidate_revision_id"]
    assert result.status == "resolved"


def test_same_semantic_candidate_after_keep_does_not_reopen_conflict(content_conflict_fixture):
    scenario = content_conflict_fixture
    service, conflict, current = scenario.service, scenario.conflict, scenario.current
    service.resolve_conflict(
        ResolveConflictCommand(conflict["id"], "keep_current", None, conflict["base_revision_id"],
                               conflict["candidate_revision_id"], "keep-1", "alice", None)
    )
    service.apply_generated_candidate(scenario.same_semantic_new_job())
    assert service.list_conflicts(current.page_path, status="pending") == []


def test_new_candidate_supersedes_old_and_stale_resolve_cannot_roll_back_generated(content_conflict_fixture):
    scenario = content_conflict_fixture
    service, old_conflict, current = scenario.service, scenario.conflict, scenario.current
    newer = service.apply_generated_candidate(scenario.new_candidate_job())
    with pytest.raises(RevisionConflict):
        service.resolve_conflict(
            ResolveConflictCommand(old_conflict["id"], "accept_candidate", None,
                                   old_conflict["base_revision_id"], old_conflict["candidate_revision_id"],
                                   "stale-resolve", "alice", None)
        )
    page = service.get_page(current.page_path)
    assert page.generated_revision_id == newer.candidate_revision_id


@pytest.mark.parametrize("resolution", ["keep_current", "accept_candidate", "merged_content"])
def test_concurrent_write_resolution_never_changes_generated_pointers(concurrent_conflict_fixture, resolution):
    service, conflict, before = concurrent_conflict_fixture
    service.resolve_conflict(
        ResolveConflictCommand(
            conflict["id"], resolution,
            before.content + "\nmerged\n" if resolution == "merged_content" else None,
            conflict["base_revision_id"], before.generated_revision_id,
            f"concurrent-{resolution}", "alice", None,
        )
    )
    after = service.get_page(before.page_path)
    assert after.generated_revision_id == before.generated_revision_id
    assert after.accepted_generated_revision_id == before.accepted_generated_revision_id
```

- [ ] **Step 2: Run conflict tests to verify RED**

Run:

```powershell
python -m pytest tests/test_wiki_revisions.py -k "conflict_resolution or candidate_supersedes or concurrent_write_resolution" -q
```

Expected: FAIL because review reconciliation and resolution commands are absent.

- [ ] **Step 3: Implement review supersede/rebuild under the page lock**

`_reconcile_pending_reviews_locked()` must compare each pending item's `expected_state_json` against current/generated/lifecycle/path. Mark stale items `superseded` with `resolved_at`, then create at most one pending item per type. A content conflict exists when current differs from generated and the generated semantic hash differs from the accepted-generated semantic hash. Store complete base/candidate revision IDs and current state JSON. A concurrent conflict uses its manual/external candidate and never updates generated fields.

Call this reconciler on every current, generated, lifecycle, or path transition, even when a generated artifact was reused.

- [ ] **Step 4: Implement all six resolution branches with CAS**

Lock review and page; require pending status, expected current/generated equality, and for content conflicts `candidate_revision_id == current generated_revision_id`. `keep_current` closes without a new revision; `accept_candidate` writes the existing candidate through a new intent; `merged_content` creates `origin='merge'` and writes it. Content resolutions set accepted-generated to candidate; concurrent resolutions preserve both generated pointers. Store resolution revision, actor/note audit, and `resolved_at`. Any mismatch raises `RevisionConflict` and leaves all pointers unchanged.

- [ ] **Step 5: Run conflict tests to verify GREEN**

Run:

```powershell
python -m pytest tests/test_wiki_revisions.py -k "conflict" -q
```

Expected: PASS.

- [ ] **Step 6: Commit conflict state machines**

```powershell
git add app/wiki_revisions.py tests/test_wiki_revisions.py
git commit -m "feat: add wiki conflict resolution state machines"
```

---

### Task 11: External Observations, Invalid Fail-Closed State, And Repair

**Files:**
- Modify: `app/wiki_revisions.py`
- Modify: `tests/test_wiki_revisions.py`

- [ ] **Step 1: Add failing valid/invalid/large observation tests**

Append to `tests/test_wiki_revisions.py`:

```python
from app.wiki_markdown import capture_file_observation


def test_valid_external_edit_becomes_current_revision_before_recompile(page_with_generated):
    service, page = page_with_generated
    target = service.settings.vault_path / page.page_path
    target.write_bytes(page.raw_bytes.replace(b"# Demo", b"# Demo\n\nObsidian edit"))
    observed = capture_file_observation(target, max_content_bytes=1_000_000)

    result = service.ingest_external_change("event-external-1", page.page_path, observed)
    replay = service.ingest_external_change("event-external-1", page.page_path, observed)

    assert result.current_revision_id == replay.current_revision_id
    assert result.replayed is False and replay.replayed is True
    changed = service.get_page(page.page_path)
    assert "Obsidian edit" in changed.content
    assert changed.generated_revision_id == page.generated_revision_id


@pytest.mark.parametrize(
    "raw,error_code",
    [(b"\xff\xfe", "invalid_utf8"), (b"---\ntitle: [bad\n---\nBody", "invalid_yaml")],
)
def test_invalid_external_bytes_are_observed_and_withdraw_projections(page_with_generated, raw, error_code):
    service, page = page_with_generated
    target = service.settings.vault_path / page.page_path
    target.write_bytes(raw)
    result = service.ingest_external_change(
        f"invalid-{error_code}", page.page_path,
        capture_file_observation(target, max_content_bytes=1_000_000),
    )
    with connect_app(service.settings) as conn:
        stored = conn.execute("SELECT * FROM wiki_pages WHERE page_id=?", (page.page_id,)).fetchone()
        observation = conn.execute(
            "SELECT * FROM wiki_file_observations WHERE id=?", (result.observation_id,)
        ).fetchone()
        jobs = conn.execute(
            "SELECT target,operation FROM knowledge_projection_jobs WHERE page_id=? AND projection_epoch=?",
            (page.page_id, stored["projection_epoch"]),
        ).fetchall()
    assert stored["lifecycle_status"] == "invalid"
    assert stored["current_revision_id"] == page.current_revision_id
    assert stored["rag_visible_revision_id"] is None
    assert observation["content_bytes"] == raw
    assert observation["error_code"] == error_code
    assert {(job[0], job[1]) for job in jobs} == {("rag", "delete"), ("gbrain", "delete")}


def test_repaired_invalid_file_restores_active_external_revision(page_with_generated):
    service, page = page_with_generated
    target = service.settings.vault_path / page.page_path
    target.write_bytes(b"invalid")
    service.ingest_external_change("invalid-first", page.page_path,
                                   capture_file_observation(target, max_content_bytes=1_000_000))
    target.write_bytes(page.raw_bytes.replace(b"# Demo", b"# Repaired"))
    repaired = service.ingest_external_change("repair", page.page_path,
                                              capture_file_observation(target, max_content_bytes=1_000_000))
    after = service.get_page(page.page_path)
    assert repaired.current_revision_id == after.current_revision_id
    assert after.lifecycle_status == "active"
    assert after.sync_error is None


def test_truncated_large_file_is_observed_without_full_blob(page_with_generated):
    service, page = page_with_generated
    target = service.settings.vault_path / page.page_path
    target.write_bytes(b"---\ntitle: Demo\n---\n" + b"x" * 200_000)
    result = service.ingest_external_change(
        "large-event", page.page_path,
        capture_file_observation(target, max_content_bytes=1_024, prefix_bytes=64),
    )
    with connect_app(service.settings) as conn:
        observation = conn.execute("SELECT * FROM wiki_file_observations WHERE id=?", (result.observation_id,)).fetchone()
    assert observation["content_bytes"] is None
    assert len(observation["content_prefix"]) == 64
    assert observation["content_truncated"] == 1
```

- [ ] **Step 2: Run external observation tests to verify RED**

Run:

```powershell
python -m pytest tests/test_wiki_revisions.py -k "external or invalid or truncated_large" -q
```

Expected: FAIL because observation ingest and fail-closed transitions are absent.

- [ ] **Step 3: Implement immutable observation storage before parsing**

Insert or reuse `wiki_file_observations` by `(page_path,file_hash)` before decoding. Always create/replay a distinct `vault_change_events` occurrence by event ID with captured expected state. Full small bytes go to `content_bytes`; truncated input stores prefix/stat only and gets `file_too_large` without loading the file again.

For valid bytes, create an `external` revision with the event/state key, render managed fields, and install through an intent. For invalid bytes, preserve the Vault file unchanged, keep current on the last valid revision for history only, set lifecycle `invalid`, observed hash/sync error, increment epoch, clear RAG watermarks, enqueue both delete projections, and create/supersede an `invalid_frontmatter` review item.

Repair closes invalid review, creates/installs a valid external revision, restores active, clears sync error, and emits upsert jobs at a newer epoch.

- [ ] **Step 4: Run observation tests to verify GREEN**

Run:

```powershell
python -m pytest tests/test_wiki_revisions.py -k "external or invalid or truncated_large" -q
```

Expected: PASS.

- [ ] **Step 5: Commit external ingestion**

```powershell
git add app/wiki_revisions.py tests/test_wiki_revisions.py
git commit -m "feat: ingest external wiki observations safely"
```

---

### Task 12: Rename, Delete, Restore, Review Invalidation, And Projection Reconcile

**Files:**
- Modify: `app/wiki_revisions.py`
- Modify: `tests/test_wiki_revisions.py`

- [ ] **Step 1: Add failing lifecycle/path/epoch tests**

Append to `tests/test_wiki_revisions.py`:

```python
def test_rename_is_audit_only_revision_and_preserves_content_pointer(page_with_generated):
    service, before = page_with_generated
    old_absolute = service.settings.vault_path / before.page_path
    new_path = "wiki/product/faq/demo-renamed.md"
    new_absolute = service.settings.vault_path / new_path
    old_absolute.rename(new_absolute)

    result = service.rename_page("rename-1", before.page_path, new_path)
    replay = service.rename_page("rename-1", before.page_path, new_path)
    after = service.get_page(new_path)

    assert result.audit_revision_id == replay.audit_revision_id
    assert replay.replayed is True
    assert after.current_revision_id == before.current_revision_id
    assert after.generated_revision_id == before.generated_revision_id
    assert after.current_revision_id == after.generated_revision_id
    assert after.projection_epoch == before.projection_epoch + 1
    with connect_app(service.settings) as conn:
        audit_revision = conn.execute(
            "SELECT origin,page_path,base_revision_id FROM wiki_page_revisions WHERE id=?",
            (result.audit_revision_id,),
        ).fetchone()
        jobs = conn.execute(
            "SELECT target,operation,revision_id,payload_json FROM knowledge_projection_jobs WHERE page_id=? AND projection_epoch=?",
            (before.page_id, after.projection_epoch),
        ).fetchall()
    assert tuple(audit_revision) == ("rename", new_path, before.current_revision_id)
    assert {(job[0], job[1], job[2]) for job in jobs} == {
        ("rag", "rename", before.current_revision_id),
        ("gbrain", "rename", before.current_revision_id),
    }


def test_delete_and_restore_same_revision_each_create_new_projection_epoch(page_with_generated):
    service, before = page_with_generated
    target = service.settings.vault_path / before.page_path
    original = target.read_bytes()
    target.unlink()

    deleted = service.delete_page("delete-1", before.page_path)
    target.write_bytes(original)
    restored = service.ingest_external_change(
        "restore-1", before.page_path,
        capture_file_observation(target, max_content_bytes=1_000_000),
    )
    after = service.get_page(before.page_path)

    assert deleted.status == "deleted"
    assert restored.status == "applied"
    assert after.current_revision_id == before.current_revision_id
    assert after.projection_epoch == before.projection_epoch + 2
    with connect_app(service.settings) as conn:
        epochs = conn.execute(
            "SELECT DISTINCT projection_epoch,operation FROM knowledge_projection_jobs WHERE page_id=? ORDER BY projection_epoch",
            (before.page_id,),
        ).fetchall()
    assert (before.projection_epoch + 1, "delete") in [tuple(row) for row in epochs]
    assert (before.projection_epoch + 2, "upsert") in [tuple(row) for row in epochs]


def test_path_or_lifecycle_change_supersedes_stale_reviews(page_with_two_conflict_types):
    service, page = page_with_two_conflict_types
    (service.settings.vault_path / page.page_path).unlink()
    service.delete_page("delete-conflicted", page.page_path)
    with connect_app(service.settings) as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM review_items WHERE page_id=? AND status='pending'", (page.page_id,)
        ).fetchone()[0]
        superseded = conn.execute(
            "SELECT COUNT(*) FROM review_items WHERE page_id=? AND status='superseded'", (page.page_id,)
        ).fetchone()[0]
    assert pending == 0
    assert superseded == 2


def test_ensure_projection_jobs_recreates_only_missing_desired_epoch(page_with_generated):
    service, page = page_with_generated
    with connect_app(service.settings) as conn:
        conn.execute("DELETE FROM knowledge_projection_jobs WHERE page_id=?", (page.page_id,))
    first = service.ensure_projection_jobs(page.page_id)
    replay = service.ensure_projection_jobs(page.page_id)
    assert first == replay
    assert len(first) == 2
```

- [ ] **Step 2: Run lifecycle tests to verify RED**

Run:

```powershell
python -m pytest tests/test_wiki_revisions.py -k "rename or delete_and_restore or lifecycle_change or ensure_projection" -q
```

Expected: FAIL because path/lifecycle transitions and projection reconciliation are absent.

- [ ] **Step 3: Implement rename as audit-only state transition**

Lock by stable page ID derived from old/new managed frontmatter or old path. Verify new path uniqueness, increment revision number, insert an immutable `origin='rename'` revision with new path/current content/base current and event-derived idempotency key, but do not move current/generated/accepted pointers. Update path, increment epoch, clear RAG watermarks, supersede stale reviews, enqueue `rename` jobs carrying `old_path`/`path`, and mark the event applied in the same transaction.

- [ ] **Step 4: Implement delete, restore, and desired projection reconciliation**

Delete sets lifecycle `deleted`, timestamp, observed hash null, increments epoch, clears watermarks, supersedes reviews, and enqueues paired deletes while preserving revisions/pointers. External ingest of a deleted page restores `active`; if bytes equal current revision it reuses current but still increments epoch/enqueues upserts, otherwise it creates an external successor through an intent.

`ensure_projection_jobs(page_id=None)` scans one/all pages, locks each, derives `upsert`, `delete`, or no-op from lifecycle/current, and uses current epoch keys. It never directly updates RAG/GBrain mappings.

- [ ] **Step 5: Run lifecycle tests to verify GREEN**

Run:

```powershell
python -m pytest tests/test_wiki_revisions.py -k "rename or delete_and_restore or lifecycle_change or ensure_projection" -q
```

Expected: PASS.

- [ ] **Step 6: Commit lifecycle transitions**

```powershell
git add app/wiki_revisions.py tests/test_wiki_revisions.py
git commit -m "feat: version wiki rename and lifecycle transitions"
```

---

### Task 13: Full Recovery Matrix, Late Backup Writer, And Audited Release

**Files:**
- Modify: `app/vault_writer.py`
- Modify: `app/wiki_revisions.py`
- Modify: `tests/test_vault_writer.py`

- [ ] **Step 1: Add failing recovery-matrix and late-writer tests**

Append to `tests/test_vault_writer.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class RecoveryScenario:
    settings: object
    intent_id: str
    page_id: str
    after_lease: datetime
    backup_path: Path
    unknown_payloads: tuple[bytes, ...]

    def observation_exists(self, raw: bytes) -> bool:
        with connect_app(self.settings) as conn:
            row = conn.execute(
                "SELECT 1 FROM wiki_file_observations WHERE page_id=? AND content_bytes=?",
                (self.page_id, raw),
            ).fetchone()
        return row is not None


class RecoveryIntentFactory:
    def __init__(self, settings, intent_id: str):
        self.settings = settings
        self.intent_id = intent_id

    def materialize(self, target_kind: str, backup_kind: str) -> RecoveryScenario:
        with connect_app(self.settings) as conn:
            intent = conn.execute(
                "SELECT * FROM vault_write_intents WHERE id=?", (self.intent_id,)
            ).fetchone()
            page = conn.execute(
                "SELECT * FROM wiki_pages WHERE page_id=?", (intent["page_id"],)
            ).fetchone()
            current = conn.execute(
                "SELECT content FROM wiki_page_revisions WHERE id=?",
                (page["current_revision_id"],),
            ).fetchone()[0].encode("utf-8")
            intended = conn.execute(
                "SELECT content FROM wiki_page_revisions WHERE id=?",
                (intent["revision_id"],),
            ).fetchone()[0].encode("utf-8")
        writer = AtomicVaultWriter(self.settings.vault_path)
        target = self.settings.vault_path / intent["target_path"]
        backup = writer.backup_path(self.intent_id)
        target.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
        payloads = {
            "expected": current,
            "intended": intended,
            "unknown": b"unknown-target" if target_kind == "unknown" else b"unknown-backup",
            "missing": None,
        }
        target_bytes = payloads[target_kind]
        backup_bytes = (
            b"unknown-backup" if backup_kind == "unknown" else payloads[backup_kind]
        )
        if target_bytes is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(target_bytes)
        if backup_bytes is not None:
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(backup_bytes)
        with connect_app(self.settings) as conn:
            conn.execute(
                """
                UPDATE vault_write_intents
                SET status='recovery_required',executor_owner=NULL,lease_expires_at='t0'
                WHERE id=?
                """,
                (self.intent_id,),
            )
        unknown = tuple(
            raw for raw in (target_bytes, backup_bytes)
            if raw is not None and raw.startswith(b"unknown-")
        )
        return RecoveryScenario(
            self.settings,self.intent_id,intent["page_id"],
            datetime.now(timezone.utc) + timedelta(seconds=1),backup,unknown,
        )


@pytest.fixture
def recovery_intent_fixture(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    return RecoveryIntentFactory(settings, intent_id)


@dataclass(frozen=True)
class AppliedIntentScenario:
    settings: object
    intent_id: str
    page_id: str
    backup_path: Path
    backup_hash: str


@pytest.fixture
def applied_intent_fixture(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    result = IntentExecutor(settings, owner="fixture-apply").execute(intent_id)
    assert result is not None and result.intent_status == "applied"
    writer = AtomicVaultWriter(settings.vault_path)
    backup_path = writer.backup_path(intent_id)
    with connect_app(settings) as conn:
        page_id = conn.execute(
            "SELECT page_id FROM vault_write_intents WHERE id=?", (intent_id,)
        ).fetchone()[0]
    return AppliedIntentScenario(
        settings,intent_id,page_id,backup_path,compute_file_hash(backup_path.read_bytes())
    )


@pytest.mark.parametrize(
    "target_kind,backup_kind,expected_status,expected_current",
    [
        ("expected", "missing", "applied", "intended"),
        ("missing", "expected", "applied", "intended"),
        ("intended", "expected", "applied", "intended"),
        ("unknown", "expected", "superseded", "external_target"),
        ("intended", "unknown", "superseded", "external_backup"),
        ("unknown", "unknown", "recovery_required", "unchanged"),
        ("missing", "unknown", "superseded", "external_backup"),
        ("missing", "missing", "failed", "unchanged"),
    ],
)
def test_recovery_matrix_preserves_every_unknown_byte(
    recovery_intent_fixture, target_kind, backup_kind, expected_status, expected_current
):
    scenario = recovery_intent_fixture.materialize(target_kind, backup_kind)
    result = IntentExecutor(scenario.settings, owner="recovery").reconcile_one(
        scenario.intent_id, now=scenario.after_lease
    )
    assert result.intent_status == expected_status
    assert result.current_kind == expected_current
    for raw in scenario.unknown_payloads:
        assert scenario.observation_exists(raw)
    assert scenario.backup_path.exists() if backup_kind != "missing" else True


def test_retained_backup_late_write_becomes_observation_and_concurrent_conflict(applied_intent_fixture):
    scenario = applied_intent_fixture
    scenario.backup_path.write_bytes(b"late obsidian write")

    changed = IntentExecutor(scenario.settings, owner="monitor").reconcile_retained_backups()

    assert changed == [scenario.intent_id]
    with connect_app(scenario.settings) as conn:
        intent = conn.execute(
            "SELECT backup_retention_status,backup_last_observed_hash FROM vault_write_intents WHERE id=?",
            (scenario.intent_id,),
        ).fetchone()
        conflict = conn.execute(
            "SELECT issue_type,status FROM review_items WHERE page_id=? ORDER BY created_at DESC LIMIT 1",
            (scenario.page_id,),
        ).fetchone()
        observation = conn.execute(
            "SELECT content_bytes FROM wiki_file_observations WHERE page_id=? ORDER BY observed_at DESC LIMIT 1",
            (scenario.page_id,),
        ).fetchone()
    assert intent[0] == "change_detected"
    assert tuple(conflict) == ("concurrent_write_conflict", "pending")
    assert observation[0] == b"late obsidian write"


def test_retained_backup_release_requires_expected_hash_and_records_audit(applied_intent_fixture):
    scenario = applied_intent_fixture
    service = WikiRevisionService(scenario.settings)
    with pytest.raises(RevisionConflict):
        service.release_retained_backup(scenario.intent_id, "wrong", actor="admin")
    result = service.release_retained_backup(
        scenario.intent_id, scenario.backup_hash, actor="admin"
    )
    assert result.status == "released"
    assert not scenario.backup_path.exists()
    with connect_app(scenario.settings) as conn:
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE event_type='vault_backup_released'"
        ).fetchone()[0]
    assert audit_count == 1
```

- [ ] **Step 2: Run recovery tests to verify RED**

Run:

```powershell
python -m pytest tests/test_vault_writer.py -k "recovery_matrix or late_write or backup_release" -q
```

Expected: FAIL because unknown-byte handoff, backup monitoring, and release are absent.

- [ ] **Step 3: Implement atomic human-wins handoff**

For unknown target/backup bytes, save observations before any pointer change. Under one page lock transaction, create the external revision and successor intent, mark old intent `superseded`, and CAS `pending_write_intent_id` directly from old ID to successor ID. If any successor preparation fails, leave old intent `recovery_required` and still referenced. There must never be an intermediate null pending pointer.

When both target and backup are unknown, save both observations, keep path-visible target as primary candidate, retain backup as secondary conflict, keep projections fail-closed, and require human resolution. Missing/missing marks failed, clears only by matching intent CAS, retains current historical revision, and enqueues projection deletes.

- [ ] **Step 4: Implement retained-backup monitoring and explicit release**

`reconcile_retained_backups()` scans `applied` intents with retained/change-detected backups, stream-hashes existing files, and for every hash change stores raw observation/external candidate and reconciles `concurrent_write_conflict`. It never auto-unlinks a backup based on elapsed stability.

`release_retained_backup()` locks page+intent, requires `applied`, retained/change-detected state, exact expected backup hash, and admin actor supplied by API; records current/backup hash, observation/intent IDs in audit, unlinks only after DB preconditions, fsyncs the hidden directory, then marks `released`.

- [ ] **Step 5: Run all writer tests to verify GREEN**

Run:

```powershell
python -m pytest tests/test_vault_writer.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit complete recovery behavior**

```powershell
git add app/vault_writer.py app/wiki_revisions.py tests/test_vault_writer.py
git commit -m "feat: preserve concurrent vault writes during recovery"
```

---

### Task 14: Revision, Conflict, Save, Status, And Backup APIs

**Files:**
- Modify: `app/models.py:44-55,187-209`
- Modify: `app/catalog.py:208-382`
- Modify: `app/api.py:13-61,219-264,291-312,369-375`
- Create: `tests/test_wiki_revision_api.py`

- [ ] **Step 1: Add failing API contract tests**

Create `tests/test_wiki_revision_api.py`:

```python
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import connect_app, init_app_db
from app.main import app
from app.wiki_revisions import (
    CompileCandidateCommand,
    ManualSaveCommand,
    WikiRevisionService,
)


@dataclass(frozen=True)
class ApiWikiPage:
    settings: object
    encoded_path: str
    absolute_path: Path
    content: str
    current_revision_id: str


@pytest.fixture
def api_wiki_page(tmp_path, monkeypatch):
    settings = get_settings().model_copy()
    settings.database_backend = "sqlite"
    settings.database_path = tmp_path / "api.db"
    settings.vault_path = tmp_path / "vault"
    page_path = "wiki/product/faq/api.md"
    absolute = settings.vault_path / page_path
    absolute.parent.mkdir(parents=True)
    absolute.write_text(
        "---\ntitle: API\nsource_ids: [src_api]\nreview_status: draft\n---\n# API\n",
        encoding="utf-8",
    )
    init_app_db(settings)
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,domain,page_type,title,source_ids_json,review_status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (page_path,"product","faq","API",'["src_api"]',"draft","t0","t0"),
        )
    monkeypatch.setattr("app.api.get_settings", lambda: settings)
    monkeypatch.setattr("app.main.settings", settings)
    current = WikiRevisionService(settings).get_page(page_path)
    client = TestClient(app)
    return client, ApiWikiPage(
        settings,quote(page_path, safe="/"),absolute,current.content,current.current_revision_id
    )


@dataclass(frozen=True)
class ApiConflict:
    review_id: str
    encoded_path: str
    current_revision_id: str


@pytest.fixture
def api_content_conflict(api_wiki_page):
    client, api_page = api_wiki_page
    service = WikiRevisionService(api_page.settings)
    page_path = "wiki/product/faq/api.md"
    legacy = service.get_page(page_path)
    metadata = legacy.metadata
    service.apply_generated_candidate(
        CompileCandidateCommand(
            page_path=page_path,content=legacy.content,domain=metadata["domain"],
            page_type=metadata["page_type"],title=metadata["title"],
            source_ids=metadata["source_ids"],owner=metadata.get("owner"),
            source_hash="api-generated-1",compiler_version="wiki-revision-v1",
            compile_job_id="api-compile-1",
        )
    )
    generated = service.get_page(page_path)
    service.prepare_manual_save(
        ManualSaveCommand(
            page_path=page_path,content=generated.content + "\nAPI human edit.\n",
            expected_revision_id=generated.current_revision_id,request_id="api-human",
            actor="test",owner=None,note=None,review_status="reviewed",
        )
    )
    current = service.get_page(page_path)
    service.apply_generated_candidate(
        CompileCandidateCommand(
            page_path=page_path,content=legacy.content + "\nGenerated v2.\n",
            domain=metadata["domain"],page_type=metadata["page_type"],title=metadata["title"],
            source_ids=metadata["source_ids"],owner=metadata.get("owner"),
            source_hash="api-generated-2",compiler_version="wiki-revision-v1",
            compile_job_id="api-compile-2",
        )
    )
    conflict = service.list_conflicts(page_path, status="pending")[0]
    return client, ApiConflict(conflict["id"],quote(page_path, safe="/"),current.current_revision_id)


def test_put_requires_expected_revision_and_returns_428(api_wiki_page):
    client, page = api_wiki_page
    response = client.put(
        f"/api/internal/wiki/pages/{page.encoded_path}",
        json={"content": page.content, "request_id": "missing-precondition"},
    )
    assert response.status_code == 428
    assert response.json()["detail"]["code"] == "expected_revision_required"


def test_stale_put_returns_409_with_latest_revision_without_changing_file(api_wiki_page):
    client, page = api_wiki_page
    before = page.absolute_path.read_bytes()
    response = client.put(
        f"/api/internal/wiki/pages/{page.encoded_path}",
        json={
            "content": page.content + "\nstale",
            "expected_revision_id": "wrev_stale",
            "request_id": "stale-save",
            "review_status": "draft",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["current_revision_id"] == page.current_revision_id
    assert page.absolute_path.read_bytes() == before


def test_revision_list_is_paginated_metadata_and_detail_contains_full_content(api_wiki_page):
    client, page = api_wiki_page
    listing = client.get(
        f"/api/internal/wiki/pages/{page.encoded_path}/revisions?limit=1&offset=0"
    )
    assert listing.status_code == 200
    assert listing.json()["items"]
    assert "content" not in listing.json()["items"][0]
    revision_id = listing.json()["items"][0]["id"]
    detail = client.get(f"/api/internal/wiki/revisions/{revision_id}")
    assert detail.status_code == 200
    assert detail.json()["content"]


def test_conflict_api_exposes_type_and_rejects_stale_expected_generated(api_content_conflict):
    client, conflict = api_content_conflict
    listing = client.get(
        f"/api/internal/wiki/pages/{conflict.encoded_path}/conflicts"
    )
    assert listing.status_code == 200
    assert listing.json()[0]["issue_type"] == "content_conflict"
    response = client.post(
        f"/api/internal/wiki/conflicts/{conflict.review_id}/resolve",
        json={
            "resolution": "accept_candidate",
            "expected_current_revision_id": conflict.current_revision_id,
            "expected_generated_revision_id": "wrev_stale",
            "request_id": "resolve-stale",
        },
    )
    assert response.status_code == 409


def test_status_api_changes_frontmatter_revision_not_only_metadata(api_wiki_page):
    client, page = api_wiki_page
    response = client.patch(
        f"/api/internal/wiki/pages/{page.encoded_path}/status",
        json={
            "review_status": "stale",
            "expected_revision_id": page.current_revision_id,
            "request_id": "status-api-1",
            "note": "expired",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["current_revision_id"] != page.current_revision_id
    content = client.get(f"/api/internal/wiki/pages/{page.encoded_path}").json()["content"]
    assert "review_status: stale" in content
```

- [ ] **Step 2: Run API tests to verify RED**

Run:

```powershell
python -m pytest tests/test_wiki_revision_api.py -q
```

Expected: FAIL because request/response models, routes, and error mapping are absent.

- [ ] **Step 3: Add concrete request/response models**

Use optional expected IDs so the endpoint can produce 428 instead of Pydantic's 422:

```python
class WikiPageSaveRequest(BaseModel):
    content: str
    expected_revision_id: str | None = None
    request_id: str
    review_status: str = Field(default="draft", pattern="^(draft|reviewed|stale|rejected)$")
    owner: str | None = None
    note: str | None = None


class WikiStatusUpdateRequest(BaseModel):
    review_status: str = Field(pattern="^(draft|reviewed|stale|rejected)$")
    expected_revision_id: str | None = None
    request_id: str
    owner: str | None = None
    note: str | None = None


class ConflictResolveRequest(BaseModel):
    resolution: Literal["keep_current", "accept_candidate", "merged_content"]
    merged_content: str | None = None
    expected_current_revision_id: str
    expected_generated_revision_id: str | None = None
    request_id: str
    note: str | None = None


class WikiPageContentResponse(BaseModel):
    path: str
    page_id: str
    content: str
    current_revision_id: str
    generated_revision_id: str | None = None
    accepted_generated_revision_id: str | None = None
    lifecycle_status: str
    projection_epoch: int
    write_in_progress: bool = False
    write_intent_id: str | None = None
    metadata: dict
```

Add metadata/detail/list/conflict response models so OpenAPI does not expose database JSON strings.

- [ ] **Step 4: Replace catalog mutation internals with service calls**

`read_wiki_page()` calls `service.get_page()`; in captured/installed windows it returns the database current revision content and `write_in_progress`/intent ID rather than 404. `save_wiki_page()` and `update_wiki_page_status()` build the Task 8 commands with authenticated actor passed from API. Add wrappers for revision list/detail, conflict list/resolve, and backup release. `update_review_item()` must refuse conflict issue types and direct callers to the resolve endpoint; it may retain legacy changed-page review behavior.

- [ ] **Step 5: Add routes in non-greedy order and map domain errors**

Register `/wiki/revisions/{revision_id}` and `/wiki/conflicts/{review_id}/resolve` before the greedy page route; register page suffix routes (`/revisions`, `/conflicts`, `/status`) before `GET/PUT /wiki/pages/{page_path:path}`. Add an error mapper used by every Wiki endpoint:

```python
def raise_wiki_http(exc: Exception) -> NoReturn:
    if isinstance(exc, PreconditionRequired):
        raise HTTPException(428, detail={"code": "expected_revision_required"}) from exc
    if isinstance(exc, RevisionConflict):
        raise HTTPException(
            409,
            detail={
                "code": "revision_conflict",
                "message": str(exc),
                "current_revision_id": exc.current_revision_id,
                "pending_intent_id": exc.pending_intent_id,
            },
        ) from exc
    if isinstance(exc, PageNotFound):
        raise HTTPException(404, detail={"code": "wiki_page_not_found"}) from exc
    if isinstance(exc, InvalidWikiDocument):
        raise HTTPException(409, detail={"code": exc.error_code, "observation_id": exc.observation_id}) from exc
    raise HTTPException(400, detail={"code": "wiki_mutation_failed", "message": str(exc)}) from exc
```

Release backup is an admin-only endpoint and requires expected backup hash in its request. All actor IDs come from `current_user`, never from client payload.

- [ ] **Step 6: Run API tests to verify GREEN**

Run:

```powershell
python -m pytest tests/test_wiki_revision_api.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit the API contract**

```powershell
git add app/models.py app/catalog.py app/api.py tests/test_wiki_revision_api.py
git commit -m "feat: expose wiki revisions and conflict api"
```

---

### Task 15: Web Client Optimistic Concurrency

**Files:**
- Modify: `frontend/src/api/client.ts:16-29`
- Modify: `frontend/src/types.ts:34-50,161-166`
- Modify: `frontend/src/App.tsx:71-76,286-329`
- Modify: `tests/test_frontend_payload.py`

- [ ] **Step 1: Add failing source-contract tests**

Append to `tests/test_frontend_payload.py`:

```python
def test_wiki_mutations_send_expected_revision_and_request_id():
    app_source = Path(__file__).resolve().parents[1] / "frontend" / "src" / "App.tsx"
    source = app_source.read_text(encoding="utf-8")
    save = source.split("async function savePage() {", 1)[1].split("\n  async function markPageStale", 1)[0]
    status = source.split("async function markPageStale", 1)[1].split("\n  async function updateReview", 1)[0]
    load = source.split("async function loadPage", 1)[1].split("\n  async function savePage", 1)[0]
    assert "current_revision_id: page.current_revision_id" in load
    assert "expected_revision_id: editor.current_revision_id" in save
    assert "request_id: crypto.randomUUID()" in save
    assert "expected_revision_id" in status
    assert "request_id: crypto.randomUUID()" in status
```

- [ ] **Step 2: Run frontend payload test to verify RED**

Run:

```powershell
python -m pytest tests/test_frontend_payload.py -q
```

Expected: FAIL because the editor does not retain/send a revision precondition.

- [ ] **Step 3: Add typed API errors and revision-bearing client state**

In `frontend/src/api/client.ts`, define `ApiError` with `status` and parsed `detail`; throw it for non-2xx responses. Extend `WikiPage`, `ReviewItem`, and `EditorState` with `page_id`, `current_revision_id`, generated/accepted IDs, lifecycle, issue type, candidate/base IDs, and pending intent fields returned by the API.

- [ ] **Step 4: Send exact preconditions and recover from 409**

`loadPage()` must store `current_revision_id`. `savePage()` and `markPageStale()` send `expected_revision_id` and a fresh `crypto.randomUUID()` request ID. On `ApiError.status === 409`, reload the page, keep the user's unsaved text in a local variable, and show a conflict message; do not automatically retry against the new revision. On success, use the mutation response's new revision immediately before the general refresh.

The concrete save payload is:

```typescript
body: JSON.stringify({
  content: editor.content,
  expected_revision_id: editor.current_revision_id,
  request_id: crypto.randomUUID(),
  review_status: editor.review_status,
  owner: editor.owner || null,
  note: "管理端保存",
})
```

- [ ] **Step 5: Verify payload and TypeScript build**

Run:

```powershell
python -m pytest tests/test_frontend_payload.py -q
npm --prefix frontend run build
```

Expected: pytest PASS and Vite build exits 0 without TypeScript errors.

- [ ] **Step 6: Commit Web preconditions**

```powershell
git add frontend/src/api/client.ts frontend/src/types.ts frontend/src/App.tsx tests/test_frontend_payload.py
git commit -m "feat: add wiki optimistic concurrency to console"
```

---

### Task 16: Cross-Backend Concurrency, Crash Points, Audit, And Full Regression

**Files:**
- Modify: `tests/test_wiki_revision_postgres.py`
- Modify: `tests/test_vault_writer.py`
- Modify: `tests/test_internal_flow.py:102-153`
- Modify: `tests/test_wiki_revisions.py`

- [ ] **Step 1: Add PostgreSQL concurrent-writer test**

Append to `tests/test_wiki_revision_postgres.py`:

```python
from concurrent.futures import ThreadPoolExecutor

from app.db import connect_app
from app.wiki_revisions import ManualSaveCommand, MutationResult, RevisionConflict, WikiRevisionService


@pytest.fixture
def pg_wiki_page(tmp_path):
    settings = get_settings().model_copy()
    settings.database_backend = "postgres"
    settings.postgres_database = f"lgdo_revision_writer_{uuid.uuid4().hex[:10]}"
    settings.vault_path = tmp_path / "vault"
    page_path = "wiki/product/faq/pg-writer.md"
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True)
    target.write_text(
        "---\ntitle: PG Writer\nsource_ids: [src_pg]\nreview_status: draft\n---\n# PG Writer\n",
        encoding="utf-8",
    )
    init_postgres_schema(settings)
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,domain,page_type,title,source_ids_json,review_status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (page_path,"product","faq","PG Writer",'["src_pg"]',"draft","t0","t0"),
        )
    service = WikiRevisionService(settings)
    return service, service.get_page(page_path)


@pytest.mark.skipif(not pg_available(), reason="PostgreSQL 5432 is not available")
def test_postgres_row_lock_allows_only_one_writer_to_prepare(pg_wiki_page):
    service, page = pg_wiki_page

    def prepare(request_id: str):
        try:
            return service.prepare_manual_save(
                ManualSaveCommand(
                    page_path=page.page_path, content=page.content + request_id,
                    expected_revision_id=page.current_revision_id, request_id=request_id,
                    actor=request_id, owner=None, note=None, review_status="draft",
                ),
                execute_intent=False,
            )
        except RevisionConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(prepare, ["pg-a", "pg-b"]))
    assert sum(isinstance(item, RevisionConflict) for item in outcomes) == 1
    assert sum(isinstance(item, MutationResult) for item in outcomes) == 1
```

- [ ] **Step 2: Add crash-point and cross-process executor tests**

Append to `tests/test_vault_writer.py`:

```python
import multiprocessing

import portalocker

from app.config import Settings


def run_executor_until_release(settings_data, intent_id: str, owner: str, events, release_event) -> None:
    settings = Settings(**settings_data)
    executor = IntentExecutor(settings, owner=owner)
    if not executor.claim(intent_id, lease_seconds=30):
        events.put((owner, "claim_failed"))
        return
    lock_path = AtomicVaultWriter(settings.vault_path).lock_path(intent_id)
    with portalocker.Lock(str(lock_path), mode="a+", timeout=0):
        events.put((owner, "locked"))
        release_event.wait(10)


def run_executor_once(settings_data, intent_id: str, owner: str, events) -> None:
    settings = Settings(**settings_data)
    result = IntentExecutor(settings, owner=owner).execute(intent_id)
    events.put((owner, "observed_only" if result is None else result.intent_status))


@pytest.mark.parametrize("stop_after", ["intent_created", "captured", "installed"])
def test_reconcile_recovers_each_crash_point(wiki_intent_fixture, stop_after):
    settings, intent_id = wiki_intent_fixture
    crashed = IntentExecutor(settings, owner=f"crash-{stop_after}")
    crashed.execute(intent_id, stop_after=stop_after, lease_seconds=1)
    recovered = IntentExecutor(settings, owner=f"recover-{stop_after}")
    recovered.reconcile_all(now=datetime.now(timezone.utc) + timedelta(seconds=2))
    with connect_app(settings) as conn:
        page = conn.execute("SELECT pending_write_intent_id,current_revision_id FROM wiki_pages").fetchone()
        intent = conn.execute("SELECT status FROM vault_write_intents WHERE id=?", (intent_id,)).fetchone()
    assert page[0] is None
    assert page[1] is not None
    assert intent[0] == "applied"


def test_two_processes_cannot_hold_same_intent_os_lock(wiki_intent_fixture):
    settings, intent_id = wiki_intent_fixture
    ctx = multiprocessing.get_context("spawn")
    events = ctx.Queue()
    release_event = ctx.Event()
    first = ctx.Process(
        target=run_executor_until_release,
        args=(settings.model_dump(), intent_id, "one", events, release_event),
    )
    second = ctx.Process(
        target=run_executor_once,
        args=(settings.model_dump(), intent_id, "two", events),
    )
    first.start()
    assert events.get(timeout=10) == ("one", "locked")
    second.start()
    assert events.get(timeout=10) == ("two", "observed_only")
    release_event.set()
    first.join(10)
    second.join(10)
    assert first.exitcode == second.exitcode == 0
```

Keep both process helpers at module top level exactly as shown so Windows `spawn` can import them.

- [ ] **Step 3: Update the existing end-to-end save/status payloads**

In `tests/test_internal_flow.py`, read `current_revision_id` from the page GET response. Add it and deterministic request IDs to save/status payloads. After save, update the expected revision from its response before status. Replace the old expectation that recompile overwrites with assertions that manual text remains and one `content_conflict` is pending. Keep scan, ask, feedback, gap, eval, and delete assertions unchanged.

- [ ] **Step 4: Add audit and traceability assertion**

Append a test that performs generated, manual, status, external, rename, delete/restore, and conflict resolution transitions, then asserts each returned revision is reachable by `page_id`, current/generated/conflict pointers refer to real revisions, and `audit_logs` contains the corresponding command/event ID. Also assert revision `content/file_hash/origin/base_revision_id` remain byte-for-byte unchanged after all later operations.

- [ ] **Step 5: Run focused concurrency/recovery tests to verify RED then GREEN**

Before the last implementation corrections:

```powershell
python -m pytest tests/test_wiki_revision_postgres.py::test_postgres_row_lock_allows_only_one_writer_to_prepare tests/test_vault_writer.py -k "crash_point or two_processes" -q
```

Expected RED: at least one concurrency/crash assertion fails. Fix only lock/lease/CAS behavior exposed by the failure, then rerun the same command.

Expected GREEN: all selected tests PASS with PostgreSQL running.

- [ ] **Step 6: Run complete backend and frontend verification**

Run:

```powershell
$env:GBRAIN_IMPORT_ON_COMPILE = 'false'
python -m pytest -q
npm --prefix frontend run build
```

Expected: all Python tests PASS (PostgreSQL tests execute against the running service), no compile call waits for GBrain, and the frontend build exits 0.

- [ ] **Step 7: Inspect durable invariants directly**

Run against a test SQLite database created by the suite:

```powershell
python -m pytest tests/test_wiki_schema_migration.py tests/test_wiki_revisions.py tests/test_vault_writer.py tests/test_wiki_revision_api.py -q
```

Expected: PASS, with explicit coverage for immutable revisions, file/current alignment outside pending intents, one active conflict per type, retained backups, observation bytes, and epoch-keyed outbox jobs.

- [ ] **Step 8: Commit regression updates**

```powershell
git add tests/test_wiki_revision_postgres.py tests/test_vault_writer.py tests/test_internal_flow.py tests/test_wiki_revisions.py
git commit -m "test: verify wiki revision concurrency and recovery"
```

---

## Completion Checklist

- [ ] `vault/wiki` bytes are never overwritten unless a matching intent captured the expected file and no-replace installation succeeded.
- [ ] Every valid content transition has one immutable revision; invalid/truncated bytes have one immutable observation and no fake current revision.
- [ ] Legacy pages receive current-only snapshots; generated remains null until explicit compilation.
- [ ] Manual/status/external changes do not advance generated or accepted-generated pointers.
- [ ] Generated and concurrent conflicts use distinct pending indexes and resolution semantics.
- [ ] Current/path/lifecycle changes increment projection epoch, clear RAG visibility, and create paired durable jobs without waiting for RAG/GBrain.
- [ ] Lost DB leases or OS locks cannot finish intents/jobs or advance pointers/watermarks.
- [ ] Retained backups have no time-based deletion; late writes become observations/conflicts; release is explicit and audited.
- [ ] SQLite fresh/legacy/half-migrated, PostgreSQL, and SQLite-to-PostgreSQL paths pass.
- [ ] API returns 428 for missing expected revision and 409 with current revision for stale/pending writes.
- [ ] Existing console flow sends preconditions and preserves unsaved text on conflict.
- [ ] Full pytest suite and frontend build pass with synchronous GBrain import disabled.
