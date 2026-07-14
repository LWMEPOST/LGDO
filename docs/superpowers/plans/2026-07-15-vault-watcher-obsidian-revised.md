# Revised Vault Watcher And Obsidian Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a persistent, restart-safe Obsidian workflow in which external Markdown changes enter LGDO revision, audit, local RAG, and GBrain projection contracts without watcher-owned revision events, ambiguous identity moves, write loops, stale citations, or unauthorized deep links.

**Architecture:** `WikiRevisionService` is the sole writer and replay authority for `vault_change_events`; the watcher records raw filesystem work only in `vault_watch_occurrences` and delegates every domain mutation to revision APIs. Persistent delete grace, sync issues, and reconcile jobs make restart behavior deterministic, while one canonical path classifier is shared by live watching and startup inventory. Obsidian assets and UI are adapters over those backend contracts, not alternate synchronization engines.

**Tech Stack:** Python 3.10+, FastAPI lifespan, SQLite/PostgreSQL, `watchfiles`, `ruamel.yaml`, pytest/pytest-asyncio, React 18, TypeScript, Vitest, Testing Library, PowerShell, GBrain MCP/PGLite, DeepSeek fallback integration.

---

## Locked Architecture Invariants

1. `WikiRevisionService` exclusively creates, advances, and replays `vault_change_events`.
2. Watcher code records filesystem occurrence state in `vault_watch_occurrences`; it never inserts or updates `vault_change_events`, revisions, write intents, projection jobs, or Wiki chunks directly.
3. A terminal `vault_change_events.result_payload_json` is the replay authority. `payload_digest` binds an event ID to its immutable operation payload.
4. Unknown invalid files may create a bounded observation, sync issue, and terminal revision event, but never a placeholder `wiki_pages` row, revision, write intent, review item requiring a page, or projection job.
5. Exact-byte rename is audit-only. Rename plus an edit is one atomic `relocate_external_change()` mutation that creates an external revision.
6. Live and startup path discovery use the same hidden-path, symlink, Windows junction/reparse-point, extension, and root-containment rules.
7. Local revision and RAG convergence is tested within five seconds without DeepSeek or real GBrain. Real GBrain uses opt-in condition polling with 120-second incremental and 600-second reconcile deadlines.

## Execution Order

Complete R0, then Tasks 1 through 12 in order. Do not begin watcher orchestration before R0 is green on SQLite and PostgreSQL when available.

## File And Ownership Map

- `app/wiki_revisions.py`: external mutation parsing, identity, revision events, revisions, intents, audit, transaction-local issue capture/validation/CAS during finalize, and projection outbox transitions.
- `app/wiki_markdown.py`: bounded byte capture, safe round-trip YAML parsing, managed frontmatter rendering, file/semantic hashes.
- `app/vault_events.py`: watcher occurrence, pending-delete, standalone watcher/admin sync-issue query/CAS, and reconcile-job persistence only.
- `app/vault_watcher.py`: canonical path classification, stability observation, `watchfiles` adaptation, and per-event error isolation.
- `app/vault_sync.py`: live event ordering, page serialization, rename evidence, delete expiry, startup inventory, and reconcile execution.
- `app/obsidian.py`: versioned asset materialization, drift reporting, unique backup, atomic JSON merge, and URI construction.
- `app/main.py`: partial-failure-safe lifespan ownership.
- `app/api.py` and `app/models.py`: status, reconcile, and ACL-protected deep-link APIs.
- `frontend/src/App.tsx`, `frontend/src/features/WikiTask.tsx`, and `frontend/src/types.ts`: operator-visible sync state and Obsidian command.

## Project Re-Review Report

### 审查结论

当前项目已经具备 LLMWiki 主流程、DeepSeek 可选回答、GBrain projection、Wiki revision/write-intent 与前端知识页管理，但“Obsidian 已接入”尚未成为事实。服务进程全部启动只能证明现有入口可运行，不能补齐缺失的文件监听、Obsidian 资产、同步状态 API 或端到端验收。

代码知识图谱与仓库文件共同显示：LGDO 侧只有 `app.vault.ensure_vault()`、应用拥有的原子写入器和 Wiki revision API；没有 `app/obsidian.py`、`app/vault_watcher.py`、`app/vault_sync.py`、`/vault/status`、Obsidian deep link、版本化 `.obsidian` 资源及安装脚本。GBrain 内部虽有独立 TypeScript file watcher，但它不进入 LGDO 的 revision、intent、audit 和 projection outbox 事务边界，不能当作 LGDO 的 Obsidian 集成替代品。DeepSeek 是查询时生成器，也不承担磁盘同步。

### 根因

1. 技术栈声明把 Obsidian 当作编辑入口，但实现只完成了 LGDO 生成/读取 Vault 的单向路径。
2. 缺少外部文件变化到 `WikiRevisionService` 的正式适配层；直接复用 GBrain watcher 会绕过 LGDO 的审计与并发契约。
3. 现有 revision event 缺少足够的不可变重放结果与 watcher 持久状态，贸然监听会产生重复 revision、错误 rename、删除误判和重启丢事件。
4. 没有 Obsidian 资源分发、Windows 命令、运行状态、ACL deep link 和用户可见恢复入口，因此即使手工打开 `vault/` 也只是非托管用法。
5. 旧方案把多项并发、迁移和权限细节留给实现阶段，无法作为可直接执行的开发合同；本修订计划用 R0 和 22 项覆盖矩阵补齐了这些前置条件。

### 风险判断

- **P0 数据一致性：** watcher 若自行写 revision event/outbox，或用 hash 猜 rename，会破坏审计、页面身份和幂等重放。
- **P0 重启与并发：** 无 occurrence/delete/reconcile 持久化时，删除 grace、崩溃恢复和多进程 single-flight 均不可靠。
- **P1 安全：** deep link 若先构造 URI 再做 ACL，会泄露受限页面是否存在；missing 与 unauthorized 必须同为同体 404。
- **P1 运维：** 没有 enabled-aware health、资产 drift 和失败 occurrence 计数时，“服务已启动”可能掩盖同步实际停止。
- **P2 体验：** 没有版本化 Vault 配置、迁移 `index/` 和安全 URI 编码时，不同工作站会产生不可重复配置。

### 下一步开发建议

1. 先完成 R0，冻结 revision event replay、nullable mutation result、外部新页/恢复/relocate 与 sync issue CAS；R0 未在 SQLite 和可用 PostgreSQL 上通过前，不启动 watcher 开发。
2. 按 Tasks 1–6 建立 watcher 配置、bounded observation、持久 occurrence/delete/reconcile、canonical path 和启动对账。每个任务独立提交，禁止跨层直写 revision/outbox/chunk 表。
3. 按 Tasks 7–9 交付 Obsidian 资产、PowerShell 安装/打开命令、lifespan、状态/对账 API、ACL deep link 和前端控制；这时才可对外称为“已集成 Obsidian”。
4. 用 Task 10 的五秒本地 E2E 作为默认发布门槛，不依赖 DeepSeek/GBrain 外部服务；再用 Task 11 的 opt-in 120/600 秒 GBrain SLA 验证真实 projection。
5. 最后执行 Task 12 的默认无外网回归、PostgreSQL、PowerShell、前端 build、所有权扫描和文档验收。任何 P0 gate 失败都应阻止进入下一阶段。

---

### Task R0: External Revision Contracts

**Files:**
- Modify: `app/db.py`
- Modify: `app/migration.py`
- Modify: `app/wiki_revisions.py`
- Modify: `tests/test_wiki_revisions.py`
- Modify: `tests/test_wiki_revision_postgres.py`
- Modify: `tests/test_wiki_schema_migration.py`
- Modify: `tests/test_sqlite_to_postgres_migration.py`

- [ ] **Step 1: Add failing schema and result-authority tests**

Append focused tests that lock the event payload and nullable result contract:

```python
# tests/test_wiki_schema_migration.py
import sqlite3

import pytest

from app.config import Settings
from app.db import connect_app, init_app_db
from app.wiki_markdown import compute_file_hash, compute_semantic_hash, parse_wiki_bytes
from app.wiki_revisions import RevisionConflict, WikiRevisionService


def schema_settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_backend="sqlite",
        database_path=tmp_path / "schema.db",
        vault_path=tmp_path / "vault",
        projection_worker_enabled=False,
        vault_watch_enabled=False,
    )


def test_external_event_schema_has_payload_and_terminal_result_columns(tmp_path):
    settings = schema_settings(tmp_path)
    init_app_db(settings)
    with connect_app(settings) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info('vault_change_events')")
        }
        issue_columns = {
            row[1] for row in conn.execute("PRAGMA table_info('vault_sync_issues')")
        }
    assert {"payload_digest", "result_payload_json"} <= columns
    assert {"id", "page_id", "file_hash", "generation", "resolved_at"} <= issue_columns


# tests/test_wiki_revisions.py
from app.wiki_revisions import MutationResult


def test_mutation_result_round_trips_nullable_page_and_sync_issue():
    result = MutationResult(
        page_id=None,
        page_path="wiki/product/broken.md",
        status="invalid",
        sync_issue_id="visi_broken",
    )
    assert MutationResult.from_event_payload(result.to_event_payload()) == result
```

- [ ] **Step 2: Run the schema/result tests and verify RED**

Run:

```powershell
python -m pytest tests/test_wiki_schema_migration.py::test_external_event_schema_has_payload_and_terminal_result_columns tests/test_wiki_revisions.py::test_mutation_result_round_trips_nullable_page_and_sync_issue -q
```

Expected: FAIL because the two event columns, `vault_sync_issues`, nullable `page_id`, `sync_issue_id`, and payload codecs do not exist.

- [ ] **Step 3: Add exact SQLite/PostgreSQL DDL and migration metadata**

Extend both `SCHEMA` and `PG_SCHEMA` in `app/db.py`. Add the same tables to `MAIN_TABLES` and `TABLE_PRIMARY_KEYS`, and add both event columns to the existing SQLite upgrade map so old databases are upgraded in place:

```sql
ALTER TABLE vault_change_events ADD COLUMN payload_digest TEXT NOT NULL DEFAULT '';
ALTER TABLE vault_change_events ADD COLUMN result_payload_json TEXT;

CREATE TABLE IF NOT EXISTS vault_sync_issues (
  id TEXT PRIMARY KEY,
  page_path TEXT NOT NULL,
  file_hash TEXT NOT NULL,
  page_id TEXT,
  issue_type TEXT NOT NULL,
  error_summary TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  generation INTEGER NOT NULL DEFAULT 1,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_vault_sync_issue_open_identity
ON vault_sync_issues(page_path, file_hash, issue_type)
WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_vault_sync_issue_status
ON vault_sync_issues(status, issue_type, page_path);
```

Use `BIGINT` only for PostgreSQL size/time fields already defined elsewhere; `generation` remains `INTEGER`. The migration metadata must include:

```python
MAIN_TABLES = [
    # existing entries remain in their current order
    "vault_sync_issues",
]

TABLE_PRIMARY_KEYS["vault_sync_issues"] = "id"

VAULT_CHANGE_EVENT_ADDITIONS = {
    "payload_digest": "TEXT NOT NULL DEFAULT ''",
    "result_payload_json": "TEXT",
}
```

Consume the map in the existing `_ensure_sqlite_revision_schema()` function:

```python
event_columns = _sqlite_table_info(conn, "vault_change_events")
for name, definition in VAULT_CHANGE_EVENT_ADDITIONS.items():
    if name not in event_columns:
        conn.execute(
            f'ALTER TABLE vault_change_events ADD COLUMN "{name}" {definition}'
        )
```

Do not backfill historical rows from `wiki_pages`. Migration deliberately leaves their `payload_digest=''` and `result_payload_json=NULL`; replay of any such legacy event fails closed with `RevisionConflict("legacy vault event has no authoritative result payload")`. A caller may submit the current filesystem observation under a new event ID after intent recovery. The old row remains immutable evidence and is never treated as replay authority.

Add the complete `vault_sync_issues` column set to `app/migration.py::_KNOWN_COLUMNS`. The migration file uses a real PostgreSQL capability gate. Add this reusable setup and populated-row gate to `tests/test_sqlite_to_postgres_migration.py`:

```python
from app.db import MAIN_TABLES, init_app_db, init_postgres_schema


def empty_migration_pair(tmp_path, monkeypatch, postgres_database):
    settings = get_settings()
    sqlite_path = tmp_path / "data/source.db"
    monkeypatch.setattr(settings, "database_backend", "sqlite")
    monkeypatch.setattr(settings, "database_path", sqlite_path)
    monkeypatch.setattr(settings, "postgres_database", postgres_database)
    monkeypatch.setattr(settings, "vault_path", tmp_path / "vault")
    monkeypatch.setattr(settings, "upload_path", tmp_path / "uploads")
    init_app_db(settings)
    init_postgres_schema(settings)
    with connect_postgres(settings) as conn, conn.cursor() as cur:
        for table in reversed(MAIN_TABLES):
            cur.execute(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE')
    return settings, sqlite_path


@pytest.mark.skipif(not pg_available(), reason="PostgreSQL 5432 is not available")
def test_migrates_populated_vault_sync_issue(tmp_path, monkeypatch):
    settings, sqlite_path = empty_migration_pair(
        tmp_path, monkeypatch, "lgdo_migration_vault_issue_test"
    )
    expected = (
        "visi_migration",
        "wiki/product/broken.md",
        "f" * 64,
        None,
        "invalid_frontmatter",
        "invalid yaml",
        "open",
        3,
        "2026-07-15T00:00:00+00:00",
        "2026-07-15T00:01:00+00:00",
        None,
    )
    with sqlite3.connect(sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO vault_sync_issues(
              id,page_path,file_hash,page_id,issue_type,error_summary,status,
              generation,first_seen_at,last_seen_at,resolved_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            expected,
        )

    result = migrate_sqlite_to_postgres(settings, sqlite_path)

    assert result["tables"]["vault_sync_issues"] == 1
    with connect_postgres(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id,page_path,file_hash,page_id,issue_type,error_summary,status,
                   generation,first_seen_at,last_seen_at,resolved_at
            FROM vault_sync_issues WHERE id=%s
            """,
            (expected[0],),
        )
        assert cur.fetchone() == expected
```

R0 must leave SQLite-to-PostgreSQL migration operational before Task 3 adds watcher tables.

- [ ] **Step 4: Implement the immutable event-result codec**

Change `MutationResult` and add canonical payload helpers in `app/wiki_revisions.py`:

```python
@dataclass(frozen=True)
class MutationResult:
    page_id: str | None
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
    sync_issue_id: str | None = None
    projection_job_ids: tuple[str, ...] = ()
    replayed: bool = False

    def to_event_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["projection_job_ids"] = list(self.projection_job_ids)
        return payload

    @classmethod
    def from_event_payload(cls, payload: dict[str, Any], *, replayed: bool = False) -> "MutationResult":
        if not isinstance(payload, dict) or type(replayed) is not bool:
            raise RevisionConflict(
                "vault event result payload is invalid", current_revision_id=None
            )
        required = {
            "page_id", "page_path", "status", "projection_job_ids", "replayed"
        }
        allowed = {field.name for field in dataclass_fields(cls)}
        if not required <= payload.keys() or not payload.keys() <= allowed:
            raise RevisionConflict(
                "vault event result payload is invalid", current_revision_id=None
            )
        values = dict(payload)
        optional_id_fields = (
            "page_id",
            "revision_id",
            "current_revision_id",
            "generated_revision_id",
            "candidate_revision_id",
            "write_intent_id",
            "conflict_review_id",
            "observation_id",
            "audit_revision_id",
            "sync_issue_id",
        )
        jobs = values.get("projection_job_ids")
        stored_replayed = values.get("replayed")
        if (
            any(
                values.get(field_name) is not None
                and type(values.get(field_name)) is not str
                for field_name in optional_id_fields
            )
            or type(values.get("page_path")) is not str
            or values.get("status") not in VALID_MUTATION_STATUSES
            or type(jobs) is not list
            or not all(type(job_id) is str for job_id in jobs)
            or type(stored_replayed) is not bool
        ):
            raise RevisionConflict(
                "vault event result payload is invalid", current_revision_id=None
            )
        values["projection_job_ids"] = tuple(jobs)
        values["replayed"] = replayed or stored_replayed
        try:
            return cls(**values)
        except (TypeError, ValueError) as exc:
            raise RevisionConflict(
                "vault event result payload is invalid", current_revision_id=None
            ) from exc


def external_payload_digest(operation: str, payload: dict[str, Any]) -> str:
    encoded = canonical_state_json(
        {"operation": operation, "payload": payload}
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def observation_payload(observation: FileObservationInput) -> dict[str, Any]:
    return {
        "file_hash": observation.file_hash,
        "size_bytes": observation.size_bytes,
        "mtime_ns": observation.mtime_ns,
        "content_truncated": observation.content_truncated,
        "content_prefix_hash": hashlib.sha256(
            observation.content_prefix or b""
        ).hexdigest(),
    }
```

Every revision API must insert `payload_digest` with the event and, on a terminal transition, write `status`, `result_revision_id`, and canonical `result_payload_json` in the same transaction. Terminal replay must deserialize `result_payload_json`; it must not reconstruct a result from mutable `wiki_pages` state. Reusing an event ID with a different digest raises `RevisionConflict`.

Define `VALID_MUTATION_STATUSES = frozenset(get_args(MutationStatus))`, import `get_args` from `typing`, and import `fields as dataclass_fields` from `dataclasses`. Wrap JSON decode and codec failures in `_replay_external_event()` and `_replay_lifecycle_event()` as `RevisionConflict("vault event result payload is invalid")`; never expose `JSONDecodeError`, `TypeError`, or `KeyError`.

Add migration/replay tests that create the pre-upgrade `vault_change_events` schema manually, insert one terminal and one pending legacy row, run `init_app_db()`, and assert both rows retain empty digest/NULL payload. Replaying either old ID must raise the exact legacy error without changing `wiki_pages`; a new event ID with the same current observation must follow the normal R0 contract. Add parameterized corrupt terminal payload tests for every optional ID, the job list, stored replay flag, page path, and the explicit replay argument. Python booleans are not accepted as integers here: optional IDs require `str | None`, serialized jobs require a real JSON array of strings, and both replay flags require `type(value) is bool`.

Use these concrete tests:

```python
# tests/test_wiki_schema_migration.py
def test_legacy_vault_events_are_upgraded_but_never_reconstructed_from_page_state(tmp_path):
    settings = schema_settings(tmp_path)
    with sqlite3.connect(settings.database_path) as conn:
        conn.execute(
            """
            CREATE TABLE vault_change_events (
              id TEXT PRIMARY KEY,kind TEXT NOT NULL,page_path TEXT NOT NULL,
              old_page_path TEXT,observation_id TEXT,expected_state_json TEXT NOT NULL DEFAULT '{}',
              status TEXT NOT NULL DEFAULT 'pending',result_revision_id TEXT,
              detected_at TEXT NOT NULL,updated_at TEXT NOT NULL
            )
            """
        )
        for event_id, status in (("legacy-terminal", "applied"), ("legacy-pending", "pending")):
            conn.execute(
                """
                INSERT INTO vault_change_events(
                  id,kind,page_path,expected_state_json,status,detected_at,updated_at
                ) VALUES (?,'delete','wiki/product/legacy.md','{}',?,'2026-07-14','2026-07-14')
                """,
                (event_id, status),
            )
    init_app_db(settings)
    seed_legacy_page_at(settings, "wiki/product/legacy.md")
    service = WikiRevisionService(settings)
    with connect_app(settings) as conn:
        rows = conn.execute(
            "SELECT id,payload_digest,result_payload_json FROM vault_change_events ORDER BY id"
        ).fetchall()
        before = dict(conn.execute(
            "SELECT * FROM wiki_pages WHERE path='wiki/product/legacy.md'"
        ).fetchone())
    assert [(row["payload_digest"], row["result_payload_json"]) for row in rows] == [("", None), ("", None)]
    for event_id in ("legacy-terminal", "legacy-pending"):
        with pytest.raises(
            RevisionConflict,
            match="legacy vault event has no authoritative result payload",
        ):
            service.delete_page(event_id, "wiki/product/legacy.md")
    with connect_app(settings) as conn:
        after = dict(conn.execute(
            "SELECT * FROM wiki_pages WHERE path='wiki/product/legacy.md'"
        ).fetchone())
    assert after == before


# tests/test_wiki_revisions.py
OPTIONAL_MUTATION_RESULT_ID_FIELDS = (
    "page_id",
    "revision_id",
    "current_revision_id",
    "generated_revision_id",
    "candidate_revision_id",
    "write_intent_id",
    "conflict_review_id",
    "observation_id",
    "audit_revision_id",
    "sync_issue_id",
)


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        *[(field_name, 0) for field_name in OPTIONAL_MUTATION_RESULT_ID_FIELDS],
        pytest.param("projection_job_ids", 0, id="jobs-int"),
        pytest.param("projection_job_ids", False, id="jobs-bool"),
        pytest.param("projection_job_ids", "job_1", id="jobs-string"),
        pytest.param("projection_job_ids", ["job_1", 2], id="jobs-mixed"),
        pytest.param("replayed", "false", id="stored-replayed-string"),
        pytest.param("replayed", 0, id="stored-replayed-zero"),
        pytest.param("replayed", 1, id="stored-replayed-one"),
        pytest.param("replayed", None, id="stored-replayed-null"),
        pytest.param("page_path", 0, id="page-path-int"),
        pytest.param("page_path", False, id="page-path-bool"),
        pytest.param("page_path", None, id="page-path-null"),
    ],
)
def test_corrupt_terminal_result_payload_fails_as_revision_conflict(
    legacy_page_fixture, field_name, bad_value
):
    service, before = legacy_page_fixture
    (service.settings.vault_path / before.page_path).unlink()
    result = service.delete_page("delete-corrupt-result", before.page_path)
    assert result.status == "deleted"
    with connect_app(service.settings) as conn:
        row = conn.execute(
            "SELECT result_payload_json FROM vault_change_events WHERE id=?",
            ("delete-corrupt-result",),
        ).fetchone()
        payload = json.loads(row["result_payload_json"])
        payload[field_name] = bad_value
        conn.execute(
            "UPDATE vault_change_events SET result_payload_json=? WHERE id=?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), "delete-corrupt-result"),
        )
    with pytest.raises(RevisionConflict, match="vault event result payload is invalid"):
        service.delete_page("delete-corrupt-result", before.page_path)


@pytest.mark.parametrize("bad_replayed", ["false", 0, 1, None])
def test_mutation_result_codec_rejects_non_boolean_replay_argument(bad_replayed):
    payload = MutationResult(
        page_id=None,
        page_path="wiki/product/invalid.md",
        status="invalid",
    ).to_event_payload()
    with pytest.raises(RevisionConflict, match="vault event result payload is invalid"):
        MutationResult.from_event_payload(payload, replayed=bad_replayed)


def test_legacy_pending_external_intent_recovers_without_fabricated_event_result(
    legacy_page_fixture, monkeypatch
):
    service, before = legacy_page_fixture
    target = service.settings.vault_path / before.page_path
    target.write_bytes(before.raw_bytes + b"\nLegacy pending recovery.\n")
    def interrupt_execute(self, intent_id):
        raise RuntimeError("stop after external preparation")

    with monkeypatch.context() as scoped:
        scoped.setattr(IntentExecutor, "execute", interrupt_execute)
        with pytest.raises(RuntimeError, match="stop after external preparation"):
            service.ingest_external_change(
                "legacy-pending-intent", before.page_path,
                capture_file_observation(
                    target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
                ),
            )
    with connect_app(service.settings) as conn:
        conn.execute(
            """
            UPDATE vault_change_events
            SET payload_digest='',result_payload_json=NULL
            WHERE id='legacy-pending-intent'
            """
        )
    IntentExecutor(service.settings).reconcile_all()
    with connect_app(service.settings) as conn:
        event = conn.execute(
            "SELECT status,payload_digest,result_payload_json FROM vault_change_events WHERE id='legacy-pending-intent'"
        ).fetchone()
        intent = conn.execute(
            "SELECT status FROM vault_write_intents WHERE page_id=? ORDER BY created_at DESC LIMIT 1",
            (before.page_id,),
        ).fetchone()
    assert intent["status"] == "applied"
    assert event["status"] == "legacy_applied_unreplayable"
    assert event["payload_digest"] == ""
    assert event["result_payload_json"] is None
    with pytest.raises(
        RevisionConflict,
        match="legacy vault event has no authoritative result payload",
    ):
        service.ingest_external_change(
            "legacy-pending-intent", before.page_path,
            capture_file_observation(
                target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
            ),
        )
```

Define the migration helper in `tests/test_wiki_schema_migration.py`:

```python
def seed_legacy_page_at(settings, page_path: str) -> None:
    page_id = "page_legacy_migration"
    revision_id = "wrev_legacy_migration"
    timestamp = "2026-07-14T00:00:00+00:00"
    content = (
        "---\nid: page_legacy_migration\nlgdo_page_id: page_legacy_migration\n"
        "lgdo_revision_id: wrev_legacy_migration\ntitle: Legacy\nsource_ids: []\n"
        "domain: product\npage_type: feature\nreview_status: draft\nowner:\n"
        "---\n# Legacy\n"
    )
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,page_id,domain,page_type,title,source_ids_json,review_status,
              owner,created_at,updated_at,current_revision_id,revision_number,
              file_hash,semantic_hash,projection_epoch,lifecycle_status
            ) VALUES (?,?, 'product','feature','Legacy','[]','draft',NULL,?,?,?,1,?,?,1,'active')
            """,
            (
                page_path, page_id, timestamp, timestamp, revision_id,
                compute_file_hash(content.encode("utf-8")),
                compute_semantic_hash(parse_wiki_bytes(content.encode("utf-8"))),
            ),
        )
        conn.execute(
            """
            INSERT INTO wiki_page_revisions(
              id,page_id,page_path,revision_number,file_hash,semantic_hash,content,
              origin,source_ids_json,metadata_json,idempotency_key,created_at
            ) VALUES (?,?,?,1,?,?,?,'legacy','[]','{}',?,?)
            """,
            (
                revision_id, page_id, page_path,
                compute_file_hash(content.encode("utf-8")),
                compute_semantic_hash(parse_wiki_bytes(content.encode("utf-8"))),
                content, "legacy:migration", timestamp,
            ),
        )
```

It creates complete `domain`, `page_type`, and source metadata and does not update either legacy event row.

During `finalize_intent()`, a recovered external revision whose referenced event has `payload_digest=''` is the sole legacy exception: finalize the immutable revision/intent/page transition, set the old event status to `legacy_applied_unreplayable`, retain `result_payload_json=NULL`, and emit an audit row. Never synthesize a digest or result payload. All old event-ID replays still fail closed; a fresh event ID observes the recovered managed file normally.

- [ ] **Step 5: Run schema, migration, and existing revision replay tests**

Run:

```powershell
python -m pytest tests/test_wiki_schema_migration.py tests/test_wiki_revisions.py tests/test_sqlite_to_postgres_migration.py -k "event_schema or mutation_result or event_replay or payload or sync_issue" -q
```

Expected: PASS; existing event replay remains idempotent and new terminal results round-trip with nullable page identity.

- [ ] **Step 6: Add failing valid-new-page and source validation tests**

Add these helpers and tests to `tests/test_wiki_revisions.py`:

