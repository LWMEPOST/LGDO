from __future__ import annotations

import hashlib
import json
import os
import sqlite3
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
    FileObservationInput,
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


def _copy_file_no_replace(
    source: Path,
    destination: Path,
    *,
    chunk_size: int = 64 * 1024,
) -> None:
    with source.open("rb") as source_handle:
        with destination.open("xb") as destination_handle:
            while chunk := source_handle.read(chunk_size):
                destination_handle.write(chunk)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())


def _link_or_copy_file_no_replace(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except FileExistsError:
        raise
    except OSError:
        _copy_file_no_replace(source, destination)


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
                    source_document = parse_wiki_bytes(
                        command.content.encode("utf-8")
                    )
                    source_semantic_hash = compute_semantic_hash(source_document)
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
        if (
            page["pending_write_intent_id"] is not None
            or page["lifecycle_status"] != "active"
        ):
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

    def _lifecycle_event_state_locked(
        self,
        conn: Any,
        page: dict[str, Any],
    ) -> dict[str, Any]:
        state = self._canonical_state_locked(conn, page)
        state["page_id"] = page["page_id"]
        state["projection_epoch"] = int(page.get("projection_epoch") or 0)
        return state

    @staticmethod
    def _decoded_event_state(event: dict[str, Any]) -> dict[str, Any]:
        try:
            state = json.loads(event.get("expected_state_json") or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise RevisionConflict(
                "lifecycle event has no valid expected state",
                current_revision_id=event.get("result_revision_id"),
            ) from exc
        if not isinstance(state, dict) or not isinstance(state.get("page_id"), str):
            raise RevisionConflict(
                "lifecycle event has no stable page identity",
                current_revision_id=event.get("result_revision_id"),
            )
        return state

    @staticmethod
    def _validate_lifecycle_event_identity(
        event: dict[str, Any],
        *,
        event_id: str,
        kind: str,
        page_path: str,
        old_page_path: str | None,
    ) -> None:
        if (
            event.get("id") != event_id
            or event.get("kind") != kind
            or event.get("page_path") != page_path
            or event.get("old_page_path") != old_page_path
            or event.get("observation_id") is not None
        ):
            raise RevisionConflict(
                "lifecycle event identity changed",
                current_revision_id=event.get("result_revision_id"),
            )

    def _replay_lifecycle_event(
        self,
        event: dict[str, Any],
        *,
        kind: Literal["rename", "delete"],
    ) -> MutationResult:
        state = self._decoded_event_state(event)
        if event.get("status") != "applied":
            raise RevisionConflict(
                "lifecycle event has no replayable result",
                current_revision_id=event.get("result_revision_id")
                or state.get("current"),
            )
        page_id = state["page_id"]
        result_revision_id = event.get("result_revision_id")
        with connect_app(self.settings) as conn:
            page = conn.execute(
                "SELECT * FROM wiki_pages WHERE page_id=?",
                (page_id,),
            ).fetchone()
            if page is None:
                raise PageNotFound(page_id)
            revision = None
            if result_revision_id is not None:
                revision = conn.execute(
                    "SELECT * FROM wiki_page_revisions WHERE id=?",
                    (result_revision_id,),
                ).fetchone()
                if revision is None or revision["page_id"] != page_id:
                    raise RevisionConflict(
                        "lifecycle event result belongs to another page",
                        current_revision_id=state.get("current"),
                    )
            if kind == "rename":
                try:
                    metadata = json.loads(revision["metadata_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    metadata = None
                if (
                    revision is None
                    or revision["origin"] != "rename"
                    or revision["page_path"] != event["page_path"]
                    or revision["base_revision_id"] != state.get("current")
                    or not isinstance(metadata, dict)
                    or metadata.get("vault_change_event_id") != event["id"]
                    or metadata.get("old_page_path") != event["old_page_path"]
                    or metadata.get("page_path") != event["page_path"]
                ):
                    raise RevisionConflict(
                        "rename event audit revision does not match",
                        current_revision_id=state.get("current"),
                    )
            elif result_revision_id != state.get("current"):
                raise RevisionConflict(
                    "delete event result does not match expected current revision",
                    current_revision_id=state.get("current"),
                )
            result_epoch = int(state.get("projection_epoch") or 0) + 1
            jobs = conn.execute(
                """
                SELECT id FROM knowledge_projection_jobs
                WHERE page_id=? AND projection_epoch=? AND operation=?
                ORDER BY target
                """,
                (page_id, result_epoch, kind),
            ).fetchall()
        return MutationResult(
            page_id=page_id,
            page_path=event["page_path"],
            status="renamed" if kind == "rename" else "deleted",
            revision_id=result_revision_id,
            current_revision_id=state.get("current"),
            generated_revision_id=state.get("generated"),
            audit_revision_id=result_revision_id if kind == "rename" else None,
            projection_job_ids=tuple(str(row["id"]) for row in jobs),
            replayed=True,
        )

    def _existing_lifecycle_event(
        self,
        event_id: str,
        *,
        kind: Literal["rename", "delete"],
        page_path: str,
        old_page_path: str | None,
    ) -> dict[str, Any] | None:
        with connect_app(self.settings) as conn:
            row = conn.execute(
                "SELECT * FROM vault_change_events WHERE id=?",
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        event = dict(row)
        self._validate_lifecycle_event_identity(
            event,
            event_id=event_id,
            kind=kind,
            page_path=page_path,
            old_page_path=old_page_path,
        )
        return event

    def _persist_lifecycle_occurrence(
        self,
        event_id: str,
        *,
        kind: Literal["rename", "delete"],
        page_path: str,
        old_page_path: str | None,
        stable_page_id: str | None,
    ) -> tuple[dict[str, Any], bool]:
        lookup_path = old_page_path if kind == "rename" else page_path
        timestamp = now_iso()
        suffix = " FOR UPDATE" if self.settings.database_backend == "postgres" else ""
        with connect_app_write(self.settings) as conn:
            existing = conn.execute(
                "SELECT * FROM vault_change_events WHERE id=?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                event = dict(existing)
                self._validate_lifecycle_event_identity(
                    event,
                    event_id=event_id,
                    kind=kind,
                    page_path=page_path,
                    old_page_path=old_page_path,
                )
                return event, False

            if stable_page_id is not None:
                row = conn.execute(
                    "SELECT * FROM wiki_pages WHERE page_id=?" + suffix,
                    (stable_page_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM wiki_pages WHERE path=?" + suffix,
                    (lookup_path,),
                ).fetchone()
            if row is None or row["page_id"] is None:
                raise PageNotFound(str(lookup_path or stable_page_id))
            page = dict(row)
            if page["path"] != lookup_path:
                raise RevisionConflict(
                    "lifecycle event page path does not match stable identity",
                    current_revision_id=page.get("current_revision_id"),
                    pending_intent_id=page.get("pending_write_intent_id"),
                )
            if stable_page_id is not None and page["page_id"] != stable_page_id:
                raise RevisionConflict(
                    "lifecycle event managed page identity changed",
                    current_revision_id=page.get("current_revision_id"),
                )
            if page.get("pending_write_intent_id") is not None:
                raise RevisionConflict(
                    "page already has a pending write",
                    current_revision_id=page.get("current_revision_id"),
                    pending_intent_id=page.get("pending_write_intent_id"),
                )
            if kind == "rename":
                occupied = conn.execute(
                    "SELECT page_id FROM wiki_pages WHERE path=?" + suffix,
                    (page_path,),
                ).fetchone()
                if occupied is not None and occupied["page_id"] != page["page_id"]:
                    raise RevisionConflict(
                        "rename target path already belongs to another page",
                        current_revision_id=page.get("current_revision_id"),
                    )
            expected_state_json = canonical_state_json(
                self._lifecycle_event_state_locked(conn, page)
            )
            inserted = conn.execute(
                """
                INSERT INTO vault_change_events(
                  id,kind,page_path,old_page_path,observation_id,
                  expected_state_json,status,detected_at,updated_at
                ) VALUES (?,?,?,?,NULL,?,'pending',?,?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    event_id,
                    kind,
                    page_path,
                    old_page_path,
                    expected_state_json,
                    timestamp,
                    timestamp,
                ),
            )
            stored = conn.execute(
                "SELECT * FROM vault_change_events WHERE id=?",
                (event_id,),
            ).fetchone()
            if stored is None:
                raise WikiRevisionError(
                    f"lifecycle event insert did not produce a row: {event_id}"
                )
            event = dict(stored)
            self._validate_lifecycle_event_identity(
                event,
                event_id=event_id,
                kind=kind,
                page_path=page_path,
                old_page_path=old_page_path,
            )
            return event, inserted.rowcount == 1

    @staticmethod
    def _managed_page_id(raw_bytes: bytes) -> str | None:
        document = parse_wiki_bytes(raw_bytes)
        value = document.frontmatter.get("lgdo_page_id") or document.frontmatter.get(
            "id"
        )
        return str(value) if isinstance(value, str) and value else None

    @staticmethod
    def _is_unique_path_error(exc: Exception) -> bool:
        if isinstance(exc, sqlite3.IntegrityError):
            return "unique" in str(exc).lower() and "wiki_pages.path" in str(exc)
        return getattr(exc, "sqlstate", None) == "23505"

    def rename_page(
        self,
        event_id: str,
        old_path: str,
        new_path: str,
    ) -> MutationResult:
        self._target(old_path)
        new_target = self._target(new_path)
        if old_path == new_path:
            raise RevisionConflict(
                "rename target must differ from the old path",
                current_revision_id=None,
            )
        event = self._existing_lifecycle_event(
            event_id,
            kind="rename",
            page_path=new_path,
            old_page_path=old_path,
        )
        if event is not None and event["status"] == "applied":
            return self._replay_lifecycle_event(event, kind="rename")

        def read_renamed_file() -> tuple[bytes, str]:
            try:
                content = new_target.read_bytes()
            except OSError as exc:
                raise PageNotFound(new_path) from exc
            try:
                page_id = self._managed_page_id(content)
            except MarkdownParseError as exc:
                raise RevisionConflict(
                    "renamed file has invalid managed identity",
                    current_revision_id=None,
                ) from exc
            if page_id is None:
                raise RevisionConflict(
                    "renamed file has no managed page identity",
                    current_revision_id=None,
                )
            return content, page_id

        renamed_file: tuple[bytes, str] | None = None
        if event is None:
            renamed_file = read_renamed_file()
            event, _ = self._persist_lifecycle_occurrence(
                event_id,
                kind="rename",
                page_path=new_path,
                old_page_path=old_path,
                stable_page_id=renamed_file[1],
            )
        state = self._decoded_event_state(event)

        pending_conflict: RevisionConflict | None = None
        replay_event: dict[str, Any] | None = None
        result: MutationResult | None = None
        try:
            with self.coordinator.lock_page(page_id=state["page_id"]) as locked:
                stored_row = locked.conn.execute(
                    "SELECT * FROM vault_change_events WHERE id=?",
                    (event_id,),
                ).fetchone()
                if stored_row is None:
                    raise WikiRevisionError(f"rename event disappeared: {event_id}")
                stored = dict(stored_row)
                self._validate_lifecycle_event_identity(
                    stored,
                    event_id=event_id,
                    kind="rename",
                    page_path=new_path,
                    old_page_path=old_path,
                )
                if stored["status"] == "applied":
                    replay_event = stored
                elif stored["status"] != "pending":
                    pending_conflict = RevisionConflict(
                        "rename event is no longer pending",
                        current_revision_id=stored.get("result_revision_id")
                        or locked.page.get("current_revision_id"),
                    )
                else:
                    current_state_json = canonical_state_json(
                        self._lifecycle_event_state_locked(locked.conn, locked.page)
                    )
                    if current_state_json != stored["expected_state_json"]:
                        superseded = locked.conn.execute(
                            """
                            UPDATE vault_change_events
                            SET status='superseded',updated_at=?
                            WHERE id=? AND status='pending'
                            """,
                            (now_iso(), event_id),
                        )
                        if superseded.rowcount != 1:
                            raise WikiRevisionError(
                                "rename event stale-state CAS failed"
                            )
                        pending_conflict = RevisionConflict(
                            "rename event expected state changed",
                            current_revision_id=locked.page.get(
                                "current_revision_id"
                            ),
                            pending_intent_id=locked.page.get(
                                "pending_write_intent_id"
                            ),
                        )
                    elif locked.page.get("pending_write_intent_id") is not None:
                        pending_conflict = RevisionConflict(
                            "page already has a pending write",
                            current_revision_id=locked.page.get(
                                "current_revision_id"
                            ),
                            pending_intent_id=locked.page.get(
                                "pending_write_intent_id"
                            ),
                        )
                    elif locked.page["path"] != old_path:
                        pending_conflict = RevisionConflict(
                            "rename source path changed",
                            current_revision_id=locked.page.get(
                                "current_revision_id"
                            ),
                        )
                    else:
                        if renamed_file is None:
                            renamed_file = read_renamed_file()
                        renamed_bytes, managed_page_id = renamed_file
                        if managed_page_id != state["page_id"]:
                            raise RevisionConflict(
                                "renamed file identity does not match lifecycle event",
                                current_revision_id=state.get("current"),
                            )
                        current = locked.conn.execute(
                            "SELECT * FROM wiki_page_revisions WHERE id=?",
                            (locked.page.get("current_revision_id"),),
                        ).fetchone()
                        if current is None:
                            raise WikiRevisionError(
                                "rename page has no current revision"
                            )
                        current_bytes = current["content"].encode("utf-8")
                        if renamed_bytes != current_bytes:
                            pending_conflict = RevisionConflict(
                                "renamed file bytes differ from current revision",
                                current_revision_id=locked.page.get(
                                    "current_revision_id"
                                ),
                            )
                        else:
                            occupied = locked.conn.execute(
                                "SELECT page_id FROM wiki_pages WHERE path=?",
                                (new_path,),
                            ).fetchone()
                            if (
                                occupied is not None
                                and occupied["page_id"] != locked.page["page_id"]
                            ):
                                pending_conflict = RevisionConflict(
                                    "rename target path already belongs to another page",
                                    current_revision_id=locked.page.get(
                                        "current_revision_id"
                                    ),
                                )
                            else:
                                previous_epoch = int(
                                    locked.page.get("projection_epoch") or 0
                                )
                                next_epoch = previous_epoch + 1
                                transitioned = locked.conn.execute(
                                    """
                                    UPDATE wiki_pages
                                    SET path=?,projection_epoch=?,
                                        rag_visible_revision_id=NULL,
                                        rag_visible_epoch=NULL,updated_at=?
                                    WHERE page_id=? AND path=?
                                      AND current_revision_id=?
                                      AND projection_epoch=?
                                      AND pending_write_intent_id IS NULL
                                      AND NOT EXISTS (
                                        SELECT 1 FROM wiki_pages AS occupied
                                        WHERE occupied.path=?
                                          AND occupied.page_id<>?
                                      )
                                    """,
                                    (
                                        new_path,
                                        next_epoch,
                                        now_iso(),
                                        locked.page["page_id"],
                                        old_path,
                                        locked.page["current_revision_id"],
                                        previous_epoch,
                                        new_path,
                                        locked.page["page_id"],
                                    ),
                                )
                                if transitioned.rowcount != 1:
                                    raise RevisionConflict(
                                        "page changed during rename transition",
                                        current_revision_id=locked.page.get(
                                            "current_revision_id"
                                        ),
                                        pending_intent_id=locked.page.get(
                                            "pending_write_intent_id"
                                        ),
                                    )
                                locked.page.update(
                                    path=new_path,
                                    projection_epoch=next_epoch,
                                    rag_visible_revision_id=None,
                                    rag_visible_epoch=None,
                                )
                                state_digest = hashlib.sha256(
                                    stored["expected_state_json"].encode("utf-8")
                                ).hexdigest()
                                audit_revision = self._create_revision_locked(
                                    locked.conn,
                                    locked.page,
                                    content=current_bytes,
                                    origin="rename",
                                    base_revision_id=locked.page[
                                        "current_revision_id"
                                    ],
                                    source_ids=json.loads(
                                        current["source_ids_json"] or "[]"
                                    ),
                                    actor="vault-observer",
                                    note=None,
                                    idempotency_key=(
                                        f"rename:{event_id}:"
                                        f"{locked.page['page_id']}:{state_digest}"
                                    ),
                                    metadata={
                                        "vault_change_event_id": event_id,
                                        "old_page_path": old_path,
                                        "page_path": new_path,
                                        "projection_epoch": next_epoch,
                                    },
                                )
                                locked.conn.execute(
                                    "UPDATE review_items SET page_path=? WHERE page_id=?",
                                    (new_path, locked.page["page_id"]),
                                )
                                self._reconcile_pending_reviews_locked(
                                    locked.conn,
                                    locked.page["page_id"],
                                    allow_conflicts=False,
                                )
                                review_timestamp = now_iso()
                                locked.conn.execute(
                                    """
                                    UPDATE review_items
                                    SET status='superseded',resolved_at=?,updated_at=?
                                    WHERE page_id=? AND status='pending'
                                    """,
                                    (
                                        review_timestamp,
                                        review_timestamp,
                                        locked.page["page_id"],
                                    ),
                                )
                                self.outbox.supersede_stale(
                                    locked.conn,
                                    locked.page["page_id"],
                                    next_epoch,
                                )
                                jobs = self.outbox.enqueue_pair_for_state(
                                    locked.conn,
                                    locked.page,
                                    "rename",
                                    {"old_path": old_path, "path": new_path},
                                )
                                applied = locked.conn.execute(
                                    """
                                    UPDATE vault_change_events
                                    SET status='applied',result_revision_id=?,updated_at=?
                                    WHERE id=? AND kind='rename' AND status='pending'
                                      AND old_page_path=? AND page_path=?
                                    """,
                                    (
                                        audit_revision.id,
                                        now_iso(),
                                        event_id,
                                        old_path,
                                        new_path,
                                    ),
                                )
                                if applied.rowcount != 1:
                                    raise WikiRevisionError(
                                        "rename event status CAS failed"
                                    )
                                audit(
                                    locked.conn,
                                    "wiki_page_renamed",
                                    {
                                        "event_id": event_id,
                                        "page_id": locked.page["page_id"],
                                        "old_path": old_path,
                                        "path": new_path,
                                        "audit_revision_id": audit_revision.id,
                                    },
                                    now_iso(),
                                )
                                result = MutationResult(
                                    page_id=locked.page["page_id"],
                                    page_path=new_path,
                                    status="renamed",
                                    revision_id=audit_revision.id,
                                    current_revision_id=locked.page[
                                        "current_revision_id"
                                    ],
                                    generated_revision_id=locked.page.get(
                                        "generated_revision_id"
                                    ),
                                    audit_revision_id=audit_revision.id,
                                    projection_job_ids=tuple(jobs),
                                )
        except Exception as exc:
            if self._is_unique_path_error(exc):
                raise RevisionConflict(
                    "rename target path already belongs to another page",
                    current_revision_id=state.get("current"),
                ) from exc
            raise
        if replay_event is not None:
            return self._replay_lifecycle_event(replay_event, kind="rename")
        if pending_conflict is not None:
            raise pending_conflict
        if result is None:
            raise WikiRevisionError("rename event did not produce a result")
        return result

    def delete_page(self, event_id: str, page_path: str) -> MutationResult:
        target = self._target(page_path)
        event = self._existing_lifecycle_event(
            event_id,
            kind="delete",
            page_path=page_path,
            old_page_path=None,
        )
        if event is not None and event["status"] == "applied":
            return self._replay_lifecycle_event(event, kind="delete")
        if event is None:
            if target.exists():
                raise RevisionConflict(
                    "delete event observed a file that still exists",
                    current_revision_id=None,
                )
            event, _ = self._persist_lifecycle_occurrence(
                event_id,
                kind="delete",
                page_path=page_path,
                old_page_path=None,
                stable_page_id=None,
            )
        state = self._decoded_event_state(event)
        pending_conflict: RevisionConflict | None = None
        replay_event: dict[str, Any] | None = None
        result: MutationResult | None = None
        with self.coordinator.lock_page(page_id=state["page_id"]) as locked:
            stored_row = locked.conn.execute(
                "SELECT * FROM vault_change_events WHERE id=?",
                (event_id,),
            ).fetchone()
            if stored_row is None:
                raise WikiRevisionError(f"delete event disappeared: {event_id}")
            stored = dict(stored_row)
            self._validate_lifecycle_event_identity(
                stored,
                event_id=event_id,
                kind="delete",
                page_path=page_path,
                old_page_path=None,
            )
            if stored["status"] == "applied":
                replay_event = stored
            elif stored["status"] != "pending":
                pending_conflict = RevisionConflict(
                    "delete event is no longer pending",
                    current_revision_id=stored.get("result_revision_id")
                    or locked.page.get("current_revision_id"),
                )
            else:
                current_state_json = canonical_state_json(
                    self._lifecycle_event_state_locked(locked.conn, locked.page)
                )
                if current_state_json != stored["expected_state_json"]:
                    superseded = locked.conn.execute(
                        """
                        UPDATE vault_change_events
                        SET status='superseded',updated_at=?
                        WHERE id=? AND status='pending'
                        """,
                        (now_iso(), event_id),
                    )
                    if superseded.rowcount != 1:
                        raise WikiRevisionError("delete event stale-state CAS failed")
                    pending_conflict = RevisionConflict(
                        "delete event expected state changed",
                        current_revision_id=locked.page.get("current_revision_id"),
                        pending_intent_id=locked.page.get(
                            "pending_write_intent_id"
                        ),
                    )
                elif locked.page.get("pending_write_intent_id") is not None:
                    pending_conflict = RevisionConflict(
                        "page already has a pending write",
                        current_revision_id=locked.page.get("current_revision_id"),
                        pending_intent_id=locked.page.get(
                            "pending_write_intent_id"
                        ),
                    )
                elif locked.page["path"] != page_path:
                    pending_conflict = RevisionConflict(
                        "delete page path changed",
                        current_revision_id=locked.page.get("current_revision_id"),
                    )
                elif target.exists():
                    pending_conflict = RevisionConflict(
                        "delete event observed a file that still exists",
                        current_revision_id=locked.page.get("current_revision_id"),
                    )
                elif locked.page.get("lifecycle_status") == "deleted":
                    pending_conflict = RevisionConflict(
                        "page is already deleted by another event",
                        current_revision_id=locked.page.get("current_revision_id"),
                    )
                else:
                    previous_epoch = int(locked.page.get("projection_epoch") or 0)
                    next_epoch = previous_epoch + 1
                    timestamp = now_iso()
                    transitioned = locked.conn.execute(
                        """
                        UPDATE wiki_pages
                        SET lifecycle_status='deleted',deleted_at=?,sync_error=NULL,
                            observed_file_hash=NULL,projection_epoch=?,
                            rag_visible_revision_id=NULL,rag_visible_epoch=NULL,
                            updated_at=?
                        WHERE page_id=? AND path=? AND projection_epoch=?
                          AND current_revision_id=?
                          AND pending_write_intent_id IS NULL
                        """,
                        (
                            timestamp,
                            next_epoch,
                            timestamp,
                            locked.page["page_id"],
                            page_path,
                            previous_epoch,
                            locked.page.get("current_revision_id"),
                        ),
                    )
                    if transitioned.rowcount != 1:
                        raise RevisionConflict(
                            "page changed during delete transition",
                            current_revision_id=locked.page.get(
                                "current_revision_id"
                            ),
                            pending_intent_id=locked.page.get(
                                "pending_write_intent_id"
                            ),
                        )
                    locked.page.update(
                        lifecycle_status="deleted",
                        deleted_at=timestamp,
                        sync_error=None,
                        observed_file_hash=None,
                        projection_epoch=next_epoch,
                        rag_visible_revision_id=None,
                        rag_visible_epoch=None,
                    )
                    self._reconcile_pending_reviews_locked(
                        locked.conn,
                        locked.page["page_id"],
                        allow_conflicts=False,
                    )
                    locked.conn.execute(
                        """
                        UPDATE review_items
                        SET status='superseded',resolved_at=?,updated_at=?
                        WHERE page_id=? AND issue_type='invalid_frontmatter'
                          AND status='pending'
                        """,
                        (timestamp, timestamp, locked.page["page_id"]),
                    )
                    self.outbox.supersede_stale(
                        locked.conn,
                        locked.page["page_id"],
                        next_epoch,
                    )
                    jobs = self.outbox.enqueue_pair_for_state(
                        locked.conn,
                        {**locked.page, "current_revision_id": None},
                        "delete",
                        {"path": page_path, "reason": "deleted"},
                    )
                    applied = locked.conn.execute(
                        """
                        UPDATE vault_change_events
                        SET status='applied',result_revision_id=?,updated_at=?
                        WHERE id=? AND kind='delete' AND status='pending'
                          AND page_path=? AND old_page_path IS NULL
                        """,
                        (
                            locked.page.get("current_revision_id"),
                            timestamp,
                            event_id,
                            page_path,
                        ),
                    )
                    if applied.rowcount != 1:
                        raise WikiRevisionError("delete event status CAS failed")
                    audit(
                        locked.conn,
                        "wiki_page_deleted",
                        {
                            "event_id": event_id,
                            "page_id": locked.page["page_id"],
                            "path": page_path,
                            "current_revision_id": locked.page.get(
                                "current_revision_id"
                            ),
                        },
                        timestamp,
                    )
                    result = MutationResult(
                        page_id=locked.page["page_id"],
                        page_path=page_path,
                        status="deleted",
                        revision_id=locked.page.get("current_revision_id"),
                        current_revision_id=locked.page.get(
                            "current_revision_id"
                        ),
                        generated_revision_id=locked.page.get(
                            "generated_revision_id"
                        ),
                        projection_job_ids=tuple(jobs),
                    )
        if replay_event is not None:
            return self._replay_lifecycle_event(replay_event, kind="delete")
        if pending_conflict is not None:
            raise pending_conflict
        if result is None:
            raise WikiRevisionError("delete event did not produce a result")
        return result

    def ensure_projection_jobs(self, page_id: str | None = None) -> list[str]:
        with connect_app(self.settings) as conn:
            if page_id is None:
                rows = conn.execute(
                    """
                    SELECT page_id FROM wiki_pages
                    WHERE page_id IS NOT NULL ORDER BY page_id
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT page_id FROM wiki_pages WHERE page_id=?",
                    (page_id,),
                ).fetchall()
        page_ids = [str(row["page_id"]) for row in rows]
        if page_id is not None and not page_ids:
            raise PageNotFound(page_id)

        job_ids: list[str] = []
        for stable_page_id in page_ids:
            with self.coordinator.lock_page(page_id=stable_page_id) as locked:
                lifecycle = locked.page.get("lifecycle_status")
                current_revision_id = locked.page.get("current_revision_id")
                operation: str | None
                payload: dict[str, Any]
                projection_page = locked.page
                if lifecycle == "active" and current_revision_id is not None:
                    operation = "upsert"
                    payload = {"path": locked.page["path"]}
                elif lifecycle in {"deleted", "invalid"}:
                    operation = "delete"
                    payload = {
                        "path": locked.page["path"],
                        "reason": locked.page.get("sync_error") or lifecycle,
                    }
                    projection_page = {
                        **locked.page,
                        "current_revision_id": None,
                    }
                else:
                    operation = None
                    payload = {}
                if operation is None:
                    continue
                epoch = int(locked.page.get("projection_epoch") or 0)
                self.outbox.supersede_stale(
                    locked.conn,
                    stable_page_id,
                    epoch,
                )
                job_ids.extend(
                    self.outbox.enqueue_pair_for_state(
                        locked.conn,
                        projection_page,
                        operation,
                        payload,
                    )
                )
        return job_ids

    @staticmethod
    def _observation_matches_input(
        stored: Any,
        observation: FileObservationInput,
    ) -> bool:
        def blob(value: Any) -> bytes | None:
            return None if value is None else bytes(value)

        return (
            stored["file_hash"] == observation.file_hash
            and int(stored["size_bytes"]) == observation.size_bytes
            and int(stored["mtime_ns"]) == observation.mtime_ns
            and blob(stored["content_bytes"]) == observation.content_bytes
            and blob(stored["content_prefix"]) == observation.content_prefix
            and bool(stored["content_truncated"])
            == bool(observation.content_truncated)
        )

    def _persist_external_occurrence(
        self,
        event_id: str,
        page_path: str,
        observation: FileObservationInput,
    ) -> tuple[dict[str, Any], bool]:
        timestamp = now_iso()
        with connect_app_write(self.settings) as conn:
            existing_event = conn.execute(
                "SELECT * FROM vault_change_events WHERE id=?",
                (event_id,),
            ).fetchone()
            if existing_event is not None:
                event = dict(existing_event)
                if event["page_path"] != page_path:
                    raise RevisionConflict(
                        "external event belongs to another page",
                        current_revision_id=event["result_revision_id"],
                    )
                stored_observation = conn.execute(
                    "SELECT * FROM wiki_file_observations WHERE id=?",
                    (event["observation_id"],),
                ).fetchone()
                if (
                    stored_observation is None
                    or stored_observation["page_path"] != page_path
                    or not self._observation_matches_input(
                        stored_observation,
                        observation,
                    )
                ):
                    raise RevisionConflict(
                        "external event observation payload changed",
                        current_revision_id=event["result_revision_id"],
                    )
                return event, False

            suffix = " FOR UPDATE" if self.settings.database_backend == "postgres" else ""
            page_row = conn.execute(
                "SELECT * FROM wiki_pages WHERE path=?" + suffix,
                (page_path,),
            ).fetchone()
            if page_row is None or page_row["page_id"] is None:
                raise PageNotFound(page_path)
            page = dict(page_row)
            observed = conn.execute(
                """
                SELECT * FROM wiki_file_observations
                WHERE page_path=? AND file_hash=?
                """,
                (page_path, observation.file_hash),
            ).fetchone()
            if observed is None:
                observation_id = f"wobs_{uuid.uuid4().hex}"
                truncated = bool(observation.content_truncated)
                conn.execute(
                    """
                    INSERT INTO wiki_file_observations(
                      id,page_id,page_path,file_hash,size_bytes,mtime_ns,content_bytes,
                      content_prefix,content_truncated,parse_status,error_code,
                      error_message,observed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(page_path,file_hash) DO NOTHING
                    """,
                    (
                        observation_id,
                        page["page_id"],
                        page_path,
                        observation.file_hash,
                        observation.size_bytes,
                        observation.mtime_ns,
                        observation.content_bytes,
                        observation.content_prefix,
                        truncated,
                        "invalid" if truncated else "pending",
                        "file_too_large" if truncated else None,
                        "file exceeds the observation capture limit" if truncated else None,
                        timestamp,
                    ),
                )
            observed = conn.execute(
                """
                SELECT * FROM wiki_file_observations
                WHERE page_path=? AND file_hash=?
                """,
                (page_path, observation.file_hash),
            ).fetchone()
            if observed is None:
                raise WikiRevisionError(
                    "observation insert did not produce a natural-key row"
                )
            if (
                observed["page_id"] != page["page_id"]
                or observed["page_path"] != page_path
                or observed["file_hash"] != observation.file_hash
            ):
                raise RevisionConflict(
                    "observation belongs to another page identity",
                    current_revision_id=page["current_revision_id"],
                )
            observation_id = str(observed["id"])
            expected_state_json = canonical_state_json(
                self._canonical_state_locked(conn, page)
            )

            inserted = conn.execute(
                """
                INSERT INTO vault_change_events(
                  id,kind,page_path,observation_id,expected_state_json,status,
                  detected_at,updated_at
                ) VALUES (?,'modify',?,?,?,'pending',?,?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    event_id,
                    page_path,
                    observation_id,
                    expected_state_json,
                    timestamp,
                    timestamp,
                ),
            )
            event = conn.execute(
                "SELECT * FROM vault_change_events WHERE id=?",
                (event_id,),
            ).fetchone()
            if event is None:
                raise WikiRevisionError(
                    f"external event insert did not produce a row: {event_id}"
                )
            event_data = dict(event)
            if (
                event_data["page_path"] != page_path
                or event_data["observation_id"] != observation_id
            ):
                raise RevisionConflict(
                    "external event observation payload changed",
                    current_revision_id=page["current_revision_id"],
                )
            return event_data, inserted.rowcount == 1

    def _replay_external_event(
        self,
        event: dict[str, Any],
    ) -> MutationResult:
        with connect_app(self.settings) as conn:
            stored_observation = conn.execute(
                "SELECT * FROM wiki_file_observations WHERE id=?",
                (event["observation_id"],),
            ).fetchone()
            if (
                stored_observation is None
                or stored_observation["page_id"] is None
                or stored_observation["page_path"] != event["page_path"]
            ):
                raise RevisionConflict(
                    "external event has no stable observation page identity",
                    current_revision_id=event.get("result_revision_id"),
                )
            stable_page_id = str(stored_observation["page_id"])
            page_row = conn.execute(
                "SELECT * FROM wiki_pages WHERE page_id=?",
                (stable_page_id,),
            ).fetchone()
            if page_row is None:
                raise PageNotFound(stable_page_id)
            page = dict(page_row)
            if event["status"] not in {"applied", "invalid", "failed", "ignored"}:
                raise RevisionConflict(
                    "external change event has no replayable result",
                    current_revision_id=event["result_revision_id"]
                    or page.get("current_revision_id"),
                    pending_intent_id=page.get("pending_write_intent_id"),
                )
            try:
                expected_state = json.loads(event["expected_state_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                expected_state = None
            if not isinstance(expected_state, dict) or "current" not in expected_state:
                raise RevisionConflict(
                    "external event has no replayable expected state",
                    current_revision_id=event.get("result_revision_id"),
                    pending_intent_id=page.get("pending_write_intent_id"),
                )
            revision = None
            if event["result_revision_id"] is not None:
                revision = conn.execute(
                    "SELECT * FROM wiki_page_revisions WHERE id=?",
                    (event["result_revision_id"],),
                ).fetchone()
                if revision is None or revision["page_id"] != stable_page_id:
                    raise RevisionConflict(
                        "external event result revision belongs to another page",
                        current_revision_id=event.get("result_revision_id"),
                        pending_intent_id=page.get("pending_write_intent_id"),
                    )
            intent = None
            if event["status"] == "applied":
                try:
                    metadata = (
                        json.loads(revision["metadata_json"] or "{}")
                        if revision is not None
                        else None
                    )
                except (TypeError, json.JSONDecodeError):
                    metadata = None
                expected_key = (
                    f"external:{event['id']}:"
                    f"{hashlib.sha256(event['expected_state_json'].encode('utf-8')).hexdigest()}"
                )
                is_external_successor = (
                    revision is not None
                    and revision["origin"] == "external"
                    and isinstance(metadata, dict)
                    and metadata.get("vault_change_event_id") == event["id"]
                )
                if is_external_successor:
                    intent = conn.execute(
                        """
                        SELECT * FROM vault_write_intents
                        WHERE revision_id=? ORDER BY created_at,id LIMIT 1
                        """,
                        (event["result_revision_id"],),
                    ).fetchone()
                    if (
                        stored_observation is None
                        or intent is None
                        or revision["page_id"] != stable_page_id
                        or revision["page_path"] != event["page_path"]
                        or revision["base_revision_id"] != expected_state["current"]
                        or revision["idempotency_key"] != expected_key
                        or metadata.get("observation_id")
                        != event["observation_id"]
                        or metadata.get("observed_file_hash")
                        != stored_observation["file_hash"]
                        or intent["page_id"] != stable_page_id
                        or intent["revision_id"] != revision["id"]
                        or intent["expected_revision_id"]
                        != expected_state["current"]
                        or intent["expected_file_hash"]
                        != stored_observation["file_hash"]
                        or intent["target_path"] != event["page_path"]
                        or intent["status"] != "applied"
                    ):
                        raise RevisionConflict(
                            "applied external event does not match its revision intent",
                            current_revision_id=page.get("current_revision_id"),
                            pending_intent_id=page.get("pending_write_intent_id"),
                        )
                else:
                    observed_content = stored_observation["content_bytes"]
                    if (
                        revision is None
                        or event["result_revision_id"] != expected_state["current"]
                        or stored_observation["parse_status"] != "valid"
                        or observed_content is None
                        or bytes(observed_content)
                        != revision["content"].encode("utf-8")
                        or stored_observation["file_hash"]
                        != revision["file_hash"]
                    ):
                        raise RevisionConflict(
                            "applied external event does not match reused current bytes",
                            current_revision_id=page.get("current_revision_id"),
                            pending_intent_id=page.get("pending_write_intent_id"),
                        )
            review = None
            if event["status"] == "invalid":
                review = conn.execute(
                    """
                    SELECT id FROM review_items
                    WHERE page_id=? AND issue_type='invalid_frontmatter'
                      AND base_revision_id=?
                    ORDER BY created_at DESC,id DESC LIMIT 1
                    """,
                    (stable_page_id, event["result_revision_id"]),
                ).fetchone()
        return MutationResult(
            page_id=stable_page_id,
            page_path=event["page_path"],
            status=event["status"],
            revision_id=event["result_revision_id"],
            current_revision_id=event["result_revision_id"],
            generated_revision_id=expected_state.get("generated"),
            write_intent_id=str(intent["id"]) if intent is not None else None,
            conflict_review_id=str(review["id"]) if review is not None else None,
            observation_id=event["observation_id"],
            replayed=True,
        )

    def _prepared_external_intent_locked(
        self,
        locked: LockedPage,
        *,
        event: dict[str, Any],
        observation: dict[str, Any],
    ) -> MutationResult | None:
        revision_id = event.get("result_revision_id")
        if revision_id is None:
            return None
        revision_row = locked.conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (revision_id,),
        ).fetchone()
        intent_row = locked.conn.execute(
            """
            SELECT * FROM vault_write_intents
            WHERE revision_id=? ORDER BY created_at,id LIMIT 1
            """,
            (revision_id,),
        ).fetchone()
        try:
            expected_state = json.loads(event["expected_state_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            expected_state = None
        metadata = None
        if revision_row is not None:
            try:
                metadata = json.loads(revision_row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = None
        expected_state_json = event["expected_state_json"] or "{}"
        expected_key = (
            f"external:{event['id']}:"
            f"{hashlib.sha256(expected_state_json.encode('utf-8')).hexdigest()}"
        )
        if (
            not isinstance(expected_state, dict)
            or "current" not in expected_state
            or revision_row is None
            or intent_row is None
            or not isinstance(metadata, dict)
            or revision_row["page_id"] != locked.page["page_id"]
            or revision_row["page_path"] != event["page_path"]
            or revision_row["origin"] != "external"
            or revision_row["base_revision_id"] != expected_state["current"]
            or revision_row["idempotency_key"] != expected_key
            or metadata.get("vault_change_event_id") != event["id"]
            or metadata.get("observation_id") != event["observation_id"]
            or metadata.get("observed_file_hash") != observation["file_hash"]
            or intent_row["page_id"] != locked.page["page_id"]
            or intent_row["revision_id"] != revision_row["id"]
            or intent_row["expected_revision_id"] != expected_state["current"]
            or intent_row["expected_file_hash"] != observation["file_hash"]
            or intent_row["target_path"] != event["page_path"]
            or intent_row["status"]
            not in {"pending", "captured", "installed", "recovery_required"}
            or locked.page.get("pending_write_intent_id") != intent_row["id"]
        ):
            raise RevisionConflict(
                "pending external event does not match its prepared intent",
                current_revision_id=locked.page.get("current_revision_id"),
                pending_intent_id=locked.page.get("pending_write_intent_id"),
            )
        return MutationResult(
            page_id=locked.page["page_id"],
            page_path=event["page_path"],
            status="prepared",
            revision_id=revision_row["id"],
            current_revision_id=expected_state["current"],
            generated_revision_id=locked.page.get("generated_revision_id"),
            write_intent_id=intent_row["id"],
            observation_id=event["observation_id"],
            replayed=True,
        )

    def _invalid_external_change_locked(
        self,
        locked: LockedPage,
        *,
        event_id: str,
        observation: dict[str, Any],
        expected_state_json: str,
        error_code: str,
        error_message: str,
    ) -> MutationResult:
        page = locked.page
        timestamp = now_iso()
        locked.conn.execute(
            """
            UPDATE wiki_file_observations
            SET parse_status='invalid',error_code=?,error_message=?
            WHERE id=?
            """,
            (error_code, error_message, observation["id"]),
        )
        next_epoch = int(page["projection_epoch"] or 0) + 1
        file_sql = "file_hash IS NULL" if page.get("file_hash") is None else "file_hash=?"
        params: list[Any] = [
            error_code,
            observation["file_hash"],
            next_epoch,
            timestamp,
            page["page_id"],
            page.get("current_revision_id"),
            int(page["projection_epoch"] or 0),
        ]
        if page.get("file_hash") is not None:
            params.append(page["file_hash"])
        invalidated = locked.conn.execute(
            f"""
            UPDATE wiki_pages
            SET lifecycle_status='invalid',deleted_at=NULL,sync_error=?,observed_file_hash=?,
                projection_epoch=?,rag_visible_revision_id=NULL,rag_visible_epoch=NULL,
                updated_at=?
            WHERE page_id=? AND current_revision_id=? AND projection_epoch=?
              AND pending_write_intent_id IS NULL AND {file_sql}
            """,
            tuple(params),
        )
        if invalidated.rowcount != 1:
            raise RevisionConflict(
                "page changed while recording invalid external bytes",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        page.update(
            lifecycle_status="invalid",
            sync_error=error_code,
            observed_file_hash=observation["file_hash"],
            projection_epoch=next_epoch,
            rag_visible_revision_id=None,
            rag_visible_epoch=None,
            deleted_at=None,
        )
        self._reconcile_pending_reviews_locked(
            locked.conn,
            page["page_id"],
            allow_conflicts=False,
        )

        pending = list(
            locked.conn.execute(
                """
                SELECT * FROM review_items
                WHERE page_id=? AND issue_type='invalid_frontmatter' AND status='pending'
                ORDER BY created_at,id
                """,
                (page["page_id"],),
            ).fetchall()
        )
        matching = [
            row
            for row in pending
            if row["base_revision_id"] == page.get("current_revision_id")
            and row["candidate_revision_id"] is None
        ]
        keep = matching[0] if len(matching) == 1 else None
        for row in pending:
            if keep is not None and row["id"] == keep["id"]:
                continue
            locked.conn.execute(
                """
                UPDATE review_items
                SET status='superseded',resolved_at=?,updated_at=?
                WHERE id=? AND status='pending'
                """,
                (timestamp, timestamp, row["id"]),
            )
        review_id = str(keep["id"]) if keep is not None else f"review_{uuid.uuid4().hex}"
        review_state = canonical_state_json(self._canonical_state_locked(locked.conn, page))
        if keep is None:
            locked.conn.execute(
                """
                INSERT INTO review_items(
                  id,page_path,page_id,issue_type,status,owner,source_ids_json,
                  created_at,updated_at,base_revision_id,candidate_revision_id,
                  expected_state_json
                ) VALUES (?,?,?,'invalid_frontmatter','pending',?,?,?,?,?,NULL,?)
                """,
                (
                    review_id,
                    page["path"],
                    page["page_id"],
                    page.get("owner"),
                    page["source_ids_json"],
                    timestamp,
                    timestamp,
                    page.get("current_revision_id"),
                    review_state,
                ),
            )
        else:
            locked.conn.execute(
                """
                UPDATE review_items
                SET page_path=?,owner=?,source_ids_json=?,expected_state_json=?,updated_at=?
                WHERE id=? AND status='pending'
                """,
                (
                    page["path"],
                    page.get("owner"),
                    page["source_ids_json"],
                    review_state,
                    timestamp,
                    review_id,
                ),
            )
        self.outbox.supersede_stale(
            locked.conn,
            page["page_id"],
            next_epoch,
        )
        jobs = self.outbox.enqueue_pair_for_state(
            locked.conn,
            {
                **page,
                "current_revision_id": None,
                "projection_epoch": next_epoch,
            },
            "delete",
            {"path": page["path"], "reason": error_code},
        )
        completed = locked.conn.execute(
            """
            UPDATE vault_change_events
            SET expected_state_json=?,status='invalid',result_revision_id=?,updated_at=?
            WHERE id=? AND observation_id=? AND status='pending'
            """,
            (
                expected_state_json,
                page.get("current_revision_id"),
                timestamp,
                event_id,
                observation["id"],
            ),
        )
        if completed.rowcount != 1:
            raise RevisionConflict(
                "external event changed while recording invalid bytes",
                current_revision_id=page.get("current_revision_id"),
            )
        audit(
            locked.conn,
            "wiki_external_change_invalid",
            {
                "event_id": event_id,
                "observation_id": observation["id"],
                "error_code": error_code,
            },
            timestamp,
        )
        return MutationResult(
            page_id=page["page_id"],
            page_path=page["path"],
            status="invalid",
            revision_id=page.get("current_revision_id"),
            current_revision_id=page.get("current_revision_id"),
            generated_revision_id=page.get("generated_revision_id"),
            conflict_review_id=review_id,
            observation_id=observation["id"],
            projection_job_ids=tuple(jobs),
        )

    def _reuse_current_observation_locked(
        self,
        locked: LockedPage,
        *,
        event_id: str,
        observation: dict[str, Any],
        expected_state_json: str,
        content: bytes,
    ) -> MutationResult:
        page = locked.page
        current = locked.conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (page.get("current_revision_id"),),
        ).fetchone()
        if current is None or current["content"].encode("utf-8") != content:
            raise RevisionConflict(
                "observed bytes no longer match the current revision",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        if current["file_hash"] != observation["file_hash"]:
            raise RevisionConflict(
                "observed hash no longer matches the current revision",
                current_revision_id=page.get("current_revision_id"),
            )
        timestamp = now_iso()
        observed = locked.conn.execute(
            """
            UPDATE wiki_file_observations
            SET parse_status='valid',error_code=NULL,error_message=NULL
            WHERE id=? AND page_id=? AND file_hash=?
            """,
            (
                observation["id"],
                page["page_id"],
                observation["file_hash"],
            ),
        )
        if observed.rowcount != 1:
            raise WikiRevisionError("current-byte observation status CAS failed")
        previous_epoch = int(page.get("projection_epoch") or 0)
        next_epoch = previous_epoch + 1
        lifecycle_changed = page.get("lifecycle_status") != "active"
        restored = locked.conn.execute(
            """
            UPDATE wiki_pages
            SET lifecycle_status='active',deleted_at=NULL,sync_error=NULL,
                observed_file_hash=?,projection_epoch=?,
                rag_visible_revision_id=NULL,rag_visible_epoch=NULL,updated_at=?
            WHERE page_id=? AND path=? AND current_revision_id=?
              AND projection_epoch=? AND pending_write_intent_id IS NULL
            """,
            (
                observation["file_hash"],
                next_epoch,
                timestamp,
                page["page_id"],
                page["path"],
                page["current_revision_id"],
                previous_epoch,
            ),
        )
        if restored.rowcount != 1:
            raise RevisionConflict(
                "page changed while reusing current observed bytes",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        page.update(
            lifecycle_status="active",
            deleted_at=None,
            sync_error=None,
            observed_file_hash=observation["file_hash"],
            projection_epoch=next_epoch,
            rag_visible_revision_id=None,
            rag_visible_epoch=None,
        )
        locked.conn.execute(
            """
            UPDATE review_items
            SET status='superseded',resolved_at=?,updated_at=?
            WHERE page_id=? AND issue_type='invalid_frontmatter'
              AND status='pending'
            """,
            (timestamp, timestamp, page["page_id"]),
        )
        self._reconcile_pending_reviews_locked(
            locked.conn,
            page["page_id"],
            allow_conflicts=not lifecycle_changed,
        )
        self.outbox.supersede_stale(
            locked.conn,
            page["page_id"],
            next_epoch,
        )
        jobs = self.outbox.enqueue_pair_for_state(
            locked.conn,
            page,
            "upsert",
            {"path": page["path"]},
        )
        applied = locked.conn.execute(
            """
            UPDATE vault_change_events
            SET expected_state_json=?,status='applied',result_revision_id=?,updated_at=?
            WHERE id=? AND observation_id=? AND status='pending'
            """,
            (
                expected_state_json,
                page["current_revision_id"],
                timestamp,
                event_id,
                observation["id"],
            ),
        )
        if applied.rowcount != 1:
            raise RevisionConflict(
                "external event changed while reusing current bytes",
                current_revision_id=page.get("current_revision_id"),
            )
        audit(
            locked.conn,
            "wiki_external_change_reused_current",
            {
                "event_id": event_id,
                "observation_id": observation["id"],
                "page_id": page["page_id"],
                "revision_id": page["current_revision_id"],
            },
            timestamp,
        )
        return MutationResult(
            page_id=page["page_id"],
            page_path=page["path"],
            status="applied",
            revision_id=page["current_revision_id"],
            current_revision_id=page["current_revision_id"],
            generated_revision_id=page.get("generated_revision_id"),
            observation_id=observation["id"],
            projection_job_ids=tuple(jobs),
        )

    def ingest_external_change(
        self,
        event_id: str,
        page_path: str,
        observation: FileObservationInput,
    ) -> MutationResult:
        self._target(page_path)
        event, created = self._persist_external_occurrence(
            event_id,
            page_path,
            observation,
        )
        if not created and event["status"] not in {"pending", "prepared"}:
            return self._replay_external_event(event)

        pending_conflict: RevisionConflict | None = None
        prepared: MutationResult | None = None
        stable_page_id: str | None = None
        if not created:
            with connect_app(self.settings) as conn:
                event_observation = conn.execute(
                    "SELECT * FROM wiki_file_observations WHERE id=?",
                    (event["observation_id"],),
                ).fetchone()
            if (
                event_observation is None
                or event_observation["page_id"] is None
                or event_observation["page_path"] != event["page_path"]
            ):
                raise RevisionConflict(
                    "pending external event has no stable page identity",
                    current_revision_id=event.get("result_revision_id"),
                )
            stable_page_id = str(event_observation["page_id"])
        lock_context = (
            self.coordinator.lock_page(page_id=stable_page_id)
            if stable_page_id is not None
            else self.coordinator.lock_page(page_path)
        )
        with lock_context as locked:
            expected_state_json = canonical_state_json(
                self._canonical_state_locked(locked.conn, locked.page)
            )
            stored_event = locked.conn.execute(
                "SELECT * FROM vault_change_events WHERE id=?",
                (event_id,),
            ).fetchone()
            stored_observation = locked.conn.execute(
                "SELECT * FROM wiki_file_observations WHERE id=?",
                (event["observation_id"],),
            ).fetchone()
            if (
                stored_event is None
                or stored_event["page_path"] != page_path
                or stored_event["observation_id"] != event["observation_id"]
                or stored_observation is None
                or stored_observation["page_path"] != page_path
            ):
                raise RevisionConflict(
                    "external event observation identity changed",
                    current_revision_id=locked.page.get("current_revision_id"),
                    pending_intent_id=locked.page.get("pending_write_intent_id"),
                )
            observed = dict(stored_observation)
            stored_event_data = dict(stored_event)
            if stored_event_data["status"] not in {"pending", "prepared"}:
                return self._replay_external_event(stored_event_data)
            recorded_state_json = stored_event_data["expected_state_json"] or "{}"
            if recorded_state_json == "{}":
                locked.conn.execute(
                    """
                    UPDATE vault_change_events SET status='superseded',updated_at=?
                    WHERE id=? AND observation_id=?
                      AND status IN ('pending','prepared')
                    """,
                    (
                        now_iso(),
                        event_id,
                        observed["id"],
                    ),
                )
                pending_conflict = RevisionConflict(
                    "external event has no persisted expected state",
                    current_revision_id=locked.page.get("current_revision_id"),
                    pending_intent_id=locked.page.get("pending_write_intent_id"),
                )
            elif recorded_state_json != expected_state_json:
                locked.conn.execute(
                    """
                    UPDATE vault_change_events
                    SET status='superseded',updated_at=?
                    WHERE id=? AND observation_id=?
                      AND status IN ('pending','prepared')
                    """,
                    (now_iso(), event_id, observed["id"]),
                )
                pending_conflict = RevisionConflict(
                    "external event expected state changed before resume",
                    current_revision_id=locked.page.get("current_revision_id"),
                    pending_intent_id=locked.page.get("pending_write_intent_id"),
                )

            if pending_conflict is None and stored_event_data.get(
                "result_revision_id"
            ) is not None:
                try:
                    prepared = self._prepared_external_intent_locked(
                        locked,
                        event=stored_event_data,
                        observation=observed,
                    )
                except RevisionConflict as exc:
                    locked.conn.execute(
                        """
                        UPDATE vault_change_events
                        SET status='superseded',updated_at=?
                        WHERE id=? AND observation_id=?
                          AND status IN ('pending','prepared')
                        """,
                        (now_iso(), event_id, observed["id"]),
                    )
                    pending_conflict = exc
            elif (
                pending_conflict is None
                and locked.page.get("pending_write_intent_id") is not None
            ):
                pending_conflict = RevisionConflict(
                    "page already has an unrelated pending write",
                    current_revision_id=locked.page.get("current_revision_id"),
                    pending_intent_id=locked.page.get("pending_write_intent_id"),
                )

            if pending_conflict is None and prepared is None:
                error_code: str | None = None
                error_message = ""
                content: bytes | None = None
                document = None
                rendered: bytes | None = None
                revision_id: str | None = None
                write_token: str | None = None
                source_ids: list[str] | None = None
                metadata: dict[str, Any] | None = None
                reuse_current = False
                if bool(observed["content_truncated"]):
                    error_code = "file_too_large"
                    error_message = "file exceeds the observation capture limit"
                elif observed["content_bytes"] is None:
                    error_code = "missing_content_bytes"
                    error_message = "full observation bytes are required"
                else:
                    content = bytes(observed["content_bytes"])
                    if len(content) != int(observed["size_bytes"]):
                        error_code = "observation_size_mismatch"
                        error_message = "observation size does not match captured bytes"
                    elif compute_file_hash(content) != observed["file_hash"]:
                        error_code = "observation_hash_mismatch"
                        error_message = "observation hash does not match captured bytes"
                    else:
                        try:
                            document = parse_wiki_bytes(content)
                            source_ids = _source_ids(_plain(document.frontmatter))
                            status_value = document.frontmatter.get(
                                "review_status",
                                locked.page.get("review_status"),
                            )
                            review_status = _review_status(status_value)
                            current = locked.conn.execute(
                                "SELECT content FROM wiki_page_revisions WHERE id=?",
                                (locked.page.get("current_revision_id"),),
                            ).fetchone()
                            reuse_current = (
                                current is not None
                                and current["content"].encode("utf-8") == content
                            )
                            if reuse_current:
                                metadata = _plain(document.frontmatter)
                            else:
                                revision_id = f"wrev_{uuid.uuid4().hex}"
                                write_token = f"write_{uuid.uuid4().hex}"
                                rendered = render_managed_frontmatter(
                                    document,
                                    page_id=locked.page["page_id"],
                                    revision_id=revision_id,
                                    write_token=write_token,
                                    review_status=review_status,
                                )
                                final_document = parse_wiki_bytes(rendered)
                                metadata = _plain(final_document.frontmatter)
                        except MarkdownParseError as exc:
                            error_code = exc.code
                            error_message = str(exc)
                if error_code is not None:
                    prepared = self._invalid_external_change_locked(
                        locked,
                        event_id=event_id,
                        observation=observed,
                        expected_state_json=expected_state_json,
                        error_code=error_code,
                        error_message=error_message,
                    )
                else:
                    if reuse_current:
                        if content is None:
                            raise WikiRevisionError(
                                "current-byte observation has no content"
                            )
                        prepared = self._reuse_current_observation_locked(
                            locked,
                            event_id=event_id,
                            observation=observed,
                            expected_state_json=expected_state_json,
                            content=content,
                        )
                    else:
                        if (
                            content is None
                            or document is None
                            or rendered is None
                            or revision_id is None
                            or write_token is None
                            or source_ids is None
                            or metadata is None
                        ):
                            raise WikiRevisionError(
                                "valid observation has no parsed content"
                            )
                        locked.conn.execute(
                            """
                            UPDATE wiki_file_observations
                            SET parse_status='valid',error_code=NULL,error_message=NULL
                            WHERE id=?
                            """,
                            (observed["id"],),
                        )
                        metadata.update(
                            {
                                "vault_change_event_id": event_id,
                                "observation_id": observed["id"],
                                "observed_file_hash": observed["file_hash"],
                            }
                        )
                        state_digest = hashlib.sha256(
                            expected_state_json.encode("utf-8")
                        ).hexdigest()
                        revision = self._create_revision_locked(
                            locked.conn,
                            locked.page,
                            content=rendered,
                            origin="external",
                            base_revision_id=locked.page.get(
                                "current_revision_id"
                            ),
                            source_ids=source_ids,
                            actor="vault-observer",
                            note=None,
                            idempotency_key=f"external:{event_id}:{state_digest}",
                            metadata=metadata,
                            revision_id=revision_id,
                        )
                        intent_page = {
                            **locked.page,
                            "file_hash": observed["file_hash"],
                        }
                        intent_id = self.prepare_write_intent_locked(
                            locked.conn,
                            intent_page,
                            revision=revision,
                            expected_revision_id=locked.page.get(
                                "current_revision_id"
                            ),
                            expected_file_hash=observed["file_hash"],
                            write_token=write_token,
                        )
                        transitioned = locked.conn.execute(
                            """
                            UPDATE vault_change_events
                            SET expected_state_json=?,result_revision_id=?,updated_at=?
                            WHERE id=? AND observation_id=?
                              AND status IN ('pending','prepared')
                            """,
                            (
                                expected_state_json,
                                revision.id,
                                now_iso(),
                                event_id,
                                observed["id"],
                            ),
                        )
                        if transitioned.rowcount != 1:
                            raise RevisionConflict(
                                "external event changed while preparing revision",
                                current_revision_id=locked.page.get(
                                    "current_revision_id"
                                ),
                                pending_intent_id=intent_id,
                            )
                        prepared = MutationResult(
                            page_id=locked.page["page_id"],
                            page_path=page_path,
                            status="prepared",
                            revision_id=revision.id,
                            current_revision_id=locked.page.get(
                                "current_revision_id"
                            ),
                            generated_revision_id=locked.page.get(
                                "generated_revision_id"
                            ),
                            write_intent_id=intent_id,
                            observation_id=observed["id"],
                        )
        if pending_conflict is not None:
            raise pending_conflict
        if prepared is None:
            raise WikiRevisionError("external change did not produce a result")
        if prepared.status in {"invalid", "applied"}:
            return prepared

        from app.vault_writer import IntentExecutor

        execution = IntentExecutor(self.settings).execute(prepared.write_intent_id)
        if execution is None or execution.intent_status != "applied":
            raise RevisionConflict(
                "external change requires write recovery",
                current_revision_id=prepared.current_revision_id,
                pending_intent_id=prepared.write_intent_id,
            )
        return replace(
            self._mutation_for_intent(prepared.write_intent_id),
            observation_id=prepared.observation_id,
            replayed=prepared.replayed,
        )

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

    @staticmethod
    def _stable_recovery_id(prefix: str, *parts: str) -> str:
        digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
        return f"{prefix}_{digest[:32]}"

    def _persist_observation_locked(
        self,
        conn: Any,
        page: dict[str, Any],
        observation: FileObservationInput,
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            SELECT * FROM wiki_file_observations
            WHERE page_path=? AND file_hash=?
            """,
            (page["path"], observation.file_hash),
        ).fetchone()
        if row is None:
            observation_id = f"wobs_{uuid.uuid4().hex}"
            truncated = bool(observation.content_truncated)
            conn.execute(
                """
                INSERT INTO wiki_file_observations(
                  id,page_id,page_path,file_hash,size_bytes,mtime_ns,content_bytes,
                  content_prefix,content_truncated,parse_status,error_code,
                  error_message,observed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(page_path,file_hash) DO NOTHING
                """,
                (
                    observation_id,
                    page["page_id"],
                    page["path"],
                    observation.file_hash,
                    observation.size_bytes,
                    observation.mtime_ns,
                    observation.content_bytes,
                    observation.content_prefix,
                    truncated,
                    "invalid" if truncated else "pending",
                    "file_too_large" if truncated else None,
                    (
                        "file exceeds the observation capture limit"
                        if truncated
                        else None
                    ),
                    now_iso(),
                ),
            )
        stored = conn.execute(
            """
            SELECT * FROM wiki_file_observations
            WHERE page_path=? AND file_hash=?
            """,
            (page["path"], observation.file_hash),
        ).fetchone()
        if stored is None:
            raise WikiRevisionError("observation insert did not produce a row")

        def blob(value: Any) -> bytes | None:
            return None if value is None else bytes(value)

        if (
            stored["page_id"] != page["page_id"]
            or int(stored["size_bytes"]) != observation.size_bytes
            or blob(stored["content_bytes"]) != observation.content_bytes
            or blob(stored["content_prefix"]) != observation.content_prefix
            or bool(stored["content_truncated"])
            != bool(observation.content_truncated)
        ):
            raise RevisionConflict(
                "observation natural key belongs to different bytes",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        return dict(stored)

    @staticmethod
    def _validate_observation_content(
        observation: dict[str, Any],
    ) -> tuple[bytes | None, Any | None, str | None, str | None]:
        if bool(observation["content_truncated"]):
            return (
                None,
                None,
                "file_too_large",
                "file exceeds the observation capture limit",
            )
        if observation["content_bytes"] is None:
            return None, None, "missing_content_bytes", "full bytes are required"
        content = bytes(observation["content_bytes"])
        if len(content) != int(observation["size_bytes"]):
            return (
                None,
                None,
                "observation_size_mismatch",
                "observation size does not match captured bytes",
            )
        if compute_file_hash(content) != observation["file_hash"]:
            return (
                None,
                None,
                "observation_hash_mismatch",
                "observation hash does not match captured bytes",
            )
        try:
            document = parse_wiki_bytes(content)
            _source_ids(_plain(document.frontmatter))
            _review_status(document.frontmatter.get("review_status"))
        except MarkdownParseError as exc:
            return None, None, exc.code, str(exc)
        return content, document, None, None

    def _create_external_candidate_locked(
        self,
        conn: Any,
        page: dict[str, Any],
        *,
        observation: dict[str, Any],
        event_id: str,
        expected_state_json: str,
        actor: str,
        metadata_extra: dict[str, Any],
    ) -> RevisionRecord:
        content, document, error_code, error_message = (
            self._validate_observation_content(observation)
        )
        if error_code is not None or content is None or document is None:
            conn.execute(
                """
                UPDATE wiki_file_observations
                SET parse_status='invalid',error_code=?,error_message=?
                WHERE id=?
                """,
                (error_code, error_message, observation["id"]),
            )
            raise InvalidWikiDocument(str(observation["id"]), str(error_code))

        existing_event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
        if existing_event is not None and existing_event["result_revision_id"] is not None:
            existing_revision = conn.execute(
                "SELECT * FROM wiki_page_revisions WHERE id=?",
                (existing_event["result_revision_id"],),
            ).fetchone()
            try:
                existing_metadata = json.loads(
                    existing_revision["metadata_json"] or "{}"
                ) if existing_revision is not None else {}
            except (TypeError, json.JSONDecodeError):
                existing_metadata = {}
            if (
                existing_revision is None
                or existing_event["page_path"] != page["path"]
                or existing_event["observation_id"] != observation["id"]
                or existing_event["status"] not in {"pending", "prepared"}
                or existing_revision["page_id"] != page["page_id"]
                or existing_revision["origin"] != "external"
                or existing_revision["base_revision_id"]
                != page.get("current_revision_id")
                or existing_metadata.get("vault_change_event_id") != event_id
                or existing_metadata.get("observation_id") != observation["id"]
                or existing_metadata.get("observed_file_hash")
                != observation["file_hash"]
                or any(
                    existing_metadata.get(key) != value
                    for key, value in metadata_extra.items()
                )
            ):
                raise RevisionConflict(
                    "recovery event identity changed",
                    current_revision_id=page.get("current_revision_id"),
                    pending_intent_id=page.get("pending_write_intent_id"),
                )
            return RevisionRecord.from_row(existing_revision)

        revision_id = f"wrev_{uuid.uuid4().hex}"
        write_token = f"write_{uuid.uuid4().hex}"
        review_status = _review_status(
            document.frontmatter.get("review_status")
            or page.get("review_status")
        )
        rendered = render_managed_frontmatter(
            document,
            page_id=page["page_id"],
            revision_id=revision_id,
            write_token=write_token,
            review_status=review_status,
        )
        final_document = parse_wiki_bytes(rendered)
        metadata = _plain(final_document.frontmatter)
        metadata.update(
            {
                "vault_change_event_id": event_id,
                "observation_id": observation["id"],
                "observed_file_hash": observation["file_hash"],
                **metadata_extra,
            }
        )
        state_digest = hashlib.sha256(
            expected_state_json.encode("utf-8")
        ).hexdigest()
        revision = self._create_revision_locked(
            conn,
            page,
            content=rendered,
            origin="external",
            base_revision_id=page.get("current_revision_id"),
            source_ids=_source_ids(metadata),
            actor=actor,
            note=None,
            idempotency_key=f"external:{event_id}:{state_digest}",
            metadata=metadata,
            revision_id=revision_id,
        )
        conn.execute(
            """
            UPDATE wiki_file_observations
            SET parse_status='valid',error_code=NULL,error_message=NULL
            WHERE id=? AND page_id=?
            """,
            (observation["id"], page["page_id"]),
        )
        conn.execute(
            """
            INSERT INTO vault_change_events(
              id,kind,page_path,observation_id,expected_state_json,status,
              result_revision_id,detected_at,updated_at
            ) VALUES (?,'modify',?,?,?,'prepared',?,?,?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                event_id,
                page["path"],
                observation["id"],
                expected_state_json,
                revision.id,
                now_iso(),
                now_iso(),
            ),
        )
        event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
        if (
            event is None
            or event["page_path"] != page["path"]
            or event["observation_id"] != observation["id"]
            or event["result_revision_id"] != revision.id
            or event["status"] not in {"pending", "prepared"}
        ):
            raise RevisionConflict(
                "external candidate event changed",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        return revision

    @staticmethod
    def _candidate_event_id(candidate: Any) -> str | None:
        try:
            metadata = json.loads(candidate["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return None
        event_id = metadata.get("vault_change_event_id")
        return event_id if isinstance(event_id, str) and event_id else None

    def _recovery_displaced_lineage_locked(
        self,
        conn: Any,
        page: dict[str, Any],
        *,
        expected_intent_id: str | None = None,
    ) -> tuple[Any, str] | None:
        current_revision_id = page.get("current_revision_id")
        if not isinstance(current_revision_id, str) or not current_revision_id:
            return None
        current = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (current_revision_id,),
        ).fetchone()
        if (
            current is None
            or current["page_id"] != page["page_id"]
            or current["origin"] != "external"
        ):
            return None
        try:
            metadata = json.loads(current["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(metadata, dict):
            return None
        displaced_revision_id = metadata.get("recovery_displaced_revision_id")
        recovery_intent_id = metadata.get("recovery_intent_id")
        if (
            not isinstance(displaced_revision_id, str)
            or not displaced_revision_id
            or displaced_revision_id == current_revision_id
            or not isinstance(recovery_intent_id, str)
            or not recovery_intent_id
            or (
                expected_intent_id is not None
                and recovery_intent_id != expected_intent_id
            )
        ):
            return None
        displaced = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (displaced_revision_id,),
        ).fetchone()
        recovery_intent = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (recovery_intent_id,),
        ).fetchone()
        if (
            displaced is None
            or displaced["page_id"] != page["page_id"]
            or recovery_intent is None
            or recovery_intent["page_id"] != page["page_id"]
            or recovery_intent["status"] != "superseded"
            or recovery_intent["revision_id"] != displaced_revision_id
        ):
            return None
        successor_intent_id = next(
            (
                payload.get("successor_intent_id")
                for payload in self._audit_payloads_locked(
                    conn,
                    "wiki_recovery_handoff",
                )
                if payload.get("recovery_intent_id") == recovery_intent_id
                and payload.get("candidate_revision_id") == current_revision_id
                and isinstance(payload.get("successor_intent_id"), str)
            ),
            None,
        )
        if successor_intent_id is None:
            successor_intent_id = next(
                (
                    payload.get("write_intent_id")
                    for payload in self._audit_payloads_locked(
                        conn,
                        "wiki_conflict_resolution_prepared",
                    )
                    if payload.get("recovery_intent_id")
                    == recovery_intent_id
                    and payload.get("resolution_revision_id")
                    == current_revision_id
                    and isinstance(payload.get("write_intent_id"), str)
                ),
                None,
            )
        if successor_intent_id is None:
            return None
        successor = conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (successor_intent_id,),
        ).fetchone()
        if (
            successor is None
            or successor["page_id"] != page["page_id"]
            or successor["revision_id"] != current_revision_id
            or successor["status"] != "applied"
        ):
            return None
        return displaced, recovery_intent_id

    def _supersede_candidate_event_locked(
        self,
        conn: Any,
        candidate_revision_id: str | None,
    ) -> None:
        if candidate_revision_id is None:
            return
        candidate = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (candidate_revision_id,),
        ).fetchone()
        if candidate is None:
            return
        event_id = self._candidate_event_id(candidate)
        if event_id is None:
            return
        event = conn.execute(
            "SELECT * FROM vault_change_events WHERE id=?",
            (event_id,),
        ).fetchone()
        if event is None:
            raise WikiRevisionError("external candidate event is missing")
        if event["status"] == "superseded":
            return
        if (
            event["status"] not in {"pending", "prepared"}
            or event["result_revision_id"] != candidate_revision_id
        ):
            raise RevisionConflict(
                "external candidate event is no longer supersedable",
                current_revision_id=candidate["base_revision_id"],
            )
        changed = conn.execute(
            """
            UPDATE vault_change_events
            SET status='superseded',updated_at=?
            WHERE id=? AND result_revision_id=?
              AND status IN ('pending','prepared')
            """,
            (now_iso(), event_id, candidate_revision_id),
        )
        if changed.rowcount != 1:
            raise RevisionConflict(
                "external candidate event changed while superseding",
                current_revision_id=candidate["base_revision_id"],
            )

    def _supersede_retained_backup_review_locked(
        self,
        conn: Any,
        page: dict[str, Any],
        intent_id: str,
    ) -> bool:
        pending = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='concurrent_write_conflict'
              AND status='pending'
            ORDER BY created_at,id
            """,
            (page["page_id"],),
        ).fetchall()
        for review in pending:
            candidate = conn.execute(
                "SELECT * FROM wiki_page_revisions WHERE id=?",
                (review["candidate_revision_id"],),
            ).fetchone()
            if candidate is None:
                continue
            try:
                metadata = json.loads(candidate["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if (
                not isinstance(metadata, dict)
                or metadata.get("retained_backup_intent_id") != intent_id
            ):
                continue
            timestamp = now_iso()
            changed = conn.execute(
                """
                UPDATE review_items
                SET status='superseded',resolved_at=?,updated_at=?
                WHERE id=? AND status='pending'
                """,
                (timestamp, timestamp, review["id"]),
            )
            if changed.rowcount != 1:
                raise RevisionConflict(
                    "retained backup review changed while superseding",
                    current_revision_id=page.get("current_revision_id"),
                    pending_intent_id=page.get("pending_write_intent_id"),
                )
            self._supersede_candidate_event_locked(
                conn,
                review["candidate_revision_id"],
            )
            return True
        return False

    def _seed_concurrent_review_locked(
        self,
        conn: Any,
        page: dict[str, Any],
        *,
        candidate: RevisionRecord,
        event_id: str | None,
        expected_state_extra: dict[str, Any] | None = None,
    ) -> str:
        pending = list(
            conn.execute(
                """
                SELECT * FROM review_items
                WHERE page_id=? AND status='pending'
                  AND issue_type IN ('content_conflict','concurrent_write_conflict')
                ORDER BY created_at,id
                """,
                (page["page_id"],),
            ).fetchall()
        )
        matching = [
            row
            for row in pending
            if row["issue_type"] == "concurrent_write_conflict"
            and row["base_revision_id"] == page.get("current_revision_id")
            and row["candidate_revision_id"] == candidate.id
        ]
        keep = matching[0] if len(matching) == 1 else None
        timestamp = now_iso()
        for row in pending:
            if keep is not None and row["id"] == keep["id"]:
                continue
            if row["issue_type"] != "concurrent_write_conflict":
                continue
            changed = conn.execute(
                """
                UPDATE review_items
                SET status='superseded',resolved_at=?,updated_at=?
                WHERE id=? AND status='pending'
                """,
                (timestamp, timestamp, row["id"]),
            )
            if changed.rowcount != 1:
                raise RevisionConflict(
                    "concurrent review changed while seeding candidate",
                    current_revision_id=page.get("current_revision_id"),
                    pending_intent_id=page.get("pending_write_intent_id"),
                )
            self._supersede_candidate_event_locked(
                conn,
                row["candidate_revision_id"],
            )

        review_id = (
            str(keep["id"])
            if keep is not None
            else self._stable_recovery_id(
                "review",
                page["page_id"],
                candidate.id,
                event_id or "no-event",
            )
        )
        remaining = list(
            conn.execute(
                """
                SELECT id FROM review_items
                WHERE page_id=? AND status='pending'
                  AND issue_type IN ('content_conflict','concurrent_write_conflict')
                ORDER BY id
                """,
                (page["page_id"],),
            ).fetchall()
        )
        planned_ids = {str(row["id"]) for row in remaining}
        planned_ids.add(review_id)
        expected_state = self._canonical_state_locked(conn, page)
        expected_state["pending_conflict"] = sorted(planned_ids)
        if expected_state_extra:
            expected_state.update(expected_state_extra)
        expected_state_json = canonical_state_json(expected_state)
        conn.execute(
            """
            UPDATE review_items SET expected_state_json=?,updated_at=?
            WHERE page_id=? AND status='pending'
              AND issue_type IN ('content_conflict','concurrent_write_conflict')
            """,
            (expected_state_json, timestamp, page["page_id"]),
        )
        if keep is None:
            conn.execute(
                """
                INSERT INTO review_items(
                  id,page_path,page_id,issue_type,status,owner,source_ids_json,
                  created_at,updated_at,base_revision_id,candidate_revision_id,
                  expected_state_json
                ) VALUES (?,?,?,'concurrent_write_conflict','pending',?,?,?,?,?,?,?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    review_id,
                    page["path"],
                    page["page_id"],
                    page.get("owner"),
                    json_dump(candidate.source_ids),
                    timestamp,
                    timestamp,
                    page.get("current_revision_id"),
                    candidate.id,
                    expected_state_json,
                ),
            )
        stored = conn.execute(
            "SELECT * FROM review_items WHERE id=?",
            (review_id,),
        ).fetchone()
        if (
            stored is None
            or stored["status"] != "pending"
            or stored["candidate_revision_id"] != candidate.id
            or stored["expected_state_json"] != expected_state_json
        ):
            raise RevisionConflict(
                "concurrent review insert changed",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        if event_id is not None:
            event_updated = conn.execute(
                """
                UPDATE vault_change_events
                SET expected_state_json=?,status='prepared',updated_at=?
                WHERE id=? AND result_revision_id=?
                  AND status IN ('pending','prepared')
                """,
                (expected_state_json, timestamp, event_id, candidate.id),
            )
            if event_updated.rowcount != 1:
                raise WikiRevisionError("candidate event status CAS failed")
        return review_id

    def _fail_closed_recovery_locked(
        self,
        locked: LockedPage,
        *,
        intent_id: str,
        error_code: str,
        observed_file_hash: str | None,
    ) -> tuple[str, ...]:
        page = locked.page
        if (
            page.get("lifecycle_status") == "invalid"
            and page.get("sync_error") == error_code
            and page.get("observed_file_hash") == observed_file_hash
        ):
            return ()
        previous_epoch = int(page.get("projection_epoch") or 0)
        next_epoch = previous_epoch + 1
        expected_current = page.get("current_revision_id")
        current_sql = (
            "current_revision_id IS NULL"
            if expected_current is None
            else "current_revision_id=?"
        )
        params: list[Any] = [
            error_code,
            observed_file_hash,
            next_epoch,
            now_iso(),
            page["page_id"],
            intent_id,
        ]
        if expected_current is not None:
            params.append(expected_current)
        params.append(previous_epoch)
        changed = locked.conn.execute(
            f"""
            UPDATE wiki_pages
            SET lifecycle_status='invalid',deleted_at=NULL,sync_error=?,
                observed_file_hash=?,projection_epoch=?,
                rag_visible_revision_id=NULL,rag_visible_epoch=NULL,updated_at=?
            WHERE page_id=? AND pending_write_intent_id=?
              AND {current_sql} AND projection_epoch=?
            """,
            tuple(params),
        )
        if changed.rowcount != 1:
            raise RevisionConflict(
                "page changed while failing recovery closed",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        page.update(
            lifecycle_status="invalid",
            sync_error=error_code,
            observed_file_hash=observed_file_hash,
            projection_epoch=next_epoch,
            rag_visible_revision_id=None,
            rag_visible_epoch=None,
            deleted_at=None,
        )
        self._reconcile_pending_reviews_locked(
            locked.conn,
            page["page_id"],
            allow_conflicts=False,
        )
        timestamp = now_iso()
        locked.conn.execute(
            """
            UPDATE review_items
            SET status='superseded',resolved_at=?,updated_at=?
            WHERE page_id=? AND issue_type='invalid_frontmatter'
              AND status='pending'
            """,
            (timestamp, timestamp, page["page_id"]),
        )
        self.outbox.supersede_stale(
            locked.conn,
            page["page_id"],
            next_epoch,
        )
        return tuple(
            self.outbox.enqueue_pair_for_state(
                locked.conn,
                {
                    **page,
                    "current_revision_id": None,
                    "projection_epoch": next_epoch,
                },
                "delete",
                {"path": page["path"], "reason": error_code},
            )
        )

    def _seed_invalid_recovery_review_locked(
        self,
        conn: Any,
        page: dict[str, Any],
        *,
        observation_ids: list[str],
    ) -> str:
        review_id = self._stable_recovery_id(
            "review",
            "invalid-recovery",
            page["page_id"],
            str(page.get("projection_epoch") or 0),
            *observation_ids,
        )
        timestamp = now_iso()
        pending = conn.execute(
            """
            SELECT * FROM review_items
            WHERE page_id=? AND issue_type='invalid_frontmatter'
              AND status='pending'
            ORDER BY created_at,id
            """,
            (page["page_id"],),
        ).fetchall()
        for row in pending:
            if row["id"] == review_id:
                continue
            conn.execute(
                """
                UPDATE review_items
                SET status='superseded',resolved_at=?,updated_at=?
                WHERE id=? AND status='pending'
                """,
                (timestamp, timestamp, row["id"]),
            )
        expected_state = self._canonical_state_locked(conn, page)
        expected_state["recovery_observation_ids"] = observation_ids
        expected_state_json = canonical_state_json(expected_state)
        conn.execute(
            """
            INSERT INTO review_items(
              id,page_path,page_id,issue_type,status,owner,source_ids_json,
              created_at,updated_at,base_revision_id,candidate_revision_id,
              expected_state_json
            ) VALUES (?,?,?,'invalid_frontmatter','pending',?,?,?,?,?,NULL,?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                review_id,
                page["path"],
                page["page_id"],
                page.get("owner"),
                page["source_ids_json"],
                timestamp,
                timestamp,
                page.get("current_revision_id"),
                expected_state_json,
            ),
        )
        stored = conn.execute(
            "SELECT * FROM review_items WHERE id=?",
            (review_id,),
        ).fetchone()
        if (
            stored is None
            or stored["status"] != "pending"
            or stored["base_revision_id"] != page.get("current_revision_id")
            or stored["candidate_revision_id"] is not None
            or stored["expected_state_json"] != expected_state_json
        ):
            raise RevisionConflict(
                "invalid recovery review changed",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        return review_id

    def _prepare_recovery_successor_locked(
        self,
        locked: LockedPage,
        *,
        old_intent: Any,
        revision: RevisionRecord,
        expected_file_hash: str | None,
        write_token: str,
        executor_owner: str | None,
    ) -> str:
        page = locked.page
        successor_id = self._stable_recovery_id(
            "wint",
            str(old_intent["id"]),
            revision.id,
            expected_file_hash or "missing",
        )
        timestamp = now_iso()
        locked.conn.execute(
            """
            INSERT INTO vault_write_intents(
              id,page_id,revision_id,expected_revision_id,expected_file_hash,
              target_path,write_token,status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,'pending',?,?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                successor_id,
                page["page_id"],
                revision.id,
                page.get("current_revision_id"),
                expected_file_hash,
                page["path"],
                write_token,
                timestamp,
                timestamp,
            ),
        )
        successor = locked.conn.execute(
            "SELECT * FROM vault_write_intents WHERE id=?",
            (successor_id,),
        ).fetchone()
        if (
            successor is None
            or successor["page_id"] != page["page_id"]
            or successor["revision_id"] != revision.id
            or successor["expected_revision_id"] != page.get("current_revision_id")
            or successor["expected_file_hash"] != expected_file_hash
            or successor["target_path"] != page["path"]
            or successor["status"] != "pending"
        ):
            raise RevisionConflict(
                "recovery successor identity changed",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        owner_sql = (
            " AND executor_owner IS NULL"
            if executor_owner is None
            else " AND executor_owner=?"
        )
        owner_params: tuple[Any, ...] = (
            () if executor_owner is None else (executor_owner,)
        )
        superseded = locked.conn.execute(
            f"""
            UPDATE vault_write_intents
            SET status='superseded',executor_owner=NULL,lease_expires_at=NULL,
                last_error=NULL,updated_at=?
            WHERE id=? AND status='recovery_required'{owner_sql}
            """,
            (timestamp, old_intent["id"], *owner_params),
        )
        if superseded.rowcount != 1:
            raise RevisionConflict(
                "recovery intent changed before successor handoff",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        current_revision_id = page.get("current_revision_id")
        current_sql = (
            "current_revision_id IS NULL"
            if current_revision_id is None
            else "current_revision_id=?"
        )
        handoff_params: list[Any] = [
            successor_id,
            timestamp,
            page["page_id"],
            old_intent["id"],
        ]
        if current_revision_id is not None:
            handoff_params.append(current_revision_id)
        handed_off = locked.conn.execute(
            f"""
            UPDATE wiki_pages SET pending_write_intent_id=?,updated_at=?
            WHERE page_id=? AND pending_write_intent_id=?
              AND {current_sql}
            """,
            tuple(handoff_params),
        )
        if handed_off.rowcount != 1:
            raise RevisionConflict(
                "pending recovery pointer changed before successor handoff",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        page["pending_write_intent_id"] = successor_id
        return successor_id

    @staticmethod
    def _observation_error_locked(
        conn: Any,
        observation: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        _, _, error_code, error_message = (
            WikiRevisionService._validate_observation_content(observation)
        )
        conn.execute(
            """
            UPDATE wiki_file_observations
            SET parse_status=?,error_code=?,error_message=?
            WHERE id=?
            """,
            (
                "invalid" if error_code is not None else "valid",
                error_code,
                error_message,
                observation["id"],
            ),
        )
        return error_code, error_message

    def recover_write_intent(
        self,
        intent_id: str,
        executor_owner: str,
        *,
        target_kind: str,
        target_observation: FileObservationInput | None,
        backup_kind: str,
        backup_observation: FileObservationInput | None,
        staged_kind: str,
        staged_observation: FileObservationInput | None,
        claim_kind: str,
        claim_observation: FileObservationInput | None,
    ) -> dict[str, Any]:
        with connect_app(self.settings) as conn:
            locator = conn.execute(
                "SELECT page_id FROM vault_write_intents WHERE id=?",
                (intent_id,),
            ).fetchone()
        if locator is None:
            raise WikiRevisionError(f"write intent not found: {intent_id}")
        with self.coordinator.lock_page(page_id=locator["page_id"]) as locked:
            suffix = " FOR UPDATE" if self.settings.database_backend == "postgres" else ""
            intent = locked.conn.execute(
                "SELECT * FROM vault_write_intents WHERE id=?" + suffix,
                (intent_id,),
            ).fetchone()
            if (
                intent is None
                or intent["status"] != "recovery_required"
                or intent["executor_owner"] != executor_owner
                or locked.page.get("pending_write_intent_id") != intent_id
            ):
                raise RevisionConflict(
                    "recovery intent ownership changed",
                    current_revision_id=locked.page.get("current_revision_id"),
                    pending_intent_id=locked.page.get("pending_write_intent_id"),
                )
            intended = locked.conn.execute(
                "SELECT * FROM wiki_page_revisions WHERE id=?",
                (intent["revision_id"],),
            ).fetchone()
            if intended is None:
                raise WikiRevisionError(
                    f"write revision not found: {intent['revision_id']}"
                )

            observations: list[tuple[str, dict[str, Any]]] = []
            for source, kind, supplied in (
                ("target", target_kind, target_observation),
                ("backup", backup_kind, backup_observation),
                ("staged", staged_kind, staged_observation),
                ("claim", claim_kind, claim_observation),
            ):
                if kind != "unknown":
                    continue
                if supplied is None:
                    raise WikiRevisionError(
                        f"{source} recovery classification has no observation"
                    )
                observations.append(
                    (
                        source,
                        self._persist_observation_locked(
                            locked.conn,
                            locked.page,
                            supplied,
                        ),
                    )
                )
            if not observations:
                raise WikiRevisionError("unknown recovery has no observations")

            invalid: list[tuple[dict[str, Any], str, str]] = []
            for _, observation in observations:
                error_code, error_message = self._observation_error_locked(
                    locked.conn,
                    observation,
                )
                if error_code is not None:
                    invalid.append(
                        (observation, error_code, error_message or error_code)
                    )
            observation_ids = [str(row["id"]) for _, row in observations]
            hidden_unknown = next(
                (
                    (source, observation)
                    for source, observation in observations
                    if source in {"staged", "claim"}
                ),
                None,
            )
            if invalid or hidden_unknown is not None:
                if invalid:
                    primary, error_code, error_message = invalid[0]
                else:
                    hidden_source, primary = hidden_unknown
                    error_code = f"recovery_{hidden_source}_unknown"
                    error_message = (
                        f"{hidden_source} recovery bytes are not the "
                        "intended revision"
                    )
                    locked.conn.execute(
                        """
                        UPDATE wiki_file_observations
                        SET parse_status='valid',error_code=NULL,error_message=NULL
                        WHERE id=?
                        """,
                        (primary["id"],),
                    )
                jobs = self._fail_closed_recovery_locked(
                    locked,
                    intent_id=intent_id,
                    error_code=error_code,
                    observed_file_hash=primary["file_hash"],
                )
                review_id = self._seed_invalid_recovery_review_locked(
                    locked.conn,
                    locked.page,
                    observation_ids=observation_ids,
                )
                locked.conn.execute(
                    """
                    UPDATE vault_write_intents
                    SET executor_owner=NULL,lease_expires_at=NULL,last_error=?,updated_at=?
                    WHERE id=? AND status='recovery_required' AND executor_owner=?
                    """,
                    (error_message[:500], now_iso(), intent_id, executor_owner),
                )
                if not any(
                    payload.get("recovery_intent_id") == intent_id
                    and payload.get("review_id") == review_id
                    for payload in self._audit_payloads_locked(
                        locked.conn,
                        "wiki_recovery_invalid",
                    )
                ):
                    audit(
                        locked.conn,
                        "wiki_recovery_invalid",
                        {
                            "recovery_intent_id": intent_id,
                            "observation_ids": observation_ids,
                            "review_id": review_id,
                            "error_code": error_code,
                            "projection_job_ids": list(jobs),
                        },
                        now_iso(),
                    )
                return {
                    "intent_status": "recovery_required",
                    "current_revision_id": locked.page.get("current_revision_id"),
                    "current_kind": "unchanged",
                    "successor_intent_id": None,
                    "observation_ids": observation_ids,
                }

            expected_state_json = canonical_state_json(
                self._canonical_state_locked(locked.conn, locked.page)
            )
            primary_source, primary = observations[0]
            secondary_ids = [
                str(row["id"])
                for source, row in observations
                if source != primary_source
            ]
            primary_input = {
                "target": target_observation,
                "backup": backup_observation,
            }.get(primary_source)
            if primary_input is None:
                raise WikiRevisionError(
                    "external recovery candidate has no occurrence input"
                )
            event_id: str | None = None
            pending_recovery_reviews = locked.conn.execute(
                """
                SELECT * FROM review_items
                WHERE page_id=? AND issue_type='concurrent_write_conflict'
                  AND status='pending'
                ORDER BY created_at,id
                """,
                (locked.page["page_id"],),
            ).fetchall()
            for pending_review in pending_recovery_reviews:
                pending_candidate = locked.conn.execute(
                    "SELECT * FROM wiki_page_revisions WHERE id=?",
                    (pending_review["candidate_revision_id"],),
                ).fetchone()
                if pending_candidate is None:
                    continue
                try:
                    pending_metadata = json.loads(
                        pending_candidate["metadata_json"] or "{}"
                    )
                except (TypeError, json.JSONDecodeError):
                    continue
                if (
                    pending_metadata.get("recovery_intent_id") == intent_id
                    and pending_metadata.get("recovery_source") == primary_source
                    and pending_metadata.get("observation_id") == primary["id"]
                    and sorted(
                        pending_metadata.get(
                            "recovery_secondary_observation_ids"
                        )
                        or []
                    )
                    == sorted(secondary_ids)
                ):
                    candidate_event_id = pending_metadata.get(
                        "vault_change_event_id"
                    )
                    if isinstance(candidate_event_id, str):
                        event_id = candidate_event_id
                        break
            if event_id is None:
                event_id = self._stable_recovery_id(
                    "vevt",
                    intent_id,
                    primary_source,
                    primary["file_hash"],
                    locked.page.get("observed_file_hash") or "none",
                    str(primary_input.mtime_ns),
                )
            metadata_extra: dict[str, Any] = {
                "recovery_intent_id": intent_id,
                "recovery_source": primary_source,
                "recovery_displaced_revision_id": intent["revision_id"],
            }
            if secondary_ids:
                metadata_extra[
                    "recovery_secondary_observation_ids"
                ] = secondary_ids
            candidate = self._create_external_candidate_locked(
                locked.conn,
                locked.page,
                observation=primary,
                event_id=event_id,
                expected_state_json=expected_state_json,
                actor="vault-recovery",
                metadata_extra=metadata_extra,
            )

            if len(observations) > 1:
                jobs = self._fail_closed_recovery_locked(
                    locked,
                    intent_id=intent_id,
                    error_code="ambiguous_recovery",
                    observed_file_hash=primary["file_hash"],
                )
                recovery_state = {
                    "recovery_intent_id": intent_id,
                    "recovery_primary_observation_id": primary["id"],
                    "recovery_secondary_observation_ids": secondary_ids,
                }
                review_id = self._seed_concurrent_review_locked(
                    locked.conn,
                    locked.page,
                    candidate=candidate,
                    event_id=event_id,
                    expected_state_extra=recovery_state,
                )
                released = locked.conn.execute(
                    """
                    UPDATE vault_write_intents
                    SET executor_owner=NULL,lease_expires_at=NULL,
                        last_error='ambiguous recovery requires resolution',updated_at=?
                    WHERE id=? AND status='recovery_required' AND executor_owner=?
                    """,
                    (now_iso(), intent_id, executor_owner),
                )
                if released.rowcount != 1:
                    raise RevisionConflict(
                        "ambiguous recovery owner changed",
                        current_revision_id=locked.page.get("current_revision_id"),
                        pending_intent_id=locked.page.get("pending_write_intent_id"),
                    )
                if not any(
                    payload.get("recovery_intent_id") == intent_id
                    and payload.get("review_id") == review_id
                    for payload in self._audit_payloads_locked(
                        locked.conn,
                        "wiki_recovery_ambiguous",
                    )
                ):
                    audit(
                        locked.conn,
                        "wiki_recovery_ambiguous",
                        {
                            "recovery_intent_id": intent_id,
                            "page_id": locked.page["page_id"],
                            "review_id": review_id,
                            "candidate_revision_id": candidate.id,
                            "event_id": event_id,
                            "primary_observation_id": primary["id"],
                            "secondary_observation_ids": secondary_ids,
                            "projection_job_ids": list(jobs),
                        },
                        now_iso(),
                    )
                return {
                    "intent_status": "recovery_required",
                    "current_revision_id": locked.page.get("current_revision_id"),
                    "current_kind": "unchanged",
                    "successor_intent_id": None,
                    "observation_ids": observation_ids,
                }

            candidate_document = parse_wiki_bytes(candidate.content.encode("utf-8"))
            write_token = candidate_document.frontmatter.get("lgdo_write_token")
            if not isinstance(write_token, str) or not write_token:
                raise WikiRevisionError("recovery candidate has no write token")
            if target_kind == "unknown":
                physical_target_hash = primary["file_hash"]
            elif target_kind == "intended":
                physical_target_hash = intended["file_hash"]
            elif target_kind == "expected":
                physical_target_hash = intent["expected_file_hash"]
            elif target_kind == "missing":
                physical_target_hash = None
            else:
                raise WikiRevisionError(
                    f"invalid recovery target classification: {target_kind}"
                )
            successor_id = self._prepare_recovery_successor_locked(
                locked,
                old_intent=intent,
                revision=candidate,
                expected_file_hash=physical_target_hash,
                write_token=write_token,
                executor_owner=executor_owner,
            )
            audit(
                locked.conn,
                "wiki_recovery_handoff",
                {
                    "recovery_intent_id": intent_id,
                    "successor_intent_id": successor_id,
                    "candidate_revision_id": candidate.id,
                    "observation_ids": observation_ids,
                    "source": primary_source,
                },
                now_iso(),
            )
            return {
                "intent_status": "superseded",
                "current_revision_id": locked.page.get("current_revision_id"),
                "current_kind": f"external_{primary_source}",
                "successor_intent_id": successor_id,
                "observation_ids": observation_ids,
            }

    def fail_missing_recovery(
        self,
        intent_id: str,
        executor_owner: str,
    ) -> dict[str, Any]:
        with connect_app(self.settings) as conn:
            locator = conn.execute(
                "SELECT page_id FROM vault_write_intents WHERE id=?",
                (intent_id,),
            ).fetchone()
        if locator is None:
            raise WikiRevisionError(f"write intent not found: {intent_id}")
        with self.coordinator.lock_page(page_id=locator["page_id"]) as locked:
            suffix = " FOR UPDATE" if self.settings.database_backend == "postgres" else ""
            intent = locked.conn.execute(
                "SELECT * FROM vault_write_intents WHERE id=?" + suffix,
                (intent_id,),
            ).fetchone()
            if (
                intent is None
                or intent["status"] != "recovery_required"
                or intent["executor_owner"] != executor_owner
                or locked.page.get("pending_write_intent_id") != intent_id
            ):
                raise RevisionConflict(
                    "missing recovery intent changed",
                    current_revision_id=locked.page.get("current_revision_id"),
                    pending_intent_id=locked.page.get("pending_write_intent_id"),
                )
            jobs = self._fail_closed_recovery_locked(
                locked,
                intent_id=intent_id,
                error_code="recovery_target_and_backup_missing",
                observed_file_hash=None,
            )
            failed = locked.conn.execute(
                """
                UPDATE vault_write_intents
                SET status='failed',executor_owner=NULL,lease_expires_at=NULL,
                    last_error='target and backup are missing',updated_at=?
                WHERE id=? AND status='recovery_required' AND executor_owner=?
                """,
                (now_iso(), intent_id, executor_owner),
            )
            if failed.rowcount != 1:
                raise WikiRevisionError("missing recovery intent status CAS failed")
            cleared = locked.conn.execute(
                """
                UPDATE wiki_pages SET pending_write_intent_id=NULL,updated_at=?
                WHERE page_id=? AND pending_write_intent_id=?
                """,
                (now_iso(), locked.page["page_id"], intent_id),
            )
            if cleared.rowcount != 1:
                raise WikiRevisionError("missing recovery pointer CAS failed")
            locked.page["pending_write_intent_id"] = None
            audit(
                locked.conn,
                "wiki_recovery_missing",
                {
                    "recovery_intent_id": intent_id,
                    "page_id": locked.page["page_id"],
                    "current_revision_id": locked.page.get("current_revision_id"),
                    "projection_job_ids": list(jobs),
                },
                now_iso(),
            )
            return {
                "current_revision_id": locked.page.get("current_revision_id"),
                "observation_ids": [],
            }

    def replay_recovery_handoff(self, intent_id: str) -> dict[str, Any]:
        with connect_app(self.settings) as conn:
            old_intent = conn.execute(
                "SELECT * FROM vault_write_intents WHERE id=?",
                (intent_id,),
            ).fetchone()
            if old_intent is None or old_intent["status"] != "superseded":
                raise RevisionConflict(
                    "recovery handoff is not replayable",
                    current_revision_id=None,
                    pending_intent_id=None,
                )
            page = conn.execute(
                "SELECT * FROM wiki_pages WHERE page_id=?",
                (old_intent["page_id"],),
            ).fetchone()
            rows = conn.execute(
                """
                SELECT intent.*,revision.metadata_json
                FROM vault_write_intents AS intent
                JOIN wiki_page_revisions AS revision ON revision.id=intent.revision_id
                WHERE intent.page_id=? AND intent.id<>?
                ORDER BY intent.created_at,intent.id
                """,
                (old_intent["page_id"], intent_id),
            ).fetchall()
            resolution_payload = next(
                (
                    payload
                    for payload in self._audit_payloads_locked(
                        conn,
                        "wiki_conflict_resolution_prepared",
                    )
                    if payload.get("recovery_intent_id") == intent_id
                    and isinstance(payload.get("write_intent_id"), str)
                ),
                None,
            )
        successor = None
        metadata: dict[str, Any] = {}
        for row in rows:
            try:
                candidate_metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if candidate_metadata.get("recovery_intent_id") == intent_id:
                successor = row
                metadata = candidate_metadata
                break
        if successor is None and resolution_payload is not None:
            successor = next(
                (
                    row
                    for row in rows
                    if row["id"] == resolution_payload["write_intent_id"]
                ),
                None,
            )
            if successor is not None:
                metadata = {
                    "recovery_source": (
                        "target"
                        if resolution_payload.get("resolution")
                        == "accept_candidate"
                        else None
                    )
                }
        if successor is None:
            raise WikiRevisionError("superseded recovery has no successor intent")
        observation_ids = [metadata.get("observation_id")]
        observation_ids.extend(
            metadata.get("recovery_secondary_observation_ids") or []
        )
        return {
            "current_revision_id": (
                page["current_revision_id"] if page is not None else None
            ),
            "current_kind": (
                f"external_{metadata['recovery_source']}"
                if metadata.get("recovery_source") in {"target", "backup"}
                else "unchanged"
            ),
            "successor_intent_id": successor["id"],
            "observation_ids": [
                value for value in observation_ids if isinstance(value, str)
            ],
        }

    def reconcile_retained_backup(
        self,
        intent_id: str,
        observation: FileObservationInput,
    ) -> bool:
        with connect_app(self.settings) as conn:
            locator = conn.execute(
                "SELECT page_id FROM vault_write_intents WHERE id=?",
                (intent_id,),
            ).fetchone()
        if locator is None:
            raise WikiRevisionError(f"write intent not found: {intent_id}")
        with self.coordinator.lock_page(page_id=locator["page_id"]) as locked:
            suffix = " FOR UPDATE" if self.settings.database_backend == "postgres" else ""
            intent = locked.conn.execute(
                "SELECT * FROM vault_write_intents WHERE id=?" + suffix,
                (intent_id,),
            ).fetchone()
            if (
                intent is None
                or intent["status"] != "applied"
                or intent["backup_retention_status"]
                not in {"retained", "change_detected"}
            ):
                return False
            previous_hash = intent["backup_last_observed_hash"]
            if previous_hash == observation.file_hash:
                return False

            observed = self._persist_observation_locked(
                locked.conn,
                locked.page,
                observation,
            )
            error_code, error_message = self._observation_error_locked(
                locked.conn,
                observed,
            )
            candidate: RevisionRecord | None = None
            event_id: str | None = None
            review_id: str | None = None
            if error_code is None:
                expected_state_json = canonical_state_json(
                    self._canonical_state_locked(locked.conn, locked.page)
                )
                event_id = self._stable_recovery_id(
                    "vevt",
                    "retained-backup",
                    intent_id,
                    previous_hash or "none",
                    observation.file_hash,
                    str(observation.mtime_ns),
                )
                candidate = self._create_external_candidate_locked(
                    locked.conn,
                    locked.page,
                    observation=observed,
                    event_id=event_id,
                    expected_state_json=expected_state_json,
                    actor="vault-backup-monitor",
                    metadata_extra={
                        "retained_backup_intent_id": intent_id,
                        "retained_backup_previous_hash": previous_hash,
                    },
                )
                review_id = self._seed_concurrent_review_locked(
                    locked.conn,
                    locked.page,
                    candidate=candidate,
                    event_id=event_id,
                )
            elif self._supersede_retained_backup_review_locked(
                locked.conn,
                locked.page,
                intent_id,
            ):
                self._reconcile_pending_reviews_locked(
                    locked.conn,
                    locked.page["page_id"],
                )

            baseline_sql = (
                "backup_last_observed_hash IS NULL"
                if previous_hash is None
                else "backup_last_observed_hash=?"
            )
            params: list[Any] = [
                observation.file_hash,
                now_iso(),
                intent_id,
            ]
            if previous_hash is not None:
                params.append(previous_hash)
            advanced = locked.conn.execute(
                f"""
                UPDATE vault_write_intents
                SET backup_last_observed_hash=?,
                    backup_retention_status='change_detected',updated_at=?
                WHERE id=? AND status='applied'
                  AND backup_retention_status IN ('retained','change_detected')
                  AND {baseline_sql}
                """,
                tuple(params),
            )
            if advanced.rowcount != 1:
                raise RevisionConflict(
                    "retained backup baseline changed during reconciliation",
                    current_revision_id=locked.page.get("current_revision_id"),
                    pending_intent_id=locked.page.get("pending_write_intent_id"),
                )
            audit(
                locked.conn,
                "wiki_retained_backup_changed",
                {
                    "intent_id": intent_id,
                    "page_id": locked.page["page_id"],
                    "page_path": locked.page["path"],
                    "previous_hash": previous_hash,
                    "backup_hash": observation.file_hash,
                    "observation_id": observed["id"],
                    "event_id": event_id,
                    "candidate_revision_id": (
                        candidate.id if candidate is not None else None
                    ),
                    "review_id": review_id,
                    "error_code": error_code,
                    "error_message": error_message,
                },
                now_iso(),
            )
            return True

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
                    self._reconcile_pending_reviews_locked(
                        locked.conn,
                        locked.page["page_id"],
                    )
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
                self._record_prepared_transition_locked(
                    locked.conn,
                    locked.page,
                    intent_id=intent_id,
                    revision=revision,
                    transition_kind=transition_prefix,
                    transition_id=request_id,
                    request_id=request_id,
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

            conflict_id = self._reconcile_pending_reviews_locked(
                locked.conn,
                locked.page["page_id"],
            )
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
                self._record_prepared_transition_locked(
                    locked.conn,
                    locked.page,
                    intent_id=intent_id,
                    revision=candidate,
                    transition_kind="compile",
                    transition_id=command.compile_job_id,
                    compile_job_id=command.compile_job_id,
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

    @staticmethod
    def _resolution_command_payload(
        command: ResolveConflictCommand,
    ) -> dict[str, Any]:
        merged_hash = None
        if command.merged_content is not None:
            merged_hash = hashlib.sha256(
                command.merged_content.encode("utf-8")
            ).hexdigest()
        return {
            "review_id": command.review_id,
            "resolution": command.resolution,
            "expected_current_revision_id": command.expected_current_revision_id,
            "expected_generated_revision_id": command.expected_generated_revision_id,
            "request_id": command.request_id,
            "actor": command.actor,
            "note": command.note,
            "merged_content_hash": merged_hash,
        }

    @staticmethod
    def _audit_payloads_locked(
        conn: Any,
        event_type: str,
    ) -> Iterator[dict[str, Any]]:
        rows = conn.execute(
            "SELECT payload_json FROM audit_logs WHERE event_type=? ORDER BY id DESC",
            (event_type,),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                yield payload

    def _record_prepared_transition_locked(
        self,
        conn: Any,
        page: dict[str, Any],
        *,
        intent_id: str,
        revision: RevisionRecord,
        transition_kind: str,
        transition_id: str,
        request_id: str | None = None,
        compile_job_id: str | None = None,
    ) -> dict[str, Any]:
        if transition_kind == "compile":
            if compile_job_id != transition_id or request_id is not None:
                raise ValueError("compile transition identity is inconsistent")
            specific_identity = {"compile_job_id": compile_job_id}
        elif transition_kind in {"manual", "human", "status", "metadata"}:
            if request_id != transition_id or compile_job_id is not None:
                raise ValueError("human transition identity is inconsistent")
            specific_identity = {"request_id": request_id}
        else:
            raise ValueError(f"unsupported transition kind: {transition_kind}")
        if not transition_id:
            raise ValueError("transition identity must be nonblank")

        payload = {
            "intent_id": intent_id,
            "revision_id": revision.id,
            "page_id": page["page_id"],
            "page_path": page["path"],
            "transition_kind": transition_kind,
            "transition_id": transition_id,
            **specific_identity,
        }
        existing = [
            item
            for item in self._audit_payloads_locked(
                conn,
                "wiki_revision_transition_prepared",
            )
            if item.get("intent_id") == intent_id
        ]
        if any(item != payload for item in existing):
            raise WikiRevisionError(
                "prepared revision transition identity changed"
            )
        if existing:
            return existing[0]
        audit(
            conn,
            "wiki_revision_transition_prepared",
            payload,
            now_iso(),
        )
        return payload

    def _resolution_payload_for_command_locked(
        self,
        conn: Any,
        event_type: str,
        command: ResolveConflictCommand,
    ) -> dict[str, Any] | None:
        expected = self._resolution_command_payload(command)
        for payload in self._audit_payloads_locked(conn, event_type):
            if (
                payload.get("review_id") != command.review_id
                or payload.get("request_id") != command.request_id
            ):
                continue
            if any(payload.get(key) != value for key, value in expected.items()):
                raise RevisionConflict(
                    "conflict resolution request was reused with different input",
                    current_revision_id=command.expected_current_revision_id,
                )
            return payload
        return None

    @staticmethod
    def _resolved_mutation_from_payload(
        payload: dict[str, Any],
        *,
        replayed: bool,
    ) -> MutationResult:
        return MutationResult(
            page_id=str(payload["page_id"]),
            page_path=str(payload["page_path"]),
            status="resolved",
            revision_id=str(payload["resolution_revision_id"]),
            current_revision_id=str(payload["resolution_revision_id"]),
            generated_revision_id=payload.get("generated_revision_id"),
            candidate_revision_id=payload.get("candidate_revision_id"),
            write_intent_id=payload.get("write_intent_id"),
            conflict_review_id=str(payload["review_id"]),
            projection_job_ids=tuple(payload.get("projection_job_ids") or ()),
            replayed=replayed,
        )

    def _validate_pending_conflict_locked(
        self,
        conn: Any,
        page: dict[str, Any],
        review: Any,
        command: ResolveConflictCommand,
    ) -> Any:
        if review["status"] != "pending":
            raise RevisionConflict(
                "conflict review is no longer pending",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        if (
            page.get("current_revision_id")
            != command.expected_current_revision_id
            or page.get("generated_revision_id")
            != command.expected_generated_revision_id
            or review["base_revision_id"]
            != command.expected_current_revision_id
        ):
            raise RevisionConflict(
                "conflict resolution state changed",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        try:
            stored_expected_state = json.loads(
                review["expected_state_json"] or "{}"
            )
        except (TypeError, json.JSONDecodeError) as exc:
            raise RevisionConflict(
                "conflict review expected state is invalid",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            ) from exc
        expected_state = self._canonical_state_locked(conn, page)
        recovery_keys = {
            "recovery_intent_id",
            "recovery_primary_observation_id",
            "recovery_secondary_observation_ids",
        }
        present_recovery_keys = recovery_keys.intersection(stored_expected_state)
        if present_recovery_keys:
            if present_recovery_keys != recovery_keys:
                raise RevisionConflict(
                    "recovery conflict evidence is incomplete",
                    current_revision_id=page.get("current_revision_id"),
                    pending_intent_id=page.get("pending_write_intent_id"),
                )
            recovery_intent_id = stored_expected_state["recovery_intent_id"]
            primary_observation_id = stored_expected_state[
                "recovery_primary_observation_id"
            ]
            secondary_observation_ids = stored_expected_state[
                "recovery_secondary_observation_ids"
            ]
            if (
                not isinstance(recovery_intent_id, str)
                or not isinstance(primary_observation_id, str)
                or not isinstance(secondary_observation_ids, list)
                or not secondary_observation_ids
                or not all(
                    isinstance(value, str)
                    for value in secondary_observation_ids
                )
            ):
                raise RevisionConflict(
                    "recovery conflict evidence is invalid",
                    current_revision_id=page.get("current_revision_id"),
                    pending_intent_id=page.get("pending_write_intent_id"),
                )
            recovery_intent = conn.execute(
                "SELECT * FROM vault_write_intents WHERE id=?",
                (recovery_intent_id,),
            ).fetchone()
            if (
                recovery_intent is None
                or recovery_intent["page_id"] != page["page_id"]
                or recovery_intent["status"]
                not in {"recovery_required", "superseded"}
            ):
                raise RevisionConflict(
                    "recovery intent is no longer valid for resolution",
                    current_revision_id=page.get("current_revision_id"),
                    pending_intent_id=page.get("pending_write_intent_id"),
                )
            expected_state.update(
                {
                    key: stored_expected_state[key]
                    for key in recovery_keys
                }
            )
        displaced_intent_key = "recovery_displaced_intent_id"
        displaced_lineage = None
        if displaced_intent_key in stored_expected_state:
            displaced_intent_id = stored_expected_state[displaced_intent_key]
            if not isinstance(displaced_intent_id, str) or not displaced_intent_id:
                raise RevisionConflict(
                    "recovery-displaced conflict evidence is invalid",
                    current_revision_id=page.get("current_revision_id"),
                    pending_intent_id=page.get("pending_write_intent_id"),
                )
            displaced_lineage = self._recovery_displaced_lineage_locked(
                conn,
                page,
                expected_intent_id=displaced_intent_id,
            )
            if displaced_lineage is None:
                raise RevisionConflict(
                    "recovery-displaced conflict evidence is stale",
                    current_revision_id=page.get("current_revision_id"),
                    pending_intent_id=page.get("pending_write_intent_id"),
                )
            expected_state[displaced_intent_key] = displaced_intent_id
        expected_state_json = canonical_state_json(expected_state)
        if review["expected_state_json"] != expected_state_json:
            raise RevisionConflict(
                "conflict review expected state changed",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        candidate = conn.execute(
            "SELECT * FROM wiki_page_revisions WHERE id=?",
            (review["candidate_revision_id"],),
        ).fetchone()
        if candidate is None or candidate["page_id"] != page["page_id"]:
            raise RevisionConflict(
                "conflict candidate is no longer valid",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        if review["issue_type"] == "content_conflict":
            if review["candidate_revision_id"] != page.get("generated_revision_id"):
                raise RevisionConflict(
                    "content conflict candidate is no longer generated",
                    current_revision_id=page.get("current_revision_id"),
                    pending_intent_id=page.get("pending_write_intent_id"),
                )
        elif review["issue_type"] == "concurrent_write_conflict":
            recovery_displaced_merge = (
                candidate["origin"] == "merge"
                and displaced_lineage is not None
                and displaced_lineage[0]["id"] == candidate["id"]
            )
            if (
                candidate["origin"] not in {"manual", "external"}
                and not recovery_displaced_merge
            ):
                raise RevisionConflict(
                    "concurrent conflict candidate has no resolvable lineage",
                    current_revision_id=page.get("current_revision_id"),
                    pending_intent_id=page.get("pending_write_intent_id"),
                )
        else:
            raise RevisionConflict(
                "review is not a resolvable wiki conflict",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        return candidate

    def _record_resolved_conflict_locked(
        self,
        conn: Any,
        page: dict[str, Any],
        review: Any,
        command_payload: dict[str, Any],
        *,
        resolution_revision_id: str,
        write_intent_id: str | None,
        projection_job_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        timestamp = now_iso()
        if review["issue_type"] == "content_conflict":
            expected_accepted = page.get("accepted_generated_revision_id")
            accepted_sql = (
                "accepted_generated_revision_id IS NULL"
                if expected_accepted is None
                else "accepted_generated_revision_id=?"
            )
            generated = command_payload["expected_generated_revision_id"]
            generated_sql = (
                "generated_revision_id IS NULL"
                if generated is None
                else "generated_revision_id=?"
            )
            params: list[Any] = [
                review["candidate_revision_id"],
                timestamp,
                page["page_id"],
                resolution_revision_id,
            ]
            if generated is not None:
                params.append(generated)
            if expected_accepted is not None:
                params.append(expected_accepted)
            accepted = conn.execute(
                f"""
                UPDATE wiki_pages
                SET accepted_generated_revision_id=?,updated_at=?
                WHERE page_id=? AND current_revision_id=?
                  AND {generated_sql} AND {accepted_sql}
                """,
                tuple(params),
            )
            if accepted.rowcount != 1:
                raise RevisionConflict(
                    "accepted generated revision changed during resolution",
                    current_revision_id=page.get("current_revision_id"),
                    pending_intent_id=page.get("pending_write_intent_id"),
                )
            page["accepted_generated_revision_id"] = review[
                "candidate_revision_id"
            ]

        closed = conn.execute(
            """
            UPDATE review_items
            SET status='resolved',resolution_revision_id=?,resolved_at=?,updated_at=?
            WHERE id=? AND status='pending' AND base_revision_id=?
              AND candidate_revision_id=? AND expected_state_json=?
            """,
            (
                resolution_revision_id,
                timestamp,
                timestamp,
                review["id"],
                review["base_revision_id"],
                review["candidate_revision_id"],
                review["expected_state_json"],
            ),
        )
        if closed.rowcount != 1:
            raise RevisionConflict(
                "conflict review changed during resolution",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        payload = {
            **command_payload,
            "page_id": page["page_id"],
            "page_path": page["path"],
            "issue_type": review["issue_type"],
            "candidate_revision_id": review["candidate_revision_id"],
            "resolution_revision_id": resolution_revision_id,
            "generated_revision_id": page.get("generated_revision_id"),
            "accepted_generated_revision_id": page.get(
                "accepted_generated_revision_id"
            ),
            "write_intent_id": write_intent_id,
            "projection_job_ids": list(projection_job_ids),
            "resolved_at": timestamp,
        }
        audit(conn, "wiki_conflict_resolved", payload, timestamp)
        self._reconcile_pending_reviews_locked(conn, page["page_id"])
        return payload

    def list_conflicts(
        self,
        page_path: str,
        status: str = "pending",
    ) -> list[dict[str, Any]]:
        self._target(page_path)
        with connect_app(self.settings) as conn:
            rows = conn.execute(
                """
                SELECT * FROM review_items
                WHERE page_path=? AND status=?
                  AND issue_type IN ('content_conflict','concurrent_write_conflict')
                ORDER BY created_at,id
                """,
                (page_path, status),
            ).fetchall()
        conflicts: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["source_ids"] = json.loads(item.pop("source_ids_json") or "[]")
            item["expected_state"] = json.loads(
                item.pop("expected_state_json") or "{}"
            )
            conflicts.append(item)
        return conflicts

    def resolve_conflict(
        self,
        command: ResolveConflictCommand,
    ) -> MutationResult:
        if command.resolution == "merged_content" and command.merged_content is None:
            raise WikiRevisionError("merged_content resolution requires content")
        if command.resolution != "merged_content" and command.merged_content is not None:
            raise WikiRevisionError(
                "merged content is only valid for merged_content resolution"
            )
        with connect_app(self.settings) as conn:
            locator = conn.execute(
                "SELECT page_id FROM review_items WHERE id=?",
                (command.review_id,),
            ).fetchone()
        if locator is None or locator["page_id"] is None:
            raise RevisionConflict(
                "conflict review not found",
                current_revision_id=command.expected_current_revision_id,
            )

        intent_id: str | None = None
        command_payload = self._resolution_command_payload(command)
        with self.coordinator.lock_page(page_id=locator["page_id"]) as locked:
            suffix = (
                " FOR UPDATE" if self.settings.database_backend == "postgres" else ""
            )
            review = locked.conn.execute(
                "SELECT * FROM review_items WHERE id=?" + suffix,
                (command.review_id,),
            ).fetchone()
            if review is None:
                raise RevisionConflict(
                    "conflict review not found",
                    current_revision_id=locked.page.get("current_revision_id"),
                    pending_intent_id=locked.page.get("pending_write_intent_id"),
                )
            resolved = self._resolution_payload_for_command_locked(
                locked.conn,
                "wiki_conflict_resolved",
                command,
            )
            if resolved is not None:
                return self._resolved_mutation_from_payload(
                    resolved,
                    replayed=True,
                )
            candidate = self._validate_pending_conflict_locked(
                locked.conn,
                locked.page,
                review,
                command,
            )
            try:
                review_expected_state = json.loads(
                    review["expected_state_json"] or "{}"
                )
            except (TypeError, json.JSONDecodeError):
                review_expected_state = {}
            recovery_intent_id = review_expected_state.get(
                "recovery_intent_id"
            )
            if not isinstance(recovery_intent_id, str):
                recovery_intent_id = None
            prepared = self._resolution_payload_for_command_locked(
                locked.conn,
                "wiki_conflict_resolution_prepared",
                command,
            )
            if prepared is not None:
                intent_id = prepared.get("write_intent_id")
                if (
                    not isinstance(intent_id, str)
                    or locked.page.get("pending_write_intent_id") != intent_id
                ):
                    raise RevisionConflict(
                        "prepared conflict resolution is no longer current",
                        current_revision_id=locked.page.get("current_revision_id"),
                        pending_intent_id=locked.page.get(
                            "pending_write_intent_id"
                        ),
                    )
            elif recovery_intent_id is not None:
                if command.resolution not in {"keep_current", "accept_candidate"}:
                    raise RevisionConflict(
                        "ambiguous recovery supports keep_current or accept_candidate",
                        current_revision_id=locked.page.get("current_revision_id"),
                        pending_intent_id=locked.page.get(
                            "pending_write_intent_id"
                        ),
                    )
                recovery_intent = locked.conn.execute(
                    "SELECT * FROM vault_write_intents WHERE id=?" + suffix,
                    (recovery_intent_id,),
                ).fetchone()
                if (
                    recovery_intent is None
                    or recovery_intent["status"] != "recovery_required"
                    or locked.page.get("pending_write_intent_id")
                    != recovery_intent_id
                ):
                    raise RevisionConflict(
                        "recovery intent is no longer pending resolution",
                        current_revision_id=locked.page.get("current_revision_id"),
                        pending_intent_id=locked.page.get(
                            "pending_write_intent_id"
                        ),
                    )
                if command.resolution == "accept_candidate":
                    resolution_revision = RevisionRecord.from_row(candidate)
                else:
                    current = locked.conn.execute(
                        "SELECT * FROM wiki_page_revisions WHERE id=?",
                        (command.expected_current_revision_id,),
                    ).fetchone()
                    if current is None or current["page_id"] != locked.page["page_id"]:
                        raise RevisionConflict(
                            "current recovery revision is no longer available",
                            current_revision_id=locked.page.get(
                                "current_revision_id"
                            ),
                            pending_intent_id=locked.page.get(
                                "pending_write_intent_id"
                            ),
                        )
                    resolution_revision = RevisionRecord.from_row(current)
                resolution_document = parse_wiki_bytes(
                    resolution_revision.content.encode("utf-8")
                )
                token = resolution_document.frontmatter.get("lgdo_write_token")
                if not isinstance(token, str) or not token:
                    raise WikiRevisionError(
                        "recovery resolution revision has no managed write token"
                    )
                primary_observation = locked.conn.execute(
                    "SELECT * FROM wiki_file_observations WHERE id=?",
                    (
                        review_expected_state[
                            "recovery_primary_observation_id"
                        ],
                    ),
                ).fetchone()
                if (
                    primary_observation is None
                    or primary_observation["page_id"] != locked.page["page_id"]
                ):
                    raise RevisionConflict(
                        "recovery primary observation is no longer valid",
                        current_revision_id=locked.page.get("current_revision_id"),
                        pending_intent_id=recovery_intent_id,
                    )
                intent_id = self._prepare_recovery_successor_locked(
                    locked,
                    old_intent=recovery_intent,
                    revision=resolution_revision,
                    expected_file_hash=primary_observation["file_hash"],
                    write_token=token,
                    executor_owner=None,
                )
                prepared_payload = {
                    **command_payload,
                    "page_id": locked.page["page_id"],
                    "page_path": locked.page["path"],
                    "issue_type": review["issue_type"],
                    "candidate_revision_id": review["candidate_revision_id"],
                    "resolution_revision_id": resolution_revision.id,
                    "accepted_generated_revision_id": locked.page.get(
                        "accepted_generated_revision_id"
                    ),
                    "expected_state_json": review["expected_state_json"],
                    "write_intent_id": intent_id,
                    "recovery_intent_id": recovery_intent_id,
                }
                audit(
                    locked.conn,
                    "wiki_conflict_resolution_prepared",
                    prepared_payload,
                    now_iso(),
                )
            elif command.resolution == "keep_current":
                if locked.page.get("pending_write_intent_id") is not None:
                    raise RevisionConflict(
                        "page already has a pending write",
                        current_revision_id=locked.page.get("current_revision_id"),
                        pending_intent_id=locked.page.get(
                            "pending_write_intent_id"
                        ),
                    )
                self._supersede_candidate_event_locked(
                    locked.conn,
                    review["candidate_revision_id"],
                )
                payload = self._record_resolved_conflict_locked(
                    locked.conn,
                    locked.page,
                    review,
                    command_payload,
                    resolution_revision_id=command.expected_current_revision_id,
                    write_intent_id=None,
                )
                return self._resolved_mutation_from_payload(
                    payload,
                    replayed=False,
                )
            else:
                if locked.page.get("pending_write_intent_id") is not None:
                    raise RevisionConflict(
                        "page already has a pending write",
                        current_revision_id=locked.page.get("current_revision_id"),
                        pending_intent_id=locked.page.get(
                            "pending_write_intent_id"
                        ),
                    )
                if command.resolution == "accept_candidate":
                    resolution_revision = RevisionRecord.from_row(candidate)
                    candidate_document = parse_wiki_bytes(
                        resolution_revision.content.encode("utf-8")
                    )
                    token = candidate_document.frontmatter.get("lgdo_write_token")
                    if not isinstance(token, str) or not token:
                        raise WikiRevisionError(
                            "conflict candidate has no managed write token"
                        )
                    write_token = token
                else:
                    document = parse_wiki_bytes(
                        command.merged_content.encode("utf-8")
                    )
                    effective_status = _review_status(
                        document.frontmatter.get("review_status")
                        or locked.page.get("review_status")
                    )
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
                    state_digest = hashlib.sha256(
                        review["expected_state_json"].encode("utf-8")
                    ).hexdigest()
                    resolution_revision = self._create_revision_locked(
                        locked.conn,
                        locked.page,
                        content=rendered,
                        origin="merge",
                        base_revision_id=command.expected_current_revision_id,
                        source_ids=_source_ids(metadata),
                        actor=command.actor,
                        note=command.note,
                        idempotency_key=(
                            f"resolve:{command.review_id}:{command.request_id}:"
                            f"{state_digest}"
                        ),
                        metadata=metadata,
                        revision_id=revision_id,
                    )
                intent_id = self.prepare_write_intent_locked(
                    locked.conn,
                    locked.page,
                    revision=resolution_revision,
                    expected_revision_id=command.expected_current_revision_id,
                    expected_file_hash=locked.page.get("file_hash"),
                    write_token=write_token,
                )
                prepared_payload = {
                    **command_payload,
                    "page_id": locked.page["page_id"],
                    "page_path": locked.page["path"],
                    "issue_type": review["issue_type"],
                    "candidate_revision_id": review["candidate_revision_id"],
                    "resolution_revision_id": resolution_revision.id,
                    "accepted_generated_revision_id": locked.page.get(
                        "accepted_generated_revision_id"
                    ),
                    "expected_state_json": review["expected_state_json"],
                    "write_intent_id": intent_id,
                }
                audit(
                    locked.conn,
                    "wiki_conflict_resolution_prepared",
                    prepared_payload,
                    now_iso(),
                )

        if intent_id is None:
            raise WikiRevisionError("conflict resolution did not prepare an intent")
        from app.vault_writer import IntentExecutor

        executed = IntentExecutor(self.settings).execute(intent_id)
        if executed is None or executed.intent_status != "applied":
            with connect_app(self.settings) as conn:
                intent = conn.execute(
                    "SELECT status FROM vault_write_intents WHERE id=?",
                    (intent_id,),
                ).fetchone()
            if intent is None or intent["status"] != "applied":
                raise RevisionConflict(
                    "conflict resolution requires write recovery",
                    current_revision_id=command.expected_current_revision_id,
                    pending_intent_id=intent_id,
                )
        with connect_app(self.settings) as conn:
            resolved = self._resolution_payload_for_command_locked(
                conn,
                "wiki_conflict_resolved",
                command,
            )
        if resolved is None:
            raise WikiRevisionError(
                "applied conflict resolution has no completion audit"
            )
        return self._resolved_mutation_from_payload(resolved, replayed=False)

    def release_retained_backup(
        self,
        intent_id: str,
        expected_backup_hash: str,
        *,
        actor: str,
    ) -> BackupReleaseResult:
        if not actor.strip():
            raise RevisionConflict(
                "backup release requires a nonblank actor",
                current_revision_id=None,
            )
        with connect_app(self.settings) as conn:
            locator = conn.execute(
                """
                SELECT page_id,backup_path,status,backup_retention_status,
                       backup_last_observed_hash
                FROM vault_write_intents WHERE id=?
                """,
                (intent_id,),
            ).fetchone()
        if locator is None:
            raise RevisionConflict(
                "backup release intent not found",
                current_revision_id=None,
            )
        backup_path = self.writer.backup_path(intent_id)
        persisted_path = locator["backup_path"]
        if (
            not isinstance(persisted_path, str)
            or Path(persisted_path).resolve() != backup_path.resolve()
        ):
            raise RevisionConflict(
                "retained backup path is invalid",
                current_revision_id=None,
            )
        if (
            locator["status"] == "applied"
            and locator["backup_retention_status"] == "released"
        ):
            if backup_path.exists():
                raise RevisionConflict(
                    "released backup path reappeared",
                    current_revision_id=None,
                )
            if locator["backup_last_observed_hash"] != expected_backup_hash:
                raise RevisionConflict(
                    "released backup hash does not match replay",
                    current_revision_id=None,
                )
            with connect_app(self.settings) as conn:
                replayed = any(
                    payload.get("intent_id") == intent_id
                    and payload.get("backup_hash") == expected_backup_hash
                    for payload in self._audit_payloads_locked(
                        conn,
                        "vault_backup_released",
                    )
                )
            if not replayed:
                raise WikiRevisionError(
                    "released backup has no completion audit"
                )
            return BackupReleaseResult(
                intent_id=intent_id,
                page_id=str(locator["page_id"]),
                backup_hash=expected_backup_hash,
            )
        if not backup_path.exists():
            raise RevisionConflict(
                "retained backup path is missing",
                current_revision_id=None,
            )
        initial_hash = self.writer._stream_hash(backup_path)
        if initial_hash != expected_backup_hash:
            raise RevisionConflict(
                "retained backup hash changed before release",
                current_revision_id=None,
            )

        snapshot_root = self.writer.pending_root / ".release-snapshots"
        self.writer._ensure_directory_durable(snapshot_root)
        snapshot_path = snapshot_root / f"{intent_id}-{uuid.uuid4().hex}.bak"
        _link_or_copy_file_no_replace(backup_path, snapshot_path)
        self.writer._fsync_existing_file(snapshot_path)
        self.writer._fsync_directory(snapshot_root, strict=True)
        if self.writer._stream_hash(snapshot_path) != expected_backup_hash:
            raise RevisionConflict(
                "retained backup changed while creating release snapshot",
                current_revision_id=None,
            )

        tombstone_path = backup_path.with_name(
            f"{backup_path.name}.release-{uuid.uuid4().hex}"
        )
        claimed = False
        unlinked = False
        canonical_preserved = True
        canonical_was_displaced = False
        release_confirmed = False
        try:
            with self.coordinator.lock_page(page_id=locator["page_id"]) as locked:
                suffix = (
                    " FOR UPDATE"
                    if self.settings.database_backend == "postgres"
                    else ""
                )
                intent = locked.conn.execute(
                    "SELECT * FROM vault_write_intents WHERE id=?" + suffix,
                    (intent_id,),
                ).fetchone()
                if (
                    intent is None
                    or intent["page_id"] != locked.page["page_id"]
                    or intent["status"] != "applied"
                    or intent["backup_retention_status"]
                    not in {"retained", "change_detected"}
                    or intent["backup_last_observed_hash"]
                    != expected_backup_hash
                    or not isinstance(intent["backup_path"], str)
                    or Path(intent["backup_path"]).resolve()
                    != backup_path.resolve()
                ):
                    raise RevisionConflict(
                        "retained backup release precondition changed",
                        current_revision_id=locked.page.get(
                            "current_revision_id"
                        ),
                        pending_intent_id=locked.page.get(
                            "pending_write_intent_id"
                        ),
                    )
                canonical_was_displaced = True
                backup_path.replace(tombstone_path)
                claimed = True
                canonical_preserved = False
                release_hash = self.writer._stream_hash(tombstone_path)
                if release_hash != expected_backup_hash:
                    raise RevisionConflict(
                        "retained backup hash changed during release",
                        current_revision_id=locked.page.get(
                            "current_revision_id"
                        ),
                        pending_intent_id=locked.page.get(
                            "pending_write_intent_id"
                        ),
                    )
                if backup_path.exists():
                    raise RevisionConflict(
                        "retained backup path was replaced during release",
                        current_revision_id=locked.page.get(
                            "current_revision_id"
                        ),
                        pending_intent_id=locked.page.get(
                            "pending_write_intent_id"
                        ),
                    )
                current = locked.conn.execute(
                    "SELECT * FROM wiki_page_revisions WHERE id=?",
                    (locked.page.get("current_revision_id"),),
                ).fetchone()
                observations = locked.conn.execute(
                    """
                    SELECT id FROM wiki_file_observations
                    WHERE page_id=? AND file_hash=? ORDER BY id
                    """,
                    (locked.page["page_id"], expected_backup_hash),
                ).fetchall()
                tombstone_path.unlink()
                unlinked = True
                claimed = False
                self.writer._fsync_directory(backup_path.parent, strict=True)
                released = locked.conn.execute(
                    """
                    UPDATE vault_write_intents
                    SET backup_retention_status='released',updated_at=?
                    WHERE id=? AND status='applied'
                      AND backup_retention_status IN ('retained','change_detected')
                      AND backup_last_observed_hash=?
                    """,
                    (now_iso(), intent_id, expected_backup_hash),
                )
                if released.rowcount != 1:
                    raise RevisionConflict(
                        "retained backup release CAS failed",
                        current_revision_id=locked.page.get(
                            "current_revision_id"
                        ),
                        pending_intent_id=locked.page.get(
                            "pending_write_intent_id"
                        ),
                    )
                audit(
                    locked.conn,
                    "vault_backup_released",
                    {
                        "actor": actor.strip(),
                        "intent_id": intent_id,
                        "page_id": locked.page["page_id"],
                        "page_path": locked.page["path"],
                        "current_revision_id": locked.page.get(
                            "current_revision_id"
                        ),
                        "current_file_hash": (
                            current["file_hash"]
                            if current is not None
                            else locked.page.get("file_hash")
                        ),
                        "backup_hash": expected_backup_hash,
                        "backup_path": str(backup_path),
                        "observation_ids": [
                            str(row["id"]) for row in observations
                        ],
                    },
                    now_iso(),
                )
            release_confirmed = True
        except Exception as release_error:
            restore_error: Exception | None = None
            if not backup_path.exists():
                self.writer._ensure_directory_durable(backup_path.parent)
                restore_source: Path | None = None
                if claimed and tombstone_path.exists():
                    restore_source = tombstone_path
                elif (claimed or unlinked) and snapshot_path.exists():
                    restore_source = snapshot_path
                if restore_source is not None:
                    try:
                        _link_or_copy_file_no_replace(
                            restore_source,
                            backup_path,
                        )
                        self.writer._fsync_existing_file(backup_path)
                        self.writer._fsync_directory(
                            backup_path.parent,
                            strict=True,
                        )
                    except Exception as exc:
                        restore_error = exc
                    else:
                        claimed = False
                        canonical_preserved = True
                elif claimed or unlinked:
                    restore_error = FileNotFoundError(
                        "retained backup restore source is missing"
                    )
            elif not canonical_preserved:
                restore_error = FileExistsError(
                    "retained backup canonical path reappeared during restore"
                )
            if restore_error is not None:
                preserved_paths = [
                    path
                    for path in (backup_path, tombstone_path, snapshot_path)
                    if path.exists()
                ]
                raise OSError(
                    "retained backup release failed "
                    f"({release_error}); restore failed; recovery artifact "
                    "preserved at "
                    + ", ".join(str(path) for path in preserved_paths)
                ) from restore_error
            raise
        finally:
            if release_confirmed or (
                canonical_preserved and not canonical_was_displaced
            ):
                if tombstone_path.exists():
                    try:
                        tombstone_path.unlink()
                    except OSError:
                        pass
                snapshot_path.unlink(missing_ok=True)
        return BackupReleaseResult(
            intent_id=intent_id,
            page_id=str(locator["page_id"]),
            backup_hash=expected_backup_hash,
        )

    def _prepared_resolution_for_intent_locked(
        self,
        conn: Any,
        intent_id: str,
    ) -> dict[str, Any] | None:
        for payload in self._audit_payloads_locked(
            conn,
            "wiki_conflict_resolution_prepared",
        ):
            if payload.get("write_intent_id") == intent_id:
                return payload
        return None

    def _prepared_transition_for_intent_locked(
        self,
        conn: Any,
        intent: Any,
        revision: Any,
    ) -> dict[str, Any] | None:
        matching = [
            payload
            for payload in self._audit_payloads_locked(
                conn,
                "wiki_revision_transition_prepared",
            )
            if payload.get("intent_id") == intent["id"]
        ]
        if not matching:
            return None
        payload = matching[0]
        if any(item != payload for item in matching[1:]):
            raise WikiRevisionError(
                "prepared revision transition audits are inconsistent"
            )
        required = (
            "intent_id",
            "revision_id",
            "page_id",
            "page_path",
            "transition_kind",
            "transition_id",
        )
        if any(
            not isinstance(payload.get(key), str) or not payload[key]
            for key in required
        ):
            raise WikiRevisionError("prepared revision transition audit is invalid")
        if (
            payload["revision_id"] != revision["id"]
            or payload["page_id"] != intent["page_id"]
            or payload["page_id"] != revision["page_id"]
            or payload["page_path"] != intent["target_path"]
            or payload["page_path"] != revision["page_path"]
        ):
            raise WikiRevisionError(
                "prepared revision transition page identity changed"
            )

        kind = payload["transition_kind"]
        transition_id = payload["transition_id"]
        idempotency_key = revision["idempotency_key"]
        key_prefix, separator, state_digest = idempotency_key.rpartition(":")
        has_state_digest = (
            separator == ":"
            and len(state_digest) == 64
            and all(character in "0123456789abcdef" for character in state_digest)
        )
        if kind == "compile":
            try:
                revision_metadata = json.loads(revision["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                revision_metadata = None
            source_hash = (
                revision_metadata.get("source_hash")
                if isinstance(revision_metadata, dict)
                else None
            )
            compiler_version = (
                revision_metadata.get("compiler_version")
                if isinstance(revision_metadata, dict)
                else None
            )
            expected_key_prefix = (
                f"compile:{transition_id}:{revision['page_id']}:"
                f"{source_hash}:{compiler_version}"
            )
            valid = (
                revision["origin"] == "generated"
                and payload.get("compile_job_id") == transition_id
                and isinstance(source_hash, str)
                and isinstance(compiler_version, str)
                and has_state_digest
                and key_prefix == expected_key_prefix
            )
        else:
            valid = (
                kind in {"manual", "human", "status", "metadata"}
                and revision["origin"] == "manual"
                and payload.get("request_id") == transition_id
                and has_state_digest
                and key_prefix == f"{kind}:{transition_id}"
            )
        if not valid:
            raise WikiRevisionError(
                "prepared revision transition origin is inconsistent"
            )
        return payload

    @staticmethod
    def _applied_transition_identity(
        *,
        origin: str,
        metadata: dict[str, Any],
        resolution_payload: dict[str, Any] | None,
        prepared_transition_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        identity: dict[str, Any] = {
            "transition_kind": None,
            "transition_id": None,
        }
        if resolution_payload is not None:
            request_id = resolution_payload.get("request_id")
            if isinstance(request_id, str) and request_id:
                identity.update(
                    {
                        "transition_kind": "conflict_resolution",
                        "transition_id": request_id,
                        "request_id": request_id,
                    }
                )
            return identity

        if origin == "external":
            event_id = metadata.get("vault_change_event_id")
            if isinstance(event_id, str) and event_id:
                identity.update(
                    {
                        "transition_kind": "external",
                        "transition_id": event_id,
                        "event_id": event_id,
                    }
                )
            return identity

        if prepared_transition_payload is not None:
            kind = prepared_transition_payload["transition_kind"]
            transition_id = prepared_transition_payload["transition_id"]
            identity.update(
                {
                    "transition_kind": kind,
                    "transition_id": transition_id,
                }
            )
            if kind == "compile":
                identity["compile_job_id"] = transition_id
            else:
                identity["request_id"] = transition_id
        return identity

    def _validate_prepared_resolution_locked(
        self,
        conn: Any,
        page: dict[str, Any],
        intent: Any,
        revision: Any,
        payload: dict[str, Any],
    ) -> Any:
        required_strings = (
            "review_id",
            "resolution",
            "expected_current_revision_id",
            "request_id",
            "actor",
            "issue_type",
            "candidate_revision_id",
            "resolution_revision_id",
            "expected_state_json",
            "write_intent_id",
        )
        if any(not isinstance(payload.get(key), str) for key in required_strings):
            raise WikiRevisionError("prepared conflict resolution audit is invalid")
        if (
            payload["write_intent_id"] != intent["id"]
            or payload["resolution_revision_id"] != revision["id"]
            or payload["expected_current_revision_id"]
            != intent["expected_revision_id"]
            or payload["resolution"]
            not in {"keep_current", "accept_candidate", "merged_content"}
            or (
                payload["resolution"] == "keep_current"
                and not isinstance(payload.get("recovery_intent_id"), str)
            )
        ):
            raise RevisionConflict(
                "prepared conflict resolution no longer matches its intent",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        suffix = " FOR UPDATE" if self.settings.database_backend == "postgres" else ""
        review = conn.execute(
            "SELECT * FROM review_items WHERE id=?" + suffix,
            (payload["review_id"],),
        ).fetchone()
        if (
            review is None
            or review["issue_type"] != payload["issue_type"]
            or review["candidate_revision_id"]
            != payload["candidate_revision_id"]
            or review["expected_state_json"] != payload["expected_state_json"]
        ):
            raise RevisionConflict(
                "prepared conflict review changed before finalize",
                current_revision_id=page.get("current_revision_id"),
                pending_intent_id=page.get("pending_write_intent_id"),
            )
        command = ResolveConflictCommand(
            review_id=payload["review_id"],
            resolution=payload["resolution"],
            merged_content=None,
            expected_current_revision_id=payload[
                "expected_current_revision_id"
            ],
            expected_generated_revision_id=payload.get(
                "expected_generated_revision_id"
            ),
            request_id=payload["request_id"],
            actor=payload["actor"],
            note=payload.get("note"),
        )
        self._validate_pending_conflict_locked(conn, page, review, command)
        return review

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
            resolution_payload = self._prepared_resolution_for_intent_locked(
                locked.conn,
                intent_id,
            )
            resolution_review = None
            if resolution_payload is not None:
                resolution_review = self._validate_prepared_resolution_locked(
                    locked.conn,
                    locked.page,
                    intent,
                    revision,
                    resolution_payload,
                )
            prepared_transition_payload = None
            if resolution_payload is None and revision["origin"] != "external":
                prepared_transition_payload = (
                    self._prepared_transition_for_intent_locked(
                        locked.conn,
                        intent,
                        revision,
                    )
                )
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
            try:
                revision_content = revision["content"].encode("utf-8")
            except (AttributeError, UnicodeEncodeError) as exc:
                raise WikiRevisionError(
                    f"write revision content is invalid: {revision['id']}"
                ) from exc
            if compute_file_hash(revision_content) != revision["file_hash"]:
                raise WikiRevisionError(
                    f"write revision content hash changed: {revision['id']}"
                )
            try:
                content_document = parse_wiki_bytes(revision_content)
            except MarkdownParseError as exc:
                raise WikiRevisionError(
                    f"write revision content is invalid: {revision['id']}"
                ) from exc
            content_metadata = _plain(content_document.frontmatter)
            content_review_status = _review_status(
                content_metadata.get("review_status")
                or locked.page["review_status"]
            )
            content_owner = content_metadata.get("owner")
            try:
                loaded_metadata = json.loads(revision["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                loaded_metadata = {}
            metadata = loaded_metadata if isinstance(loaded_metadata, dict) else {}
            observed_file_hash = (
                metadata.get("observed_file_hash")
                if revision["origin"] == "external"
                else None
            ) or revision["file_hash"]
            advanced = locked.conn.execute(
                f"""
                UPDATE wiki_pages
                SET current_revision_id=?,file_hash=?,semantic_hash=?,last_write_token=?,
                    projection_epoch=?,rag_visible_revision_id=NULL,rag_visible_epoch=NULL,
                    lifecycle_status='active',deleted_at=NULL,sync_error=NULL,observed_file_hash=?,
                    pending_write_intent_id=NULL,review_status=?,owner=COALESCE(?,owner),updated_at=?
                WHERE page_id=? AND pending_write_intent_id=? AND {expected_sql}
                """,
                (
                    revision["id"],
                    revision["file_hash"],
                    revision["semantic_hash"],
                    intent["write_token"],
                    next_epoch,
                    observed_file_hash,
                    content_review_status,
                    content_owner,
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
                SET status='applied',backup_last_observed_hash=captured_file_hash,
                    backup_retention_status=CASE
                      WHEN captured_file_hash IS NULL THEN 'none'
                      ELSE 'retained'
                    END,
                    executor_owner=NULL,lease_expires_at=NULL,updated_at=?
                WHERE id=? AND status='installed' AND executor_owner=?
                """,
                (now_iso(), intent_id, executor_owner),
            )
            if applied.rowcount != 1:
                raise WikiRevisionError("intent owner/status CAS failed during finalize")
            apply_external_event = revision["origin"] == "external" and not (
                resolution_payload is not None
                and resolution_payload.get("resolution") == "keep_current"
                and isinstance(
                    resolution_payload.get("recovery_intent_id"),
                    str,
                )
            )
            if apply_external_event:
                external_event_id = metadata.get("vault_change_event_id")
                if not isinstance(external_event_id, str) or not external_event_id:
                    raise WikiRevisionError(
                        "external revision has no vault change event identity"
                    )
                event_applied = locked.conn.execute(
                    """
                    UPDATE vault_change_events
                    SET status='applied',result_revision_id=?,updated_at=?
                    WHERE id=? AND observation_id=?
                      AND status IN ('pending','prepared')
                    """,
                    (
                        revision["id"],
                        now_iso(),
                        external_event_id,
                        metadata.get("observation_id"),
                    ),
                )
                if event_applied.rowcount != 1:
                    raise WikiRevisionError(
                        "external event status CAS failed during finalize"
                    )
                timestamp = now_iso()
                locked.conn.execute(
                    """
                    UPDATE review_items
                    SET status='superseded',resolved_at=?,updated_at=?
                    WHERE page_id=? AND issue_type='invalid_frontmatter'
                      AND status='pending'
                    """,
                    (timestamp, timestamp, intent["page_id"]),
                )
            locked.page.update(
                {
                    "current_revision_id": revision["id"],
                    "file_hash": revision["file_hash"],
                    "semantic_hash": revision["semantic_hash"],
                    "projection_epoch": next_epoch,
                    "pending_write_intent_id": None,
                    "lifecycle_status": "active",
                    "sync_error": None,
                    "observed_file_hash": observed_file_hash,
                    "review_status": content_review_status,
                }
            )
            if content_owner is not None:
                locked.page["owner"] = content_owner
            displaced_candidate = None
            displaced_expected_state_extra = None
            displaced_lineage = self._recovery_displaced_lineage_locked(
                locked.conn,
                locked.page,
            )
            if (
                displaced_lineage is not None
                and displaced_lineage[0]["origin"]
                in {"manual", "external", "merge"}
            ):
                displaced_candidate = RevisionRecord.from_row(
                    displaced_lineage[0]
                )
                displaced_expected_state_extra = {
                    "recovery_displaced_intent_id": displaced_lineage[1]
                }
            if resolution_payload is not None and resolution_review is not None:
                if revision["id"] != resolution_review["candidate_revision_id"]:
                    self._supersede_candidate_event_locked(
                        locked.conn,
                        resolution_review["candidate_revision_id"],
                    )
                self._record_resolved_conflict_locked(
                    locked.conn,
                    locked.page,
                    resolution_review,
                    resolution_payload,
                    resolution_revision_id=revision["id"],
                    write_intent_id=intent_id,
                    projection_job_ids=tuple(jobs),
                )
                if displaced_candidate is not None:
                    self._seed_concurrent_review_locked(
                        locked.conn,
                        locked.page,
                        candidate=displaced_candidate,
                        event_id=None,
                        expected_state_extra=displaced_expected_state_extra,
                    )
            else:
                if displaced_candidate is not None:
                    self._seed_concurrent_review_locked(
                        locked.conn,
                        locked.page,
                        candidate=displaced_candidate,
                        event_id=None,
                        expected_state_extra=displaced_expected_state_extra,
                    )
                self._reconcile_pending_reviews_locked(
                    locked.conn,
                    intent["page_id"],
                )
            audit(
                locked.conn,
                "wiki_revision_applied",
                {
                    "page_id": intent["page_id"],
                    "page_path": locked.page["path"],
                    "intent_id": intent_id,
                    "revision_id": revision["id"],
                    "origin": revision["origin"],
                    "base_revision_id": revision["base_revision_id"],
                    **self._applied_transition_identity(
                        origin=revision["origin"],
                        metadata=metadata,
                        resolution_payload=resolution_payload,
                        prepared_transition_payload=prepared_transition_payload,
                    ),
                },
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
        *,
        allow_conflicts: bool = True,
    ) -> str | None:
        page_row = conn.execute(
            "SELECT * FROM wiki_pages WHERE page_id=?",
            (page_id,),
        ).fetchone()
        if page_row is None:
            raise PageNotFound(page_id)
        page = dict(page_row)
        pending = list(
            conn.execute(
                """
                SELECT * FROM review_items
                WHERE page_id=? AND status='pending'
                  AND issue_type IN ('content_conflict','concurrent_write_conflict')
                ORDER BY created_at,id
                """,
                (page_id,),
            ).fetchall()
        )

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

        has_content_conflict = (
            allow_conflicts
            and page["lifecycle_status"] == "active"
            and page["current_revision_id"] is not None
            and generated is not None
            and page["current_revision_id"] != page["generated_revision_id"]
            and (
                accepted is None
                or generated["semantic_hash"] != accepted["semantic_hash"]
            )
        )
        desired: dict[str, dict[str, Any]] = {}
        if has_content_conflict:
            desired["content_conflict"] = {
                "base_revision_id": page["current_revision_id"],
                "candidate_revision_id": page["generated_revision_id"],
                "owner": page["owner"],
                "source_ids_json": generated["source_ids_json"],
            }

        for review in reversed(pending):
            if not allow_conflicts or page["lifecycle_status"] != "active":
                break
            if review["issue_type"] != "concurrent_write_conflict":
                continue
            candidate = conn.execute(
                "SELECT * FROM wiki_page_revisions WHERE id=?",
                (review["candidate_revision_id"],),
            ).fetchone()
            expected_state_extra = None
            has_displaced_marker = False
            if candidate is not None:
                try:
                    review_expected_state = json.loads(
                        review["expected_state_json"] or "{}"
                    )
                except (TypeError, json.JSONDecodeError):
                    review_expected_state = {}
                if not isinstance(review_expected_state, dict):
                    review_expected_state = {}
                displaced_intent_key = "recovery_displaced_intent_id"
                has_displaced_marker = (
                    displaced_intent_key in review_expected_state
                )
                displaced_intent_id = review_expected_state.get(
                    displaced_intent_key
                )
                displaced_lineage = None
                if isinstance(displaced_intent_id, str) and displaced_intent_id:
                    displaced_lineage = (
                        self._recovery_displaced_lineage_locked(
                            conn,
                            page,
                            expected_intent_id=displaced_intent_id,
                        )
                    )
                if (
                    displaced_lineage is not None
                    and displaced_lineage[0]["id"] == candidate["id"]
                    and candidate["origin"]
                    in {"manual", "external", "merge"}
                ):
                    expected_state_extra = {
                        "recovery_displaced_intent_id": displaced_intent_id
                    }
            candidate_origin_allowed = (
                expected_state_extra is not None
                if has_displaced_marker
                else candidate is not None
                and candidate["origin"] in {"manual", "external"}
            )
            if (
                candidate is not None
                and candidate["page_id"] == page_id
                and candidate_origin_allowed
                and review["base_revision_id"] == page["current_revision_id"]
                and review["candidate_revision_id"]
                != page["current_revision_id"]
            ):
                candidate_event_id = self._candidate_event_id(candidate)
                if candidate_event_id is not None:
                    candidate_event = conn.execute(
                        "SELECT * FROM vault_change_events WHERE id=?",
                        (candidate_event_id,),
                    ).fetchone()
                    if (
                        candidate_event is None
                        or candidate_event["status"]
                        not in {"pending", "prepared"}
                        or candidate_event["result_revision_id"]
                        != candidate["id"]
                    ):
                        candidate_event_id = None
                desired["concurrent_write_conflict"] = {
                    "base_revision_id": page["current_revision_id"],
                    "candidate_revision_id": review["candidate_revision_id"],
                    "owner": review["owner"] or page["owner"],
                    "source_ids_json": candidate["source_ids_json"],
                    "expected_state_extra": expected_state_extra,
                    "event_id": candidate_event_id,
                }
                break

        matching: dict[str, Any] = {}
        for issue_type, spec in desired.items():
            candidates = []
            for review in pending:
                try:
                    review_expected_state = json.loads(
                        review["expected_state_json"] or "{}"
                    )
                except (TypeError, json.JSONDecodeError):
                    continue
                if (
                    not isinstance(review_expected_state, dict)
                    or review_expected_state.get("path") != page["path"]
                    or review["issue_type"] != issue_type
                    or review["page_path"] != page["path"]
                    or review["base_revision_id"]
                    != spec["base_revision_id"]
                    or review["candidate_revision_id"]
                    != spec["candidate_revision_id"]
                ):
                    continue
                candidates.append(review)
            if len(candidates) == 1:
                matching[issue_type] = candidates[0]

        planned_ids = {
            issue_type: (
                str(matching[issue_type]["id"])
                if issue_type in matching
                else f"review_{uuid.uuid4().hex}"
            )
            for issue_type in desired
        }
        expected_state = self._canonical_state_locked(conn, page)
        expected_state["pending_conflict"] = sorted(planned_ids.values())
        for spec in desired.values():
            if spec.get("expected_state_extra"):
                expected_state.update(spec["expected_state_extra"])
        expected_state_json = canonical_state_json(expected_state)
        stable = (
            len(pending) == len(desired)
            and len(matching) == len(desired)
            and all(
                review["expected_state_json"] == expected_state_json
                for review in matching.values()
            )
        )
        if stable:
            content = matching.get("content_conflict")
            return str(content["id"]) if content is not None else None

        timestamp = now_iso()
        matching_ids = {
            str(review["id"])
            for review in matching.values()
        }
        desired_concurrent = desired.get("concurrent_write_conflict")
        for review in pending:
            if str(review["id"]) in matching_ids:
                continue
            superseded = conn.execute(
                """
                UPDATE review_items
                SET status='superseded',resolved_at=?,updated_at=?
                WHERE id=? AND status='pending'
                """,
                (timestamp, timestamp, review["id"]),
            )
            if superseded.rowcount != 1:
                raise RevisionConflict(
                    "pending conflict changed during reconciliation",
                    current_revision_id=page.get("current_revision_id"),
                    pending_intent_id=page.get("pending_write_intent_id"),
                )
            if (
                review["issue_type"] == "concurrent_write_conflict"
                and (
                    desired_concurrent is None
                    or review["candidate_revision_id"]
                    != desired_concurrent["candidate_revision_id"]
                )
            ):
                self._supersede_candidate_event_locked(
                    conn,
                    review["candidate_revision_id"],
                )

        if not desired:
            return None
        final_ids = planned_ids
        expected_state = self._canonical_state_locked(conn, page)
        expected_state["pending_conflict"] = sorted(final_ids.values())
        for spec in desired.values():
            if spec.get("expected_state_extra"):
                expected_state.update(spec["expected_state_extra"])
        expected_state_json = canonical_state_json(expected_state)
        for issue_type in ("content_conflict", "concurrent_write_conflict"):
            spec = desired.get(issue_type)
            if spec is None:
                continue
            if issue_type in matching:
                updated = conn.execute(
                    """
                    UPDATE review_items
                    SET expected_state_json=?,updated_at=?
                    WHERE id=? AND status='pending'
                      AND base_revision_id=? AND candidate_revision_id=?
                    """,
                    (
                        expected_state_json,
                        timestamp,
                        final_ids[issue_type],
                        spec["base_revision_id"],
                        spec["candidate_revision_id"],
                    ),
                )
                if updated.rowcount != 1:
                    raise RevisionConflict(
                        "matching conflict changed during reconciliation",
                        current_revision_id=page.get("current_revision_id"),
                        pending_intent_id=page.get(
                            "pending_write_intent_id"
                        ),
                    )
            else:
                conn.execute(
                    """
                    INSERT INTO review_items(
                      id,page_path,page_id,issue_type,status,owner,source_ids_json,
                      created_at,updated_at,base_revision_id,candidate_revision_id,
                      expected_state_json
                    ) VALUES (?,?,?,?,'pending',?,?,?,?,?,?,?)
                    """,
                    (
                        final_ids[issue_type],
                        page["path"],
                        page_id,
                        issue_type,
                        spec["owner"],
                        spec["source_ids_json"],
                        timestamp,
                        timestamp,
                        spec["base_revision_id"],
                        spec["candidate_revision_id"],
                        expected_state_json,
                    ),
                )
        if (
            desired_concurrent is not None
            and desired_concurrent.get("event_id") is not None
        ):
            event_updated = conn.execute(
                """
                UPDATE vault_change_events
                SET expected_state_json=?,status='prepared',updated_at=?
                WHERE id=? AND result_revision_id=?
                  AND status IN ('pending','prepared')
                """,
                (
                    expected_state_json,
                    timestamp,
                    desired_concurrent["event_id"],
                    desired_concurrent["candidate_revision_id"],
                ),
            )
            if event_updated.rowcount != 1:
                raise WikiRevisionError(
                    "desired candidate event changed during reconciliation"
                )
        return final_ids.get("content_conflict")
