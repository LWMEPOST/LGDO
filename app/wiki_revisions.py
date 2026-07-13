from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Literal

from app.config import Settings
from app.db import audit, connect_app, connect_app_write, json_dump
from app.projection_jobs import ProjectionOutbox
from app.timeutil import now_iso
from app.wiki_markdown import (
    MarkdownParseError,
    compute_file_hash,
    compute_semantic_hash,
    parse_wiki_bytes,
    render_managed_frontmatter,
)

MutationStatus = Literal[
    "prepared",
    "applied",
    "conflicted",
    "invalid",
    "deleted",
    "renamed",
    "resolved",
    "ignored",
    "failed",
]

VALID_REVIEW_STATUSES = frozenset({"draft", "reviewed", "stale", "rejected"})


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
    def from_row(cls, row: Any) -> RevisionRecord:
        data = dict(row)
        return cls(
            id=data["id"],
            page_id=data["page_id"],
            page_path=data["page_path"],
            revision_number=int(data["revision_number"]),
            file_hash=data["file_hash"],
            semantic_hash=data["semantic_hash"],
            content=data["content"],
            origin=data["origin"],
            base_revision_id=data["base_revision_id"],
            source_ids=json.loads(data["source_ids_json"] or "[]"),
            actor=data["actor"],
            note=data["note"],
            metadata=json.loads(data["metadata_json"] or "{}"),
            idempotency_key=data["idempotency_key"],
            created_at=data["created_at"],
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


class WikiRevisionError(RuntimeError):
    pass


class _PendingCompileReplay(RuntimeError):
    def __init__(self, intent_id: str):
        super().__init__(intent_id)
        self.intent_id = intent_id


class PreconditionRequired(WikiRevisionError):
    pass


class RevisionConflict(WikiRevisionError):
    def __init__(
        self,
        message: str,
        *,
        current_revision_id: str | None,
        pending_intent_id: str | None = None,
    ):
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


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _source_ids(metadata: dict[str, Any]) -> list[str]:
    value = metadata["source_ids"] if "source_ids" in metadata else []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MarkdownParseError("invalid_source_ids", "source_ids must be a string list")
    return list(value)


def _review_status(value: Any) -> str:
    if not isinstance(value, str) or value not in VALID_REVIEW_STATUSES:
        raise MarkdownParseError(
            "invalid_review_status",
            "review_status must be draft, reviewed, stale, or rejected",
        )
    return value


def canonical_state_json(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class WikiRevisionService:
    def __init__(self, settings: Settings, *, writer: Any | None = None):
        self.settings = settings
        self.coordinator = PageMutationCoordinator(settings)
        self.outbox = ProjectionOutbox(settings)
        if writer is None:
            from app.vault_writer import AtomicVaultWriter

            writer = AtomicVaultWriter(settings.vault_path)
        self.writer = writer

    def _target(self, page_path: str) -> Path:
        if "\\" in page_path or page_path.startswith("/"):
            raise PageNotFound(page_path)
        parts = PurePosixPath(page_path).parts
        if not parts or parts[0] != "wiki" or any(part in {"", ".", ".."} for part in parts):
            raise PageNotFound(page_path)
        root = self.settings.vault_path.resolve()
        target = (root / Path(*parts)).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise PageNotFound(page_path) from exc
        return target

    @contextmanager
    def _lock_generated_page(
        self,
        command: CompileCandidateCommand,
    ) -> Iterator[tuple[LockedPage, bool]]:
        suffix = " FOR UPDATE" if self.settings.database_backend == "postgres" else ""
        with connect_app_write(self.settings) as conn:
            row = conn.execute(
                "SELECT * FROM wiki_pages WHERE path=?" + suffix,
                (command.page_path,),
            ).fetchone()
            created = False
            if row is None:
                timestamp = now_iso()
                inserted = conn.execute(
                    """
                    INSERT INTO wiki_pages(
                      path,page_id,domain,page_type,title,source_ids_json,review_status,
                      owner,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(path) DO NOTHING
                    """,
                    (
                        command.page_path,
                        f"page_{uuid.uuid4().hex}",
                        command.domain,
                        command.page_type,
                        command.title,
                        json_dump(command.source_ids),
                        "draft",
                        command.owner,
                        timestamp,
                        timestamp,
                    ),
                )
                created = inserted.rowcount == 1
                row = conn.execute(
                    "SELECT * FROM wiki_pages WHERE path=?" + suffix,
                    (command.page_path,),
                ).fetchone()
            elif row["page_id"] is None:
                page_id = f"page_{uuid.uuid4().hex}"
                assigned = conn.execute(
                    """
                    UPDATE wiki_pages SET page_id=?,updated_at=?
                    WHERE path=? AND page_id IS NULL
                    """,
                    (page_id, now_iso(), command.page_path),
                )
                if assigned.rowcount != 1:
                    raise RevisionConflict(
                        "page identity changed during compile",
                        current_revision_id=row["current_revision_id"],
                        pending_intent_id=row["pending_write_intent_id"],
                    )
                row = conn.execute(
                    "SELECT * FROM wiki_pages WHERE path=?" + suffix,
                    (command.page_path,),
                ).fetchone()
            if row is None:
                raise WikiRevisionError(
                    f"generated page could not be locked: {command.page_path}"
                )
            page = dict(row)
            pending_intent_id = page.get("pending_write_intent_id")
            if pending_intent_id is not None:
                pending = conn.execute(
                    """
                    SELECT intent.id,revision.origin,revision.semantic_hash,
                           revision.metadata_json,revision.idempotency_key
                    FROM vault_write_intents AS intent
                    JOIN wiki_page_revisions AS revision
                      ON revision.id=intent.revision_id
                    WHERE intent.id=? AND intent.page_id=?
                    """,
                    (pending_intent_id, page["page_id"]),
                ).fetchone()
                prefix = (
                    f"compile:{command.compile_job_id}:{page['page_id']}:"
                    f"{command.source_hash}:{command.compiler_version}:"
                )
                if pending is not None:
                    metadata = json.loads(pending["metadata_json"] or "{}")
                    source_semantic_hash = compute_semantic_hash(
                        parse_wiki_bytes(command.content.encode("utf-8"))
                    )
                    if (
                        pending["origin"] == "generated"
                        and pending["semantic_hash"] == source_semantic_hash
                        and metadata.get("source_hash") == command.source_hash
                        and metadata.get("compiler_version")
                        == command.compiler_version
                        and pending["idempotency_key"].startswith(prefix)
                        and len(pending["idempotency_key"]) == len(prefix) + 64
                    ):
                        raise _PendingCompileReplay(str(pending_intent_id))
            yield LockedPage(conn=conn, page=page), created

    def _read_result(
        self,
        page_path: str,
        *,
        page: dict[str, Any] | None = None,
    ) -> PageReadResult:
        if page is None:
            with connect_app(self.settings) as conn:
                row = conn.execute(
                    "SELECT * FROM wiki_pages WHERE path=?", (page_path,)
                ).fetchone()
            page = dict(row) if row is not None else None
        if page is None or page["page_id"] is None or page["current_revision_id"] is None:
            raise WikiRevisionError(f"page has no current revision: {page_path}")
        if page["pending_write_intent_id"] is not None:
            with connect_app(self.settings) as conn:
                revision = conn.execute(
                    "SELECT content FROM wiki_page_revisions WHERE id=?",
                    (page["current_revision_id"],),
                ).fetchone()
            if revision is None:
                raise WikiRevisionError(
                    f"current revision not found: {page['current_revision_id']}"
                )
            raw = revision["content"].encode("utf-8")
        else:
            raw = self._target(page_path).read_bytes()
        document = parse_wiki_bytes(raw)
        return PageReadResult(
            page_id=page["page_id"],
            page_path=page_path,
            content=raw.decode("utf-8"),
            raw_bytes=raw,
            current_revision_id=page["current_revision_id"],
            generated_revision_id=page["generated_revision_id"],
            accepted_generated_revision_id=page["accepted_generated_revision_id"],
            lifecycle_status=page["lifecycle_status"],
            projection_epoch=int(page["projection_epoch"] or 0),
            sync_error=page["sync_error"],
            write_in_progress=page["pending_write_intent_id"] is not None,
            write_intent_id=page["pending_write_intent_id"],
            metadata=_plain(document.frontmatter),
        )

    def get_page(self, page_path: str) -> PageReadResult:
        target = self._target(page_path)
        intent_id: str | None = None
        with self.coordinator.lock_page(page_path) as locked:
            if locked.page.get("current_revision_id") is not None:
                return self._read_result(page_path, page=locked.page)
            if locked.page.get("pending_write_intent_id") is not None:
                intent_id = locked.page["pending_write_intent_id"]
            else:
                original = target.read_bytes()
                original_hash = compute_file_hash(original)
                page_id = locked.page.get("page_id") or f"page_{uuid.uuid4().hex}"
                revision_id = f"wrev_{uuid.uuid4().hex}"
                write_token = f"write_{uuid.uuid4().hex}"
                document = parse_wiki_bytes(original)
                rendered = render_managed_frontmatter(
                    document,
                    page_id=page_id,
                    revision_id=revision_id,
                    write_token=write_token,
                    review_status=str(document.frontmatter.get("review_status") or locked.page["review_status"]),
                )
                assigned = locked.conn.execute(
                    "UPDATE wiki_pages SET page_id=?,updated_at=? WHERE path=? AND page_id IS NULL",
                    (page_id, now_iso(), page_path),
                )
                if locked.page.get("page_id") is None and assigned.rowcount != 1:
                    raise RevisionConflict(
                        "page identity changed while bootstrapping",
                        current_revision_id=locked.page.get("current_revision_id"),
                    )
                locked.page["page_id"] = page_id
                final_document = parse_wiki_bytes(rendered)
                metadata = _plain(final_document.frontmatter)
                revision = self._create_revision_locked(
                    locked.conn,
                    locked.page,
                    content=rendered,
                    origin="legacy",
                    base_revision_id=None,
                    source_ids=_source_ids(metadata),
                    actor="legacy-bootstrap",
                    note=None,
                    idempotency_key=f"legacy:{page_id}:{original_hash}",
                    metadata=metadata,
                    revision_id=revision_id,
                )
                intent_id = self.prepare_write_intent_locked(
                    locked.conn,
                    locked.page,
                    revision=revision,
                    expected_revision_id=None,
                    expected_file_hash=original_hash,
                    write_token=write_token,
                )
        if intent_id is None:
            raise WikiRevisionError("legacy bootstrap did not prepare an intent")

        from app.vault_writer import IntentExecutor

        result = IntentExecutor(self.settings).execute(intent_id)
        if result is None or result.intent_status != "applied":
            raise RevisionConflict(
                "legacy bootstrap requires recovery",
                current_revision_id=None,
                pending_intent_id=intent_id,
            )
        return self.get_page(page_path)

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
            page_id=page["page_id"],
            page_path=page["path"],
            revision_number=next_number,
            file_hash=compute_file_hash(content),
            semantic_hash=compute_semantic_hash(document),
            content=content.decode("utf-8"),
            origin=origin,
            base_revision_id=base_revision_id,
            source_ids=source_ids,
            actor=actor,
            note=note,
            metadata=metadata,
            idempotency_key=idempotency_key,
            created_at=now_iso(),
        )
        conn.execute(
            """
            INSERT INTO wiki_page_revisions(
              id,page_id,page_path,revision_number,file_hash,semantic_hash,content,origin,
              base_revision_id,source_ids_json,actor,note,metadata_json,idempotency_key,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record.id,
                record.page_id,
                record.page_path,
                record.revision_number,
                record.file_hash,
                record.semantic_hash,
                record.content,
                record.origin,
                record.base_revision_id,
                json_dump(record.source_ids),
                record.actor,
                record.note,
                json_dump(record.metadata),
                record.idempotency_key,
                record.created_at,
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

    def prepare_write_intent_locked(
        self,
        conn: Any,
        page: dict[str, Any],
        *,
        revision: RevisionRecord,
        expected_revision_id: str | None,
        expected_file_hash: str | None,
        write_token: str,
    ) -> str:
        if page.get("current_revision_id") != expected_revision_id:
            raise RevisionConflict(
                "current revision changed before intent preparation",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        if expected_revision_id is not None and page.get("file_hash") != expected_file_hash:
            raise RevisionConflict(
                "current file hash changed before intent preparation",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        if page.get("pending_write_intent_id") is not None:
            raise RevisionConflict(
                "page already has a pending write",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        intent_id = f"wint_{uuid.uuid4().hex}"
        timestamp = now_iso()
        conn.execute(
            """
            INSERT INTO vault_write_intents(
              id,page_id,revision_id,expected_revision_id,expected_file_hash,target_path,
              write_token,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,'pending',?,?)
            """,
            (
                intent_id,
                page["page_id"],
                revision.id,
                expected_revision_id,
                expected_file_hash,
                page["path"],
                write_token,
                timestamp,
                timestamp,
            ),
        )
        expected_current_sql = (
            "current_revision_id IS NULL"
            if expected_revision_id is None
            else "current_revision_id=?"
        )
        expected_generated = page.get("generated_revision_id")
        generated_sql = (
            "generated_revision_id IS NULL"
            if expected_generated is None
            else "generated_revision_id=?"
        )
        params: list[Any] = [intent_id, timestamp, page["page_id"]]
        if expected_revision_id is not None:
            params.append(expected_revision_id)
        if expected_generated is not None:
            params.append(expected_generated)
        prepared = conn.execute(
            f"""
            UPDATE wiki_pages SET pending_write_intent_id=?,updated_at=?
            WHERE page_id=? AND pending_write_intent_id IS NULL
              AND {expected_current_sql} AND {generated_sql}
            """,
            tuple(params),
        )
        if prepared.rowcount != 1:
            raise RevisionConflict(
                "page changed while preparing write intent",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        page["pending_write_intent_id"] = intent_id
        return intent_id

    def _canonical_state_locked(
        self,
        conn: Any,
        page: dict[str, Any],
    ) -> dict[str, Any]:
        pending_conflicts = conn.execute(
            """
            SELECT id FROM review_items
            WHERE page_id=? AND status='pending'
              AND issue_type IN ('content_conflict','concurrent_write_conflict')
            ORDER BY id
            """,
            (page["page_id"],),
        ).fetchall()
        return {
            "current": page.get("current_revision_id"),
            "generated": page.get("generated_revision_id"),
            "accepted_generated": page.get("accepted_generated_revision_id"),
            "lifecycle": page.get("lifecycle_status"),
            "path": page.get("path"),
            "file_hash": page.get("file_hash"),
            "pending_conflict": [row["id"] for row in pending_conflicts],
        }

    def _transition_replay_locked(
        self,
        conn: Any,
        page: dict[str, Any],
        *,
        transition_key: str,
        transition_prefix: str,
        request_id: str,
        expected_revision_id: str,
        allow_path_transition_replay: bool,
    ) -> tuple[RevisionRecord, dict[str, Any]] | None:
        row = conn.execute(
            """
            SELECT * FROM wiki_page_revisions
            WHERE page_id=? AND idempotency_key=? AND base_revision_id=?
            """,
            (page["page_id"], transition_key, expected_revision_id),
        ).fetchone()
        if (
            row is None
            and allow_path_transition_replay
            and page.get("pending_write_intent_id") is not None
        ):
            pending = conn.execute(
                """
                SELECT revision.*,base.page_path AS transition_base_path
                FROM vault_write_intents AS intent
                JOIN wiki_page_revisions AS revision
                  ON revision.id=intent.revision_id
                JOIN wiki_page_revisions AS base
                  ON base.id=revision.base_revision_id
                WHERE intent.id=? AND revision.page_id=?
                  AND revision.base_revision_id=?
                """,
                (
                    page["pending_write_intent_id"],
                    page["page_id"],
                    expected_revision_id,
                ),
            ).fetchone()
            prefix = f"{transition_prefix}:{request_id}:"
            replay_key = None
            if pending is not None:
                original_page = {
                    **page,
                    "path": pending["transition_base_path"],
                }
                original_state = canonical_state_json(
                    self._canonical_state_locked(conn, original_page)
                )
                original_digest = hashlib.sha256(
                    original_state.encode("utf-8")
                ).hexdigest()
                replay_key = f"{prefix}{original_digest}"
            if (
                pending is not None
                and pending["idempotency_key"] == replay_key
            ):
                row = pending
        if (
            row is None
            and page.get("current_revision_id") != expected_revision_id
        ):
            prefix = f"{transition_prefix}:{request_id}:"
            candidates = conn.execute(
                """
                SELECT * FROM wiki_page_revisions
                WHERE page_id=? AND base_revision_id=?
                ORDER BY revision_number DESC
                """,
                (page["page_id"], expected_revision_id),
            ).fetchall()
            row = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate["idempotency_key"].startswith(prefix)
                    and len(candidate["idempotency_key"]) == len(prefix) + 64
                ),
                None,
            )
        if row is None:
            return None
        intent = conn.execute(
            """
            SELECT * FROM vault_write_intents
            WHERE revision_id=? ORDER BY created_at,id LIMIT 1
            """,
            (row["id"],),
        ).fetchone()
        if intent is None:
            raise WikiRevisionError(
                f"manual revision has no write intent: {row['id']}"
            )
        return RevisionRecord.from_row(row), dict(intent)

    def _prepare_human_mutation(
        self,
        *,
        page_path: str,
        expected_revision_id: str,
        request_id: str,
        transition_prefix: str,
        actor: str,
        note: str | None,
        source_content: bytes | None,
        metadata_changes: dict[str, Any],
        review_status: str | None,
        review_status_required: bool,
        owner: str | None,
        target_page_path: str | None,
        page_domain: str | None,
        execute_intent: bool,
        locked_page: LockedPage | None = None,
    ) -> MutationResult:
        if target_page_path is not None:
            self._target(target_page_path)
        if locked_page is not None and execute_intent:
            raise ValueError("locked mutation preparation cannot execute its intent")
        replayed = False
        replay_applied = False
        intent_id: str
        lock_context = (
            nullcontext(locked_page)
            if locked_page is not None
            else self.coordinator.lock_page(page_path)
        )
        with lock_context as locked:
            state_json = canonical_state_json(
                self._canonical_state_locked(locked.conn, locked.page)
            )
            state_digest = hashlib.sha256(state_json.encode("utf-8")).hexdigest()
            transition_key = (
                f"{transition_prefix}:{request_id}:{state_digest}"
            )
            replay = self._transition_replay_locked(
                locked.conn,
                locked.page,
                transition_key=transition_key,
                transition_prefix=transition_prefix,
                request_id=request_id,
                expected_revision_id=expected_revision_id,
                allow_path_transition_replay=target_page_path is not None,
            )
            if replay is not None:
                revision, intent = replay
                replayed = True
                intent_id = intent["id"]
                replay_applied = intent["status"] == "applied"
                prepared = MutationResult(
                    page_id=locked.page["page_id"],
                    page_path=locked.page["path"],
                    status="applied" if replay_applied else "prepared",
                    revision_id=revision.id,
                    current_revision_id=(
                        revision.id
                        if replay_applied
                        else locked.page.get("current_revision_id")
                    ),
                    generated_revision_id=locked.page.get("generated_revision_id"),
                    write_intent_id=intent_id,
                    replayed=True,
                )
            else:
                if locked.page.get("current_revision_id") != expected_revision_id:
                    raise RevisionConflict(
                        f"{transition_prefix} mutation precondition failed",
                        current_revision_id=locked.page.get("current_revision_id"),
                        pending_intent_id=locked.page.get("pending_write_intent_id"),
                    )
                if locked.page.get("pending_write_intent_id") is not None:
                    raise RevisionConflict(
                        "page already has a pending write",
                        current_revision_id=locked.page.get("current_revision_id"),
                        pending_intent_id=locked.page.get("pending_write_intent_id"),
                    )
                if target_page_path is not None or page_domain is not None:
                    previous_path = locked.page["path"]
                    next_path = target_page_path or previous_path
                    next_domain = page_domain or locked.page["domain"]
                    transitioned = locked.conn.execute(
                        """
                        UPDATE wiki_pages SET path=?,domain=?,updated_at=?
                        WHERE page_id=? AND path=? AND current_revision_id=?
                          AND pending_write_intent_id IS NULL
                        """,
                        (
                            next_path,
                            next_domain,
                            now_iso(),
                            locked.page["page_id"],
                            previous_path,
                            expected_revision_id,
                        ),
                    )
                    if transitioned.rowcount != 1:
                        raise RevisionConflict(
                            "page changed during metadata transition",
                            current_revision_id=locked.page.get(
                                "current_revision_id"
                            ),
                            pending_intent_id=locked.page.get(
                                "pending_write_intent_id"
                            ),
                        )
                    locked.conn.execute(
                        "UPDATE review_items SET page_path=? WHERE page_path=?",
                        (next_path, previous_path),
                    )
                    locked.page["path"] = next_path
                    locked.page["domain"] = next_domain
                if source_content is None:
                    current = locked.conn.execute(
                        "SELECT content FROM wiki_page_revisions WHERE id=?",
                        (expected_revision_id,),
                    ).fetchone()
                    if current is None:
                        raise WikiRevisionError(
                            f"current revision not found: {expected_revision_id}"
                        )
                    mutation_content = current["content"].encode("utf-8")
                else:
                    mutation_content = source_content
                document = parse_wiki_bytes(mutation_content)
                for key, value in metadata_changes.items():
                    if not isinstance(key, str) or not key:
                        raise MarkdownParseError(
                            "invalid_metadata_key",
                            "metadata keys must be non-empty strings",
                        )
                    document.frontmatter[key] = value
                if owner is not None:
                    document.frontmatter["owner"] = owner
                if review_status_required:
                    status_value = review_status
                elif "review_status" in document.frontmatter:
                    status_value = document.frontmatter["review_status"]
                else:
                    status_value = locked.page.get("review_status")
                effective_status = _review_status(status_value)
                revision_id = f"wrev_{uuid.uuid4().hex}"
                write_token = f"write_{uuid.uuid4().hex}"
                rendered = render_managed_frontmatter(
                    document,
                    page_id=locked.page["page_id"],
                    revision_id=revision_id,
                    write_token=write_token,
                    review_status=effective_status,
                )
                final_document = parse_wiki_bytes(rendered)
                metadata = _plain(final_document.frontmatter)
                revision = self._create_revision_locked(
                    locked.conn,
                    locked.page,
                    content=rendered,
                    origin="manual",
                    base_revision_id=expected_revision_id,
                    source_ids=_source_ids(metadata),
                    actor=actor,
                    note=note,
                    idempotency_key=transition_key,
                    metadata=metadata,
                    revision_id=revision_id,
                )
                intent_id = self.prepare_write_intent_locked(
                    locked.conn,
                    locked.page,
                    revision=revision,
                    expected_revision_id=expected_revision_id,
                    expected_file_hash=locked.page.get("file_hash"),
                    write_token=write_token,
                )
                prepared = MutationResult(
                    page_id=locked.page["page_id"],
                    page_path=locked.page["path"],
                    status="prepared",
                    revision_id=revision.id,
                    current_revision_id=expected_revision_id,
                    generated_revision_id=locked.page.get("generated_revision_id"),
                    write_intent_id=intent_id,
                )
        if replay_applied:
            return replace(self._mutation_for_intent(intent_id), replayed=True)
        if not execute_intent:
            return prepared

        from app.vault_writer import IntentExecutor

        result = IntentExecutor(self.settings).execute(intent_id)
        if result is None or result.intent_status != "applied":
            raise RevisionConflict(
                f"{transition_prefix} mutation requires recovery",
                current_revision_id=expected_revision_id,
                pending_intent_id=intent_id,
            )
        return replace(self._mutation_for_intent(intent_id), replayed=replayed)

    def prepare_manual_save(
        self,
        command: ManualSaveCommand,
        *,
        execute_intent: bool = True,
    ) -> MutationResult:
        return self._prepare_human_mutation(
            page_path=command.page_path,
            expected_revision_id=command.expected_revision_id,
            request_id=command.request_id,
            transition_prefix="manual",
            actor=command.actor,
            note=command.note,
            source_content=command.content.encode("utf-8"),
            metadata_changes={},
            review_status=command.review_status,
            review_status_required=True,
            owner=command.owner,
            target_page_path=None,
            page_domain=None,
            execute_intent=execute_intent,
        )

    def update_status(
        self,
        command: StatusUpdateCommand,
        *,
        execute_intent: bool = True,
    ) -> MutationResult:
        return self._prepare_human_mutation(
            page_path=command.page_path,
            expected_revision_id=command.expected_revision_id,
            request_id=command.request_id,
            transition_prefix="status",
            actor=command.actor,
            note=command.note,
            source_content=None,
            metadata_changes={},
            review_status=command.review_status,
            review_status_required=True,
            owner=command.owner,
            target_page_path=None,
            page_domain=None,
            execute_intent=execute_intent,
        )

    def update_metadata(
        self,
        command: MetadataUpdateCommand,
        *,
        execute_intent: bool = True,
        target_page_path: str | None = None,
        page_domain: str | None = None,
        locked_page: LockedPage | None = None,
    ) -> MutationResult:
        return self._prepare_human_mutation(
            page_path=command.page_path,
            expected_revision_id=command.expected_revision_id,
            request_id=command.request_id,
            transition_prefix="metadata",
            actor=command.actor,
            note=command.note,
            source_content=None,
            metadata_changes=command.changes,
            review_status=None,
            review_status_required=False,
            owner=None,
            target_page_path=target_page_path,
            page_domain=page_domain,
            execute_intent=execute_intent,
            locked_page=locked_page,
        )

    def apply_generated_candidate(
        self,
        command: CompileCandidateCommand,
    ) -> MutationResult:
        try:
            return self._apply_generated_candidate_once(command)
        except _PendingCompileReplay as replay:
            return self._execute_generated_intent(
                replay.intent_id,
                replayed=True,
            )

    def _apply_generated_candidate_once(
        self,
        command: CompileCandidateCommand,
    ) -> MutationResult:
        target = self._target(command.page_path)
        with connect_app(self.settings) as conn:
            existing = conn.execute(
                """
                SELECT current_revision_id,pending_write_intent_id
                FROM wiki_pages WHERE path=?
                """,
                (command.page_path,),
            ).fetchone()
        if (
            existing is not None
            and existing["current_revision_id"] is None
            and existing["pending_write_intent_id"] is None
            and target.exists()
        ):
            self.get_page(command.page_path)

        intent_id: str | None = None
        prepared: MutationResult
        with self._lock_generated_page(command) as (locked, created):
            if locked.page.get("pending_write_intent_id") is not None:
                raise RevisionConflict(
                    "page already has a pending write",
                    current_revision_id=locked.page.get("current_revision_id"),
                    pending_intent_id=locked.page.get("pending_write_intent_id"),
                )

            old_current = locked.page.get("current_revision_id")
            old_generated = locked.page.get("generated_revision_id")
            current_revision = None
            if old_current is not None:
                current_revision = locked.conn.execute(
                    "SELECT * FROM wiki_page_revisions WHERE id=?",
                    (old_current,),
                ).fetchone()
                if current_revision is None:
                    raise WikiRevisionError(
                        f"current revision not found: {old_current}"
                    )

            if target.exists():
                disk_hash = compute_file_hash(target.read_bytes())
                if (
                    current_revision is None
                    or disk_hash != current_revision["file_hash"]
                ):
                    raise RevisionConflict(
                        "vault file changed outside the revision state machine",
                        current_revision_id=old_current,
                    )
            else:
                disk_hash = None
                if old_current is not None:
                    raise RevisionConflict(
                        "vault file is missing outside the revision state machine",
                        current_revision_id=old_current,
                    )

            state_json = canonical_state_json(
                self._canonical_state_locked(locked.conn, locked.page)
            )
            state_hash = hashlib.sha256(state_json.encode("utf-8")).hexdigest()
            compile_prefix = (
                f"compile:{command.compile_job_id}:{locked.page['page_id']}:"
                f"{command.source_hash}:{command.compiler_version}:"
            )
            source_document = parse_wiki_bytes(command.content.encode("utf-8"))
            candidate_semantic_hash = compute_semantic_hash(source_document)
            latest_generated = locked.conn.execute(
                """
                SELECT * FROM wiki_page_revisions
                WHERE page_id=? AND origin='generated'
                ORDER BY revision_number DESC LIMIT 1
                """,
                (locked.page["page_id"],),
            ).fetchone()
            candidate: RevisionRecord | None = None
            candidate_replayed = False
            write_token: str | None = None
            if latest_generated is not None:
                latest_metadata = json.loads(
                    latest_generated["metadata_json"] or "{}"
                )
                if (
                    latest_generated["semantic_hash"] == candidate_semantic_hash
                    and latest_metadata.get("source_hash") == command.source_hash
                    and latest_metadata.get("compiler_version")
                    == command.compiler_version
                ):
                    candidate = RevisionRecord.from_row(latest_generated)
                    candidate_replayed = (
                        candidate.idempotency_key.startswith(compile_prefix)
                        and len(candidate.idempotency_key)
                        == len(compile_prefix) + 64
                    )

            if candidate is None:
                revision_id = f"wrev_{uuid.uuid4().hex}"
                write_token = f"write_{uuid.uuid4().hex}"
                rendered = render_managed_frontmatter(
                    source_document,
                    page_id=locked.page["page_id"],
                    revision_id=revision_id,
                    write_token=write_token,
                    review_status="draft",
                )
                final_document = parse_wiki_bytes(rendered)
                revision_metadata = _plain(final_document.frontmatter)
                revision_metadata["source_hash"] = command.source_hash
                revision_metadata["compiler_version"] = command.compiler_version
                candidate = self._create_revision_locked(
                    locked.conn,
                    locked.page,
                    content=rendered,
                    origin="generated",
                    base_revision_id=old_current,
                    source_ids=command.source_ids,
                    actor=command.actor,
                    note=None,
                    idempotency_key=f"{compile_prefix}{state_hash}",
                    metadata=revision_metadata,
                    revision_id=revision_id,
                )
                candidate_replayed = candidate.id != revision_id
            candidate_document = parse_wiki_bytes(candidate.content.encode("utf-8"))
            token = candidate_document.frontmatter.get("lgdo_write_token")
            write_token = str(token) if token is not None else None

            timestamp = now_iso()
            locked.conn.execute(
                """
                UPDATE review_items
                SET status='superseded',resolved_at=?,updated_at=?
                WHERE page_id=? AND issue_type='content_conflict' AND status='pending'
                  AND (candidate_revision_id IS NULL OR candidate_revision_id<>?)
                """,
                (
                    timestamp,
                    timestamp,
                    locked.page["page_id"],
                    candidate.id,
                ),
            )
            expected_generated_sql = (
                "generated_revision_id IS NULL"
                if old_generated is None
                else "generated_revision_id=?"
            )
            params: list[Any] = [
                candidate.id,
                command.domain,
                command.page_type,
                command.title,
                json_dump(command.source_ids),
                command.owner,
                timestamp,
                locked.page["page_id"],
            ]
            if old_generated is not None:
                params.append(old_generated)
            advanced = locked.conn.execute(
                f"""
                UPDATE wiki_pages
                SET generated_revision_id=?,domain=?,page_type=?,title=?,
                    source_ids_json=?,owner=COALESCE(?,owner),updated_at=?
                WHERE page_id=? AND {expected_generated_sql}
                """,
                tuple(params),
            )
            if advanced.rowcount != 1:
                raise RevisionConflict(
                    "generated revision changed during compile",
                    current_revision_id=old_current,
                    pending_intent_id=locked.page.get("pending_write_intent_id"),
                )
            locked.page["generated_revision_id"] = candidate.id
            locked.page["domain"] = command.domain
            locked.page["page_type"] = command.page_type
            locked.page["title"] = command.title
            locked.page["source_ids_json"] = json_dump(command.source_ids)
            if command.owner is not None:
                locked.page["owner"] = command.owner

            should_write = (
                old_current != candidate.id
                and (
                    created
                    or (
                        old_current is not None
                        and old_current == old_generated
                        and current_revision is not None
                        and disk_hash == current_revision["file_hash"]
                    )
                )
            )
            if should_write:
                if write_token is None:
                    raise WikiRevisionError(
                        f"generated revision has no write token: {candidate.id}"
                    )
                intent_id = self.prepare_write_intent_locked(
                    locked.conn,
                    locked.page,
                    revision=candidate,
                    expected_revision_id=old_current,
                    expected_file_hash=disk_hash,
                    write_token=write_token,
                )
                prepared = MutationResult(
                    page_id=locked.page["page_id"],
                    page_path=locked.page["path"],
                    status="prepared",
                    revision_id=candidate.id,
                    current_revision_id=old_current,
                    generated_revision_id=candidate.id,
                    candidate_revision_id=candidate.id,
                    write_intent_id=intent_id,
                    replayed=candidate_replayed,
                )
            else:
                conflict_id = self._reconcile_pending_reviews_locked(
                    locked.conn,
                    locked.page["page_id"],
                )
                prepared = MutationResult(
                    page_id=locked.page["page_id"],
                    page_path=locked.page["path"],
                    status="conflicted" if conflict_id is not None else "ignored",
                    revision_id=candidate.id,
                    current_revision_id=old_current,
                    generated_revision_id=candidate.id,
                    candidate_revision_id=candidate.id,
                    conflict_review_id=conflict_id,
                    replayed=candidate_replayed,
                )

        if intent_id is None:
            return prepared

        return self._execute_generated_intent(
            intent_id,
            replayed=prepared.replayed,
            prepared=prepared,
        )

    def _prepared_generated_mutation(
        self,
        intent_id: str,
        *,
        replayed: bool,
    ) -> MutationResult:
        with connect_app(self.settings) as conn:
            intent = conn.execute(
                "SELECT * FROM vault_write_intents WHERE id=?",
                (intent_id,),
            ).fetchone()
            if intent is None:
                raise WikiRevisionError(f"write intent not found: {intent_id}")
            revision = conn.execute(
                "SELECT * FROM wiki_page_revisions WHERE id=?",
                (intent["revision_id"],),
            ).fetchone()
            if revision is None:
                raise WikiRevisionError(
                    f"write revision not found: {intent['revision_id']}"
                )
            page = conn.execute(
                "SELECT * FROM wiki_pages WHERE page_id=?",
                (intent["page_id"],),
            ).fetchone()
            if page is None:
                raise PageNotFound(str(intent["page_id"]))
        return MutationResult(
            page_id=page["page_id"],
            page_path=page["path"],
            status="prepared",
            revision_id=revision["id"],
            current_revision_id=page["current_revision_id"],
            generated_revision_id=page["generated_revision_id"],
            candidate_revision_id=revision["id"],
            write_intent_id=intent_id,
            replayed=replayed,
        )

    def _execute_generated_intent(
        self,
        intent_id: str,
        *,
        replayed: bool,
        prepared: MutationResult | None = None,
    ) -> MutationResult:
        pending = prepared or self._prepared_generated_mutation(
            intent_id,
            replayed=replayed,
        )
        from app.vault_writer import IntentExecutor

        result = IntentExecutor(self.settings).execute(intent_id)
        if result is not None:
            if result.intent_status != "applied":
                raise RevisionConflict(
                    "generated compile requires write recovery",
                    current_revision_id=pending.current_revision_id,
                    pending_intent_id=intent_id,
                )
            return replace(
                self._mutation_for_intent(intent_id),
                replayed=replayed,
            )

        with connect_app(self.settings) as conn:
            intent = conn.execute(
                "SELECT status FROM vault_write_intents WHERE id=?",
                (intent_id,),
            ).fetchone()
        if intent is None:
            raise WikiRevisionError(f"write intent not found: {intent_id}")
        if intent["status"] == "applied":
            return replace(
                self._mutation_for_intent(intent_id),
                replayed=replayed,
            )
        raise RevisionConflict(
            "generated compile intent is owned by another executor",
            current_revision_id=pending.current_revision_id,
            pending_intent_id=intent_id,
        )

    def _mutation_for_intent(self, intent_id: str) -> MutationResult:
        with connect_app(self.settings) as conn:
            intent = conn.execute(
                "SELECT * FROM vault_write_intents WHERE id=?",
                (intent_id,),
            ).fetchone()
            page = conn.execute(
                "SELECT * FROM wiki_pages WHERE page_id=?",
                (intent["page_id"],),
            ).fetchone()
            jobs = conn.execute(
                "SELECT id FROM knowledge_projection_jobs WHERE page_id=? AND revision_id=? ORDER BY target",
                (page["page_id"], intent["revision_id"]),
            ).fetchall()
        return MutationResult(
            page_id=page["page_id"],
            page_path=page["path"],
            status="applied",
            revision_id=intent["revision_id"],
            current_revision_id=intent["revision_id"],
            generated_revision_id=page["generated_revision_id"],
            write_intent_id=intent_id,
            projection_job_ids=tuple(row["id"] for row in jobs),
        )

    def finalize_intent(self, intent_id: str, executor_owner: str) -> MutationResult:
        with connect_app(self.settings) as read_conn:
            locator = read_conn.execute(
                "SELECT page_id,target_path FROM vault_write_intents WHERE id=?",
                (intent_id,),
            ).fetchone()
        if locator is None:
            raise WikiRevisionError(f"write intent not found: {intent_id}")
        target = self._target(locator["target_path"])
        with self.coordinator.lock_page(page_id=locator["page_id"]) as locked:
            installed_hash = self.writer._stream_hash(target)
            suffix = " FOR UPDATE" if self.settings.database_backend == "postgres" else ""
            intent = locked.conn.execute(
                "SELECT * FROM vault_write_intents WHERE id=?" + suffix,
                (intent_id,),
            ).fetchone()
            if intent is None:
                raise WikiRevisionError(f"write intent not found: {intent_id}")
            revision = locked.conn.execute(
                "SELECT * FROM wiki_page_revisions WHERE id=?",
                (intent["revision_id"],),
            ).fetchone()
            if revision is None:
                raise WikiRevisionError(f"write revision not found: {intent['revision_id']}")
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
            expected_params = (
                ()
                if intent["expected_revision_id"] is None
                else (intent["expected_revision_id"],)
            )
            next_epoch = int(locked.page["projection_epoch"] or 0) + 1
            metadata = json.loads(revision["metadata_json"] or "{}")
            advanced = locked.conn.execute(
                f"""
                UPDATE wiki_pages
                SET current_revision_id=?,file_hash=?,semantic_hash=?,last_write_token=?,
                    projection_epoch=?,rag_visible_revision_id=NULL,rag_visible_epoch=NULL,
                    lifecycle_status='active',sync_error=NULL,observed_file_hash=?,
                    pending_write_intent_id=NULL,review_status=?,owner=COALESCE(?,owner),updated_at=?
                WHERE page_id=? AND pending_write_intent_id=? AND {expected_sql}
                """,
                (
                    revision["id"],
                    revision["file_hash"],
                    revision["semantic_hash"],
                    intent["write_token"],
                    next_epoch,
                    revision["file_hash"],
                    metadata.get("review_status") or locked.page["review_status"],
                    metadata.get("owner"),
                    now_iso(),
                    intent["page_id"],
                    intent_id,
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
                {
                    **locked.page,
                    "current_revision_id": revision["id"],
                    "projection_epoch": next_epoch,
                },
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
                (now_iso(), intent_id, executor_owner),
            )
            if applied.rowcount != 1:
                raise WikiRevisionError("intent owner/status CAS failed during finalize")
            self._reconcile_pending_reviews_locked(locked.conn, intent["page_id"])
            audit(
                locked.conn,
                "wiki_revision_applied",
                {"intent_id": intent_id, "revision_id": revision["id"]},
                now_iso(),
            )
            return MutationResult(
                page_id=intent["page_id"],
                page_path=locked.page["path"],
                status="applied",
                revision_id=revision["id"],
                current_revision_id=revision["id"],
                generated_revision_id=locked.page["generated_revision_id"],
                write_intent_id=intent_id,
                projection_job_ids=tuple(jobs),
            )

    def _reconcile_pending_reviews_locked(
        self,
        conn: Any,
        page_id: str,
    ) -> str | None:
        page_row = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (page_id,),
        ).fetchone()
        if page_row is None:
            raise PageNotFound(page_id)
        page = dict(page_row)
        pending = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='content_conflict' AND status='pending'
            ORDER BY created_at,id
            """,
            (page_id,),
        ).fetchall()

        generated = None
        if page["generated_revision_id"] is not None:
            generated = conn.execute(
                "SELECT * FROM wiki_page_revisions WHERE id=?",
                (page["generated_revision_id"],),
            ).fetchone()
            if generated is None:
                raise WikiRevisionError(
                    f"generated revision not found: {page['generated_revision_id']}"
                )
        accepted = None
        if page["accepted_generated_revision_id"] is not None:
            accepted = conn.execute(
                "SELECT * FROM wiki_page_revisions WHERE id=?",
                (page["accepted_generated_revision_id"],),
            ).fetchone()
            if accepted is None:
                raise WikiRevisionError(
                    "accepted generated revision not found: "
                    f"{page['accepted_generated_revision_id']}"
                )

        has_conflict = (
            page["current_revision_id"] is not None
            and generated is not None
            and page["current_revision_id"] != page["generated_revision_id"]
            and (
                accepted is None
                or generated["semantic_hash"] != accepted["semantic_hash"]
            )
        )
        current_state_json = canonical_state_json(
            self._canonical_state_locked(conn, page)
        )
        matching = None
        for review in pending:
            if (
                has_conflict
                and review["base_revision_id"] == page["current_revision_id"]
                and review["candidate_revision_id"]
                == page["generated_revision_id"]
                and review["expected_state_json"] == current_state_json
            ):
                matching = review
                continue
            timestamp = now_iso()
            conn.execute(
                """
                UPDATE review_items
                SET status='superseded',resolved_at=?,updated_at=?
                WHERE id=? AND status='pending'
                """,
                (timestamp, timestamp, review["id"]),
            )

        if not has_conflict:
            return None
        if matching is not None:
            return str(matching["id"])

        timestamp = now_iso()
        review_id = f"review_{uuid.uuid4().hex}"
        expected_state = self._canonical_state_locked(conn, page)
        expected_state["pending_conflict"] = sorted(
            [*expected_state["pending_conflict"], review_id]
        )
        expected_state_json = canonical_state_json(expected_state)
        conn.execute(
            """
            INSERT INTO review_items(
              id,page_path,page_id,issue_type,status,owner,source_ids_json,
              created_at,updated_at,base_revision_id,candidate_revision_id,
              expected_state_json
            ) VALUES (?,?,?,'content_conflict','pending',?,?,?,?,?,?,?)
            """,
            (
                review_id,
                page["path"],
                page_id,
                page["owner"],
                generated["source_ids_json"],
                timestamp,
                timestamp,
                page["current_revision_id"],
                page["generated_revision_id"],
                expected_state_json,
            ),
        )
        return review_id