```python
def external_page_bytes(*, title="External", source_ids=(), domain="product", body="Body") -> bytes:
    rendered_sources = ", ".join(source_ids)
    return (
        "---\n"
        f"title: {title}\n"
        f"source_ids: [{rendered_sources}]\n"
        f"domain: {domain}\n"
        "page_type: feature\n"
        "review_status: draft\n"
        "owner:\n"
        "---\n"
        f"# {title}\n{body}\n"
    ).encode("utf-8")


def seed_source(settings, source_id: str, *, domain="product", status="active") -> None:
    timestamp = "2026-07-15T00:00:00+00:00"
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO sources(
              id,domain,title,source_type,original_path,raw_path,content_hash,
              size_bytes,status,metadata_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source_id, domain, source_id, "markdown", source_id,
                f"raw/{domain}/{source_id}.md", "a" * 64, 1, status,
                "{}", timestamp, timestamp,
            ),
        )


def test_external_new_page_accepts_empty_sources_and_creates_managed_revision(tmp_path):
    settings = make_settings(tmp_path)
    init_app_db(settings)
    page_path = "wiki/product/new-empty.md"
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(external_page_bytes(source_ids=()))
    service = WikiRevisionService(settings)

    applied = service.ingest_external_change(
        "external-new-empty", page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )

    with connect_app(settings) as conn:
        page = dict(conn.execute("SELECT * FROM wiki_pages WHERE path=?", (page_path,)).fetchone())
        revision = dict(conn.execute("SELECT * FROM wiki_page_revisions WHERE id=?", (page["current_revision_id"],)).fetchone())
    assert applied.status == "applied"
    assert applied.page_id == page["page_id"]
    assert json.loads(page["source_ids_json"]) == []
    assert revision["origin"] == "external"


@pytest.mark.parametrize(
    ("source_id", "status", "expected_code"),
    [("src_missing", None, "unknown_source"), ("src_inactive", "inactive", "inactive_source")],
)
def test_external_sources_must_exist_and_be_active(tmp_path, source_id, status, expected_code):
    settings = make_settings(tmp_path)
    init_app_db(settings)
    if status is not None:
        seed_source(settings, source_id, status=status)
    page_path = "wiki/product/source-check.md"
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(external_page_bytes(source_ids=(source_id,)))

    service = WikiRevisionService(settings)
    event_id = f"external-{expected_code}"
    observation = capture_file_observation(
        target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
    )
    result = service.ingest_external_change(
        event_id, page_path, observation,
    )
    replay = service.ingest_external_change(event_id, page_path, observation)

    assert result.status == "invalid"
    assert result.page_id is None
    assert result.sync_issue_id
    assert replay.replayed is True
    assert replay.to_event_payload() | {"replayed": False} == result.to_event_payload()
    with connect_app(settings) as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "wiki_pages", "wiki_page_revisions", "vault_write_intents",
                "review_items", "knowledge_projection_jobs",
            )
        }
        assert counts == {table: 0 for table in counts}
        assert conn.execute(
            "SELECT COUNT(*) FROM vault_sync_issues WHERE page_path=?",
            (page_path,),
        ).fetchone()[0] == 1


def test_active_source_from_another_domain_is_valid(tmp_path):
    settings = make_settings(tmp_path)
    init_app_db(settings)
    seed_source(settings, "src_support", domain="support", status="active")
    page_path = "wiki/product/cross-domain.md"
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(external_page_bytes(source_ids=("src_support",), domain="product"))
    result = WikiRevisionService(settings).ingest_external_change(
        "external-cross-domain", page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert result.status == "applied"
    assert result.page_id is not None


def test_unknown_invalid_external_file_has_no_fake_domain_rows(tmp_path):
    settings = make_settings(tmp_path)
    init_app_db(settings)
    page_path = "wiki/product/unknown-invalid.md"
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"---\ntitle: [broken\n---\n# Broken\n")
    result = WikiRevisionService(settings).ingest_external_change(
        "external-unknown-invalid", page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    with connect_app(settings) as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "wiki_pages", "wiki_page_revisions", "vault_write_intents",
                "review_items", "knowledge_projection_jobs",
            )
        }
        observation_count = conn.execute(
            "SELECT COUNT(*) FROM wiki_file_observations WHERE page_path=?", (page_path,)
        ).fetchone()[0]
        event = conn.execute(
            "SELECT status,result_payload_json FROM vault_change_events WHERE id=?",
            ("external-unknown-invalid",),
        ).fetchone()
        issue_count = conn.execute(
            "SELECT COUNT(*) FROM vault_sync_issues WHERE page_path=? AND status='open'",
            (page_path,),
        ).fetchone()[0]
    assert result.page_id is None
    assert result.sync_issue_id
    assert counts == {table: 0 for table in counts}
    assert observation_count == 1
    assert issue_count == 1
    assert event["status"] == "invalid"
    assert json.loads(event["result_payload_json"])["sync_issue_id"] == result.sync_issue_id
    replay = WikiRevisionService(settings).ingest_external_change(
        "external-unknown-invalid", page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert replay.replayed is True
    assert replay.sync_issue_id == result.sync_issue_id
```

- [ ] **Step 7: Run new-page/source tests and verify RED**

Run:

```powershell
python -m pytest tests/test_wiki_revisions.py -k "external_new_page or external_sources or another_domain" -q
```

Expected: FAIL because ingest still requires an existing page and has no database-backed source validation.

- [ ] **Step 8: Add one external-input classifier and source validator**

Add these exact internal contracts to `app/wiki_revisions.py`; all new-page, modify, restore, and relocate paths use them:

```python
@dataclass(frozen=True)
class ExternalDocumentInput:
    document: ParsedWikiDocument
    metadata: dict[str, Any]
    title: str
    domain: str
    page_type: str
    source_ids: list[str]
    review_status: str
    owner: str | None
    declared_page_id: str | None


def _optional_owner(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise MarkdownParseError("invalid_owner", "owner must be a string or null")
    return value


def _validate_external_sources(conn: Any, source_ids: list[str]) -> None:
    if not source_ids:
        return
    placeholders = ",".join("?" for _ in source_ids)
    rows = conn.execute(
        f"SELECT id,status FROM sources WHERE id IN ({placeholders})",
        source_ids,
    ).fetchall()
    by_id = {str(row["id"]): str(row["status"]) for row in rows}
    missing = [source_id for source_id in source_ids if source_id not in by_id]
    inactive = [source_id for source_id in source_ids if by_id.get(source_id) != "active"]
    if missing:
        raise MarkdownParseError("unknown_source", f"unknown source_ids: {', '.join(missing)}")
    if inactive:
        raise MarkdownParseError("inactive_source", f"inactive source_ids: {', '.join(inactive)}")


def _classify_external_document(
    conn: Any,
    content: bytes,
    *,
    bound_page: dict[str, Any] | None,
) -> ExternalDocumentInput:
    document = parse_wiki_bytes(content)
    metadata = _plain(document.frontmatter)
    source_ids = (
        _source_ids(metadata)
        if "source_ids" in metadata
        else json.loads(bound_page["source_ids_json"] or "[]")
        if bound_page is not None
        else []
    )
    _validate_external_sources(conn, source_ids)
    title = metadata.get("title", bound_page.get("title") if bound_page else None)
    domain = metadata.get("domain", bound_page.get("domain") if bound_page else None)
    page_type = metadata.get(
        "page_type", bound_page.get("page_type") if bound_page else None
    )
    if not isinstance(title, str) or not title.strip():
        raise MarkdownParseError("invalid_title", "title must be a non-empty string")
    if not isinstance(domain, str) or not domain.strip():
        raise MarkdownParseError("invalid_domain", "domain must be a non-empty string")
    if not isinstance(page_type, str) or not page_type.strip():
        raise MarkdownParseError("invalid_page_type", "page_type must be a non-empty string")
    first_id = metadata.get("lgdo_page_id")
    second_id = metadata.get("id")
    if first_id is not None and second_id is not None and first_id != second_id:
        raise MarkdownParseError("identity_mismatch", "id and lgdo_page_id must match")
    declared = first_id or second_id
    if declared is not None and not isinstance(declared, str):
        raise MarkdownParseError("invalid_page_id", "managed page ID must be a string")
    return ExternalDocumentInput(
        document=document,
        metadata=metadata,
        title=title,
        domain=domain,
        page_type=page_type,
        source_ids=source_ids,
        review_status=_review_status(
            metadata.get(
                "review_status",
                bound_page.get("review_status") if bound_page else "draft",
            )
        ),
        owner=(
            _optional_owner(metadata.get("owner"))
            if "owner" in metadata
            else bound_page.get("owner") if bound_page else None
        ),
        declared_page_id=declared,
    )
```

Import and use the existing `ParsedWikiDocument` type from `app/wiki_markdown.py`; do not define a duplicate parser model.

The fallback is allowed only when `bound_page` came from the already locked canonical path. A new path passes `bound_page=None` and must contain valid `title`, `domain`, and `page_type`. If a bound document explicitly lists a source, that source must still exist and be active; update the existing `legacy_page_fixture`/`seed_legacy_page()` test helpers to insert active rows for every listed fixture source such as `src_1`. Missing bound-page metadata is rendered back into the next managed revision, producing a controlled frontmatter upgrade.

Before calling `render_managed_frontmatter()`, explicitly write the resolved business metadata into the round-trip document so fallback values reach revision bytes and disk:

```python
parsed.document.frontmatter["title"] = parsed.title
parsed.document.frontmatter["domain"] = parsed.domain
parsed.document.frontmatter["page_type"] = parsed.page_type
parsed.document.frontmatter["source_ids"] = parsed.source_ids
parsed.document.frontmatter["review_status"] = parsed.review_status
parsed.document.frontmatter["owner"] = parsed.owner
```

The bound-page upgrade test must parse the final disk file after auto-execution and assert all six values, including `owner is None`; checking only `wiki_pages` is insufficient.

Add `test_bound_legacy_page_missing_metadata_uses_locked_values_and_upgrades_frontmatter`: seed a bound page whose file omits `domain`, `page_type`, `source_ids`, and `owner`, edit its body, ingest it, and assert the applied revision/frontmatter and all six `wiki_pages` fields use the locked values. Add a second test proving an explicitly present unknown source on the same bound page is invalid rather than falling back.

- [ ] **Step 9: Implement valid new-page preparation without a placeholder row**

Refactor `_persist_external_occurrence()` so it captures the observation and event first, classifies bytes inside the same write transaction, and chooses one of three explicit branches:

```python
def _prepare_new_external_page_locked(
    self,
    conn: Any,
    *,
    event_id: str,
    page_path: str,
    observed: dict[str, Any],
    parsed: ExternalDocumentInput,
    payload_digest: str,
) -> MutationResult:
    if parsed.declared_page_id is not None:
        raise MarkdownParseError(
            "unproven_page_identity",
            "a new page path cannot claim a managed page ID without rename evidence",
        )
    page_id = f"page_{uuid.uuid4().hex}"
    timestamp = now_iso()
    conn.execute(
        """
        INSERT INTO wiki_pages(
          path,page_id,domain,page_type,title,source_ids_json,review_status,
          owner,created_at,updated_at,lifecycle_status
        ) VALUES (?,?,?,?,?,?,?,?,?,?,'active')
        """,
        (
            page_path, page_id, parsed.domain, parsed.page_type, parsed.title,
            json_dump(parsed.source_ids), "draft", parsed.owner, timestamp, timestamp,
        ),
    )
    page = dict(conn.execute("SELECT * FROM wiki_pages WHERE page_id=?", (page_id,)).fetchone())
    return self._prepare_external_revision_locked(
        conn,
        page=page,
        event_id=event_id,
        observation=observed,
        parsed=parsed,
        payload_digest=payload_digest,
        force_new_revision=True,
        transition_kind="external_create",
    )
```

`_prepare_external_revision_locked()` must render new managed revision/write-token fields, create one `origin='external'` revision, prepare one crash-safe write intent, and leave outbox creation until `finalize_intent()`. If the surrounding transaction rolls back, page, revision, intent, observation linkage, and event preparation roll back together.

The public `ingest_external_change()` and `relocate_external_change()` contracts remain synchronous auto-execute operations: after internal preparation they call `IntentExecutor.execute(write_intent_id)`, finalize the page/event/outbox, and return terminal `status='applied'`. They return terminal `invalid` without an intent for invalid input. `status='prepared'` is internal/recovery state only; if auto-execution cannot reach applied, the public call raises `RevisionConflict` with the pending intent ID.

- [ ] **Step 10: Implement unknown-invalid terminal events with no fake domain rows**

Add a transaction-local issue upsert and terminal result writer:

```python
def _upsert_sync_issue_locked(
    conn: Any,
    *,
    page_path: str,
    file_hash: str,
    page_id: str | None,
    issue_type: str,
    error_summary: str,
) -> tuple[str, int]:
    issue_id = "visi_" + hashlib.sha256(
        f"{page_path}\0{file_hash}\0{issue_type}".encode("utf-8")
    ).hexdigest()[:24]
    timestamp = now_iso()
    conn.execute(
        """
        INSERT INTO vault_sync_issues(
          id,page_path,file_hash,page_id,issue_type,error_summary,status,
          generation,first_seen_at,last_seen_at
        ) VALUES (?,?,?,?,?,?,'open',1,?,?)
        ON CONFLICT(id) DO UPDATE SET
          page_id=excluded.page_id,error_summary=excluded.error_summary,
          generation=vault_sync_issues.generation+1,last_seen_at=excluded.last_seen_at,
          status='open',resolved_at=NULL
        """,
        (
            issue_id, page_path, file_hash, page_id, issue_type,
            error_summary[:1000], timestamp, timestamp,
        ),
    )
    row = conn.execute("SELECT generation FROM vault_sync_issues WHERE id=?", (issue_id,)).fetchone()
    return issue_id, int(row["generation"])


def _finish_revision_event_locked(conn: Any, event_id: str, result: MutationResult) -> None:
    payload = canonical_state_json(result.to_event_payload())
    changed = conn.execute(
        """
        UPDATE vault_change_events
        SET status=?,result_revision_id=?,result_payload_json=?,updated_at=?
        WHERE id=? AND status IN ('pending','prepared')
        """,
        (result.status, result.revision_id, payload, now_iso(), event_id),
    )
    if changed.rowcount != 1:
        raise RevisionConflict(
            "revision event terminal result changed",
            current_revision_id=result.current_revision_id,
        )
```

When parsing, size, source, or unproven-identity validation fails before a page exists, persist the bounded `wiki_file_observations` row with `page_id=NULL`, upsert the issue, and finish the service-owned event with:

```python
result = MutationResult(
    page_id=None,
    page_path=page_path,
    status="invalid",
    observation_id=observation_id,
    sync_issue_id=issue_id,
)
```

Do not insert `wiki_pages`, `wiki_page_revisions`, `vault_write_intents`, `review_items`, or `knowledge_projection_jobs` in this branch.

- [ ] **Step 11: Run new-page, invalid, source, and replay tests**

Run:

```powershell
python -m pytest tests/test_wiki_revisions.py -k "external_new_page or external_sources or another_domain or unknown_invalid or event_replay" -q
```

Expected: PASS; invalid unknown input has one observation, one terminal event, one issue, and zero page/revision/intent/job rows.

- [ ] **Step 12: Add failing restore, relocate, exact-rename, finalize, and issue-CAS tests**

Add named tests with these assertions to `tests/test_wiki_revisions.py`. Extend its imports with `connect_app_write`, `parse_wiki_bytes`, and from `app.wiki_revisions` the planned `SYNC_ISSUE_REFS_METADATA_KEY`, `_decode_sync_issue_refs`, and `_upsert_sync_issue_locked` symbols:

```python
def test_deleted_identical_bytes_restore_creates_new_external_revision(legacy_page_fixture):
    service, before = legacy_page_fixture
    (service.settings.vault_path / before.page_path).unlink()
    service.delete_page("delete-before-restore", before.page_path)
    target = service.settings.vault_path / before.page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(before.raw_bytes)
    restored = service.ingest_external_change(
        "restore-identical", before.page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert restored.status == "applied"
    assert restored.revision_id != before.current_revision_id
    with connect_app(service.settings) as conn:
        revision = conn.execute(
            "SELECT origin FROM wiki_page_revisions WHERE id=?", (restored.revision_id,)
        ).fetchone()
    assert revision["origin"] == "external"


def test_invalid_identical_historical_bytes_restore_creates_new_external_revision(legacy_page_fixture):
    service, before = legacy_page_fixture
    target = service.settings.vault_path / before.page_path
    target.write_bytes(b"---\ntitle: [broken\n---\n# Broken\n")
    invalid = service.ingest_external_change(
        "invalid-before-identical-restore", before.page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert invalid.status == "invalid"
    target.write_bytes(before.raw_bytes)
    restored = service.ingest_external_change(
        "invalid-identical-restore", before.page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert restored.status == "applied"
    assert restored.revision_id != before.current_revision_id


def test_relocate_with_edit_is_one_external_revision_and_atomic(legacy_page_fixture):
    service, before = legacy_page_fixture
    old_path = before.page_path
    new_path = "wiki/product/faq/relocated.md"
    old_target = service.settings.vault_path / old_path
    new_target = service.settings.vault_path / new_path
    new_target.parent.mkdir(parents=True, exist_ok=True)
    edited = before.raw_bytes.replace(b"# Demo", b"# Relocated")
    old_target.rename(new_target)
    new_target.write_bytes(edited)
    result = service.relocate_external_change(
        "relocate-edited", old_path, new_path, before.page_id,
        capture_file_observation(
            new_target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert result.status == "applied"
    assert result.revision_id != before.current_revision_id
    with connect_app(service.settings) as conn:
        page = conn.execute("SELECT * FROM wiki_pages WHERE page_id=?", (before.page_id,)).fetchone()
        revision = conn.execute("SELECT * FROM wiki_page_revisions WHERE id=?", (result.revision_id,)).fetchone()
    assert page["path"] == new_path
    assert revision["origin"] == "external"
    assert revision["page_path"] == new_path


def test_exact_rename_stays_audit_only(legacy_page_fixture):
    service, before = legacy_page_fixture
    new_path = "wiki/product/faq/exact-renamed.md"
    old_target = service.settings.vault_path / before.page_path
    new_target = service.settings.vault_path / new_path
    new_target.parent.mkdir(parents=True, exist_ok=True)
    old_target.rename(new_target)
    result = service.rename_page("rename-exact", before.page_path, new_path)
    assert result.status == "renamed"
    assert result.current_revision_id == before.current_revision_id
    assert result.audit_revision_id != before.current_revision_id


def test_finalize_external_revision_syncs_all_page_fields_and_clears_owner(legacy_page_fixture):
    service, before = legacy_page_fixture
    seed_source(service.settings, "src_cross", domain="support", status="active")
    target = service.settings.vault_path / before.page_path
    target.write_bytes(
        external_page_bytes(
            title="Changed title", source_ids=("src_cross",), domain="operations", body="Changed"
        ).replace(b"page_type: feature", b"page_type: policy")
    )
    applied = service.ingest_external_change(
        "finalize-six-fields", before.page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert applied.status == "applied"
    with connect_app(service.settings) as conn:
        page = dict(conn.execute("SELECT * FROM wiki_pages WHERE page_id=?", (before.page_id,)).fetchone())
    assert (
        page["title"], page["domain"], page["page_type"],
        json.loads(page["source_ids_json"]), page["review_status"], page["owner"],
    ) == ("Changed title", "operations", "policy", ["src_cross"], "draft", None)


class StopAfterRepairInstall(RuntimeError):
    pass


def create_real_invalid_issue(service, event_id: str, page_path: str):
    target = service.settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"---\ntitle: [broken\n---\n# Broken\n")
    invalid = service.ingest_external_change(
        event_id,
        page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert invalid.status == "invalid"
    assert invalid.sync_issue_id is not None
    with connect_app(service.settings) as conn:
        issue = dict(conn.execute(
            "SELECT * FROM vault_sync_issues WHERE id=?",
            (invalid.sync_issue_id,),
        ).fetchone())
    assert issue["status"] == "open"
    assert issue["generation"] == 1
    return target, issue


def prepare_real_repair_without_finalize(
    monkeypatch, service, event_id: str, page_path: str, target
):
    installed: dict[str, str] = {}

    def install_only(executor, intent_id):
        installed["intent_id"] = intent_id
        installed["owner"] = executor.owner
        assert executor.claim(intent_id, lease_seconds=30)
        executor.capture_and_install(intent_id, stop_after="installed")
        raise StopAfterRepairInstall("repair installed before finalize")

    observation = capture_file_observation(
        target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(IntentExecutor, "execute", install_only)
        with pytest.raises(StopAfterRepairInstall, match="repair installed before finalize"):
            service.ingest_external_change(event_id, page_path, observation)
    return installed


@pytest.mark.parametrize("bump_after_prepare", [False, True])
def test_successful_repair_resolves_only_captured_issue_generation(
    tmp_path, monkeypatch, bump_after_prepare
):
    settings = make_settings(tmp_path)
    init_app_db(settings)
    service = WikiRevisionService(settings)
    page_path = "wiki/product/repair.md"
    target, issue = create_real_invalid_issue(
        service, "repair-invalid", page_path
    )
    target.write_bytes(
        external_page_bytes(
            title="Repaired page", source_ids=(), domain="product", body="Repaired body"
        )
    )
    installed = prepare_real_repair_without_finalize(
        monkeypatch, service, "repair-valid", page_path, target
    )
    with connect_app(settings) as conn:
        revision = dict(conn.execute(
            """
            SELECT r.id,r.metadata_json
            FROM vault_write_intents i
            JOIN wiki_page_revisions r ON r.id=i.revision_id
            WHERE i.id=?
            """,
            (installed["intent_id"],),
        ).fetchone())
    metadata = json.loads(revision["metadata_json"])
    assert metadata[SYNC_ISSUE_REFS_METADATA_KEY] == [
        {"id": issue["id"], "generation": 1}
    ]

    if bump_after_prepare:
        with connect_app_write(settings) as conn:
            bumped_id, bumped_generation = _upsert_sync_issue_locked(
                conn,
                page_path=page_path,
                file_hash=issue["file_hash"],
                page_id=None,
                issue_type=issue["issue_type"],
                error_summary="same invalid observation seen again",
            )
        assert (bumped_id, bumped_generation) == (issue["id"], 2)

    applied = service.finalize_intent(
        installed["intent_id"], installed["owner"]
    )
    assert applied.status == "applied"
    assert applied.revision_id == revision["id"]
    with connect_app(settings) as conn:
        issue_after = conn.execute(
            "SELECT status,generation FROM vault_sync_issues WHERE id=?",
            (issue["id"],),
        ).fetchone()
        page_after = conn.execute(
            "SELECT current_revision_id,pending_write_intent_id FROM wiki_pages WHERE path=?",
            (page_path,),
        ).fetchone()
        event_after = conn.execute(
            "SELECT status,result_revision_id FROM vault_change_events WHERE id='repair-valid'"
        ).fetchone()
    assert tuple(issue_after) == (
        ("open", 2) if bump_after_prepare else ("resolved", 1)
    )
    assert tuple(page_after) == (revision["id"], None)
    assert tuple(event_after) == ("applied", revision["id"])


def test_user_frontmatter_cannot_forge_reserved_sync_issue_refs(tmp_path):
    settings = make_settings(tmp_path)
    init_app_db(settings)
    service = WikiRevisionService(settings)
    _, victim_issue = create_real_invalid_issue(
        service, "victim-invalid", "wiki/product/victim.md"
    )
    page_path = "wiki/product/forger.md"
    target = settings.vault_path / page_path
    target.write_text(
        "---\ntitle: Forger\nsource_ids: []\ndomain: product\n"
        "page_type: feature\nreview_status: draft\nowner:\n"
        f"{SYNC_ISSUE_REFS_METADATA_KEY}:\n"
        f"  - id: {victim_issue['id']}\n    generation: 1\n"
        "---\n# Forger\nValid user content.\n",
        encoding="utf-8",
    )
    applied = service.ingest_external_change(
        "forged-issue-ref",
        page_path,
        capture_file_observation(
            target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
        ),
    )
    assert applied.status == "applied"
    with connect_app(settings) as conn:
        revision = conn.execute(
            "SELECT metadata_json FROM wiki_page_revisions WHERE id=?",
            (applied.revision_id,),
        ).fetchone()
        victim_after = conn.execute(
            "SELECT status,generation FROM vault_sync_issues WHERE id=?",
            (victim_issue["id"],),
        ).fetchone()
    assert json.loads(revision["metadata_json"])[SYNC_ISSUE_REFS_METADATA_KEY] == []
    assert SYNC_ISSUE_REFS_METADATA_KEY not in parse_wiki_bytes(
        target.read_bytes()
    ).frontmatter
    assert tuple(victim_after) == ("open", 1)


@pytest.mark.parametrize(
    "bad_refs",
    [
        {},
        [{"id": 7, "generation": 1}],
        [{"id": "visi_1", "generation": True}],
        [{"id": "visi_1", "generation": 0}],
        [{"id": "visi_1", "generation": 1, "extra": "forged"}],
        [
            {"id": "visi_1", "generation": 1},
            {"id": "visi_1", "generation": 1},
        ],
    ],
)
def test_sync_issue_revision_metadata_codec_rejects_corruption(bad_refs):
    with pytest.raises(RevisionConflict, match="sync issue metadata is invalid"):
        _decode_sync_issue_refs(
            {SYNC_ISSUE_REFS_METADATA_KEY: bad_refs}
        )


def test_finalize_rejects_cross_path_sync_issue_metadata_before_page_cas(
    tmp_path, monkeypatch
):
    settings = make_settings(tmp_path)
    init_app_db(settings)
    service = WikiRevisionService(settings)
    repair_path = "wiki/product/finalize-metadata-repair.md"
    repair_target, repair_issue = create_real_invalid_issue(
        service, "metadata-repair-invalid", repair_path
    )
    _, victim_issue = create_real_invalid_issue(
        service, "metadata-victim-invalid", "wiki/product/metadata-victim.md"
    )
    repair_target.write_bytes(
        external_page_bytes(
            title="Metadata repair",
            source_ids=(),
            domain="product",
            body="Valid repaired content",
        )
    )
    installed = prepare_real_repair_without_finalize(
        monkeypatch,
        service,
        "metadata-repair-valid",
        repair_path,
        repair_target,
    )
    with connect_app(settings) as conn:
        revision = dict(conn.execute(
            """
            SELECT r.id,r.metadata_json,i.page_id
            FROM vault_write_intents i
            JOIN wiki_page_revisions r ON r.id=i.revision_id
            WHERE i.id=?
            """,
            (installed["intent_id"],),
        ).fetchone())
        metadata = json.loads(revision["metadata_json"])
        metadata[SYNC_ISSUE_REFS_METADATA_KEY] = [
            {"id": victim_issue["id"], "generation": 1}
        ]
        conn.execute(
            "UPDATE wiki_page_revisions SET metadata_json=? WHERE id=?",
            (json.dumps(metadata, sort_keys=True, separators=(",", ":")), revision["id"]),
        )
        page_before = dict(conn.execute(
            """
            SELECT current_revision_id,projection_epoch,rag_visible_revision_id,
                   rag_visible_epoch,pending_write_intent_id
            FROM wiki_pages WHERE page_id=?
            """,
            (revision["page_id"],),
        ).fetchone())
        event_before = dict(conn.execute(
            """
            SELECT status,result_revision_id,result_payload_json
            FROM vault_change_events WHERE id='metadata-repair-valid'
            """
        ).fetchone())
        jobs_before = conn.execute(
            "SELECT COUNT(*) FROM knowledge_projection_jobs WHERE page_id=?",
            (revision["page_id"],),
        ).fetchone()[0]
        issues_before = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id,status,generation,resolved_at FROM vault_sync_issues
                WHERE id IN (?,?) ORDER BY id
                """,
                (repair_issue["id"], victim_issue["id"]),
            ).fetchall()
        ]

    with pytest.raises(RevisionConflict, match="sync issue metadata is invalid"):
        service.finalize_intent(installed["intent_id"], installed["owner"])

    with connect_app(settings) as conn:
        page_after = dict(conn.execute(
            """
            SELECT current_revision_id,projection_epoch,rag_visible_revision_id,
                   rag_visible_epoch,pending_write_intent_id
            FROM wiki_pages WHERE page_id=?
            """,
            (revision["page_id"],),
        ).fetchone())
        event_after = dict(conn.execute(
            """
            SELECT status,result_revision_id,result_payload_json
            FROM vault_change_events WHERE id='metadata-repair-valid'
            """
        ).fetchone())
        jobs_after = conn.execute(
            "SELECT COUNT(*) FROM knowledge_projection_jobs WHERE page_id=?",
            (revision["page_id"],),
        ).fetchone()[0]
        issues_after = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id,status,generation,resolved_at FROM vault_sync_issues
                WHERE id IN (?,?) ORDER BY id
                """,
                (repair_issue["id"], victim_issue["id"]),
            ).fetchall()
        ]
    assert page_after == page_before
    assert event_after == event_before
    assert jobs_after == jobs_before
    assert issues_after == issues_before
```

- [ ] **Step 13: Run lifecycle/relocate/finalize tests and verify RED**

Run:

```powershell
python -m pytest tests/test_wiki_revisions.py -k "restore_creates or relocate_with_edit or exact_rename or finalize_external_revision_syncs or successful_repair or forge_reserved_sync_issue" -q
```

Expected: FAIL because restore currently reuses current bytes, relocate does not exist, finalize does not synchronize all fields or clear owner, and real repair metadata/CAS handling does not exist.

- [ ] **Step 14: Force a new external revision for lifecycle restoration**

Change the reuse decision inside `ingest_external_change()` to the following exact rule:

```python
reuse_current = (
    locked.page["lifecycle_status"] == "active"
    and current is not None
    and current["content"].encode("utf-8") == content
)
```

For `invalid` or `deleted`, always call:

```python
self._prepare_external_revision_locked(
    locked.conn,
    page=locked.page,
    event_id=event_id,
    observation=observed,
    parsed=parsed,
    payload_digest=payload_digest,
    force_new_revision=True,
    transition_kind="external_restore",
)
```

Preserve active-page loop suppression: an active page with the exact managed bytes remains an ignored/reused occurrence and creates no second revision.

- [ ] **Step 15: Implement atomic relocate and retain exact-byte rename**

Add the public API exactly as follows:

```python
def relocate_external_change(
    self,
    event_id: str,
    old_page_path: str,
    new_page_path: str,
    expected_page_id: str,
    observation: FileObservationInput,
) -> MutationResult:
```

Its transaction must:

