from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
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
    value = metadata.get("source_ids") or []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MarkdownParseError("invalid_source_ids", "source_ids must be a string list")
    return list(value)


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

    def prepare_manual_save(
        self,
        command: ManualSaveCommand,
        *,
        execute_intent: bool = True,
    ) -> MutationResult:
        revision_id = f"wrev_{uuid.uuid4().hex}"
        write_token = f"write_{uuid.uuid4().hex}"
        source_document = parse_wiki_bytes(command.content.encode("utf-8"))
        with self.coordinator.lock_page(command.page_path) as locked:
            if locked.page.get("current_revision_id") != command.expected_revision_id:
                raise RevisionConflict(
                    "manual save precondition failed",
                    current_revision_id=locked.page.get("current_revision_id"),
                    pending_intent_id=locked.page.get("pending_write_intent_id"),
                )
            rendered = render_managed_frontmatter(
                source_document,
                page_id=locked.page["page_id"],
                revision_id=revision_id,
                write_token=write_token,
                review_status=command.review_status,
            )
            final_document = parse_wiki_bytes(rendered)
            metadata = _plain(final_document.frontmatter)
            if command.owner is not None:
                metadata["owner"] = command.owner
            revision = self._create_revision_locked(
                locked.conn,
                locked.page,
                content=rendered,
                origin="manual",
                base_revision_id=command.expected_revision_id,
                source_ids=_source_ids(metadata),
                actor=command.actor,
                note=command.note,
                idempotency_key=f"manual:{command.request_id}",
                metadata=metadata,
                revision_id=revision_id,
            )
            intent_id = self.prepare_write_intent_locked(
                locked.conn,
                locked.page,
                revision=revision,
                expected_revision_id=command.expected_revision_id,
                expected_file_hash=locked.page.get("file_hash"),
                write_token=write_token,
            )
            prepared = MutationResult(
                page_id=locked.page["page_id"],
                page_path=command.page_path,
                status="prepared",
                revision_id=revision.id,
                current_revision_id=command.expected_revision_id,
                generated_revision_id=locked.page.get("generated_revision_id"),
                write_intent_id=intent_id,
            )
        if not execute_intent:
            return prepared

        from app.vault_writer import IntentExecutor

        result = IntentExecutor(self.settings).execute(intent_id)
        if result is None or result.intent_status != "applied":
            raise RevisionConflict(
                "manual save requires recovery",
                current_revision_id=command.expected_revision_id,
                pending_intent_id=intent_id,
            )
        return self._mutation_for_intent(intent_id)

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
                "SELECT id FROM knowledge_projection_jobs WHERE page_id=? AND projection_epoch=? ORDER BY target",
                (page["page_id"], page["projection_epoch"]),
            ).fetchall()
        return MutationResult(
            page_id=page["page_id"],
            page_path=page["path"],
            status="applied",
            revision_id=intent["revision_id"],
            current_revision_id=page["current_revision_id"],
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

    def _reconcile_pending_reviews_locked(self, conn: Any, page_id: str) -> None:
        return None