1. Validate both canonical paths and bind `payload_digest` to operation, paths, expected ID, and observation payload.
2. Lock `wiki_pages` by `expected_page_id`; require `path == old_page_path`, no pending write, and no different page at `new_page_path`.
3. Require observation bytes to declare `expected_page_id`, pass all metadata/source validation, and differ from current revision bytes.
4. Insert/reuse one observation and one service-owned `kind='relocate'` event.
5. Atomically update `wiki_pages.path`, create one external revision at `new_page_path`, and prepare its write intent.
6. Persist terminal result only during `finalize_intent()`; a crash before commit leaves no path/revision/intent/event fragment.

Keep `rename_page()` audit-only and require target bytes to equal current revision bytes. Both methods use `result_payload_json` replay and reject event-ID payload changes.

- [ ] **Step 16: Revalidate revision sources, synchronize six page fields, and resolve captured issue generations**

In `finalize_intent()`, parse and validate the installed revision again. Before the page CAS, requery every `source_id` from that installed revision inside the same finalize write transaction. PostgreSQL must acquire a shared row lock; SQLite performs the ordinary select inside its already serialized write transaction:

```python
def _revalidate_revision_sources_for_finalize_locked(
    self,
    conn: Any,
    source_ids: list[str],
    *,
    current_revision_id: str | None,
) -> None:
    if not source_ids:
        return
    placeholders = ",".join("?" for _ in source_ids)
    lock_suffix = " FOR SHARE" if self.settings.database_backend == "postgres" else ""
    rows = conn.execute(
        f"SELECT id,status FROM sources WHERE id IN ({placeholders}){lock_suffix}",
        source_ids,
    ).fetchall()
    by_id = {str(row["id"]): str(row["status"]) for row in rows}
    invalid = [
        source_id
        for source_id in source_ids
        if by_id.get(source_id) != "active"
    ]
    if invalid:
        raise RevisionConflict(
            "revision sources are no longer active: " + ", ".join(invalid),
            current_revision_id=current_revision_id,
        )
```

Load the immutable revision list and require the installed bytes to agree with it before taking source locks:

```python
revision_source_ids = json.loads(revision["source_ids_json"] or "[]")
if revision_source_ids != content_source_ids:
    raise RevisionConflict(
        "installed revision source identity changed",
        current_revision_id=locked.page.get("current_revision_id"),
    )
self._revalidate_revision_sources_for_finalize_locked(
    locked.conn,
    revision_source_ids,
    current_revision_id=locked.page.get("current_revision_id"),
)
```

Run this after parsing the installed revision and validating its server metadata, but immediately before the page update below. Do not validate the earlier page snapshot or preparation-time list. A missing row and a non-`active` row are the same fail-closed conflict.

Then use one CAS update with no owner coalescing. Build the nullable predicate portably for SQLite and PostgreSQL:

```python
expected_revision_id = intent["expected_revision_id"]
expected_sql = (
    "current_revision_id IS NULL"
    if expected_revision_id is None
    else "current_revision_id=?"
)
expected_params = () if expected_revision_id is None else (expected_revision_id,)
advanced = locked.conn.execute(
    f"""
    UPDATE wiki_pages
    SET current_revision_id=?,file_hash=?,semantic_hash=?,last_write_token=?,
        projection_epoch=?,rag_visible_revision_id=NULL,rag_visible_epoch=NULL,
        lifecycle_status='active',deleted_at=NULL,sync_error=NULL,
        observed_file_hash=?,pending_write_intent_id=NULL,
        title=?,domain=?,page_type=?,source_ids_json=?,review_status=?,owner=?,updated_at=?
    WHERE page_id=? AND pending_write_intent_id=? AND {expected_sql}
    """,
    (
        revision["id"], revision["file_hash"], revision["semantic_hash"],
        intent["write_token"], next_epoch, observed_file_hash,
        content_title, content_domain, content_page_type,
        json_dump(content_source_ids), content_review_status, content_owner,
        now_iso(), intent["page_id"], intent_id, *expected_params,
    ),
)
```

Never use `IS ?`; PostgreSQL accepts `IS NULL` for null and `=?` for a value.

The source lock and page CAS define the winner. If a source deactivate/delete transaction obtains its row lock first, finalize waits at `FOR SHARE`, observes the committed missing/inactive source, and rolls back without advancing the page, issue, outbox, or revision event. If finalize obtains all source share locks first and commits the page CAS, a source writer waits until that commit; the finalized page therefore had only active sources at its linearization point. The page CAS still arbitrates concurrent page writers after source validation.

Define one reserved internal metadata key at module scope. It belongs only to server-generated `wiki_page_revisions.metadata_json`; it is not a managed or user-editable frontmatter field:

```python
SYNC_ISSUE_REFS_METADATA_KEY = "_lgdo_sync_issue_refs"


def _capture_sync_issue_refs_locked(
    conn: Any, page_path: str
) -> list[dict[str, int | str]]:
    rows = conn.execute(
        """
        SELECT id,generation FROM vault_sync_issues
        WHERE page_path=? AND status='open'
        ORDER BY id
        """,
        (page_path,),
    ).fetchall()
    return [
        {"id": str(row["id"]), "generation": int(row["generation"])}
        for row in rows
    ]


def _invalid_sync_issue_metadata() -> RevisionConflict:
    return RevisionConflict(
        "revision sync issue metadata is invalid",
        current_revision_id=None,
    )


def _decode_sync_issue_refs(
    metadata: Any,
) -> tuple[tuple[str, int], ...]:
    if type(metadata) is not dict:
        raise _invalid_sync_issue_metadata()
    raw_refs = metadata.get(SYNC_ISSUE_REFS_METADATA_KEY, [])
    if type(raw_refs) is not list:
        raise _invalid_sync_issue_metadata()
    seen: set[str] = set()
    refs: list[tuple[str, int]] = []
    for raw_ref in raw_refs:
        if type(raw_ref) is not dict or set(raw_ref) != {"id", "generation"}:
            raise _invalid_sync_issue_metadata()
        issue_id = raw_ref["id"]
        generation = raw_ref["generation"]
        if (
            type(issue_id) is not str
            or not issue_id
            or type(generation) is not int
            or generation < 1
            or issue_id in seen
        ):
            raise _invalid_sync_issue_metadata()
        seen.add(issue_id)
        refs.append((issue_id, generation))
    return tuple(refs)


def _validate_sync_issue_ref_paths_locked(
    conn: Any,
    page_path: str,
    refs: tuple[tuple[str, int], ...],
) -> None:
    if not refs:
        return
    placeholders = ",".join("?" for _ in refs)
    rows = conn.execute(
        f"SELECT id,page_path FROM vault_sync_issues WHERE id IN ({placeholders})",
        [issue_id for issue_id, _generation in refs],
    ).fetchall()
    by_id = {str(row["id"]): str(row["page_path"]) for row in rows}
    if any(by_id.get(issue_id) != page_path for issue_id, _generation in refs):
        raise _invalid_sync_issue_metadata()


def _resolve_sync_issue_cas_locked(
    conn: Any,
    page_path: str,
    issue_id: str,
    generation: int,
) -> bool:
    timestamp = now_iso()
    changed = conn.execute(
        """
        UPDATE vault_sync_issues
        SET status='resolved',resolved_at=?,last_seen_at=?
        WHERE id=? AND page_path=? AND status='open' AND generation=?
        """,
        (timestamp, timestamp, issue_id, page_path, generation),
    )
    return changed.rowcount == 1
```

During external preparation, set `revision_page_path` to the same already-normalized path written to `wiki_page_revisions.page_path` (`page_path` for create/modify/restore and `new_page_path` for relocate). Query `_capture_sync_issue_refs_locked(conn, revision_page_path)` in the same write transaction that creates the revision and assign the returned JSON list to `revision_metadata[SYNC_ISSUE_REFS_METADATA_KEY]`. Build `revision_metadata` only from service-owned fields; never merge this key from `ExternalDocumentInput.metadata`. Immediately after YAML parsing, call `document.frontmatter.pop(SYNC_ISSUE_REFS_METADATA_KEY, None)` before `_plain(document.frontmatter)` and before rendering managed bytes. The user-supplied value is discarded without being interpreted, copied to disk, or used for issue resolution.

During finalize, JSON-decode `revision["metadata_json"]`, call `_decode_sync_issue_refs()`, and call `_validate_sync_issue_ref_paths_locked(locked.conn, revision["page_path"], issue_refs)` before the page CAS. Malformed types, extra keys, duplicate IDs, non-positive or Boolean generations, missing issues, and cross-path references fail before any page/outbox/event advancement. After `advanced.rowcount == 1`, call `_resolve_sync_issue_cas_locked()` for each captured tuple and only then create outbox rows and finish the revision event, all in the same transaction. Finalize must not call `VaultEventStore.resolve_issue()`, because that standalone API opens a separate transaction. A matching open generation resolves; an issue whose generation changed after preparation remains open while the valid page finalize still commits. A page CAS loser raises before resolution, and any later failure rolls the issue updates back with the page update.

Build outbox payloads from the six updated values, not the pre-finalize `locked.page` snapshot. `owner=None` must write SQL NULL.

- [ ] **Step 17: Run all focused SQLite revision tests**

Run:

```powershell
python -m pytest tests/test_wiki_revisions.py tests/test_wiki_schema_migration.py -q
```

Expected: PASS with no new placeholder page, duplicate revision, stale issue resolution, or changed existing conflict behavior.

- [ ] **Step 18: Add PostgreSQL and crash/concurrency contract tests**

Append these named gates to `tests/test_wiki_revision_postgres.py` using the existing `postgres_settings` fixture and thread-pool pattern. Import `threading`, `PgCompatConnection`, `connect_app_write`, `json_dump`, and `IntentExecutor` alongside the file's existing revision test imports:

```python
def test_postgres_external_schema_has_payload_issue_and_partial_indexes(postgres_settings):
    with connect_postgres(postgres_settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name='vault_change_events'
            """
        )
        columns = {row[0] for row in cur.fetchall()}
        cur.execute("SELECT indexname FROM pg_indexes WHERE tablename='vault_sync_issues'")
        indexes = {row[0] for row in cur.fetchall()}
    assert {"payload_digest", "result_payload_json"} <= columns
    assert "idx_vault_sync_issue_open_identity" in indexes


def test_concurrent_postgres_external_create_replays_one_event_without_duplicates(
    postgres_settings,
):
    settings = postgres_settings
    page_path = "wiki/product/concurrent-external.md"
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        b"---\ntitle: Concurrent external\nsource_ids: []\n"
        b"domain: product\npage_type: feature\nreview_status: draft\nowner:\n"
        b"---\n# Concurrent external\nBody\n"
    )
    observation = capture_file_observation(
        target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
    )

    def ingest(_worker: int):
        return WikiRevisionService(settings).ingest_external_change(
            "pg-create-shared-event", page_path, observation
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(ingest, (1, 2)))
    with connect_postgres(settings) as conn, conn.cursor() as cur:
        cur.execute("SELECT page_id FROM wiki_pages WHERE path=%s", (page_path,))
        pages = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM wiki_page_revisions WHERE page_path=%s", (page_path,))
        revisions = cur.fetchone()[0]
        cur.execute(
            """
            SELECT COUNT(*) FROM vault_write_intents i
            JOIN wiki_page_revisions r ON r.id=i.revision_id WHERE r.page_path=%s
            """,
            (page_path,),
        )
        intents = cur.fetchone()[0]
    assert len(pages) == 1
    assert revisions == 1
    assert intents == 1
    assert [result.status for result in outcomes] == ["applied", "applied"]
    assert sorted(result.replayed for result in outcomes) == [False, True]
    assert len({result.page_id for result in outcomes}) == 1


@pytest.mark.parametrize("source_mutation", ["deactivate", "delete"])
def test_postgres_source_mutation_wins_against_finalize_without_advancing_state(
    monkeypatch, postgres_settings, source_mutation
):
    settings = postgres_settings
    suffix = source_mutation
    source_id = f"src_finalize_race_{suffix}"
    page_id = f"page_finalize_race_{suffix}"
    page_path = f"wiki/product/finalize-source-race-{suffix}.md"
    event_id = f"pg-finalize-source-race-{suffix}"
    timestamp = "2026-07-15T00:00:00+00:00"
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\ntitle: Source race\n"
        f"source_ids: [{source_id}]\nlgdo_page_id: {page_id}\n"
        "domain: product\npage_type: policy\nreview_status: draft\nowner:\n"
        "---\n# Source race\nBefore finalize.\n",
        encoding="utf-8",
    )
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO sources(
              id,domain,title,source_type,original_path,raw_path,content_hash,
              size_bytes,status,metadata_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                source_id, "product", "Source race", "markdown", "obsidian",
                f"raw/product/{suffix}.md", "a" * 64, 1, "active", "{}",
                timestamp, timestamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,page_id,domain,page_type,title,source_ids_json,review_status,
              owner,created_at,updated_at,lifecycle_status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,'active')
            """,
            (
                page_path, page_id, "product", "policy", "Source race",
                json_dump([source_id]), "draft", None, timestamp, timestamp,
            ),
        )

    service = WikiRevisionService(settings)
    before = service.get_page(page_path)
    target.write_bytes(before.raw_bytes + b"\nPrepared external repair.\n")
    observation = capture_file_observation(
        target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
    )
    installed: dict[str, str] = {}

    def install_without_finalize(executor, intent_id):
        installed["intent_id"] = intent_id
        installed["owner"] = executor.owner
        assert executor.claim(intent_id, lease_seconds=30)
        executor.capture_and_install(intent_id, stop_after="installed")
        raise RuntimeError("stop after install before finalize")

    monkeypatch.setattr(IntentExecutor, "execute", install_without_finalize)
    with pytest.raises(RuntimeError, match="stop after install before finalize"):
        service.ingest_external_change(event_id, page_path, observation)

    with connect_app(settings) as conn:
        page_before_finalize = dict(conn.execute(
            """
            SELECT current_revision_id,projection_epoch,rag_visible_revision_id,
                   rag_visible_epoch,pending_write_intent_id
            FROM wiki_pages WHERE page_id=?
            """,
            (page_id,),
        ).fetchone())
        event_before_finalize = dict(conn.execute(
            """
            SELECT status,result_revision_id,result_payload_json
            FROM vault_change_events WHERE id=?
            """,
            (event_id,),
        ).fetchone())
        jobs_before_finalize = conn.execute(
            "SELECT COUNT(*) FROM knowledge_projection_jobs WHERE page_id=?",
            (page_id,),
        ).fetchone()[0]
    assert page_before_finalize["current_revision_id"] == before.current_revision_id
    assert page_before_finalize["pending_write_intent_id"] == installed["intent_id"]
    assert event_before_finalize == {
        "status": "prepared",
        "result_revision_id": None,
        "result_payload_json": None,
    }

    source_lock_held = threading.Event()
    release_source_mutation = threading.Event()
    share_lock_attempted = threading.Event()
    original_execute = PgCompatConnection.execute

    def observe_share_lock(self, query, params=None):
        normalized = " ".join(query.split())
        if (
            normalized.startswith("SELECT id,status FROM sources WHERE id IN")
            and normalized.endswith("FOR SHARE")
        ):
            share_lock_attempted.set()
        return original_execute(self, query, params)

    monkeypatch.setattr(PgCompatConnection, "execute", observe_share_lock)

    def mutate_source_first():
        with connect_app_write(settings) as conn:
            if source_mutation == "deactivate":
                changed = conn.execute(
                    "UPDATE sources SET status='inactive' WHERE id=?", (source_id,)
                )
            else:
                changed = conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
            assert changed.rowcount == 1
            source_lock_held.set()
            if not release_source_mutation.wait(timeout=10):
                raise AssertionError("source mutation was not released")

    with ThreadPoolExecutor(max_workers=2) as pool:
        mutation_future = pool.submit(mutate_source_first)
        assert source_lock_held.wait(timeout=10)
        finalize_future = pool.submit(
            service.finalize_intent,
            installed["intent_id"],
            installed["owner"],
        )
        try:
            assert share_lock_attempted.wait(timeout=10)
            assert not finalize_future.done()
        finally:
            release_source_mutation.set()
        mutation_future.result(timeout=20)
        with pytest.raises(RevisionConflict, match="sources are no longer active"):
            finalize_future.result(timeout=20)

    with connect_app(settings) as conn:
        page_after = dict(conn.execute(
            """
            SELECT current_revision_id,projection_epoch,rag_visible_revision_id,
                   rag_visible_epoch,pending_write_intent_id
            FROM wiki_pages WHERE page_id=?
            """,
            (page_id,),
        ).fetchone())
        event_after = dict(conn.execute(
            """
            SELECT status,result_revision_id,result_payload_json
            FROM vault_change_events WHERE id=?
            """,
            (event_id,),
        ).fetchone())
        jobs_after = conn.execute(
            "SELECT COUNT(*) FROM knowledge_projection_jobs WHERE page_id=?",
            (page_id,),
        ).fetchone()[0]
    assert page_after == page_before_finalize
    assert event_after == event_before_finalize
    assert jobs_after == jobs_before_finalize
```

Add one private no-op crash boundary to `WikiRevisionService` and call it at the two named transaction boundaries; do not add sleeps:

```python
def _fault(self, point: str) -> None:
    return None
```

Call `_fault("new_page_before_prepare_commit")` after all new-page page/revision/intent/event writes but before the write transaction exits. Call `_fault("relocate_after_prepare_commit_before_execute")` only after relocate preparation has committed and immediately before `IntentExecutor.execute()`. These positions distinguish rollback atomicity from crash-safe recovery.

Append the concrete SQLite gates to `tests/test_wiki_revisions.py`:

```python
class InjectedRevisionCrash(RuntimeError):
    pass


def test_new_page_precommit_crash_rolls_back_every_domain_row(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    init_app_db(settings)
    page_path = "wiki/product/precommit-crash.md"
    target = settings.vault_path / page_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(external_page_bytes(source_ids=()))
    observation = capture_file_observation(
        target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
    )
    service = WikiRevisionService(settings)

    def crash(point: str):
        if point == "new_page_before_prepare_commit":
            raise InjectedRevisionCrash(point)

    monkeypatch.setattr(service, "_fault", crash)
    with pytest.raises(InjectedRevisionCrash, match="new_page_before_prepare_commit"):
        service.ingest_external_change("new-page-crash", page_path, observation)

    with connect_app(settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM wiki_page_revisions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM vault_write_intents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM vault_change_events").fetchone()[0] == 0

    monkeypatch.setattr(service, "_fault", lambda point: None)
    applied = service.ingest_external_change("new-page-crash", page_path, observation)
    assert applied.status == "applied"


def test_relocate_postcommit_crash_leaves_one_recoverable_intent(
    legacy_page_fixture, monkeypatch
):
    service, before = legacy_page_fixture
    old_path = before.page_path
    new_path = "wiki/product/relocate-crash.md"
    old_target = service.settings.vault_path / old_path
    new_target = service.settings.vault_path / new_path
    new_target.parent.mkdir(parents=True, exist_ok=True)
    old_target.rename(new_target)
    new_target.write_bytes(before.raw_bytes + b"\nRelocate after commit.\n")
    observation = capture_file_observation(
        new_target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
    )

    def crash(point: str):
        if point == "relocate_after_prepare_commit_before_execute":
            raise InjectedRevisionCrash(point)

    monkeypatch.setattr(service, "_fault", crash)
    with pytest.raises(
        InjectedRevisionCrash,
        match="relocate_after_prepare_commit_before_execute",
    ):
        service.relocate_external_change(
            "relocate-postcommit-crash",
            old_path,
            new_path,
            before.page_id,
            observation,
        )

    with connect_app(service.settings) as conn:
        page = dict(conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?", (before.page_id,)
        ).fetchone())
        intent = dict(conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (page["pending_write_intent_id"],),
        ).fetchone())
        event = dict(conn.execute(
            "SELECT * FROM vault_change_events WHERE id='relocate-postcommit-crash'"
        ).fetchone())
    assert page["path"] == old_path
    assert intent["status"] in {"pending", "claimed", "installed"}
    assert event["status"] == "prepared"

    monkeypatch.setattr(service, "_fault", lambda point: None)
    IntentExecutor(service.settings).reconcile_all()
    replay = service.relocate_external_change(
        "relocate-postcommit-crash",
        old_path,
        new_path,
        before.page_id,
        observation,
    )
    assert replay.status == "applied"
    assert replay.replayed is True
    assert service.get_page(new_path).current_revision_id == replay.revision_id
```

Add this deterministic PostgreSQL race to `tests/test_wiki_revision_postgres.py`:

```python
def test_postgres_relocate_pending_intent_fences_manual_save(
    monkeypatch, postgres_settings
):
    settings = postgres_settings
    old_path = "wiki/product/relocate-race.md"
    new_path = "wiki/product/relocate-race-new.md"
    old_target = settings.vault_path / old_path
    new_target = settings.vault_path / new_path
    old_target.parent.mkdir(parents=True, exist_ok=True)
    old_target.write_text(
        "---\ntitle: Relocate race\nsource_ids: []\ndomain: product\n"
        "page_type: feature\nreview_status: draft\nowner:\n"
        "---\n# Relocate race\nBefore.\n",
        encoding="utf-8",
    )
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,domain,page_type,title,source_ids_json,review_status,
              created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (old_path, "product", "feature", "Relocate race", "[]", "draft", "t0", "t0"),
        )
    before = WikiRevisionService(settings).get_page(old_path)
    new_target.parent.mkdir(parents=True, exist_ok=True)
    old_target.rename(new_target)
    new_target.write_bytes(before.raw_bytes + b"\nEdited during relocate.\n")
    observation = capture_file_observation(
        new_target, max_content_bytes=1_000_000, prefix_bytes=64 * 1024
    )
    prepared = threading.Event()
    release = threading.Event()
    relocate_service = WikiRevisionService(settings)

    def block_after_prepare(point: str):
        if point != "relocate_after_prepare_commit_before_execute":
            return
        prepared.set()
        if not release.wait(timeout=10):
            raise AssertionError("manual-save race did not release relocate")

    monkeypatch.setattr(relocate_service, "_fault", block_after_prepare)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            relocate_service.relocate_external_change,
            "pg-relocate-race",
            old_path,
            new_path,
            before.page_id,
            observation,
        )
        try:
            assert prepared.wait(timeout=10)
            with connect_app(settings) as conn:
                pending_intent_id = conn.execute(
                    "SELECT pending_write_intent_id FROM wiki_pages WHERE page_id=?",
                    (before.page_id,),
                ).fetchone()["pending_write_intent_id"]
            with pytest.raises(RevisionConflict) as conflict:
                WikiRevisionService(settings).prepare_manual_save(
                    ManualSaveCommand(
                        page_path=old_path,
                        content=before.content + "\nManual loser.\n",
                        expected_revision_id=before.current_revision_id,
                        request_id="pg-manual-loser",
                        actor="postgres-test",
                        owner=None,
                        note=None,
                        review_status="draft",
                    ),
                    execute_intent=False,
                )
            assert conflict.value.pending_intent_id == pending_intent_id
        finally:
            release.set()
        relocated = future.result(timeout=20)

    assert relocated.status == "applied"
    with connect_app(settings) as conn:
        row = conn.execute(
            """
            SELECT p.path,p.current_revision_id,p.pending_write_intent_id,
                   r.origin,r.page_path
            FROM wiki_pages p
            JOIN wiki_page_revisions r ON r.id=p.current_revision_id
            WHERE p.page_id=?
            """,
            (before.page_id,),
        ).fetchone()
        external_count = conn.execute(
            """
            SELECT COUNT(*) FROM wiki_page_revisions
            WHERE page_id=? AND origin='external'
            """,
            (before.page_id,),
        ).fetchone()[0]
    assert tuple(row) == (
        new_path, relocated.revision_id, None, "external", new_path
    )
    assert external_count == 1
```

The race uses events, never timing sleeps.

- [ ] **Step 19: Run SQLite and opt-in PostgreSQL R0 gates**

Run:

```powershell
python -m pytest tests/test_wiki_revisions.py tests/test_wiki_schema_migration.py -q
python -m pytest tests/test_wiki_revision_postgres.py -k "external_schema or external_create or relocate" -q
```

Expected: SQLite PASS. PostgreSQL PASS when available; existing fixture-level skip is allowed only when the capability probe cannot connect. No database created by the test may be left owned after fixture teardown.

- [ ] **Step 20: Commit the external revision contract**

```powershell
git add app/db.py app/migration.py app/wiki_revisions.py tests/test_wiki_revisions.py tests/test_wiki_revision_postgres.py tests/test_wiki_schema_migration.py tests/test_sqlite_to_postgres_migration.py
git commit -m "feat: define external vault revision contracts"
```

---

### Task 1: Watcher Dependencies And Validated Configuration

**Files:**
- Modify: `pyproject.toml`
- Modify: `app/config.py`
- Modify: `.env.example`
- Modify: `tests/conftest.py`
- Create: `tests/test_vault_config.py`

- [ ] **Step 1: Write failing watcher configuration tests**

Create `tests/test_vault_config.py`:

```python
import pytest
from pydantic import ValidationError

from app.config import Settings


def test_vault_watcher_defaults_cover_stability_and_delete_grace():
    settings = Settings(_env_file=None)
    required = max(
        5000,
        settings.vault_watch_debounce_ms
        + int(settings.vault_watch_stability_timeout_seconds * 1000)
        + settings.vault_rename_safety_margin_ms,
    )
    assert settings.vault_watch_enabled is True
    assert settings.vault_watch_max_file_bytes == 5 * 1024 * 1024
    assert settings.vault_watch_max_prefix_bytes == 64 * 1024
    assert settings.vault_rename_grace_ms >= required
    assert settings.vault_watch_concurrency == 4
    assert settings.vault_reconcile_lease_seconds == 30
    assert settings.obsidian_vault_name is None


@pytest.mark.parametrize(
    "updates",
    [
        {"vault_watch_concurrency": 0},
        {"vault_watch_debounce_ms": 0},
        {"vault_watch_stability_timeout_seconds": 0},
        {"vault_rename_safety_margin_ms": -1},
        {"vault_rename_grace_ms": 0},
        {"vault_watch_max_file_bytes": 0},
        {"vault_watch_max_prefix_bytes": 0},
        {"vault_watch_max_file_bytes": 1024, "vault_watch_max_prefix_bytes": 2048},
        {"vault_reconcile_lease_seconds": 0},
        {
            "vault_watch_debounce_ms": 750,
            "vault_watch_stability_timeout_seconds": 3,
            "vault_rename_safety_margin_ms": 1500,
            "vault_rename_grace_ms": 5000,
        },
    ],
)
def test_vault_watcher_rejects_unsafe_configuration(updates):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **updates)
```

- [ ] **Step 2: Run configuration tests and verify RED**

Run: `python -m pytest tests/test_vault_config.py -q`

Expected: FAIL because watcher and Obsidian settings are not defined.

- [ ] **Step 3: Add dependencies and exact settings validation**

Add `watchfiles>=0.24.0` to project dependencies and `pytest-asyncio>=0.23.0` to the existing `dev` extra in `pyproject.toml`. Add this validator to `app/config.py`:

```python
from pydantic import model_validator


class Settings(BaseSettings):
    # existing fields remain unchanged
    vault_watch_enabled: bool = True
    vault_watch_debounce_ms: int = 750
    vault_watch_stability_timeout_seconds: float = 3.0
    vault_watch_max_file_bytes: int = 5 * 1024 * 1024
    vault_watch_max_prefix_bytes: int = 64 * 1024
    vault_rename_grace_ms: int = 5000
    vault_rename_safety_margin_ms: int = 1000
    vault_watch_concurrency: int = 4
    vault_reconcile_lease_seconds: int = 30
    obsidian_vault_name: str | None = None

    @model_validator(mode="after")
    def validate_vault_runtime(self) -> "Settings":
        if self.vault_watch_debounce_ms < 1:
            raise ValueError("vault_watch_debounce_ms must be positive")
        if self.vault_watch_stability_timeout_seconds <= 0:
            raise ValueError("vault_watch_stability_timeout_seconds must be positive")
        if self.vault_rename_safety_margin_ms < 0:
            raise ValueError("vault_rename_safety_margin_ms must be non-negative")
        if self.vault_rename_grace_ms < 1:
            raise ValueError("vault_rename_grace_ms must be positive")
        required_grace = max(
            5000,
            self.vault_watch_debounce_ms
            + int(self.vault_watch_stability_timeout_seconds * 1000)
            + self.vault_rename_safety_margin_ms,
        )
        if self.vault_rename_grace_ms < required_grace:
            raise ValueError(
                f"vault_rename_grace_ms must be at least {required_grace}"
            )
        if self.vault_watch_concurrency < 1:
            raise ValueError("vault_watch_concurrency must be at least 1")
        if self.vault_watch_max_file_bytes < 1:
            raise ValueError("vault_watch_max_file_bytes must be positive")
        if self.vault_watch_max_prefix_bytes < 1:
            raise ValueError("vault_watch_max_prefix_bytes must be positive")
        if self.vault_watch_max_prefix_bytes > self.vault_watch_max_file_bytes:
            raise ValueError("vault_watch_max_prefix_bytes cannot exceed max file bytes")
        if self.vault_reconcile_lease_seconds < 3:
            raise ValueError("vault_reconcile_lease_seconds must be at least 3")
        return self

    @property
    def effective_obsidian_vault_name(self) -> str:
        return self.obsidian_vault_name or self.vault_path.resolve().name
```

Document the exact uppercase keys in `.env.example`:

```dotenv
VAULT_WATCH_ENABLED=true
VAULT_WATCH_DEBOUNCE_MS=750
VAULT_WATCH_STABILITY_TIMEOUT_SECONDS=3
VAULT_WATCH_MAX_FILE_BYTES=5242880
VAULT_WATCH_MAX_PREFIX_BYTES=65536
VAULT_RENAME_GRACE_MS=5000
VAULT_RENAME_SAFETY_MARGIN_MS=1000
VAULT_WATCH_CONCURRENCY=4
VAULT_RECONCILE_LEASE_SECONDS=30
PROJECTION_WORKER_ENABLED=true
OBSIDIAN_VAULT_NAME=
```

- [ ] **Step 4: Disable background integrations in ordinary tests**

Extend the existing autouse fixture in `tests/conftest.py`; do not create a second autouse fixture:

```python
@pytest.fixture(autouse=True)
def disable_background_integrations(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "projection_worker_enabled", False)
    monkeypatch.setattr(settings, "gbrain_enabled", False)
    monkeypatch.setattr(settings, "vault_watch_enabled", False)
```

- [ ] **Step 5: Install and run the configuration gate**

Run:

```powershell
python -m pip install -e ".[dev]"
python -m pytest tests/test_vault_config.py -q
```

Expected: PASS, with no watcher or projection worker started by the test process.

- [ ] **Step 6: Commit configuration**

```powershell
git add pyproject.toml app/config.py .env.example tests/conftest.py tests/test_vault_config.py
git commit -m "feat: configure vault watcher runtime"
```

---

### Task 2: Bounded Round-Trip File Observation

**Files:**
- Create: `app/vault_watcher.py`
- Create: `tests/test_vault_watcher.py`
- Consume: `app/wiki_markdown.py`

- [ ] **Step 1: Write failing stability and bounded-capture tests**

Create `tests/test_vault_watcher.py`:

```python
import asyncio

import pytest

from app.vault_watcher import ObservationChanged, UnstableVaultFile, wait_for_stable_observation
from app.wiki_markdown import (
    FrontmatterLimits,
    capture_file_observation,
    parse_wiki_bytes,
    render_managed_frontmatter,
)


def test_stable_observation_preserves_comments_line_endings_and_raw_hash(tmp_path):
    page = tmp_path / "page.md"
    raw = (
        b"---\r\n# owner comment\r\ntitle: Refund\r\nsource_ids: []\r\n"
        b"domain: product\r\npage_type: policy\r\nreview_status: draft\r\n---\r\n# Refund\r\n"
    )
    page.write_bytes(raw)
    limits = FrontmatterLimits(max_file_bytes=1024 * 1024, max_prefix_bytes=64 * 1024)
    observation = asyncio.run(
        wait_for_stable_observation(page, limits, timeout_seconds=0.2, poll_interval=0)
    )
    document = parse_wiki_bytes(observation.content_bytes or b"", limits)
    rendered = render_managed_frontmatter(
        document, page_id="page_1", revision_id="wrev_1", write_token="write_1"
    )
    assert observation.content_bytes == raw
    assert observation.content_truncated is False
    assert b"# owner comment" in rendered
    assert len(observation.file_hash) == 64


def test_sparse_oversized_observation_keeps_only_bounded_prefix(tmp_path):
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


def test_unstable_file_times_out_before_capture(monkeypatch, tmp_path):
    page = tmp_path / "moving.md"
    page.write_text("first", encoding="utf-8")
    signatures = iter([(5, 1), (6, 2), (7, 3), (8, 4)])

    async def changing_signature(_path):
        return next(signatures)

    monkeypatch.setattr("app.vault_watcher._stat_signature", changing_signature)
    with pytest.raises(UnstableVaultFile, match="did not stabilize"):
        asyncio.run(
            wait_for_stable_observation(
                page, FrontmatterLimits(), timeout_seconds=0, poll_interval=0
            )
        )


def test_file_change_between_stat_and_capture_is_not_returned_as_stable(monkeypatch, tmp_path):
    page = tmp_path / "capture-race.md"
    page.write_text("first", encoding="utf-8")
    real_capture = capture_file_observation

    def changing_capture(path, *, max_content_bytes, prefix_bytes):
        path.write_text("second version", encoding="utf-8")
        return real_capture(
            path,
            max_content_bytes=max_content_bytes,
            prefix_bytes=prefix_bytes,
        )

    monkeypatch.setattr("app.vault_watcher.capture_file_observation", changing_capture)
    with pytest.raises(ObservationChanged, match="changed during capture"):
        asyncio.run(
            wait_for_stable_observation(
                page, FrontmatterLimits(), timeout_seconds=0.2, poll_interval=0
            )
        )
```

- [ ] **Step 2: Run observation tests and verify RED**

Run: `python -m pytest tests/test_vault_watcher.py -q`

Expected: FAIL because `app.vault_watcher` does not exist.

- [ ] **Step 3: Implement only the stability/capture boundary**

Create `app/vault_watcher.py` with this initial public surface:

```python
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from app.wiki_markdown import (
    FileObservationInput,
    FrontmatterLimits,
    ObservationChanged as CaptureObservationChanged,
    capture_file_observation,
)


class UnstableVaultFile(RuntimeError):
    pass


class ObservationChanged(UnstableVaultFile):
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
            try:
                observation = await asyncio.to_thread(
                    capture_file_observation,
                    path,
                    max_content_bytes=limits.max_file_bytes,
                    prefix_bytes=limits.max_prefix_bytes,
                )
            except CaptureObservationChanged as exc:
                previous = None
                if time.monotonic() >= deadline:
                    raise ObservationChanged(
                        f"file changed during capture: {path}"
                    ) from exc
                await asyncio.sleep(poll_interval)
                continue
            if (observation.size_bytes, observation.mtime_ns) == current:
                return observation
            previous = None
            if time.monotonic() >= deadline:
                raise ObservationChanged(f"file changed during capture: {path}")
            await asyncio.sleep(poll_interval)
            continue
        previous = current
        if time.monotonic() >= deadline:
            raise UnstableVaultFile(f"file did not stabilize before timeout: {path}")
        await asyncio.sleep(poll_interval)
```

Do not parse YAML or write files in this module. `capture_file_observation()` remains the single bounded byte reader.

- [ ] **Step 4: Run observation and parser regression tests**

Run:

```powershell
python -m pytest tests/test_vault_watcher.py tests/test_wiki_markdown.py -q
```

Expected: PASS; the oversized test does not allocate or persist the full sparse file.

- [ ] **Step 5: Commit bounded observation**

```powershell
git add app/vault_watcher.py tests/test_vault_watcher.py
git commit -m "feat: observe stable vault files safely"
```

---

### Task 3: Watcher Occurrences, Delete Grace, Sync Issues, And Reconcile Jobs

**Files:**
- Modify: `app/db.py`
- Modify: `app/migration.py`
- Create: `app/vault_events.py`
- Create: `tests/test_vault_persistence.py`
- Modify: `tests/test_sqlite_to_postgres_migration.py`
- Modify: `tests/test_wiki_revision_postgres.py`

- [ ] **Step 1: Write failing persistence ownership and uniqueness tests**

Create `tests/test_vault_persistence.py` with local settings plus these gates:

```python
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.db import connect_app, init_app_db
from app.vault_events import VaultEventStore, VaultOccurrenceConflict


def settings_for(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_backend="sqlite",
        database_path=tmp_path / "vault-events.db",
        vault_path=tmp_path / "vault",
        vault_watch_enabled=False,
        projection_worker_enabled=False,
    )


def test_watcher_occurrence_never_creates_revision_event(tmp_path):
    settings = settings_for(tmp_path)
    init_app_db(settings)
    store = VaultEventStore(settings)
    occurrence = store.begin_occurrence(
        "add", "wiki/product/new.md",
        detected_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
    )
    store.finish_occurrence(occurrence.id, "invalid", sync_issue_id="visi_1")
    with connect_app(settings) as conn:
        watcher_rows = conn.execute("SELECT COUNT(*) FROM vault_watch_occurrences").fetchone()[0]
        revision_rows = conn.execute("SELECT COUNT(*) FROM vault_change_events").fetchone()[0]
    assert watcher_rows == 1
    assert revision_rows == 0


@pytest.mark.parametrize(
    ("changed_kind", "changed_path", "changed_old_path"),
    [
        ("modify", "wiki/product/new.md", None),
        ("add", "wiki/product/other.md", None),
        ("add", "wiki/product/new.md", "wiki/product/old.md"),
    ],
)
def test_occurrence_begin_replays_same_payload_and_rejects_changed_payload(
    tmp_path, changed_kind, changed_path, changed_old_path
):
    settings = settings_for(tmp_path)
    init_app_db(settings)
    store = VaultEventStore(settings)
    occurrence_id = "vocc_stable_identity"
    first = store.begin_occurrence(
        "add", "wiki/product/new.md",
        detected_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
        occurrence_id=occurrence_id,
    )
    replay = store.begin_occurrence(
        "add", "wiki/product/new.md",
        detected_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
        occurrence_id=occurrence_id,
    )
    assert replay == first
    assert replay.detected_at == datetime(2026, 7, 15, tzinfo=timezone.utc)
    with pytest.raises(VaultOccurrenceConflict, match="payload changed"):
        store.begin_occurrence(
            changed_kind,
            changed_path,
            old_page_path=changed_old_path,
            detected_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
            occurrence_id=occurrence_id,
        )


def test_occurrence_finish_is_terminal_cas_and_exactly_idempotent(tmp_path):
    settings = settings_for(tmp_path)
    init_app_db(settings)
    store = VaultEventStore(settings)
    pending = store.begin_occurrence(
        "modify", "wiki/product/new.md",
        detected_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
        occurrence_id="vocc_terminal_cas",
    )
    for non_terminal in ("pending", "prepared"):
        with pytest.raises(VaultOccurrenceConflict, match="terminal status"):
            store.finish_occurrence(pending.id, non_terminal)
    assert store.finish_occurrence(
        pending.id,
        "applied",
        page_id="page_1",
        revision_id="wrev_1",
    ) is True
    assert store.finish_occurrence(
        pending.id,
        "applied",
        page_id="page_1",
        revision_id="wrev_1",
    ) is False
    with connect_app(settings) as conn:
        terminal_before_replay = dict(conn.execute(
            "SELECT * FROM vault_watch_occurrences WHERE id=?", (pending.id,)
        ).fetchone())
    replayed_terminal = store.begin_occurrence(
        "modify", "wiki/product/new.md",
        detected_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
        occurrence_id=pending.id,
    )
    assert replayed_terminal.status == "applied"
    with connect_app(settings) as conn:
        terminal_after_replay = dict(conn.execute(
            "SELECT * FROM vault_watch_occurrences WHERE id=?", (pending.id,)
        ).fetchone())
    assert terminal_after_replay == terminal_before_replay
    with pytest.raises(VaultOccurrenceConflict, match="terminal result changed"):
        store.finish_occurrence(
            pending.id,
            "applied",
            page_id="page_1",
            revision_id="wrev_2",
        )
    with connect_app(settings) as conn:
        row = conn.execute(
            "SELECT status,result_page_id,result_revision_id FROM vault_watch_occurrences WHERE id=?",
            (pending.id,),
        ).fetchone()
    assert tuple(row) == ("applied", "page_1", "wrev_1")


def test_duplicate_delete_occurrences_share_one_pending_absence_cycle(tmp_path):
    settings = settings_for(tmp_path)
    init_app_db(settings)
    store = VaultEventStore(settings)
    detected = datetime(2026, 7, 15, tzinfo=timezone.utc)
    first = store.get_or_create_pending_delete(
        occurrence_id="vocc_delete_a", page_id="page_1",
        old_page_path="wiki/product/a.md", file_hash="a" * 64,
        semantic_hash="b" * 64, detected_at=detected,
        expires_at=detected + timedelta(seconds=5),
    )
    second = store.get_or_create_pending_delete(
        occurrence_id="vocc_delete_b", page_id="page_1",
        old_page_path="wiki/product/a.md", file_hash="a" * 64,
        semantic_hash="b" * 64, detected_at=detected,
        expires_at=detected + timedelta(seconds=5),
    )
    assert first.id == second.id
    store.complete_delete(first.id)
    third = store.get_or_create_pending_delete(
        occurrence_id="vocc_delete_c", page_id="page_1",
        old_page_path="wiki/product/a.md", file_hash="a" * 64,
        semantic_hash="b" * 64, detected_at=detected + timedelta(seconds=10),
        expires_at=detected + timedelta(seconds=15),
    )
    assert third.id != first.id


def test_concurrent_reconcile_requests_return_one_persisted_job(tmp_path):
    settings = settings_for(tmp_path)
    init_app_db(settings)
    store = VaultEventStore(settings)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(lambda _: store.request_reconcile("admin"), range(2)))
    assert jobs[0].id == jobs[1].id
    assert jobs[0].status in {"queued", "running"}
```

The watcher occurrence payload is the canonical operation identity only: `kind`, canonical `page_path`, and canonical `old_page_path`. `detected_at` is audit timing and must never participate in identity. Import `hashlib`, `json`, and `uuid`, then define the digest and conflict contract in `app/vault_events.py`:

```python
TERMINAL_OCCURRENCE_STATUSES = frozenset(
    {
        "applied", "ignored", "invalid", "deferred", "deleted",
        "renamed", "resolved", "conflicted", "failed",
    }
)


class VaultOccurrenceConflict(RuntimeError):
    pass


def occurrence_payload_digest(
    kind: str,
    page_path: str,
    old_page_path: str | None,
) -> str:
    encoded = json.dumps(
        {
            "kind": kind,
            "old_page_path": old_page_path,
            "page_path": page_path,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
```

- [ ] **Step 2: Run persistence tests and verify RED**

Run: `python -m pytest tests/test_vault_persistence.py -q`

Expected: FAIL because the three watcher persistence tables, reconcile table, and store do not exist.

- [ ] **Step 3: Add exact watcher coordination DDL**

Add equivalent SQLite/PostgreSQL definitions to `app/db.py`:

```sql
CREATE TABLE IF NOT EXISTS vault_watch_occurrences (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  page_path TEXT NOT NULL,
  old_page_path TEXT,
  payload_digest TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  result_page_id TEXT,
  result_revision_id TEXT,
  sync_issue_id TEXT,
  error_summary TEXT,
  detected_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vault_watch_occurrence_pending_add
ON vault_watch_occurrences(kind, status, detected_at);

CREATE TABLE IF NOT EXISTS pending_vault_deletes (
  id TEXT PRIMARY KEY,
  occurrence_id TEXT NOT NULL UNIQUE,
  page_id TEXT NOT NULL,
  old_page_path TEXT NOT NULL,
  file_hash TEXT,
  semantic_hash TEXT,
  detected_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  matched_occurrence_id TEXT,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_vault_delete_due
ON pending_vault_deletes(status, expires_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_vault_delete_active_absence
ON pending_vault_deletes(page_id, old_page_path)
WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS vault_reconcile_jobs (
  id TEXT PRIMARY KEY,
  scope TEXT NOT NULL DEFAULT 'full',
  requested_by TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_expires_at TEXT,
  result_json TEXT,
  error_summary TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_vault_reconcile_single_flight
ON vault_reconcile_jobs(scope)
WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS idx_vault_reconcile_claim
ON vault_reconcile_jobs(status, lease_expires_at, created_at);
```

Preserve R0's existing `vault_sync_issues` entries in `MAIN_TABLES`/`TABLE_PRIMARY_KEYS`; Task 3 adds only `vault_watch_occurrences`, `pending_vault_deletes`, and `vault_reconcile_jobs`. Migration order keeps the already-registered issue table after R0's observation/event tables and before watcher occurrences that can reference its ID logically. Add a uniqueness assertion for `MAIN_TABLES` so a repeated entry fails immediately.

R0 already creates and registers `vault_sync_issues`; Task 3 must not emit a second table definition. Extend `app/migration.py::_KNOWN_COLUMNS` only with the exact column sets for `vault_watch_occurrences`, `pending_vault_deletes`, and `vault_reconcile_jobs`, including reconcile attempts/lease fields and watcher result fields. This is required because `_quote_identifier()` rejects any column absent from `_KNOWN_COLUMNS`.

- [ ] **Step 4: Implement the watcher persistence API without revision-event SQL**

Create `app/vault_events.py` with frozen row types and this public surface:

```python
@dataclass(frozen=True)
class VaultWatchOccurrence:
    id: str
    kind: str
    page_path: str
    old_page_path: str | None
    payload_digest: str
    status: str
    detected_at: datetime


@dataclass(frozen=True)
class PendingVaultDelete:
    id: str
    occurrence_id: str
    page_id: str
    old_page_path: str
    file_hash: str | None
    semantic_hash: str | None
    detected_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class VaultReconcileJob:
    id: str
    status: str
    requested_by: str
    attempts: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    result: dict[str, int] | None
    error_summary: str | None


```

The concrete `VaultEventStore` exposes these exact signatures:

- `begin_occurrence(self, kind: str, page_path: str, *, detected_at: datetime, old_page_path: str | None = None, occurrence_id: str | None = None) -> VaultWatchOccurrence`
- `finish_occurrence(self, occurrence_id: str, status: str, *, page_id: str | None = None, revision_id: str | None = None, sync_issue_id: str | None = None, error_summary: str | None = None) -> bool`
- `pending_occurrences(self) -> list[VaultWatchOccurrence]`
- `has_unclassified_add_before(self, cutoff: datetime) -> bool`
- `get_or_create_pending_delete(self, *, occurrence_id: str, page_id: str, old_page_path: str, file_hash: str | None, semantic_hash: str | None, detected_at: datetime, expires_at: datetime) -> PendingVaultDelete`
- `list_due_deletes(self, now: datetime) -> list[PendingVaultDelete]`
- `get_pending_delete(self, delete_id: str) -> PendingVaultDelete | None`
- `find_pending_delete_for_path(self, page_path: str) -> PendingVaultDelete | None`
- `find_pending_deletes(self, *, page_id: str) -> list[PendingVaultDelete]`
- `cancel_delete(self, delete_id: str, occurrence_id: str) -> bool`
- `complete_delete(self, delete_id: str) -> bool`
- `request_reconcile(self, requested_by: str) -> VaultReconcileJob`
- `claim_reconcile(self, job_id: str, owner: str, *, now: datetime, lease_seconds: int) -> bool`
- `renew_reconcile(self, job_id: str, owner: str, *, now: datetime, lease_seconds: int) -> bool`
- `finish_reconcile(self, job_id: str, owner: str, result: dict[str, int]) -> bool`
- `fail_reconcile(self, job_id: str, owner: str, error: str) -> bool`
- `requeue_reconcile(self, job_id: str, owner: str) -> bool`
- `get_reconcile(self, job_id: str) -> VaultReconcileJob | None`
- `active_reconcile(self) -> VaultReconcileJob | None`
- `latest_reconcile(self) -> VaultReconcileJob | None`
- `status_snapshot(self) -> dict[str, int | str | None]`

`begin_occurrence()` computes `occurrence_payload_digest(kind, page_path, old_page_path)`, generates `vocc_<uuid>` only when the caller omitted an ID, performs `INSERT ... ON CONFLICT DO NOTHING`, and selects the row by ID in the same write transaction. If the selected digest differs, raise `VaultOccurrenceConflict("watch occurrence payload changed")`. If it matches, return the selected row byte-for-byte without updating its status, timestamps, or result fields; therefore the same ID and payload are idempotent even when `detected_at` differs.

`finish_occurrence()` first rejects any status outside `TERMINAL_OCCURRENCE_STATUSES` with `VaultOccurrenceConflict("watch occurrence requires a terminal status")`. Normalize the requested terminal tuple as `(status, page_id, revision_id, sync_issue_id, error_summary)` and issue one `UPDATE ... WHERE id=? AND status='pending'`. Return `True` only when that CAS changes one row. On a zero-row CAS, reselect the row: return `False` only when all five stored result fields exactly equal the requested tuple; raise `VaultOccurrenceConflict("watch occurrence terminal result changed")` for every different terminal result, and raise `VaultOccurrenceConflict("watch occurrence does not exist")` when no row exists. The failed CAS path never overwrites the winner.

Every method uses parameterized SQL against only `vault_watch_occurrences`, `pending_vault_deletes`, `vault_sync_issues`, `vault_reconcile_jobs`, and read-only `wiki_pages`/intent queries. Add `test_vault_event_store_source_has_no_revision_event_sql`, which scans `inspect.getsource(VaultEventStore)` and asserts the string `vault_change_events` is absent.

`get_or_create_pending_delete()` uses this insert followed by a same-transaction select:

```sql
INSERT INTO pending_vault_deletes(
  id,occurrence_id,page_id,old_page_path,file_hash,semantic_hash,
  detected_at,expires_at,status,updated_at
) VALUES (?,?,?,?,?,?,?,?,'pending',?)
ON CONFLICT DO NOTHING;

SELECT * FROM pending_vault_deletes
WHERE occurrence_id=?
   OR (page_id=? AND old_page_path=? AND status='pending')
ORDER BY CASE WHEN occurrence_id=? THEN 0 ELSE 1 END,id
LIMIT 1;
```

`request_reconcile()` inserts a UUID job with `scope='full'` and `status='queued'` using `ON CONFLICT DO NOTHING`, then selects the one row where `scope='full' AND status IN ('queued','running')`. A missing selected row is a persistence error, not a second insert loop. A stale running row remains the same single-flight job ID and is reclaimable; it is never replaced by a second active row.

Claim uses one CAS statement:

```sql
UPDATE vault_reconcile_jobs
SET status='running',attempts=attempts+1,lease_owner=?,lease_expires_at=?,
    started_at=COALESCE(started_at,?),updated_at=?
WHERE id=?
  AND (
    status='queued'
    OR (status='running' AND (lease_expires_at IS NULL OR lease_expires_at<=?))
  );
```

Renew, finish, fail, and requeue all require matching `id`, `status='running'`, and `lease_owner`. Finish/fail clear lease fields and become terminal; cancellation requeues and clears lease fields. This prevents a former owner from completing after another process reclaims the job.

`latest_reconcile()` selects one row with `ORDER BY created_at DESC,id DESC LIMIT 1`. Implement `status_snapshot()` as a read-only transaction with portable aggregates, never by inspecting in-memory tasks:

```python
def status_snapshot(self) -> dict[str, int | str | None]:
    with connect_app(self.settings) as conn:
        occurrence = conn.execute(
            """
            SELECT
              SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
              MAX(updated_at) AS last_event_at
            FROM vault_watch_occurrences
            """
        ).fetchone()
        pending_deletes = conn.execute(
            "SELECT COUNT(*) FROM pending_vault_deletes WHERE status='pending'"
        ).fetchone()[0]
        open_issues = conn.execute(
            "SELECT COUNT(*) FROM vault_sync_issues WHERE status='open'"
        ).fetchone()[0]
        invalid_pages = conn.execute(
            "SELECT COUNT(*) FROM wiki_pages WHERE lifecycle_status='invalid'"
        ).fetchone()[0]
        latest_error = conn.execute(
            """
            SELECT error_summary FROM vault_watch_occurrences
            WHERE error_summary IS NOT NULL
            ORDER BY updated_at DESC,id DESC LIMIT 1
            """
        ).fetchone()
    return {
        "pending_occurrences": int(occurrence["pending"] or 0),
        "failed_occurrences": int(occurrence["failed"] or 0),
        "pending_deletes": int(pending_deletes),
        "open_issues": int(open_issues),
        "invalid_pages": int(invalid_pages),
        "last_event_at": occurrence["last_event_at"],
        "last_error": latest_error["error_summary"] if latest_error else None,
    }
```

- [ ] **Step 5: Add sync-issue CAS methods to the same store**

Expose R0's issue persistence without duplicating its identity or generation rules:

```python
@dataclass(frozen=True)
class SyncIssueRef:
    id: str
    generation: int


def list_open_issue_refs(self, page_path: str) -> tuple[SyncIssueRef, ...]:
    with connect_app(self.settings) as conn:
        rows = conn.execute(
            """
            SELECT id,generation FROM vault_sync_issues
            WHERE page_path=? AND status='open' ORDER BY id
            """,
            (page_path,),
        ).fetchall()
    return tuple(SyncIssueRef(str(row["id"]), int(row["generation"])) for row in rows)


def resolve_issue(self, issue: SyncIssueRef) -> bool:
    timestamp = now_iso()
    with connect_app(self.settings) as conn:
        changed = conn.execute(
            """
            UPDATE vault_sync_issues
            SET status='resolved',resolved_at=?,last_seen_at=?
            WHERE id=? AND generation=? AND status='open'
            """,
            (timestamp, timestamp, issue.id, issue.generation),
        )
    return changed.rowcount == 1
```

Keep the CAS predicate on both ID and generation.

Add `test_sync_issue_generation_cas_rejects_stale_resolver`: capture generation 1, observe the same open issue again so it becomes generation 2, assert `resolve_issue(SyncIssueRef(id, 1))` is false and status remains open, then assert generation 2 resolves exactly once.

- [ ] **Step 6: Add SQLite/PostgreSQL index and migration tests**

Extend `tests/test_vault_persistence.py` to assert both partial unique indexes from `PRAGMA index_list`. Extend `tests/test_wiki_revision_postgres.py` to query `pg_indexes` for the same names and add `test_postgres_concurrent_duplicate_deletes_share_one_pending_absence_cycle` plus the concurrent reconcile request test. Extend `tests/test_sqlite_to_postgres_migration.py` expected table metadata with all four new tables.

Append this populated migration test, reusing R0's `empty_migration_pair()`:

```python
@pytest.mark.skipif(not pg_available(), reason="PostgreSQL 5432 is not available")
def test_migrates_populated_vault_coordination_tables(tmp_path, monkeypatch):
    settings, sqlite_path = empty_migration_pair(
        tmp_path, monkeypatch, "lgdo_migration_vault_coordination_test"
    )
    t0 = "2026-07-15T00:00:00+00:00"
    rows = {
        "vault_sync_issues": (
            (
                "id", "page_path", "file_hash", "page_id", "issue_type",
                "error_summary", "status", "generation", "first_seen_at",
                "last_seen_at", "resolved_at",
            ),
            (
                "visi_coord", "wiki/product/old.md", "a" * 64, "page_coord",
                "watch_failure", "denied", "open", 2, t0, t0, None,
            ),
        ),
        "vault_watch_occurrences": (
            (
                "id", "kind", "page_path", "old_page_path", "payload_digest",
                "status", "result_page_id", "result_revision_id", "sync_issue_id",
                "error_summary", "detected_at", "updated_at",
            ),
            (
                "vwoc_coord", "delete", "wiki/product/old.md", None, "b" * 64,
                "pending", None, None, "visi_coord", None, t0, t0,
            ),
        ),
        "pending_vault_deletes": (
            (
                "id", "occurrence_id", "page_id", "old_page_path", "file_hash",
                "semantic_hash", "detected_at", "expires_at", "status",
                "matched_occurrence_id", "updated_at",
            ),
            (
                "vdel_coord", "vwoc_coord", "page_coord", "wiki/product/old.md",
                "c" * 64, "d" * 64, t0, "2026-07-15T00:00:05+00:00",
                "pending", None, t0,
            ),
        ),
        "vault_reconcile_jobs": (
            (
                "id", "scope", "requested_by", "status", "attempts", "lease_owner",
                "lease_expires_at", "result_json", "error_summary", "created_at",
                "started_at", "finished_at", "updated_at",
            ),
            (
                "vrec_coord", "full", "admin", "succeeded", 1, None, None,
                '{"ingested":2}', None, t0, t0, t0, t0,
            ),
        ),
    }
    with sqlite3.connect(sqlite_path) as conn:
        for table, (columns, values) in rows.items():
            placeholders = ",".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders})",
                values,
            )

    result = migrate_sqlite_to_postgres(settings, sqlite_path)

    with connect_postgres(settings) as conn, conn.cursor() as cur:
        for table, (columns, values) in rows.items():
            assert result["tables"][table] == 1
            cur.execute(
                f"SELECT {','.join(columns)} FROM {table} WHERE id=%s",
                (values[0],),
            )
            assert cur.fetchone() == values
```

An empty-table count test is insufficient.

Add `test_expired_running_reconcile_is_reclaimed_by_exactly_one_worker`: worker A claims at `T0`, worker B fails before lease expiry, then two worker threads attempt at `T0 + lease + 1 second`; exactly one succeeds, attempts becomes 2, and the winner alone may finish. Run the same CAS test on SQLite and PostgreSQL. Add `test_restart_returns_and_reclaims_same_single_flight_job_id` to prove a stale running row does not permanently block or produce a second job.

Add this store-level renewal gate on both backends; it tests renewal separately from stale reclaim:

```python
def test_reconcile_renewal_extends_lease_and_blocks_takeover(vault_event_store):
    store = vault_event_store
    t0 = datetime(2026, 7, 15, tzinfo=timezone.utc)
    job = store.request_reconcile("admin")
    assert store.claim_reconcile(job.id, "owner-a", now=t0, lease_seconds=30)
    assert store.renew_reconcile(
        job.id, "owner-a", now=t0 + timedelta(seconds=20), lease_seconds=30
    )
    assert not store.claim_reconcile(
        job.id, "owner-b", now=t0 + timedelta(seconds=31), lease_seconds=30
    )
    assert store.claim_reconcile(
        job.id, "owner-b", now=t0 + timedelta(seconds=51), lease_seconds=30
    )
    assert not store.finish_reconcile(job.id, "owner-a", {"ingested": 1})
    assert store.finish_reconcile(job.id, "owner-b", {"ingested": 1})
```

- [ ] **Step 7: Run persistence and migration gates**

Run:

```powershell
python -m pytest tests/test_vault_persistence.py tests/test_sqlite_to_postgres_migration.py -q
python -m pytest tests/test_wiki_revision_postgres.py -k "pending_vault_delete or vault_reconcile" -q
```

Expected: SQLite PASS. PostgreSQL PASS when available. Two distinct delete occurrences in one absence cycle share one pending row; two reconcile callers share one active job.

- [ ] **Step 8: Commit watcher persistence**

```powershell
git add app/db.py app/migration.py app/vault_events.py tests/test_vault_persistence.py tests/test_sqlite_to_postgres_migration.py tests/test_wiki_revision_postgres.py
git commit -m "feat: persist vault watcher coordination"
```

---

### Task 4: Canonical Live And Startup Filtering With Error Isolation

**Files:**
- Modify: `app/vault_watcher.py`
- Modify: `tests/test_vault_watcher.py`

- [ ] **Step 1: Write failing canonical-filter and adapter-survival tests**

Append to `tests/test_vault_watcher.py`:

```python
import os
import subprocess
from pathlib import Path

from watchfiles import Change

from app.config import Settings
from app.vault_watcher import (
    VaultFsEvent,
    VaultWatchAdapter,
    canonical_wiki_path,
    iter_canonical_wiki_files,
    normalize_watchfiles_batch,
)


def test_live_and_inventory_reject_hidden_and_parent_symlink_paths(tmp_path):
    vault = tmp_path / "vault"
    wiki = vault / "wiki"
    valid = wiki / "product" / "page.md"
    hidden = wiki / ".hidden" / "secret.md"
    valid.parent.mkdir(parents=True, exist_ok=True)
    hidden.parent.mkdir(parents=True, exist_ok=True)
    valid.write_text("# valid", encoding="utf-8")
    hidden.write_text("# hidden", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "linked.md").write_text("# linked", encoding="utf-8")
    link = wiki / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        link = None

    changes = {(Change.added, str(valid)), (Change.added, str(hidden))}
    if link is not None:
        changes.add((Change.added, str(link / "linked.md")))
    events = normalize_watchfiles_batch(changes, vault)
    inventory = [path.as_posix() for path in iter_canonical_wiki_files(vault)]
    assert events == [VaultFsEvent("add", "wiki/product/page.md")]
    assert inventory == [valid.as_posix()]


def test_wiki_root_reparse_and_resolve_errors_fail_closed(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    wiki = vault / "wiki"
    page = wiki / "page.md"
    wiki.mkdir(parents=True, exist_ok=True)
    page.write_text("# page", encoding="utf-8")
    monkeypatch.setattr(
        "app.vault_watcher._is_reparse_point",
        lambda path: path == wiki,
    )
    assert canonical_wiki_path(vault, page) is None
    assert list(iter_canonical_wiki_files(vault)) == []

    monkeypatch.setattr("app.vault_watcher._is_reparse_point", lambda path: False)
    original_resolve = Path.resolve

    def fail_candidate_resolve(self, *args, **kwargs):
        if self == page:
            raise OSError("resolve failed")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_candidate_resolve)
    assert canonical_wiki_path(vault, page) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics only")
def test_windows_junction_is_rejected_without_privilege_skip(tmp_path):
    vault = tmp_path / "vault"
    wiki = vault / "wiki"
    outside = tmp_path / "outside"
    wiki.mkdir(parents=True, exist_ok=True)
    outside.mkdir()
    (outside / "junction.md").write_text("# junction", encoding="utf-8")
    junction = wiki / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert normalize_watchfiles_batch(
        {(Change.added, str(junction / "junction.md"))}, vault
    ) == []
    assert list(iter_canonical_wiki_files(vault)) == []


@pytest.mark.asyncio
async def test_watch_adapter_survives_handler_error_and_processes_next_batch(tmp_path):
    batches = []
    calls = 0

    async def fake_awatch(path, **kwargs):
        page = Path(path) / "page.md"
        page.write_text("one", encoding="utf-8")
        yield {(Change.added, str(page))}
        page.write_text("two", encoding="utf-8")
        yield {(Change.modified, str(page))}

    async def handler(events):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first batch failed")
        batches.append(events)

    errors = []
    settings = Settings(_env_file=None, vault_path=tmp_path / "vault")
    (settings.vault_path / "wiki").mkdir(parents=True, exist_ok=True)
    adapter = VaultWatchAdapter(
        settings, handler, watch_factory=fake_awatch, error_handler=errors.append
    )
    await adapter.run(max_batches=2)
    assert len(errors) == 1
    assert batches == [[VaultFsEvent("modify", "wiki/page.md")]]


@pytest.mark.asyncio
async def test_watch_adapter_survives_normalization_error(tmp_path, monkeypatch):
    calls = 0
    handled = []
    errors = []

    async def fake_awatch(path, **kwargs):
        page = Path(path) / "page.md"
        page.write_text("one", encoding="utf-8")
        yield {(Change.added, str(page))}
        yield {(Change.modified, str(page))}

    real_normalize = normalize_watchfiles_batch

    def fail_once(changes, vault_path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("normalization denied")
        return real_normalize(changes, vault_path)

    monkeypatch.setattr("app.vault_watcher.normalize_watchfiles_batch", fail_once)
    settings = Settings(_env_file=None, vault_path=tmp_path / "vault")
    (settings.vault_path / "wiki").mkdir(parents=True, exist_ok=True)

    async def handler(events):
        handled.append(events)

    adapter = VaultWatchAdapter(
        settings, handler, watch_factory=fake_awatch, error_handler=errors.append
    )
    await adapter.run(max_batches=2)
    assert len(errors) == 1
    assert handled == [[VaultFsEvent("modify", "wiki/page.md")]]


def test_inventory_skips_unreadable_directory_and_continues(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    good = vault / "wiki/good/page.md"
    bad = vault / "wiki/bad"
    good.parent.mkdir(parents=True, exist_ok=True)
    bad.mkdir(parents=True, exist_ok=True)
    good.write_text("# good", encoding="utf-8")
    real_scandir = os.scandir

    def guarded_scandir(path):
        if Path(path) == bad:
            raise PermissionError("unreadable directory")
        return real_scandir(path)

    monkeypatch.setattr("app.vault_watcher.os.scandir", guarded_scandir)
    errors = []
    files = list(iter_canonical_wiki_files(vault, error_handler=lambda path, exc: errors.append((path, exc))))
    assert files == [good]
    assert len(errors) == 1
```

- [ ] **Step 2: Run canonical-filter tests and verify RED**

Run:

```powershell
python -m pytest tests/test_vault_watcher.py -k "hidden or junction or wiki_root or survives_handler" -q
```

Expected: FAIL because canonical traversal, reparse detection, normalized events, and adapter isolation are absent.

- [ ] **Step 3: Implement one canonical path classifier**

Add to `app/vault_watcher.py`:

```python
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_reparse_point(path: Path) -> bool:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return bool(getattr(stat, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def canonical_wiki_path(vault_path: Path, raw_path: str | Path) -> str | None:
    try:
        vault_root = vault_path.resolve()
        wiki_root = vault_root / "wiki"
        if wiki_root.is_symlink() or _is_reparse_point(wiki_root):
            return None
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = vault_root / candidate
        lexical = candidate.relative_to(vault_root)
    except (OSError, RuntimeError, ValueError):
        return None
    if not lexical.parts or lexical.parts[0] != "wiki":
        return None
    relative = Path(*lexical.parts[1:])
    if not relative.parts or any(part.startswith(".") for part in relative.parts):
        return None
    if relative.suffix.lower() != ".md":
        return None
    cursor = wiki_root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.exists() and (cursor.is_symlink() or _is_reparse_point(cursor)):
            return None
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(wiki_root.resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    if candidate.exists() and not candidate.is_file():
        return None
    return (Path("wiki") / relative).as_posix()


def iter_canonical_wiki_files(
    vault_path: Path,
    *,
    error_handler: Callable[[Path, Exception], None] | None = None,
) -> Iterator[Path]:
    report = error_handler or (lambda path, exc: None)
    wiki_root = vault_path / "wiki"
    if (
        not wiki_root.exists()
        or wiki_root.is_symlink()
        or _is_reparse_point(wiki_root)
    ):
        return
    stack = [wiki_root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name, reverse=True)
        except OSError as exc:
            report(directory, exc)
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                if entry.name.startswith(".") or entry.is_symlink() or _is_reparse_point(path):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                elif entry.is_file(follow_symlinks=False) and canonical_wiki_path(vault_path, path):
                    yield path
            except OSError as exc:
                report(path, exc)
```

Both live normalization and Task 6 startup inventory must call these functions; neither may use an independent `rglob()` filter.

- [ ] **Step 4: Implement normalized events and failure-isolated watch adapter**

Add:

```python
@dataclass(frozen=True, order=True)
class VaultFsEvent:
    kind: str
    page_path: str


def normalize_watchfiles_batch(
    changes: set[tuple[Change, str]], vault_path: Path,
) -> list[VaultFsEvent]:
    seen: dict[str, set[Change]] = {}
    absolute: dict[str, Path] = {}
    for change, raw_path in changes:
        page_path = canonical_wiki_path(vault_path, raw_path)
        if page_path is None:
            continue
        seen.setdefault(page_path, set()).add(change)
        absolute[page_path] = Path(raw_path)
    events = []
    for page_path in sorted(seen):
        path = absolute[page_path]
        if not path.exists():
            kind = "delete"
        elif Change.added in seen[page_path]:
            kind = "add"
        else:
            kind = "modify"
        events.append(VaultFsEvent(kind, page_path))
    return events


class VaultWatchAdapter:
    def __init__(self, settings, handler, *, watch_factory=awatch, error_handler=None):
        self.settings = settings
        self.handler = handler
        self.watch_factory = watch_factory
        self.error_handler = error_handler or (lambda exc: None)
        self._task: asyncio.Task[None] | None = None

    async def run(self, *, max_batches: int | None = None) -> None:
        handled = 0
        async for changes in self.watch_factory(
            self.settings.vault_path / "wiki",
            debounce=self.settings.vault_watch_debounce_ms,
            step=min(250, self.settings.vault_watch_debounce_ms),
            recursive=True,
        ):
            try:
                events = normalize_watchfiles_batch(changes, self.settings.vault_path)
                if not events:
                    continue
                await self.handler(events)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error_handler(exc)
            handled += 1
            if max_batches is not None and handled >= max_batches:
                return
```

Implement `start()` and `stop()` with one owned task, readiness event, cancellation, and `await` of the cancelled task. Do not suppress exceptions other than the expected `CancelledError` during cancellation.

- [ ] **Step 5: Run all watcher filtering and observation tests**

Run: `python -m pytest tests/test_vault_watcher.py -q`

Expected: PASS. On Windows, junction creation must succeed or fail the test with command output; do not broadly skip `OSError`.

- [ ] **Step 6: Commit canonical filtering**

```powershell
git add app/vault_watcher.py tests/test_vault_watcher.py
git commit -m "feat: filter vault events canonically"
```

---

### Task 5: Live Event Orchestration And Rename Evidence

**Files:**
- Create: `app/vault_sync.py`
- Modify: `app/vault_events.py`
- Create: `tests/test_vault_sync.py`

- [ ] **Step 1: Write failing delegation, rename-branch, and isolation tests**

Create `tests/test_vault_sync.py` with deterministic fakes and these tests:

```python
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.db import connect_app, init_app_db, json_dump
from app.vault_sync import VaultSyncService
from app.vault_watcher import VaultFsEvent


@dataclass(frozen=True)
class FakeResult:
    status: str
    page_id: str | None
    revision_id: str | None = None
    sync_issue_id: str | None = None


class FakeRevisionService:
    def __init__(self, fail_paths=()):
        self.calls = []
        self.fail_paths = set(fail_paths)

    def ingest_external_change(self, event_id, page_path, observation):
        self.calls.append(("ingest", event_id, page_path, observation.file_hash))
        if page_path in self.fail_paths:
            raise RuntimeError(f"failed {page_path}")
        return FakeResult("applied", "page_new", "wrev_new")

    def rename_page(self, event_id, old_page_path, new_page_path):
        self.calls.append(("rename", event_id, old_page_path, new_page_path))
        return FakeResult("renamed", "page_1", "wrev_rename")

    def relocate_external_change(
        self, event_id, old_page_path, new_page_path, expected_page_id, observation
    ):
        self.calls.append(
            ("relocate", event_id, old_page_path, new_page_path, expected_page_id, observation.file_hash)
        )
        return FakeResult("applied", expected_page_id, "wrev_external")

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
        database_path=tmp_path / "vault-sync.db",
        vault_path=tmp_path / "vault",
        vault_watch_enabled=False,
        projection_worker_enabled=False,
    )


def seed_page(settings, *, page_id="page_1", path="wiki/product/old.md", file_hash="a" * 64):
    init_app_db(settings)
    timestamp = "2026-07-15T00:00:00+00:00"
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(
              path,page_id,domain,page_type,title,source_ids_json,review_status,
              created_at,updated_at,current_revision_id,revision_number,file_hash,
              semantic_hash,projection_epoch,lifecycle_status
            ) VALUES (?,?, 'product','feature','Old',?,'draft',?,?, 'wrev_1',1,?,?,1,'active')
            """,
            (path, page_id, json_dump([]), timestamp, timestamp, file_hash, "b" * 64),
        )


def managed_bytes(page_id="page_1", body="# Old\n") -> bytes:
    return (
        "---\n"
        f"id: {page_id}\nlgdo_page_id: {page_id}\n"
        "lgdo_revision_id: wrev_1\ntitle: Old\nsource_ids: []\n"
        "domain: product\npage_type: feature\nreview_status: draft\n---\n"
        + body
    ).encode("utf-8")


def test_exact_pending_rename_uses_audit_only_api(tmp_path):
    settings = settings_for(tmp_path)
    exact = managed_bytes()
    seed_page(settings, file_hash=hashlib.sha256(exact).hexdigest())
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)
    detected = datetime(2026, 7, 15, tzinfo=timezone.utc)
    asyncio.run(service.handle_batch([VaultFsEvent("delete", "wiki/product/old.md")], detected_at=detected))
    new_path = settings.vault_path / "wiki/product/new.md"
    new_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.write_bytes(exact)
    asyncio.run(service.handle_batch([VaultFsEvent("add", "wiki/product/new.md")], detected_at=detected))
    assert [call[0] for call in revisions.calls] == ["rename"]


def test_pending_rename_with_edit_uses_atomic_relocate(tmp_path):
    settings = settings_for(tmp_path)
    original = managed_bytes()
    seed_page(settings, file_hash=hashlib.sha256(original).hexdigest())
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)
    detected = datetime(2026, 7, 15, tzinfo=timezone.utc)
    asyncio.run(service.handle_batch([VaultFsEvent("delete", "wiki/product/old.md")], detected_at=detected))
    new_path = settings.vault_path / "wiki/product/edited.md"
    new_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.write_bytes(managed_bytes(body="# Edited\n"))
    asyncio.run(service.handle_batch([VaultFsEvent("add", "wiki/product/edited.md")], detected_at=detected))
    assert [call[0] for call in revisions.calls] == ["relocate"]


@pytest.mark.parametrize("replacement", [managed_bytes(), managed_bytes(body="# Edited in place\n")])
def test_same_path_delete_add_is_atomic_save_not_rename(tmp_path, replacement):
    settings = settings_for(tmp_path)
    original = managed_bytes()
    seed_page(settings, file_hash=hashlib.sha256(original).hexdigest())
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)
    detected = datetime(2026, 7, 15, tzinfo=timezone.utc)
    asyncio.run(
        service.handle_batch(
            [VaultFsEvent("delete", "wiki/product/old.md")], detected_at=detected
        )
    )
    same_path = settings.vault_path / "wiki/product/old.md"
    same_path.parent.mkdir(parents=True, exist_ok=True)
    same_path.write_bytes(replacement)
    asyncio.run(
        service.handle_batch(
            [VaultFsEvent("add", "wiki/product/old.md")],
            detected_at=detected + timedelta(milliseconds=100),
        )
    )
    assert [call[0] for call in revisions.calls] == ["ingest"]
    assert service.snapshot()["pending_deletes"] == 0


def test_active_id_without_pending_delete_is_isolated_by_revision_service(tmp_path):
    settings = settings_for(tmp_path)
    seed_page(settings)
    new_path = settings.vault_path / "wiki/product/unproven.md"
    new_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.write_bytes(managed_bytes())
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)
    asyncio.run(service.handle_batch([VaultFsEvent("add", "wiki/product/unproven.md")]))
    assert [call[0] for call in revisions.calls] == ["ingest"]
    assert not any(call[0] in {"rename", "relocate"} for call in revisions.calls)


def test_one_event_failure_does_not_cancel_sibling_or_future_batch(tmp_path):
    settings = settings_for(tmp_path)
    bad = settings.vault_path / "wiki/product/bad.md"
    good = settings.vault_path / "wiki/product/good.md"
    later = settings.vault_path / "wiki/product/later.md"
    for path in (bad, good, later):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(external_page_bytes(source_ids=()))
    revisions = FakeRevisionService(fail_paths={"wiki/product/bad.md"})
    service = VaultSyncService(settings, revisions=revisions)
    asyncio.run(service.handle_batch([VaultFsEvent("add", "wiki/product/bad.md"), VaultFsEvent("add", "wiki/product/good.md")]))
    asyncio.run(service.handle_batch([VaultFsEvent("add", "wiki/product/later.md")]))
    assert {call[2] for call in revisions.calls if call[0] == "ingest"} == {
        "wiki/product/bad.md", "wiki/product/good.md", "wiki/product/later.md"
    }
    assert service.snapshot()["failed_occurrences"] == 1


def test_page_lock_registry_is_empty_after_many_unique_pages(tmp_path):
    settings = settings_for(tmp_path)
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)
    for index in range(100):
        path = settings.vault_path / f"wiki/product/page-{index}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(external_page_bytes(source_ids=()))
    asyncio.run(
        service.handle_batch(
            [VaultFsEvent("add", f"wiki/product/page-{index}.md") for index in range(100)]
        )
    )
    assert service._page_locks == {}
```

Import `hashlib` and define `external_page_bytes()` in this file with the same valid frontmatter shape used in R0; do not import test helpers from another test module.

- [ ] **Step 2: Run live orchestration tests and verify RED**

Run: `python -m pytest tests/test_vault_sync.py -k "pending_rename or active_id or event_failure" -q`

Expected: FAIL because `VaultSyncService` and its evidence-based branches do not exist.

- [ ] **Step 3: Add occurrence helpers and read-only identity lookups**

Extend `VaultEventStore` with parameterized reads only:

```python
def page_by_path(self, page_path: str) -> dict[str, Any] | None:
    with connect_app(self.settings) as conn:
        row = conn.execute("SELECT * FROM wiki_pages WHERE path=?", (page_path,)).fetchone()
    return dict(row) if row is not None else None


def has_active_intent(self, page_id: str) -> bool:
    with connect_app(self.settings) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM vault_write_intents
            WHERE page_id=? AND status IN ('pending','captured','installed','recovery_required')
            LIMIT 1
            """,
            (page_id,),
        ).fetchone()
    return row is not None
```

Keep the Task 3 source scan proving `VaultEventStore` contains no `vault_change_events` reference.

- [ ] **Step 4: Implement delete-first occurrence ordering**

Create `app/vault_sync.py` with constructor state, page locks, and `_queue_delete()`:

```python
class VaultSyncService:
    def __init__(self, settings, *, revisions=None, events=None, intent_executor=None):
        self.settings = settings
        self.revisions = revisions or WikiRevisionService(settings)
        self.events = events or VaultEventStore(settings)
        self.intent_executor = intent_executor or IntentExecutor(settings)
        self.limits = FrontmatterLimits(
            max_file_bytes=settings.vault_watch_max_file_bytes,
            max_prefix_bytes=settings.vault_watch_max_prefix_bytes,
        )
        self._semaphore = asyncio.Semaphore(settings.vault_watch_concurrency)
        self._page_locks: dict[str, PageLockEntry] = {}
        self._page_locks_guard = asyncio.Lock()
        self.running = False
        self.last_event_at: str | None = None
        self.last_error: str | None = None

    @asynccontextmanager
    async def _page_lock(self, key: str):
        async with self._page_locks_guard:
            entry = self._page_locks.get(key)
            if entry is None:
                entry = PageLockEntry(lock=asyncio.Lock(), users=0)
                self._page_locks[key] = entry
            entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            async with self._page_locks_guard:
                entry.users -= 1
                if entry.users == 0:
                    self._page_locks.pop(key, None)

    async def _queue_delete(self, occurrence, detected_at):
        page = self.events.page_by_path(occurrence.page_path)
        if page is None:
            self.events.finish_occurrence(occurrence.id, "ignored")
            return
        self.events.get_or_create_pending_delete(
            occurrence_id=occurrence.id,
            page_id=page["page_id"],
            old_page_path=occurrence.page_path,
            file_hash=page["file_hash"],
            semantic_hash=page["semantic_hash"],
            detected_at=detected_at,
            expires_at=detected_at + timedelta(milliseconds=self.settings.vault_rename_grace_ms),
        )
        self.events.finish_occurrence(occurrence.id, "deferred", page_id=page["page_id"])
```

`handle_batch()` must persist every normalized event as `vault_watch_occurrences`, queue all deletes before processing adds/modifies, and never create a `vault_change_events` row itself.

Define `PageLockEntry` as a mutable dataclass with `lock: asyncio.Lock` and `users: int`. Waiting callers increment `users` before acquiring, so an entry is removed only after the owner and all waiters exit.

- [ ] **Step 5: Implement exact rename versus edited relocate**

After stable observation, first handle same-path pending deletion. This is an editor atomic-save pattern, never a rename:

```python
same_path = self.events.find_pending_delete_for_path(occurrence.page_path)
if same_path is not None:
    async with self._page_lock(same_path.page_id):
        current_pending = self.events.get_pending_delete(same_path.id)
        absolute = self.settings.vault_path / occurrence.page_path
        if (
            current_pending is None
            or not absolute.exists()
            or self.events.has_active_intent(same_path.page_id)
        ):
            raise RevisionConflict(
                "same-path save evidence changed",
                current_revision_id=None,
            )
        arrived_before_deadline = occurrence.detected_at <= current_pending.expires_at
        self.events.cancel_delete(current_pending.id, occurrence.id)
        result = await asyncio.to_thread(
            self.revisions.ingest_external_change,
            occurrence.id, occurrence.page_path, observation,
        )
        self.events.finish_occurrence(
            occurrence.id, result.status, page_id=result.page_id,
            revision_id=result.revision_id, sync_issue_id=result.sync_issue_id,
            error_summary=None if arrived_before_deadline else "reappeared after delete grace",
        )
    return
```

Exact same-path bytes are suppressed by R0's active-page rule; edited bytes create an external revision. Lock scope rechecks path existence, pending status, active intent, and arrival deadline before cancellation/ingest.

For different paths, parse only enough frontmatter to read `id`/`lgdo_page_id`, then query pending-delete evidence. A missing stable page ID is never matched by hash and goes through ordinary `ingest_external_change()` validation. Use these exact branch conditions:

```python
candidates = self.events.find_pending_deletes(page_id=page_id) if page_id else []
if len(candidates) == 1:
    pending = candidates[0]
    async with self._page_lock(pending.page_id):
        if observation.file_hash == pending.file_hash:
            result = await asyncio.to_thread(
                self.revisions.rename_page,
                occurrence.id, pending.old_page_path, occurrence.page_path,
            )
        else:
            result = await asyncio.to_thread(
                self.revisions.relocate_external_change,
                occurrence.id, pending.old_page_path, occurrence.page_path,
                pending.page_id, observation,
            )
        if result.status in {"renamed", "applied", "ignored"}:
            self.events.cancel_delete(pending.id, occurrence.id)
        self.events.finish_occurrence(
            occurrence.id, result.status, page_id=result.page_id,
            revision_id=result.revision_id, sync_issue_id=result.sync_issue_id,
        )
    return
```

If there are multiple candidates, create an `ambiguous_rename` sync issue and finish only this occurrence as invalid. If there is no pending proof, call `ingest_external_change()`; R0 then rejects an unproven active ID without moving it.

- [ ] **Step 6: Isolate each event failure without swallowing cancellation**

Wrap each occurrence, not the entire batch:

```python
async def _handle_occurrence(self, occurrence, *, stability_poll_interval: float) -> None:
    try:
        await self._apply_file_occurrence(
            occurrence, stability_poll_interval=stability_poll_interval
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        self.last_error = str(exc)[:500]
        self.events.finish_occurrence(
            occurrence.id, "failed", error_summary=self.last_error
        )


await asyncio.gather(
    *(self._handle_occurrence(item, stability_poll_interval=stability_poll_interval)
      for item in occurrences if item.kind in {"add", "modify"})
)
```

The adapter remains alive because Task 4 also isolates handler-level failures. `snapshot()` reads counters from the persistent store, not in-memory task counts.

```python
def snapshot(self) -> dict[str, int | str | bool | None]:
    persisted = self.events.status_snapshot()
    return {
        **persisted,
        "running": self.running,
        "last_event_at": self.last_event_at or persisted["last_event_at"],
        "last_error": self.last_error or persisted["last_error"],
    }
```

- [ ] **Step 7: Run live orchestration and revision integration tests**

Run:

```powershell
python -m pytest tests/test_vault_sync.py -k "pending_rename or active_id or event_failure" -q
python -m pytest tests/test_wiki_revisions.py -k "rename or relocate or unproven_page_identity" -q
```

Expected: PASS. Exact rename leaves current unchanged; edited rename prepares one external revision; one failed occurrence does not cancel other work.

- [ ] **Step 8: Commit live orchestration**

```powershell
git add app/vault_events.py app/vault_sync.py tests/test_vault_sync.py
git commit -m "feat: orchestrate vault changes with rename evidence"
```

---

### Task 6: Delete Expiry And Startup Reconcile

**Files:**
- Modify: `app/vault_events.py`
- Modify: `app/vault_sync.py`
- Modify: `tests/test_vault_sync.py`

- [ ] **Step 1: Add failing expiry, replay, and startup-proof tests**

Append these named cases to `tests/test_vault_sync.py`:

```python
def test_delete_expiry_waits_for_predeadline_unclassified_add(tmp_path):
    settings = settings_for(tmp_path)
    seed_page(settings)
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)
    detected = datetime(2026, 7, 15, tzinfo=timezone.utc)
    delete = service.events.begin_occurrence("delete", "wiki/product/old.md", detected_at=detected)
    asyncio.run(service._queue_delete(delete, detected))
    service.events.begin_occurrence(
        "add", "wiki/product/slow.md", detected_at=detected + timedelta(seconds=4)
    )
    assert asyncio.run(service.expire_deletes(detected + timedelta(seconds=6))) == 0
    assert not any(call[0] == "delete" for call in revisions.calls)


def test_true_delete_rechecks_path_and_intent_then_applies(tmp_path):
    settings = settings_for(tmp_path)
    seed_page(settings)
    revisions = FakeRevisionService()
    service = VaultSyncService(settings, revisions=revisions)
    detected = datetime(2026, 7, 15, tzinfo=timezone.utc)
    asyncio.run(service.handle_batch([VaultFsEvent("delete", "wiki/product/old.md")], detected_at=detected))
    assert asyncio.run(service.expire_deletes(detected + timedelta(seconds=6))) == 1
    assert [call[0] for call in revisions.calls] == ["delete"]


def test_startup_unique_missing_path_is_explicit_offline_rename_evidence(tmp_path):
    settings = settings_for(tmp_path)
    original = managed_bytes()
    seed_page(settings, file_hash=hashlib.sha256(original).hexdigest())
    new_path = settings.vault_path / "wiki/product/offline.md"
    new_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.write_bytes(original)
    revisions = FakeRevisionService()
    result = asyncio.run(VaultSyncService(settings, revisions=revisions).reconcile_startup(stability_poll_interval=0))
    assert result["renamed"] == 1
    assert [call[0] for call in revisions.calls] == ["rename", "ensure_projection"]


def test_startup_edited_unique_move_uses_atomic_relocate(tmp_path):
    settings = settings_for(tmp_path)
    original = managed_bytes()
    seed_page(settings, file_hash=hashlib.sha256(original).hexdigest())
    new_path = settings.vault_path / "wiki/product/offline-edited.md"
    new_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.write_bytes(original + b"\nOffline edit.\n")
    revisions = FakeRevisionService()
    result = asyncio.run(
        VaultSyncService(settings, revisions=revisions).reconcile_startup(
            stability_poll_interval=0
        )
    )
    assert result["relocated"] == 1
    assert [call[0] for call in revisions.calls] == ["relocate", "ensure_projection"]


def test_startup_active_id_with_old_path_present_is_isolated(tmp_path):
    settings = settings_for(tmp_path)
    original = managed_bytes()
    seed_page(settings, file_hash=hashlib.sha256(original).hexdigest())
    old_path = settings.vault_path / "wiki/product/old.md"
    copied = settings.vault_path / "wiki/product/copied.md"
    copied.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_bytes(original)
    copied.write_bytes(original)
    revisions = FakeRevisionService()
    result = asyncio.run(VaultSyncService(settings, revisions=revisions).reconcile_startup(stability_poll_interval=0))
    assert result["duplicate_page_ids"] == 1
    assert not any(call[0] in {"rename", "relocate"} for call in revisions.calls)


def test_startup_missing_active_page_persists_grace_across_restart(tmp_path):
    settings = settings_for(tmp_path)
    seed_page(settings)
    revisions = FakeRevisionService()
    first = VaultSyncService(settings, revisions=revisions)
    result = asyncio.run(first.reconcile_startup(stability_poll_interval=0))
    assert result["missing"] == 1
    assert first.snapshot()["pending_deletes"] == 1
    assert not any(call[0] == "delete" for call in revisions.calls)

    restarted = VaultSyncService(settings, revisions=revisions)
    with connect_app(settings) as conn:
        expires_at = datetime.fromisoformat(
            conn.execute(
                "SELECT expires_at FROM pending_vault_deletes WHERE status='pending'"
            ).fetchone()["expires_at"]
        )
    assert asyncio.run(restarted.expire_deletes(expires_at + timedelta(seconds=1))) == 1
    assert [call[0] for call in revisions.calls].count("delete") == 1
    assert any(call[0] == "ensure_projection" for call in revisions.calls)


def test_startup_inventory_reuses_canonical_filter_and_survives_bad_file(tmp_path):
    settings = settings_for(tmp_path)
    valid = settings.vault_path / "wiki/product/valid.md"
    hidden = settings.vault_path / "wiki/.hidden/ignored.md"
    valid.parent.mkdir(parents=True, exist_ok=True)
    hidden.parent.mkdir(parents=True, exist_ok=True)
    valid.write_bytes(external_page_bytes(source_ids=()))
    hidden.write_bytes(external_page_bytes(source_ids=()))
    revisions = FakeRevisionService(fail_paths={"wiki/product/valid.md"})
    result = asyncio.run(VaultSyncService(settings, revisions=revisions).reconcile_startup(stability_poll_interval=0))
    assert result["failed"] == 1
    assert all(".hidden" not in call[2] for call in revisions.calls if call[0] == "ingest")
```

Import `timedelta`. Keep the existing test-local helpers from Task 5.

- [ ] **Step 2: Run expiry/startup tests and verify RED**

Run: `python -m pytest tests/test_vault_sync.py -k "delete_expiry or true_delete or startup_" -q`

Expected: FAIL because expiry, replay, canonical inventory, and startup proof classification are absent.

- [ ] **Step 3: Implement delete expiry with the persistent add barrier**

Add:

```python
async def expire_deletes(self, now: datetime | None = None) -> int:
    current_time = now or datetime.now(timezone.utc)
    applied = 0
    for pending in self.events.list_due_deletes(current_time):
        if self.events.has_unclassified_add_before(pending.expires_at):
            continue
        absolute = self.settings.vault_path / pending.old_page_path
        if absolute.exists() or self.events.has_active_intent(pending.page_id):
            continue
        async with self._lock(pending.page_id):
            if absolute.exists() or self.events.has_active_intent(pending.page_id):
                continue
            result = await asyncio.to_thread(
                self.revisions.delete_page, pending.occurrence_id, pending.old_page_path
            )
            if result.status in {"deleted", "ignored"}:
                self.events.complete_delete(pending.id)
                applied += 1
    return applied
```

The watcher occurrence and revision event may share the same opaque ID because they live in different tables; only `WikiRevisionService.delete_page()` writes the revision event.

- [ ] **Step 4: Replay pending watcher occurrences before inventory**

Implement `_replay_pending_occurrences()` so delete occurrences re-enter `_queue_delete()` and add/modify occurrences re-enter `_handle_occurrence()`. A missing add/modify path becomes terminal ignored. Replay reads `vault_watch_occurrences` only; the revision service independently replays its own event from `result_payload_json`.

Test by persisting an add occurrence, creating its file, constructing a second `VaultSyncService`, and asserting one ingest call plus zero pending watcher occurrences after startup.

- [ ] **Step 5: Implement canonical startup inventory and proof classification**

Use `iter_canonical_wiki_files()` from Task 4:

```python
async def _inventory(self, *, stability_poll_interval: float):
    entries = []
    for absolute in iter_canonical_wiki_files(
        self.settings.vault_path,
        error_handler=lambda path, exc: self._record_inventory_failure(
            path.relative_to(self.settings.vault_path).as_posix()
            if path.is_relative_to(self.settings.vault_path)
            else "wiki",
            exc,
        ),
    ):
        page_path = canonical_wiki_path(self.settings.vault_path, absolute)
        if page_path is None:
            continue
        try:
            observation = await wait_for_stable_observation(
                absolute, self.limits,
                timeout_seconds=self.settings.vault_watch_stability_timeout_seconds,
                poll_interval=stability_poll_interval,
            )
            page_id = self._frontmatter_page_id(observation)
            entries.append((page_path, observation, page_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_inventory_failure(page_path, exc)
    return entries
```

Classify identity before mutation:

- Duplicate disk ID, occupied original path, active write intent, invalid candidate, or multiple candidates: isolate and create issues; never move the page.
- Live rename proof is a unique pending delete from Task 5.
- Startup rename proof is exactly one valid candidate ID, the database old path absent in the same startup snapshot, no active intent, and no second disk path with that ID.
- Exact startup bytes call `rename_page`; edited startup bytes call `relocate_external_change` with the stable observation.
- Files with no ID call `ingest_external_change`; deleted pages reappearing at their canonical path also call ingest and R0 creates a restoration revision.
- An active/invalid database page whose path is absent from the complete startup snapshot and has no candidate creates a persisted watcher `delete` occurrence and one pending-delete row. It remains searchable during grace, survives restart, and calls `delete_page()` only after expiry rechecks path/intent/add barriers. The service-owned delete event then enqueues RAG/GBrain removal work.

Create a `vault_watch_occurrences` row for every automatic startup action so a crash can replay it. Do not move a page based only on an unproven live add.

- [ ] **Step 6: Implement startup ordering and drift result**

Add the public method:

```python
async def reconcile_startup(
    self, *, stability_poll_interval: float = 0.05, reconcile_intents: bool = True
) -> dict[str, int]:
    if reconcile_intents:
        await asyncio.to_thread(self.intent_executor.reconcile_all)
    replayed = await self._replay_pending_occurrences(
        stability_poll_interval=stability_poll_interval
    )
    inventory = await self._inventory(stability_poll_interval=stability_poll_interval)
    result = await self._reconcile_inventory(inventory, replayed_paths=replayed)
    projection_jobs = await asyncio.to_thread(self.revisions.ensure_projection_jobs, None)
    result["projection_jobs"] = len(projection_jobs)
    return result
```

Return integer keys `renamed`, `relocated`, `ingested`, `missing`, `replayed`, `duplicate_page_ids`, `failed`, and `projection_jobs`. Per-entry failures increment `failed` and do not terminate inventory reconciliation.

- [ ] **Step 7: Run full sync/revision/writer state-machine tests**

Run:

```powershell
python -m pytest tests/test_vault_sync.py tests/test_wiki_revisions.py tests/test_vault_writer.py -q
```

Expected: PASS. Slow pre-deadline add blocks expiry; restart replays occurrences; exact/edited offline rename follows the correct revision API; hidden/reparse paths never enter inventory.

- [ ] **Step 8: Commit expiry and startup reconcile**

```powershell
git add app/vault_events.py app/vault_sync.py tests/test_vault_sync.py
git commit -m "feat: reconcile vault deletes and offline changes"
```

---

### Task 7: Versioned Obsidian Assets And Safe Windows Commands

**Files:**
- Create: `app/obsidian.py`
- Modify: `app/vault.py`
- Modify: `app/wiki.py`
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
- Modify: `tests/test_internal_flow.py`

- [ ] **Step 1: Write failing asset, index-path, backup, atomic-write, and PowerShell tests**

Create `tests/test_obsidian_assets.py` with these named gates:

```python
import inspect
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.obsidian import build_obsidian_uri, ensure_obsidian_vault, main
from app.vault import ensure_vault


ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ROOT = ROOT / "resources" / "obsidian-vault"


def test_versioned_assets_exclude_workspace_and_plugin_state():
    app_config = json.loads((RESOURCE_ROOT / ".obsidian/app.json").read_text(encoding="utf-8"))
    plugins = json.loads((RESOURCE_ROOT / ".obsidian/core-plugins.json").read_text(encoding="utf-8"))
    template = (RESOURCE_ROOT / "templates/Wiki Page.md").read_text(encoding="utf-8")
    assert app_config["newFileFolderPath"] == "wiki"
    assert {"backlink", "outgoing-link", "tag-pane", "templates"} <= set(plugins)
    assert "lgdo_page_id" not in template
    assert "lgdo_revision_id" not in template
    assert not list(RESOURCE_ROOT.rglob("workspace*.json"))
    assert not (RESOURCE_ROOT / ".obsidian/plugins").exists()


def test_two_refreshes_in_one_second_use_distinct_backups(tmp_path, monkeypatch):
    vault = tmp_path / "LGDO Vault"
    config = vault / ".obsidian/app.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('{"newFileFolderPath":"custom-a"}\n', encoding="utf-8")
    first = ensure_obsidian_vault(vault, refresh=True)
    config.write_text('{"newFileFolderPath":"custom-b"}\n', encoding="utf-8")
    second = ensure_obsidian_vault(vault, refresh=True)
    assert first.backup_dir != second.backup_dir
    assert (first.backup_dir / ".obsidian/app.json").exists()
    assert (second.backup_dir / ".obsidian/app.json").exists()


def test_atomic_json_replace_failure_preserves_original(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    config = vault / ".obsidian/app.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    original = b'{"newFileFolderPath":"custom"}\n'
    config.write_bytes(original)

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr("app.obsidian.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        ensure_obsidian_vault(vault, refresh=True)
    assert config.read_bytes() == original
    assert not list(config.parent.glob(".app.json.*.tmp"))


POWERSHELL_HOSTS = [
    host
    for host in (("powershell.exe" if os.name == "nt" else None), "pwsh")
    if host is not None and shutil.which(host) is not None
]


@pytest.mark.parametrize("powershell_host", POWERSHELL_HOSTS)
def test_install_script_accepts_absolute_vault_path_from_other_cwd(
    tmp_path, powershell_host
):
    vault = (tmp_path / "Absolute Vault").resolve()
    result = subprocess.run(
        [
            powershell_host, "-NoProfile", "-File", str((ROOT / "scripts/install-obsidian-vault.ps1").resolve()),
            "-VaultPath", str(vault),
        ],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    )
    payload = json.loads(result.stdout)
    assert (vault / ".obsidian/app.json").exists()
    assert Path(payload["vault_path"]) == vault


def test_obsidian_uri_encodes_chinese_spaces_and_nested_path():
    uri = build_obsidian_uri("LGDO 知识库", "wiki/产品/退款 政策.md")
    assert uri.startswith("obsidian://open?")
    assert "%E7%9F%A5%E8%AF%86%E5%BA%93" in uri
    assert "%E9%80%80%E6%AC%BE%20%E6%94%BF%E7%AD%96.md" in uri


def test_obsidian_uri_encodes_all_reserved_characters():
    uri = build_obsidian_uri("LGDO #?&%", "wiki/product/a #?&%.md")
    assert all(encoded in uri for encoded in ("%23", "%3F", "%26", "%25"))
    assert "LGDO #" not in uri


def test_obsidian_cli_install_and_link_emit_json(tmp_path, capsys):
    vault = (tmp_path / "CLI Vault").resolve()
    assert main(["install", "--vault", str(vault)]) == 0
    install_payload = json.loads(capsys.readouterr().out)
    assert Path(install_payload["vault_path"]) == vault
    assert main([
        "link", "--vault-name", "LGDO #?&%", "--page-path", "wiki/product/a #?&%.md"
    ]) == 0
    link_payload = json.loads(capsys.readouterr().out)
    assert link_payload["url"].startswith("obsidian://open?")


def test_legacy_index_migration_moves_missing_and_deduplicates_identical(tmp_path):
    vault = tmp_path / "vault"
    legacy = vault / "index"
    current = vault / "indexes"
    legacy.mkdir(parents=True, exist_ok=True)
    current.mkdir(parents=True, exist_ok=True)
    (legacy / "missing.md").write_text("legacy-only", encoding="utf-8")
    (legacy / "same.md").write_text("same", encoding="utf-8")
    (current / "same.md").write_text("same", encoding="utf-8")

    ensure_vault(vault)

    assert (current / "missing.md").read_text(encoding="utf-8") == "legacy-only"
    assert (current / "same.md").read_text(encoding="utf-8") == "same"
    assert not legacy.exists()


def test_legacy_index_migration_quarantines_conflict_without_overwrite(tmp_path):
    vault = tmp_path / "vault"
    legacy = vault / "index/product.md"
    current = vault / "indexes/product.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    current.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("legacy", encoding="utf-8")
    current.write_text("current", encoding="utf-8")

    ensure_vault(vault)

    assert current.read_text(encoding="utf-8") == "current"
    quarantined = list((vault / ".lgdo/index-migration").glob("*/product.md"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "legacy"
    assert not legacy.exists()


def test_ensure_vault_does_not_install_obsidian_assets():
    assert "ensure_obsidian_vault" not in inspect.getsource(ensure_vault)


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell coverage")
def test_windows_powershell_open_script_reuses_python_uri(tmp_path):
    result = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-File",
            str((ROOT / "scripts/open-obsidian.ps1").resolve()),
            "-VaultName", "LGDO #?&%", "-PagePath", "wiki/product/a #?&%.md",
            "-PrintOnly",
        ],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    )
    assert result.stdout.strip() == build_obsidian_uri(
        "LGDO #?&%", "wiki/product/a #?&%.md"
    )
```

Extend `tests/test_internal_flow.py` with `test_compile_writes_indexes_plural_only`, asserting `vault/indexes/<domain>_index.md` exists and `vault/index/` does not.

- [ ] **Step 2: Run asset tests and verify RED**

Run:

```powershell
python -m pytest tests/test_obsidian_assets.py tests/test_internal_flow.py::test_compile_writes_indexes_plural_only -q
```

Expected: FAIL because assets/helpers/scripts are absent and compiler indexes still use `index/`.

- [ ] **Step 3: Add exact version-controlled Obsidian resources**

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

Create `.obsidian/core-plugins.json` with only:

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

Create `.obsidian/templates.json` pointing to `templates`, `templates/Wiki Page.md` with `title`, empty `source_ids`, `domain`, `page_type`, `review_status`, nullable `owner`, `tags`, and `aliases`, and `indexes/Home.md` linking Wiki domains, reviews, sync status, and logs. Do not include managed page/revision/write-token IDs in the template.

- [ ] **Step 4: Implement atomic merge, UUID backups, and URI construction**

Create `app/obsidian.py` with:

```python
RESOURCE_ROOT = Path(__file__).resolve().parents[1] / "resources" / "obsidian-vault"
MANAGED_JSON = (
    Path(".obsidian/app.json"),
    Path(".obsidian/core-plugins.json"),
    Path(".obsidian/templates.json"),
)
COPY_IF_MISSING = (
    Path("templates/Wiki Page.md"),
    Path("indexes/Home.md"),
    Path("README.md"),
)


@dataclass(frozen=True)
class ObsidianInstallResult:
    vault_path: Path
    installed: list[str]
    drifted: list[str]
    backup_dir: Path | None


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _new_backup_dir(vault_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = vault_path / ".lgdo/obsidian-backups" / f"{stamp}-{uuid.uuid4().hex}"
    backup.mkdir(parents=True, exist_ok=False)
    return backup


def build_obsidian_uri(vault_name: str, page_path: str) -> str:
    return "obsidian://open?" + urlencode(
        {"vault": vault_name, "file": page_path.replace("\\", "/")},
        quote_via=quote,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install")
    install.add_argument("--vault", type=Path, required=True)
    install.add_argument("--refresh", action="store_true")
    link = commands.add_parser("link")
    link.add_argument("--vault-name", required=True)
    link.add_argument("--page-path", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            result = ensure_obsidian_vault(args.vault.resolve(), refresh=args.refresh)
            payload = {
                "operation": "install",
                "vault_path": str(result.vault_path),
                "installed": result.installed,
                "drifted": result.drifted,
                "backup_dir": str(result.backup_dir) if result.backup_dir else None,
            }
        else:
            payload = {
                "operation": "link",
                "url": build_obsidian_uri(args.vault_name, args.page_path),
            }
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

`ensure_obsidian_vault()` copies missing resources, reports drift without overwrite when `refresh=False`, and when refreshing creates one unique backup directory, copies every changed target before replacement, merges only LGDO-managed JSON keys/core-plugin membership, then calls `_atomic_write_json()`.

- [ ] **Step 5: Fix all runtime index paths**

Change `app/vault.py::VAULT_DIRS` from `index` to `indexes` and add `templates`. `ensure_vault()` must not import or call `ensure_obsidian_vault()`; Obsidian installation is owned only by FastAPI lifespan and the explicit CLI.

Add `migrate_legacy_indexes(vault_path)` to `app/vault.py` and call it from `ensure_vault()` after `indexes/` exists. For every file beneath legacy `index/`: atomically move it to the same relative path under `indexes/` when the target is absent; delete the legacy copy when bytes are identical; when bytes differ, preserve the target and move the legacy file under `.lgdo/index-migration/<UTC timestamp>-<UUID>/`. Remove empty legacy directories. Never overwrite either version.

Add tests for all three migration branches and a source inspection assertion that `ensure_vault` contains no `ensure_obsidian_vault` reference. Change `app/wiki.py::write_indexes()` to:

```python
index_path = settings.vault_path / "indexes" / f"{domain}_index.md"
```

Search `app`, `tests`, `README.md`, and `README.zh-CN.md` for the literal runtime path `vault/index/` and update every remaining generated-index reference to `vault/indexes/`.

- [ ] **Step 6: Add rooted-path-safe PowerShell scripts**

Create `scripts/install-obsidian-vault.ps1`:

```powershell
param([string]$VaultPath = "vault", [switch]$Refresh)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if ([IO.Path]::IsPathRooted($VaultPath)) {
  $resolved = [IO.Path]::GetFullPath($VaultPath)
} else {
  $resolved = [IO.Path]::GetFullPath((Join-Path $root $VaultPath))
}
$arguments = @("-m", "app.obsidian", "install", "--vault", $resolved)
if ($Refresh) { $arguments += "--refresh" }
Push-Location $root
try { & python @arguments } finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
```

Create `scripts/open-obsidian.ps1` with mandatory `VaultName`, `PagePath`, and optional `PrintOnly`. It must call Python rather than duplicate URI encoding:

```powershell
param(
  [Parameter(Mandatory=$true)][string]$VaultName,
  [Parameter(Mandatory=$true)][string]$PagePath,
  [switch]$PrintOnly
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
  $json = & python -m app.obsidian link --vault-name $VaultName --page-path $PagePath
  if ($LASTEXITCODE -ne 0) { throw "Obsidian link generation failed" }
  $uri = ($json | ConvertFrom-Json).url
} finally { Pop-Location }
if ($PrintOnly) { Write-Output $uri; exit 0 }
try { Start-Process $uri -ErrorAction Stop | Out-Null }
catch { throw "Obsidian could not be opened. Install Obsidian and register the obsidian:// protocol. URI: $uri" }
```

Run the rooted install test with `pwsh` when available and the link test with built-in `powershell.exe` on Windows. Add `.gitignore` entries only for `workspace*.json`, `.obsidian/plugins/`, and `.trash/` beneath versioned resources; runtime `vault/` remains ignored.

- [ ] **Step 7: Run assets, PowerShell, and compiler index tests**

Run:

```powershell
python -m pytest tests/test_obsidian_assets.py tests/test_internal_flow.py::test_compile_writes_indexes_plural_only -q
scripts/open-obsidian.ps1 -VaultName "LGDO 知识库" -PagePath "wiki/产品/退款 政策.md" -PrintOnly
```

Expected: pytest PASS; the command prints one encoded `obsidian://open` URI. Repeated refresh backups are distinct and injected replace failure preserves original JSON bytes.

- [ ] **Step 8: Commit Obsidian distribution**

```powershell
git add app/obsidian.py app/vault.py app/wiki.py resources/obsidian-vault scripts/install-obsidian-vault.ps1 scripts/open-obsidian.ps1 .gitignore tests/test_obsidian_assets.py tests/test_internal_flow.py
git commit -m "feat: distribute safe Obsidian vault assets"
```

---

### Task 8: Lifespan, Persisted Reconcile, Status, And Authorized Deep Links

**Files:**
- Modify: `app/vault_sync.py`
- Modify: `app/projection_worker.py`
- Modify: `app/main.py`
- Modify: `app/models.py`
- Modify: `app/api.py`
- Create: `tests/test_vault_api.py`
- Modify: `tests/test_projection_worker.py`
- Modify: `tests/test_projection_api.py`
- Modify: `tests/test_wiki_revision_api.py`

- [ ] **Step 1: Write failing API authorization, single-flight, health, and cleanup tests**

Create `tests/test_vault_api.py` using `TestClient`, the current settings override pattern, and trusted headers:

```python
import json

import pytest
from fastapi.testclient import TestClient

import app.api as api_module
import app.main as main_module
from app.api import current_user
from app.auth import UserContext
from app.config import Settings
from app.db import connect_app, init_app_db
from app.main import app
from app.projection_jobs import ProjectionOutbox
from app.vault_sync import VaultSyncService


@pytest.fixture
def configured_client(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        database_backend="sqlite",
        database_path=tmp_path / "data/app.db",
        vault_path=tmp_path / "vault",
        upload_path=tmp_path / "uploads",
        vault_watch_enabled=False,
        projection_worker_enabled=False,
        gbrain_enabled=False,
        gbrain_endpoint=None,
        gbrain_projection_api_key=None,
        gbrain_managed_source_id=None,
    )
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "settings", settings)
    app.dependency_overrides[current_user] = lambda: UserContext(
        user_id="admin", role="admin", acl_tags=("*",)
    )
    try:
        with TestClient(app) as client:
            yield client, settings
    finally:
        app.dependency_overrides.pop(current_user, None)


@pytest.fixture
def vault_sync_service(tmp_path):
    settings = Settings(
        _env_file=None,
        database_backend="sqlite",
        database_path=tmp_path / "data/reconcile.db",
        vault_path=tmp_path / "vault",
        vault_watch_enabled=False,
        projection_worker_enabled=False,
        gbrain_enabled=False,
    )
    init_app_db(settings)
    return VaultSyncService(settings)


def test_reconcile_endpoint_is_persisted_single_flight(configured_client):
    client, settings = configured_client
    first = client.post("/api/internal/vault/reconcile")
    second = client.post("/api/internal/vault/reconcile")
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    with connect_app(settings) as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM vault_reconcile_jobs WHERE status IN ('queued','running')"
        ).fetchone()[0]
    assert active == 1


def test_disabled_gbrain_failed_backlog_is_raw_diagnostic_only(configured_client):
    client, settings = configured_client
    outbox = ProjectionOutbox(settings)
    with connect_app(settings) as conn:
        failed_id = outbox.enqueue(
            conn,
            target="gbrain",
            operation="upsert",
            page_id="page-disabled-failed",
            revision_id="wrev-disabled-failed",
            projection_epoch=1,
            payload={"path": "wiki/product/disabled-failed.md"},
        )
        conn.execute(
            """
            UPDATE knowledge_projection_jobs
            SET status='failed',attempts=5,last_error='disabled target failure'
            WHERE id=?
            """,
            (failed_id,),
        )
        outbox.enqueue(
            conn,
            target="gbrain",
            operation="upsert",
            page_id="page-disabled-pending",
            revision_id="wrev-disabled-pending",
            projection_epoch=1,
            payload={"path": "wiki/product/disabled-pending.md"},
        )

    status = client.get("/api/internal/vault/status").json()
    assert status["projection"]["gbrain"]["enabled"] is False
    assert status["projection"]["gbrain"]["failed"] == 1
    assert status["projection"]["gbrain"]["pending"] == 1
    assert status["projection"]["gbrain"]["degraded"] is False
    assert status["projection_backlog"] == 0
    assert status["configured"] is False
    assert status["running"] is False
    assert status["degraded"] is False


def test_vault_status_falls_back_without_state_or_absolute_path_leak(configured_client):
    client, settings = configured_client
    vault_sync = app.state.vault_sync
    obsidian_status = app.state.obsidian_status
    del app.state.vault_sync
    del app.state.obsidian_status
    try:
        response = client.get("/api/internal/vault/status")
    finally:
        app.state.vault_sync = vault_sync
        app.state.obsidian_status = obsidian_status

    assert response.status_code == 200
    body = response.json()
    assert body["running"] is False
    assert body["pending_occurrences"] == 0
    assert str(settings.vault_path.resolve()) not in json.dumps(body, ensure_ascii=False)
```

The fixture must patch both `app.api.get_settings` and `app.main.settings` before entering `with TestClient(app)`; constructing a bare client without the context manager does not run lifespan. Extend `tests/test_projection_api.py::projection_api` to use the same context-manager pattern and add `"enabled": True` to the existing exact enabled-GBrain health dictionary assertion.

Append the ACL behavior to `tests/test_wiki_revision_api.py`, reusing `api_content_conflict` so the source metadata already contains `{"acl_tags":["finance"]}`:

```python
def test_obsidian_link_missing_and_unauthorized_are_indistinguishable(
    api_content_conflict,
):
    client, conflict = api_content_conflict
    outsider = {
        "X-LGDO-User": "outsider",
        "X-LGDO-Role": "viewer",
        "X-LGDO-ACL-Tags": "support",
    }
    finance = {
        "X-LGDO-User": "finance_user",
        "X-LGDO-Role": "viewer",
        "X-LGDO-ACL-Tags": "finance",
    }
    missing = client.get(
        "/api/internal/wiki/pages/wiki/product/missing.md/obsidian-link",
        headers=finance,
    )
    hidden = client.get(
        f"/api/internal/wiki/pages/{conflict.encoded_path}/obsidian-link",
        headers=outsider,
    )
    visible = client.get(
        f"/api/internal/wiki/pages/{conflict.encoded_path}/obsidian-link",
        headers=finance,
    )

    expected = {"detail": {"code": "wiki_page_not_found"}}
    assert missing.status_code == hidden.status_code == 404
    assert missing.json() == hidden.json() == expected
    assert conflict.encoded_path not in hidden.text
    assert visible.status_code == 200
    assert visible.json()["url"].startswith("obsidian://open?")


def test_obsidian_link_maps_catalog_permission_error_to_hidden_404(
    api_content_conflict, monkeypatch
):
    client, conflict = api_content_conflict

    def deny(*args, **kwargs):
        raise PermissionError("do not disclose")

    monkeypatch.setattr("app.api.catalog.read_wiki_page", deny)
    response = client.get(
        f"/api/internal/wiki/pages/{conflict.encoded_path}/obsidian-link"
    )
    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "wiki_page_not_found"}}
```

Also add `("/api/internal/wiki/pages/{page_path:path}/obsidian-link", "GET")` to `specific_routes` in `test_specific_wiki_routes_are_registered_before_greedy_page_routes`. This is the explicit route-order regression gate.

Add `test_lifespan_cleans_both_runtime_owners_at_every_failure_point`, parameterized to make each of `intent_executor.reconcile_all`, `vault_sync.reconcile_before_watcher_start`, `projection_worker.start`, and `vault_sync.start` raise after recording entry. Assert `vault_sync.stop` and `projection_worker.stop` are both attempted exactly once for every failure point and no owned task remains running.

- [ ] **Step 2: Run API/lifespan tests and verify RED**

Run:

```powershell
python -m pytest tests/test_vault_api.py -q
python -m pytest tests/test_projection_worker.py tests/test_projection_api.py -k "gbrain or health" -q
```

Expected: FAIL because routes/models/lifespan do not exist and projection health marks disabled unconfigured GBrain degraded.

- [ ] **Step 3: Make GBrain projection health explicitly enabled-aware**

Change `projection_health()` in `app/projection_worker.py`:

```python
gbrain_enabled = bool(settings.gbrain_enabled)
gbrain_configured = bool(
    gbrain_enabled
    and settings.gbrain_endpoint
    and settings.gbrain_projection_api_key
    and settings.gbrain_managed_source_id
)
counts["gbrain"].update(
    {
        "enabled": gbrain_enabled,
        "configured": gbrain_configured,
        "degraded": bool(
            gbrain_enabled
            and (counts["gbrain"]["failed"] or not gbrain_configured)
        ),
    }
)
```

When computing Vault backlog/degraded, omit the GBrain target entirely when `enabled` is false. Keep its raw counts in the response for diagnostics, but they do not make Vault unhealthy while disabled.

- [ ] **Step 4: Add persisted reconcile execution to `VaultSyncService`**

Use Task 3 jobs as the source of truth:

```python
class ReconcileLeaseLost(RuntimeError):
    pass


def request_reconcile(self, requested_by: str) -> VaultReconcileJob:
    job = self.events.request_reconcile(requested_by)
    tasks = getattr(self, "_reconcile_tasks", {})
    self._reconcile_tasks = tasks
    if job.id not in tasks or tasks[job.id].done():
        tasks[job.id] = asyncio.create_task(
            self._run_reconcile_job(job.id), name=f"vault-reconcile-{job.id}"
        )
    return job


async def _renew_reconcile_lease(self, job_id: str) -> None:
    interval = self.settings.vault_reconcile_lease_seconds / 3
    while True:
        await asyncio.sleep(interval)
        renewed = await asyncio.to_thread(
            self.events.renew_reconcile,
            job_id,
            self._reconcile_owner,
            now=datetime.now(timezone.utc),
            lease_seconds=self.settings.vault_reconcile_lease_seconds,
        )
        if not renewed:
            raise ReconcileLeaseLost(f"vault reconcile lease lost: {job_id}")


async def _run_reconcile_job(self, job_id: str) -> None:
    async with self._reconcile_lock:
        while not await asyncio.to_thread(
            self.events.claim_reconcile,
            job_id,
            self._reconcile_owner,
            now=datetime.now(timezone.utc),
            lease_seconds=self.settings.vault_reconcile_lease_seconds,
        ):
            current = await asyncio.to_thread(self.events.get_reconcile, job_id)
            if current is None or current.status in {"succeeded", "failed"}:
                return
            await asyncio.sleep(0.25)
        work = asyncio.create_task(
            self.reconcile_startup(reconcile_intents=False),
            name=f"vault-reconcile-work-{job_id}",
        )
        heartbeat = asyncio.create_task(
            self._renew_reconcile_lease(job_id),
            name=f"vault-reconcile-lease-{job_id}",
        )
        try:
            done, _pending = await asyncio.wait(
                {work, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in done:
                lease_error = heartbeat.exception()
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
                if lease_error is None:
                    raise ReconcileLeaseLost(
                        f"vault reconcile heartbeat stopped: {job_id}"
                    )
                raise lease_error
            result = await work
        except asyncio.CancelledError:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            await asyncio.to_thread(
                self.events.requeue_reconcile, job_id, self._reconcile_owner
            )
            raise
        except ReconcileLeaseLost:
            raise
        except Exception as exc:
            failed = await asyncio.to_thread(
                self.events.fail_reconcile,
                job_id,
                self._reconcile_owner,
                str(exc),
            )
            if not failed:
                raise ReconcileLeaseLost(
                    f"vault reconcile failure lease lost: {job_id}"
                ) from exc
        else:
            finished = await asyncio.to_thread(
                self.events.finish_reconcile,
                job_id,
                self._reconcile_owner,
                result,
            )
            if not finished:
                raise RuntimeError(f"vault reconcile completion lease lost: {job_id}")
        finally:
            work.cancel()
            heartbeat.cancel()
            await asyncio.gather(work, heartbeat, return_exceptions=True)


async def reconcile_before_watcher_start(self) -> dict[str, int]:
    active = await asyncio.to_thread(self.events.active_reconcile)
    if active is not None:
        await self._run_reconcile_job(active.id)
        finished = await asyncio.to_thread(self.events.get_reconcile, active.id)
        if finished is None or finished.status != "succeeded" or finished.result is None:
            raise RuntimeError(f"persisted vault reconcile did not succeed: {active.id}")
        return finished.result
    async with self._reconcile_lock:
        return await self.reconcile_startup(reconcile_intents=False)
```

Initialize `_reconcile_lock = asyncio.Lock()`, `_reconcile_owner = f"vault-reconcile-{uuid.uuid4().hex}"`, and `_reconcile_tasks: dict[str, asyncio.Task[None]] = {}` in the constructor. The lock serializes untracked startup inventory and persisted jobs in one process; the database lease fences multiple processes. At shutdown, cancel owned reconcile tasks, await them with `return_exceptions=True`, and let cancellation requeue only when this owner still holds the lease. A terminal job permits the next request to create a new ID.

Add `test_startup_and_persisted_reconcile_share_one_global_authority`: create a queued job, run two `VaultSyncService` instances against one database, and assert only one fake inventory call executes, both callers observe the same succeeded job, and watcher start is recorded only after that job completes.

Add these two async lease gates to `tests/test_vault_api.py`:

```python
@pytest.mark.asyncio
async def test_reconcile_lease_loss_cancels_inventory_before_finish(
    vault_sync_service, monkeypatch
):
    service = vault_sync_service
    job = service.events.request_reconcile("admin")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_reconcile(*, reconcile_intents):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def lose_lease(job_id):
        await started.wait()
        raise ReconcileLeaseLost(f"vault reconcile lease lost: {job_id}")

    def forbidden_finish(*args, **kwargs):
        raise AssertionError("lost owner attempted to finish reconcile")

    monkeypatch.setattr(service, "reconcile_startup", blocked_reconcile)
    monkeypatch.setattr(service, "_renew_reconcile_lease", lose_lease)
    monkeypatch.setattr(service.events, "finish_reconcile", forbidden_finish)
    with pytest.raises(ReconcileLeaseLost, match="lease lost"):
        await service._run_reconcile_job(job.id)
    assert cancelled.is_set()
    assert service.events.get_reconcile(job.id).status == "running"


@pytest.mark.asyncio
async def test_reconcile_heartbeat_prevents_takeover_during_long_inventory(
    vault_sync_service, monkeypatch
):
    service = vault_sync_service
    monkeypatch.setattr(service.settings, "vault_reconcile_lease_seconds", 1)
    job = service.events.request_reconcile("admin")
    release = asyncio.Event()
    renewals = asyncio.Event()
    renewal_count = 0
    real_renew = service.events.renew_reconcile

    def counted_renew(*args, **kwargs):
        nonlocal renewal_count
        renewed = real_renew(*args, **kwargs)
        renewal_count += int(renewed)
        if renewal_count >= 4:
            service.loop.call_soon_threadsafe(renewals.set)
        return renewed

    async def long_reconcile(*, reconcile_intents):
        await release.wait()
        return {"ingested": 1}

    monkeypatch.setattr(service.events, "renew_reconcile", counted_renew)
    monkeypatch.setattr(service, "reconcile_startup", long_reconcile)
    service.loop = asyncio.get_running_loop()
    running = asyncio.create_task(service._run_reconcile_job(job.id))
    await asyncio.wait_for(renewals.wait(), timeout=3)
    assert not service.events.claim_reconcile(
        job.id,
        "owner-b",
        now=datetime.now(timezone.utc),
        lease_seconds=1,
    )
    release.set()
    await running
    assert service.events.get_reconcile(job.id).status == "succeeded"
```

The second test waits for four successful renewals, so inventory has run beyond the original one-second lease; it never uses a fixed assertion sleep.

- [ ] **Step 5: Add response models and authorized routes**

Add to `app/models.py`:

```python
class VaultReconcileJobResponse(BaseModel):
    job_id: str
    status: str
    result: dict[str, int] | None = None
    error_summary: str | None = None


class ObsidianLinkResponse(BaseModel):
    url: str


class VaultStatusResponse(BaseModel):
    configured: bool
    running: bool
    clean: bool
    degraded: bool
    last_event_at: str | None = None
    last_error: str | None = None
    pending_occurrences: int = 0
    failed_occurrences: int = 0
    pending_deletes: int = 0
    open_issues: int = 0
    invalid_pages: int = 0
    projection_backlog: int = 0
    projection: dict[str, dict] = Field(default_factory=dict)
    obsidian: dict = Field(default_factory=dict)
    reconcile: VaultReconcileJobResponse | None = None
```

Import `catalog` as a module, `build_obsidian_uri`, and `projection_health` in `app/api.py`. Add these routes. The deep-link route must appear before the generic page read route and reuse its non-disclosure contract:

```python
@router.post("/vault/reconcile", response_model=VaultReconcileJobResponse, status_code=202)
async def vault_reconcile_endpoint(request: Request, user: UserContext = Depends(current_user)):
    require_account_admin(user)
    job = request.app.state.vault_sync.request_reconcile(user.user_id)
    return {"job_id": job.id, "status": job.status, "result": job.result, "error_summary": job.error_summary}


@router.get("/vault/reconcile/{job_id}", response_model=VaultReconcileJobResponse)
def vault_reconcile_job_endpoint(job_id: str, request: Request, user: UserContext = Depends(current_user)):
    require_account_admin(user)
    job = request.app.state.vault_sync.events.get_reconcile(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="vault reconcile job not found")
    return {"job_id": job.id, "status": job.status, "result": job.result, "error_summary": job.error_summary}


@router.get("/vault/status", response_model=VaultStatusResponse)
def vault_status_endpoint(
    request: Request,
    user: UserContext = Depends(current_user),
):
    settings = get_settings()
    vault_sync = getattr(request.app.state, "vault_sync", None)
    snapshot = vault_sync.snapshot() if vault_sync is not None else {
        "pending_occurrences": 0,
        "failed_occurrences": 0,
        "pending_deletes": 0,
        "open_issues": 0,
        "invalid_pages": 0,
        "last_event_at": None,
        "last_error": None,
    }
    projection = projection_health(settings)

    def target_is_enabled(target: str, state: dict) -> bool:
        return target != "gbrain" or bool(state.get("enabled", False))

    projection_backlog = sum(
        int(state.get("pending", 0)) + int(state.get("running", 0))
        for target, state in projection.items()
        if target_is_enabled(target, state)
    )
    projection_failed = any(
        int(state.get("failed", 0)) > 0
        for target, state in projection.items()
        if target_is_enabled(target, state)
    )
    reconcile = None
    if vault_sync is not None:
        reconcile = (
            vault_sync.events.active_reconcile()
            or vault_sync.events.latest_reconcile()
        )
    raw_obsidian = getattr(request.app.state, "obsidian_status", {}) or {}
    obsidian = {
        "installed": list(raw_obsidian.get("installed", [])),
        "drifted": list(raw_obsidian.get("drifted", [])),
        "vault_name": raw_obsidian.get(
            "vault_name", settings.effective_obsidian_vault_name
        ),
    }
    reconcile_failed = reconcile is not None and reconcile.status == "failed"
    degraded = bool(
        snapshot["failed_occurrences"]
        or snapshot["open_issues"]
        or snapshot["invalid_pages"]
        or projection_failed
        or obsidian["drifted"]
        or reconcile_failed
    )
    busy = bool(
        snapshot["pending_occurrences"]
        or snapshot["pending_deletes"]
        or projection_backlog
        or (reconcile is not None and reconcile.status in {"queued", "running"})
    )
    reconcile_payload = None if reconcile is None else {
        "job_id": reconcile.id,
        "status": reconcile.status,
        "result": reconcile.result,
        "error_summary": reconcile.error_summary,
    }
    return {
        "configured": bool(settings.vault_watch_enabled),
        "running": bool(vault_sync is not None and vault_sync.running),
        "clean": not degraded and not busy,
        "degraded": degraded,
        "last_event_at": snapshot["last_event_at"],
        "last_error": snapshot["last_error"],
        "pending_occurrences": snapshot["pending_occurrences"],
        "failed_occurrences": snapshot["failed_occurrences"],
        "pending_deletes": snapshot["pending_deletes"],
        "open_issues": snapshot["open_issues"],
        "invalid_pages": snapshot["invalid_pages"],
        "projection_backlog": projection_backlog,
        "projection": projection,
        "obsidian": obsidian,
        "reconcile": reconcile_payload,
    }


@router.get("/wiki/pages/{page_path:path}/obsidian-link", response_model=ObsidianLinkResponse)
def obsidian_link_endpoint(page_path: str, user: UserContext = Depends(current_user)):
    settings = get_settings()
    try:
        page = catalog.read_wiki_page(settings, page_path, user_context=user)
    except PermissionError:
        raise_wiki_http(PageNotFound(page_path))
    except Exception as exc:
        raise_wiki_http(exc)
    return {"url": build_obsidian_uri(settings.effective_obsidian_vault_name, page["path"])}
```

The status response contains only counters, timestamps, relative managed-asset names, and the logical Vault name. Never serialize `vault_path`, `backup_dir`, watched absolute paths, or exception representations containing them.

- [ ] **Step 6: Replace lifespan with partial-failure-safe ownership**

Wrap all fallible startup work after object construction in the cleanup region:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    vault_sync = None
    projection_worker = None
    try:
        ensure_vault(settings.vault_path)
        init_app_db(settings)
        obsidian = ensure_obsidian_vault(settings.vault_path)
        intent_executor = IntentExecutor(settings)
        vault_sync = VaultSyncService(settings, intent_executor=intent_executor)
        app.state.vault_sync = vault_sync
        projection_worker = ProjectionWorker(settings)
        app.state.projection_worker = projection_worker
        app.state.obsidian_status = {
            "installed": obsidian.installed,
            "drifted": obsidian.drifted,
            "vault_name": settings.effective_obsidian_vault_name,
        }
        await asyncio.to_thread(intent_executor.reconcile_all)
        await vault_sync.reconcile_before_watcher_start()
        if settings.projection_worker_enabled:
            await projection_worker.start()
        if settings.vault_watch_enabled:
            await vault_sync.start()
        yield
    finally:
        try:
            if vault_sync is not None:
                await vault_sync.stop()
        finally:
            if projection_worker is not None:
                await projection_worker.stop()
```

Both `stop()` methods must be idempotent and clean partial starts. The nested `finally` guarantees projection cleanup even if watcher cleanup raises.

- [ ] **Step 7: Run API, authorization, health, and lifecycle tests**

Run:

```powershell
python -m pytest tests/test_vault_api.py tests/test_projection_worker.py tests/test_projection_api.py tests/test_wiki_revision_api.py -q
```

Expected: PASS. Concurrent reconcile calls share one persisted job; disabled GBrain is neither misconfigured nor degraded; missing/unauthorized deep links fail through existing page/ACL behavior; every startup failure cleans both runtime owners.

- [ ] **Step 8: Commit runtime and API integration**

```powershell
git add app/vault_sync.py app/projection_worker.py app/main.py app/models.py app/api.py tests/test_vault_api.py tests/test_projection_worker.py tests/test_projection_api.py tests/test_wiki_revision_api.py
git commit -m "feat: expose safe vault sync runtime APIs"
```

---

### Task 9: Frontend Sync State And Obsidian Command

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `frontend/vite.config.ts`
- Create: `frontend/src/test/setup.ts`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/App.tsx`
- Create: `frontend/src/App.test.tsx`
- Modify: `frontend/src/features/WikiTask.tsx`
- Create: `frontend/src/features/WikiTask.test.tsx`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Write failing sync-state, command, optional-status, and role tests**

Create `frontend/src/test/setup.ts`:

```typescript
import "@testing-library/jest-dom/vitest";
```

Create `frontend/src/features/WikiTask.test.tsx` with a shared render helper and these cases:

```tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { WikiTask } from "./WikiTask";

const page = {
  path: "wiki/产品/退款 政策.md",
  page_id: "page_refund",
  domain: "product",
  page_type: "policy",
  title: "退款政策",
  review_status: "draft",
  lifecycle_status: "active",
  sync_error: null,
};

const status = {
  configured: true,
  running: true,
  clean: false,
  degraded: true,
  pending_occurrences: 1,
  failed_occurrences: 0,
  pending_deletes: 0,
  open_issues: 1,
  invalid_pages: 0,
  projection_backlog: 0,
  projection: {},
  obsidian: {},
  reconcile: null,
};

const editor = {
  path: "",
  page_id: "",
  content: "",
  current_revision_id: "",
  generated_revision_id: null,
  accepted_generated_revision_id: null,
  lifecycle_status: "active",
  projection_epoch: 0,
  write_in_progress: false,
  write_intent_id: null,
  review_status: "draft",
  owner: "",
};

function renderTask(role: "admin" | "viewer", overrides: Record<string, unknown> = {}) {
  const openInObsidian = vi.fn(async () => undefined);
  const requestReconcile = vi.fn(async () => undefined);
  render(
    <WikiTask
      pages={[page]}
      activeSpaceFilter={{ id: "all", label: "全部", desc: "全部", kind: "all", count: 1 }}
      clearSpaceFilter={vi.fn()}
      editor={editor}
      setEditor={vi.fn()}
      loadPage={vi.fn(async () => undefined)}
      savePage={vi.fn(async () => undefined)}
      markPageStale={vi.fn(async () => undefined)}
      openInObsidian={openInObsidian}
      requestReconcile={requestReconcile}
      vaultStatus={status}
      currentUser={{ user_id: "user", role, acl_tags: [], auth_provider: "test" }}
      showToast={vi.fn()}
      {...overrides}
    />,
  );
  return { openInObsidian, requestReconcile };
}

test("opens the selected canonical page through the backend link", () => {
  const { openInObsidian } = renderTask("viewer");
  fireEvent.click(screen.getByRole("button", { name: "在 Obsidian 中打开" }));
  expect(openInObsidian).toHaveBeenCalledWith(page.path);
});

test("shows compact degraded sync state", () => {
  renderTask("viewer");
  expect(screen.getByText("同步异常")).toBeInTheDocument();
  expect(screen.getByText("1 个待处理事件")).toBeInTheDocument();
  expect(screen.getByText("1 个同步问题")).toBeInTheDocument();
});

test("viewer cannot see the reconcile command", () => {
  renderTask("viewer");
  expect(screen.queryByRole("button", { name: "立即对账" })).not.toBeInTheDocument();
});

test("admin can request reconcile", () => {
  const { requestReconcile } = renderTask("admin");
  fireEvent.click(screen.getByRole("button", { name: "立即对账" }));
  expect(requestReconcile).toHaveBeenCalledTimes(1);
});

test("unknown status is not presented as healthy", () => {
  renderTask("viewer", { vaultStatus: null });
  expect(screen.getByText("同步状态未知")).toBeInTheDocument();
  expect(screen.getByText("同步状态未知").closest(".sync-status"))
    .toHaveClass("unknown");
});

test("disabled watcher has a distinct label", () => {
  renderTask("viewer", {
    vaultStatus: { ...status, configured: false, running: false, degraded: false },
  });
  expect(screen.getByText("同步未启用")).toBeInTheDocument();
});
```

Create `frontend/src/App.test.tsx` to lock the optional/required split and the existing pending-review query:

```tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { App } from "./App";
import { setAuthToken } from "./api/client";

afterEach(() => {
  setAuthToken("");
  window.localStorage.clear();
  vi.unstubAllGlobals();
});

test("vault status failure does not discard required workspace data", async () => {
  const page = {
    path: "wiki/product/refund.md",
    page_id: "page_refund",
    domain: "product",
    page_type: "policy",
    title: "退款政策",
    review_status: "draft",
    lifecycle_status: "active",
  };
  const responses: Record<string, unknown> = {
    "/api/internal/auth/me": {
      user_id: "viewer", role: "viewer", acl_tags: [], auth_provider: "test",
    },
    "/api/internal/sources": [],
    "/api/internal/ingest/reports": [],
    "/api/internal/wiki/pages": [page],
    "/api/internal/reviews?status=pending": [],
    "/api/internal/gaps": [],
    "/api/internal/rag/status": {},
    "/api/internal/wiki/pages/wiki/product/refund.md/obsidian-link": {
      url: "obsidian://open?vault=LGDO&file=wiki%2Fproduct%2Frefund.md",
    },
  };
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), "http://localhost").pathname
      + new URL(String(input), "http://localhost").search;
    if (path === "/api/internal/vault/status") {
      return new Response('{"detail":"not ready"}', {
        status: 503,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (!(path in responses)) throw new Error(`unexpected request: ${path}`);
    return new Response(JSON.stringify(responses[path]), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  const navigateTo = vi.fn();

  setAuthToken("test-token");
  render(<App navigateTo={navigateTo} />);
  fireEvent.click(await screen.findByRole("button", { name: /知识页/ }));

  expect(await screen.findByText("退款政策")).toBeInTheDocument();
  fireEvent.click(screen.getAllByRole("button", { name: "在 Obsidian 中打开" })[0]);
  await waitFor(() => expect(navigateTo).toHaveBeenCalledWith(
    "obsidian://open?vault=LGDO&file=wiki%2Fproduct%2Frefund.md",
  ));
  expect(screen.queryByText("登录")).not.toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining("/api/internal/reviews?status=pending"),
    expect.anything(),
  );
});
```

- [ ] **Step 2: Install test dependencies and verify RED**

Add script `"test": "vitest run"`, runtime dependency `lucide-react`, and dev dependencies `@testing-library/jest-dom`, `@testing-library/react`, `@types/react`, `@types/react-dom`, `jsdom`, and `vitest` to `frontend/package.json`.

Run:

```powershell
npm --prefix frontend install
npm --prefix frontend test -- WikiTask.test.tsx
```

Expected: FAIL because Vault status, Obsidian, reconcile, and role-aware props are absent.

- [ ] **Step 3: Add exact frontend types**

Append to `frontend/src/types.ts`:

```typescript
export interface VaultReconcileJob {
  job_id: string;
  status: "queued" | "running" | "succeeded" | "failed" | string;
  result?: Record<string, number> | null;
  error_summary?: string | null;
}

export interface VaultStatus {
  configured: boolean;
  running: boolean;
  clean: boolean;
  degraded: boolean;
  last_event_at?: string | null;
  last_error?: string | null;
  pending_occurrences: number;
  failed_occurrences: number;
  pending_deletes: number;
  open_issues: number;
  invalid_pages: number;
  projection_backlog: number;
  projection: Record<string, Record<string, unknown>>;
  obsidian: Record<string, unknown>;
  reconcile?: VaultReconcileJob | null;
}
```

Add `sync_error?: string | null` to `WikiPage`. Do not weaken existing `WikiPageContentResponse` or mutation types.

- [ ] **Step 4: Load Vault status as an optional refresh dependency**

In `App.tsx`, keep required data in the existing `Promise.all`, but isolate Vault status failure inside its own promise:

```typescript
const vaultStatusRequest = api<VaultStatus>("/api/internal/vault/status")
  .catch(() => null);

const [nextSources, nextReports, nextPages, nextReviews, nextGaps, nextRagStatus, nextVaultStatus] =
  await Promise.all([
    api<SourceRecord[]>("/api/internal/sources"),
    api<IngestReport[]>("/api/internal/ingest/reports"),
    api<WikiPage[]>("/api/internal/wiki/pages"),
    api<ReviewItem[]>("/api/internal/reviews?status=pending"),
    api<KnowledgeGap[]>("/api/internal/gaps"),
    api<RagStatus>("/api/internal/rag/status"),
    vaultStatusRequest,
  ]);
setVaultStatus(nextVaultStatus);
```

The optional failure leaves `vaultStatus=null` and does not reject `refresh()`. Change the existing component declaration to `export function App({ navigateTo = (url: string) => window.location.assign(url) }: { navigateTo?: (url: string) => void } = {})`, then add these handlers inside the component:

```typescript
async function openInObsidian(path: string) {
  const result = await api<{ url: string }>(
    `/api/internal/wiki/pages/${encodePath(path)}/obsidian-link`,
  );
  navigateTo(result.url);
}

async function requestVaultReconcile() {
  const job = await api<VaultReconcileJob>("/api/internal/vault/reconcile", { method: "POST" });
  setVaultStatus((current) => current ? { ...current, reconcile: job } : current);
  showToast(`Vault 对账已进入${job.status}`);
}
```

Move the existing `authLoading` and `!currentUser` returns above construction of the section-content map. Only after the `!currentUser` guard may `currentUser` be passed to `WikiTask`; this preserves the `AuthUser | null` state while giving `WikiTask` a non-null `AuthUser` prop.

- [ ] **Step 5: Add the icon command, compact state, and admin-only reconcile control**

In `WikiTask.tsx`, use `ExternalLink` for the page command and `RefreshCw` for reconcile. Render reconcile only when `currentUser.role` is `admin` or `owner`, or ACL tags include `*`:

```tsx
const canReconcile =
  currentUser.role === "admin" ||
  currentUser.role === "owner" ||
  currentUser.acl_tags.includes("*");
const syncTone = !vaultStatus
  ? "unknown"
  : vaultStatus.degraded
    ? "warn"
    : "ok";
const syncLabel = !vaultStatus
  ? "同步状态未知"
  : !vaultStatus.configured
    ? "同步未启用"
    : vaultStatus.degraded
      ? "同步异常"
      : vaultStatus.running
        ? "同步运行中"
        : "同步已停止";

<div className={`sync-status ${syncTone}`}>
  <strong>{syncLabel}</strong>
  <span>{vaultStatus?.pending_occurrences ?? 0} 个待处理事件</span>
  <span>{vaultStatus?.open_issues ?? 0} 个同步问题</span>
  {canReconcile && (
    <button type="button" aria-label="立即对账" title="立即对账" onClick={() => requestReconcile().catch((error) => showToast(error.message))}>
      <RefreshCw aria-hidden="true" size={16} />
    </button>
  )}
</div>
```

Add an `ExternalLink` icon button to every page row and the selected editor. The button invokes only the backend-generated link; it performs no synchronization itself.

Add compact, non-floating `.sync-status`, `.sync-status.unknown`, `.sync-status.warn`, and 32-by-32 icon-button rules to `frontend/src/styles.css`; unknown uses the existing neutral text/border colors, not the healthy treatment. Keep text wrapping enabled and do not introduce a nested card.

- [ ] **Step 6: Configure Vitest and run frontend gates**

Add to `frontend/vite.config.ts`:

```typescript
test: {
  environment: "jsdom",
  setupFiles: ["./src/test/setup.ts"],
},
```

Run:

```powershell
npm --prefix frontend test
npm --prefix frontend run build
```

Expected: all Vitest tests PASS and TypeScript/Vite build exits 0. A failed optional Vault status request does not prevent required workspace data from rendering; viewer has no reconcile command.

- [ ] **Step 7: Commit frontend workflow**

```powershell
git add frontend/package.json frontend/package-lock.json frontend/vite.config.ts frontend/src/test/setup.ts frontend/src/types.ts frontend/src/App.tsx frontend/src/features/WikiTask.tsx frontend/src/features/WikiTask.test.tsx
git add frontend/src/App.test.tsx frontend/src/styles.css
git commit -m "feat: expose Obsidian sync controls in console"
```

---

### Task 10: Deterministic Five-Second Local Workflow

**Files:**
- Create: `tests/test_vault_obsidian_e2e.py`
- Modify: `tests/test_internal_flow.py`

- [ ] **Step 1: Write the failing real-watcher local E2E**

Create `tests/test_vault_obsidian_e2e.py`:

```python
import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.auth import UserContext
from app.config import Settings
from app.db import connect_app, init_app_db, json_dump
from app.models import AskRequest
from app.projection_worker import ProjectionWorker
from app.search import CITATION_REFUSAL, ask
from app.vault_sync import VaultSyncService


async def eventually_async(probe, *, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await probe()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition did not converge within {timeout} seconds")


@pytest.mark.asyncio
async def test_real_obsidian_add_modify_rename_delete_reaches_current_local_rag_within_five_seconds(tmp_path, monkeypatch):
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
        projection_worker_enabled=True,
        projection_poll_seconds=0.05,
        gbrain_enabled=False,
        deepseek_api_key=None,
        deepseek_model=None,
    )
    init_app_db(settings)
    timestamp = "2026-07-15T00:00:00+00:00"
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO sources(
              id,domain,title,source_type,original_path,raw_path,content_hash,
              size_bytes,status,metadata_json,created_at,updated_at
            ) VALUES ('src_refund','product','Refund','markdown','obsidian',
              'raw/product/refund.md',?,1,'active',?,?,?)
            """,
            ("a" * 64, json_dump({"acl_tags": ["internal"]}), timestamp, timestamp),
        )
    page = settings.vault_path / "wiki/product/refund.md"
    renamed = settings.vault_path / "wiki/product/refund-renamed.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    service = VaultSyncService(settings)
    worker = ProjectionWorker(settings)
    def reject_external_network(*args, **kwargs):
        raise AssertionError("local E2E attempted an external request")

    monkeypatch.setattr("app.llm.urllib.request.urlopen", reject_external_network)
    monkeypatch.setattr("app.gbrain._call_http_mcp", reject_external_network)

    await worker.start()
    await service.start()
    try:
        page.write_text(
            "---\ntitle: Refund\nsource_ids: [src_refund]\ndomain: product\n"
            "page_type: policy\nreview_status: draft\nowner:\n---\n"
            "# Refund\nFinance approves enterprise refunds.\n",
            encoding="utf-8",
        )

        def page_projection_state(page_path):
            with connect_app(settings) as conn:
                row = conn.execute(
                    """
                    SELECT page_id,current_revision_id,projection_epoch,
                           rag_visible_revision_id,rag_visible_epoch
                    FROM wiki_pages
                    WHERE path=? AND lifecycle_status='active'
                    """,
                    (page_path,),
                ).fetchone()
            return dict(row) if row is not None else None

        def citation_for(response, page_path, page_id, expected_text):
            state = page_projection_state(page_path)
            if (
                state is None
                or state["page_id"] != page_id
                or state["current_revision_id"] is None
                or state["rag_visible_revision_id"] != state["current_revision_id"]
                or state["rag_visible_epoch"] != state["projection_epoch"]
            ):
                return None
            return next(
                (
                    citation
                    for citation in response.citations
                    if citation.origin == "wiki"
                    and citation.wiki_page == page_path
                    and citation.page_id == page_id
                    and citation.revision_id == state["current_revision_id"]
                    and expected_text in citation.snippet
                ),
                None,
            )

        async def project_and_ask(
            expected_path, expected_page_id, expected_text, question
        ):
            response = await asyncio.to_thread(
                ask,
                settings,
                AskRequest(question=question, domain="product"),
                UserContext(user_id="admin", role="admin", acl_tags=("*",)),
            )
            citation = citation_for(
                response, expected_path, expected_page_id, expected_text
            )
            return response if citation is not None else None

        async def page_identity_visible():
            state = page_projection_state("wiki/product/refund.md")
            return state["page_id"] if state is not None else None

        expected_page_id = await eventually_async(page_identity_visible)

        add_response = await eventually_async(
            lambda: project_and_ask(
                "wiki/product/refund.md",
                expected_page_id,
                "Finance approves enterprise refunds.",
                "Who approves enterprise refunds?",
            )
        )
        add_citation = citation_for(
            add_response,
            "wiki/product/refund.md",
            expected_page_id,
            "Finance approves enterprise refunds.",
        )
        assert add_citation is not None
        add_revision = add_citation.revision_id

        managed = page.read_text(encoding="utf-8")
        page.write_text(
            managed.replace(
                "Finance approves enterprise refunds.",
                "Legal and finance approve enterprise refunds.",
            ),
            encoding="utf-8",
        )
        modify_response = await eventually_async(
            lambda: project_and_ask(
                "wiki/product/refund.md",
                expected_page_id,
                "Legal and finance approve enterprise refunds.",
                "Who now approves enterprise refunds?",
            )
        )
        modify_citation = citation_for(
            modify_response,
            "wiki/product/refund.md",
            expected_page_id,
            "Legal and finance approve enterprise refunds.",
        )
        assert modify_citation is not None
        assert modify_citation.revision_id != add_revision

        page.rename(renamed)
        rename_response = await eventually_async(
            lambda: project_and_ask(
                "wiki/product/refund-renamed.md",
                expected_page_id,
                "Legal and finance approve enterprise refunds.",
                "Who now approves enterprise refunds?",
            )
        )
        rename_citation = citation_for(
            rename_response,
            "wiki/product/refund-renamed.md",
            expected_page_id,
            "Legal and finance approve enterprise refunds.",
        )
        assert rename_citation is not None
        assert rename_citation.revision_id == modify_citation.revision_id

        renamed.unlink()

        async def pending_delete_visible():
            with connect_app(settings) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM pending_vault_deletes WHERE status='pending'"
                ).fetchone()[0]
            return count if count == 1 else None

        await eventually_async(pending_delete_visible)
        applied = await service.expire_deletes(
            datetime.now(timezone.utc) + timedelta(seconds=6)
        )
        assert applied == 1

        async def deleted_from_local_rag():
            response = await asyncio.to_thread(
                ask,
                settings,
                AskRequest(question="Who now approves enterprise refunds?", domain="product"),
                UserContext(user_id="admin", role="admin", acl_tags=("*",)),
            )
            if response.citations:
                return None
            if response.answer != CITATION_REFUSAL or response.confidence != "low":
                return None
            return response

        delete_response = await eventually_async(deleted_from_local_rag)
        assert delete_response.citations == []
        assert delete_response.answer == CITATION_REFUSAL
        assert delete_response.confidence == "low"
        with connect_app(settings) as conn:
            deleted_page = conn.execute(
                """
                SELECT page_id,lifecycle_status,rag_visible_revision_id,rag_visible_epoch
                FROM wiki_pages WHERE path=?
                """,
                ("wiki/product/refund-renamed.md",),
            ).fetchone()
            remaining_chunks = conn.execute(
                "SELECT COUNT(*) FROM wiki_chunks WHERE page_id=?",
                (deleted_page["page_id"],),
            ).fetchone()[0]
        assert deleted_page["lifecycle_status"] == "deleted"
        assert deleted_page["rag_visible_revision_id"] is None
        assert deleted_page["rag_visible_epoch"] is None
        assert remaining_chunks == 0
    finally:
        try:
            await service.stop()
        finally:
            await worker.stop()
```

- [ ] **Step 2: Add a managed-write loop-suppression E2E**

Append this second real-adapter test:

```python
@pytest.mark.asyncio
async def test_managed_writeback_event_does_not_create_second_revision(tmp_path):
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
        gbrain_enabled=False,
    )
    init_app_db(settings)
    page = settings.vault_path / "wiki/product/loop.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    service = VaultSyncService(settings)

    def revision_count():
        with connect_app(settings) as conn:
            return conn.execute(
                """
                SELECT COUNT(*) FROM wiki_page_revisions
                WHERE page_id=(SELECT page_id FROM wiki_pages WHERE path=?)
                """,
                ("wiki/product/loop.md",),
            ).fetchone()[0]

    await service.start()
    try:
        page.write_text(
            "---\ntitle: Loop\nsource_ids: []\ndomain: product\n"
            "page_type: feature\nreview_status: draft\nowner:\n---\n# Loop\n",
            encoding="utf-8",
        )

        async def first_managed_revision():
            if revision_count() == 1 and page.exists() and "lgdo_revision_id" in page.read_text(encoding="utf-8"):
                return True
            return None

        await eventually_async(first_managed_revision)

        async def watcher_idle():
            with connect_app(settings) as conn:
                pending_occurrences = conn.execute(
                    "SELECT COUNT(*) FROM vault_watch_occurrences WHERE status='pending'"
                ).fetchone()[0]
                page_state = conn.execute(
                    "SELECT pending_write_intent_id FROM wiki_pages WHERE path=?",
                    ("wiki/product/loop.md",),
                ).fetchone()
            return bool(
                pending_occurrences == 0
                and page_state is not None
                and page_state["pending_write_intent_id"] is None
            ) or None

        await eventually_async(watcher_idle)
        with connect_app(settings) as conn:
            previous_occurrences = conn.execute(
                "SELECT COUNT(*) FROM vault_watch_occurrences"
            ).fetchone()[0]
        page.write_bytes(page.read_bytes())

        async def rewrite_observed():
            with connect_app(settings) as conn:
                total = conn.execute(
                    "SELECT COUNT(*) FROM vault_watch_occurrences"
                ).fetchone()[0]
                pending = conn.execute(
                    "SELECT COUNT(*) FROM vault_watch_occurrences WHERE status='pending'"
                ).fetchone()[0]
            return total if total > previous_occurrences and pending == 0 else None

        await eventually_async(rewrite_observed)
        assert revision_count() == 1
        with connect_app(settings) as conn:
            explicit_rewrite = conn.execute(
                """
                SELECT status FROM vault_watch_occurrences
                ORDER BY detected_at DESC,id DESC LIMIT 1
                """
            ).fetchone()
        assert explicit_rewrite["status"] == "ignored"
    finally:
        await service.stop()
```

Neither E2E may call `handle_batch()`, construct `VaultFsEvent`, or call `ProjectionWorker.run_once()` directly. The first test starts and stops the production worker task; every synchronous `ask()` call runs through `asyncio.to_thread()` so polling never blocks the event loop.

- [ ] **Step 3: Run E2E and verify RED**

Run: `python -m pytest tests/test_vault_obsidian_e2e.py -q`

Expected: FAIL until real watchfiles startup, occurrence orchestration, writeback suppression, local projection, and citation mapping converge.

- [ ] **Step 4: Make only integration corrections exposed by the E2E**

Maintain these ownership rules while correcting failures:

```python
# app/vault_sync.py orders occurrences and calls revision APIs.
# app/wiki_revisions.py owns revision events, revisions, intents, audit, issues, and outbox.
# app/projection_worker.py owns wiki_chunks and visible revision/epoch switching.
# app/search.py owns current-visible and ACL Citation gates.
```

Do not add direct revision, outbox, chunk, or `vault_change_events` SQL to watcher modules. Update `tests/test_internal_flow.py` only where its save requests require the existing `expected_revision_id` contract.

- [ ] **Step 5: Run deterministic local acceptance**

Run:

```powershell
python -m pytest tests/test_vault_obsidian_e2e.py tests/test_internal_flow.py -q
```

Expected: PASS. Each add/modify/rename visibility phase and pending-delete observation converges within five seconds; forced expiry then removes local retrieval within five seconds. No DeepSeek or GBrain request occurs.

- [ ] **Step 6: Commit local E2E**

```powershell
git add tests/test_vault_obsidian_e2e.py tests/test_internal_flow.py app/vault_sync.py app/wiki_revisions.py app/projection_worker.py app/search.py
git commit -m "test: prove local Obsidian convergence"
```

---

### Task 11: Real GBrain Incremental And Reconcile SLA

**Files:**
- Modify: `tests/integration/test_gbrain_pglite_projection.py`

- [ ] **Step 1: Add the failing opt-in watcher-driven SLA test name**

Run before adding the function:

```powershell
$env:RUN_GBRAIN_E2E = "1"
python -m pytest -m gbrain_e2e tests/integration/test_gbrain_pglite_projection.py::test_obsidian_watcher_incremental_and_reconcile_meet_sla -v
```

Expected: FAIL with test not found.

- [ ] **Step 2: Add a condition-polling watcher integration test**

Append an async test using the existing `gbrain_pglite_server`, `gbrain_e2e_settings`, `_query()`, and `to_gbrain_manifest_path()` helpers. LGDO source validation and the GBrain managed namespace are different identities: seed an active LGDO `sources.id` and reference that ID in Markdown; never put `gbrain_pglite_server.source_id` in `source_ids`.

```python
def watcher_markdown(title: str, source_id: str, token: str) -> str:
    return (
        "---\n"
        f"title: {title}\n"
        f"source_ids: [{source_id}]\n"
        "domain: product\n"
        "page_type: feature\n"
        "review_status: draft\n"
        "owner:\n"
        "---\n"
        f"# {title}\n{token}\n"
    )


@pytest.mark.gbrain_e2e
@pytest.mark.asyncio
async def test_obsidian_watcher_incremental_and_reconcile_meet_sla(
    gbrain_pglite_server,
    gbrain_e2e_settings,
):
    settings = gbrain_e2e_settings.model_copy(
        update={
            "vault_watch_enabled": True,
            "vault_watch_debounce_ms": 50,
            "vault_watch_stability_timeout_seconds": 0.25,
            "vault_rename_grace_ms": 5000,
            "projection_worker_enabled": True,
            "projection_poll_seconds": 0.1,
        }
    )
    service = VaultSyncService(settings)
    worker = ProjectionWorker(settings)
    token = f"obsidianwatcher{uuid.uuid4().hex}"
    lgdo_source_id = f"src_obsidian_{uuid.uuid4().hex}"
    timestamp = datetime.now(timezone.utc).isoformat()
    with connect_app(settings) as conn:
        conn.execute(
            """
            INSERT INTO sources(
              id,domain,title,source_type,original_path,raw_path,content_hash,
              size_bytes,status,metadata_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                lgdo_source_id,
                "product",
                "Obsidian watcher source",
                "markdown",
                "obsidian-e2e",
                f"raw/product/{lgdo_source_id}.md",
                "a" * 64,
                1,
                "active",
                "{}",
                timestamp,
                timestamp,
            ),
        )
    old_rel = "wiki/product/obsidian-sla.md"
    new_rel = "wiki/product/obsidian-sla-renamed.md"
    old_manifest = to_gbrain_manifest_path(old_rel)
    new_manifest = to_gbrain_manifest_path(new_rel)
    old_path = settings.vault_path / old_rel
    new_path = settings.vault_path / new_rel
    old_path.parent.mkdir(parents=True, exist_ok=True)

    async def search_paths(query: str) -> set[str]:
        rows = await asyncio.to_thread(_query, settings, query)
        return {str(item.get("source_path")) for item in rows}

    async def poll(deadline_seconds: float, predicate):
        started = time.monotonic()
        while time.monotonic() - started < deadline_seconds:
            paths = await search_paths(token)
            if predicate(paths):
                return time.monotonic() - started
            await asyncio.sleep(0.25)
        raise AssertionError(f"GBrain condition exceeded {deadline_seconds} seconds")

    await worker.start()
    await service.start()
    try:
        old_path.write_text(
            watcher_markdown("Obsidian watcher SLA", lgdo_source_id, token),
            encoding="utf-8",
        )
        assert await poll(120, lambda paths: old_manifest in paths) < 120
        old_path.rename(new_path)
        assert await poll(600, lambda paths: new_manifest in paths and old_manifest not in paths) < 600
        new_path.unlink()
        assert await poll(600, lambda paths: old_manifest not in paths and new_manifest not in paths) < 600
    finally:
        try:
            await service.stop()
        finally:
            await worker.stop()
```

The test must start the production `VaultSyncService` and `ProjectionWorker` and use real file writes. It must not call `lgdo_vault_sync`, `handle_batch()`, `run_once()`, or construct watcher events. `_query()` already validates the MCP list payload; do not call `.get("results")` on it. All `source_path` assertions use manifest-relative values from `to_gbrain_manifest_path()`.

- [ ] **Step 3: Run default and opt-in selections separately**

Run:

```powershell
python -m pytest -m "not gbrain_e2e" -q
$env:RUN_GBRAIN_E2E = "1"
python -m pytest -m gbrain_e2e tests/integration/test_gbrain_pglite_projection.py::test_obsidian_watcher_incremental_and_reconcile_meet_sla -v
```

Expected: the complete default suite PASSes with every module-level `gbrain_e2e` test deselected; the opt-in test PASSes with incremental under 120 seconds and rename/delete reconcile under 600 seconds using condition polling rather than fixed sleeps. Do not run `-m "not gbrain_e2e"` against only the module-level-marked GBrain file because that selects zero tests and pytest exits 5.

- [ ] **Step 4: Commit real GBrain acceptance**

```powershell
git add tests/integration/test_gbrain_pglite_projection.py
git commit -m "test: enforce Obsidian GBrain watcher SLA"
```

---

### Task 12: Operator Documentation And Full Regression Gates

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Document the exact operator workflow in both languages**

Document these PowerShell commands and their meaning in `README.md` and `README.zh-CN.md`:

```powershell
$vaultPath = [IO.Path]::GetFullPath((Join-Path $PWD "vault"))
scripts/install-obsidian-vault.ps1 -VaultPath $vaultPath
scripts/install-obsidian-vault.ps1 -VaultPath $vaultPath -Refresh
scripts/open-obsidian.ps1 -VaultName "LGDO" -PagePath "wiki/product/example.md"
$session = Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/api/internal/auth/login `
  -ContentType "application/json" `
  -Body (@{
    username = $env:LGDO_ADMIN_USERNAME
    password = $env:LGDO_ADMIN_PASSWORD
  } | ConvertTo-Json)
$adminHeaders = @{ Authorization = "Bearer $($session.token)" }
Invoke-RestMethod -Headers $adminHeaders http://127.0.0.1:8000/api/internal/vault/status
Invoke-RestMethod -Headers $adminHeaders -Method Post http://127.0.0.1:8000/api/internal/vault/reconcile
```

Document that `X-LGDO-*` identity headers are permitted only when `auth_dev_fallback=true` in a local development environment; production operator examples must use the login-issued Bearer token above.

State explicitly:

- Only `vault/wiki/**/*.md` is editable content input.
- `indexes/`, `reviews/`, `logs/`, `normalized/`, and `jsonl/` are generated views.
- Refresh makes a unique backup and atomically replaces only LGDO-managed JSON.
- Runtime `vault/` remains ignored; stable assets live under `resources/obsidian-vault/`.
- GBrain disabled is a valid state, not degraded/misconfigured.
- Real GBrain SLA is opt-in; ordinary CI performs no DeepSeek/GBrain request.
- `/vault/status`, persisted reconcile job state, and the Web Wiki workspace expose synchronization health.

- [ ] **Step 2: Run backend default CI**

Add a default-suite fail-fast network guard to `tests/conftest.py`. It allows loopback dependencies such as PostgreSQL/fake servers and exempts explicitly opted-in GBrain E2E tests, while rejecting accidental real external sockets:

```python
import socket


@pytest.fixture(autouse=True)
def reject_nonlocal_network_in_default_suite(request, monkeypatch):
    if request.node.get_closest_marker("gbrain_e2e") is not None:
        yield
        return
    real_connect = socket.socket.connect

    def guarded_connect(sock, address):
        if isinstance(address, tuple) and address:
            host = str(address[0]).strip("[]").lower()
            if host not in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
                raise AssertionError(
                    f"default test attempted non-loopback network access: {host}"
                )
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    yield
```

Run:

```powershell
$env:RUN_GBRAIN_E2E = "0"
$env:GBRAIN_ENABLED = "false"
$env:DEEPSEEK_API_KEY = ""
python -m pytest -m "not gbrain_e2e" -q
```

Expected: PASS with zero failures. Any non-loopback request fails at the socket guard; DeepSeek/GBrain are also disabled in the process environment.

- [ ] **Step 3: Run frontend tests and production build**

Run:

```powershell
npm --prefix frontend test
npm --prefix frontend run build
```

Expected: all Vitest tests PASS; TypeScript and Vite build exit 0.

- [ ] **Step 4: Run schema/platform/resource checks**

Run:

```powershell
python -m pytest tests/test_wiki_revision_postgres.py -q
$freshVault = Join-Path $env:TEMP ("lgdo-obsidian-final-check-" + [guid]::NewGuid().ToString("N"))
python -m app.obsidian install --vault $freshVault
scripts/open-obsidian.ps1 -VaultName "LGDO 知识库" -PagePath "wiki/产品/退款 政策.md" -PrintOnly
python -c "from app.config import Settings; s=Settings(_env_file=None); assert s.vault_rename_grace_ms >= max(5000, s.vault_watch_debounce_ms + int(s.vault_watch_stability_timeout_seconds*1000) + s.vault_rename_safety_margin_ms)"
```

Expected: PostgreSQL tests PASS when capability is available; installer prints structured JSON containing the absolute Vault path; link command prints one encoded URI; config assertion exits 0.

- [ ] **Step 5: Prove ownership and placeholder gates**

Run:

```powershell
$ownershipMatches = rg -n "vault_change_events" app/vault_events.py app/vault_watcher.py app/vault_sync.py
if ($LASTEXITCODE -eq 0) { $ownershipMatches; throw "watcher ownership violation" }
if ($LASTEXITCODE -gt 1) { throw "ownership scan failed" }
$placeholderMatches = rg -n "T[B]D|T[O]DO|implement la[t]er|[Ss]imilar to Task|:\s+\.\.\.$" docs/superpowers/plans/2026-07-15-vault-watcher-obsidian-revised.md
if ($LASTEXITCODE -eq 0) { $placeholderMatches; throw "plan placeholder found" }
if ($LASTEXITCODE -gt 1) { throw "placeholder scan failed" }
git diff --check
if ($LASTEXITCODE -ne 0) { throw "git diff check failed" }
git status --short
```

Expected: both `rg` commands print no matches; `git diff --check` exits 0; status contains only intentional implementation files and preserves unrelated user changes.

- [ ] **Step 6: Run the opt-in real GBrain SLA acceptance**

Run:

```powershell
$env:RUN_GBRAIN_E2E = "1"
python -m pytest -m gbrain_e2e tests/integration/test_gbrain_pglite_projection.py::test_obsidian_watcher_incremental_and_reconcile_meet_sla -v
```

Expected: PASS within the configured 120/600-second condition deadlines. A genuinely unavailable external prerequisite is reported as an explicit fixture skip, not converted into a passing assertion.

- [ ] **Step 7: Commit operator documentation**

```powershell
git add README.md README.zh-CN.md tests/conftest.py
git commit -m "docs: document and guard Obsidian vault operations"
```

---

## Coverage Matrix

| # | Locked correction | Task | Required named test/gate |
| --- | --- | --- | --- |
| 1 | Watcher records filesystem work only in `vault_watch_occurrences` with immutable occurrence identity and terminal CAS | 3 | `test_watcher_occurrence_never_creates_revision_event`; `test_occurrence_begin_replays_same_payload_and_rejects_changed_payload`; `test_occurrence_finish_is_terminal_cas_and_exactly_idempotent` |
| 2 | `WikiRevisionService` exclusively owns `vault_change_events` | R0, 3, 12 | `test_legacy_vault_events_are_upgraded_but_never_reconstructed_from_page_state`; `test_vault_event_store_source_has_no_revision_event_sql`; Task 12 `$ownershipMatches` zero-match gate |
| 3 | `ingest_external_change()` supports valid new pages | R0 | `test_external_new_page_accepts_empty_sources_and_creates_managed_revision` |
| 4 | Rename plus edit uses atomic, crash-replayable `relocate_external_change()` | R0, 5 | `test_relocate_with_edit_is_one_external_revision_and_atomic`; `test_relocate_postcommit_crash_leaves_one_recoverable_intent`; `test_pending_rename_with_edit_uses_atomic_relocate` |
| 5 | `MutationResult` keeps nullable IDs and rejects coercive event payload/replay types | R0 | `test_mutation_result_round_trips_nullable_page_and_sync_issue`; `test_corrupt_terminal_result_payload_fails_as_revision_conflict`; `test_mutation_result_codec_rejects_non_boolean_replay_argument` |
| 6 | Unknown/source-invalid file creates observation/issue/terminal event but no fake domain rows/jobs | R0 | `test_external_sources_must_exist_and_be_active`; `test_unknown_invalid_external_file_has_no_fake_domain_rows` |
| 7 | Empty `source_ids: []` is valid | R0, 10 | `test_external_new_page_accepts_empty_sources_and_creates_managed_revision`; `test_managed_writeback_event_does_not_create_second_revision` |
| 8 | Listed sources exist and are active at preparation and finalize; cross-domain is allowed | R0 | `test_external_sources_must_exist_and_be_active`; `test_active_source_from_another_domain_is_valid`; `test_postgres_source_mutation_wins_against_finalize_without_advancing_state` |
| 9 | Restore always creates an external revision, even for identical historical bytes | R0 | `test_deleted_identical_bytes_restore_creates_new_external_revision`; `test_invalid_identical_historical_bytes_restore_creates_new_external_revision` |
| 10 | Active ID moves only with unique pending-delete/startup proof | 5, 6 | `test_active_id_without_pending_delete_is_isolated_by_revision_service`; `test_startup_unique_missing_path_is_explicit_offline_rename_evidence`; `test_startup_active_id_with_old_path_present_is_isolated` |
| 11 | Exact-byte rename is audit-only; edited rename is relocate | R0, 5, 6 | `test_exact_rename_stays_audit_only`; `test_exact_pending_rename_uses_audit_only_api`; `test_pending_rename_with_edit_uses_atomic_relocate`; `test_startup_edited_unique_move_uses_atomic_relocate` |
| 12 | Finalize synchronizes six fields including nullable owner | R0 | `test_finalize_external_revision_syncs_all_page_fields_and_clears_owner` |
| 13 | Duplicate delete events share one active absence cycle through a partial unique index | 3 | `test_duplicate_delete_occurrences_share_one_pending_absence_cycle`; `test_postgres_concurrent_duplicate_deletes_share_one_pending_absence_cycle` |
| 14 | Per-event errors do not terminate sibling/future watcher work | 4, 5 | `test_watch_adapter_survives_handler_error_and_processes_next_batch`; `test_watch_adapter_survives_normalization_error`; `test_one_event_failure_does_not_cancel_sibling_or_future_batch` |
| 15 | Live/startup share hidden, symlink, junction/reparse filtering | 4, 6 | `test_live_and_inventory_reject_hidden_and_parent_symlink_paths`; `test_windows_junction_is_rejected_without_privilege_skip`; `test_startup_inventory_reuses_canonical_filter_and_survives_bad_file` |
| 16 | Sync issues resolve from validated server metadata with generation CAS only after real repair | R0, 3 | `test_successful_repair_resolves_only_captured_issue_generation`; `test_user_frontmatter_cannot_forge_reserved_sync_issue_refs`; `test_sync_issue_revision_metadata_codec_rejects_corruption`; `test_finalize_rejects_cross_path_sync_issue_metadata_before_page_cas`; `test_sync_issue_generation_cas_rejects_stale_resolver` |
| 17 | Reconcile is persisted, leased, and single-flight | 3, 8 | `test_concurrent_reconcile_requests_return_one_persisted_job`; `test_reconcile_endpoint_is_persisted_single_flight`; `test_reconcile_renewal_extends_lease_and_blocks_takeover`; `test_reconcile_lease_loss_cancels_inventory_before_finish` |
| 18 | Deep link requires canonical page existence and existing read ACL | 8 | `test_obsidian_link_missing_and_unauthorized_are_indistinguishable`; `test_obsidian_link_maps_catalog_permission_error_to_hidden_404`; `test_specific_wiki_routes_are_registered_before_greedy_page_routes` |
| 19 | Disabled GBrain is not degraded or misconfigured | 8 | `test_disabled_gbrain_failed_backlog_is_raw_diagnostic_only` |
| 20 | Lifespan cleans every partial startup failure | 8 | `test_lifespan_cleans_both_runtime_owners_at_every_failure_point` |
| 21 | `index/` becomes `indexes/`, including `app/wiki.py` and legacy data | 7 | `test_compile_writes_indexes_plural_only`; `test_legacy_index_migration_moves_missing_and_deduplicates_identical`; `test_legacy_index_migration_quarantines_conflict_without_overwrite` |
| 22 | PowerShell rooted paths, unique backups, and atomic JSON replacement | 7 | `test_install_script_accepts_absolute_vault_path_from_other_cwd`; `test_windows_powershell_open_script_reuses_python_uri`; `test_two_refreshes_in_one_second_use_distinct_backups`; `test_atomic_json_replace_failure_preserves_original` |

## Completion Checklist

- [ ] Every watcher mutation delegates to a revision API; watcher modules contain no revision-event/outbox/revision/chunk writes.
- [ ] Watcher occurrence IDs bind to canonical kind/path payloads, and only an exact terminal result can replay after the pending CAS.
- [ ] Terminal revision event replay uses validated `result_payload_json`, and event ID reuse validates `payload_digest`.
- [ ] Finalize revalidates immutable revision sources under the backend-appropriate lock before page CAS/outbox/event advancement.
- [ ] Unknown invalid input cannot create a page, revision, intent, review item requiring a page, or projection job.
- [ ] Sync-issue refs come only from server revision metadata; repair resolution uses page-path and generation CAS after the page winner.
- [ ] SQLite and PostgreSQL concurrency/crash tests cover create, relocate, source-vs-finalize, issue CAS, delete-cycle uniqueness, and reconcile single-flight.
- [ ] Live and startup discovery use the same canonical path functions, including Windows reparse rejection.
- [ ] Deep links call `catalog.read_wiki_page()` before URI construction and the route precedes the catch-all page route.
- [ ] Lifespan stops watcher/reconcile ownership before projection worker, including partial startup failure.
- [ ] Frontend optional Vault status failure does not break required refresh; viewer cannot request reconcile.
- [ ] Local real-watcher E2E stays within five seconds per local phase without external services.
- [ ] Real GBrain SLA remains opt-in and uses condition polling.
- [ ] Both operator READMEs describe editable/read-only boundaries, refresh backup behavior, status, and reconcile.

## Execution Handoff

Plan execution starts only after the plan self-review commands pass. Use `superpowers:subagent-driven-development` for one implementation task at a time with specification and quality review between tasks, or `superpowers:executing-plans` for checkpointed batches.
