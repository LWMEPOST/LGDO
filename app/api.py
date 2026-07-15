from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import NoReturn

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi import Depends, Request
from fastapi.responses import StreamingResponse

from app import catalog
from app.aliases import delete_entity_alias, list_entity_aliases, seed_default_entity_aliases, upsert_entity_alias
from app.accounts import authenticate_account, create_account, list_accounts, revoke_session, update_account
from app.auth import UserContext, resolve_user_context
from app.catalog import (
    delete_source,
    get_wiki_revision,
    list_knowledge_gaps,
    list_ingest_reports,
    list_review_items,
    list_sources,
    list_wiki_page_conflicts,
    list_wiki_page_revisions,
    list_wiki_pages,
    rag_status,
    read_source_preview,
    release_wiki_backup,
    resolve_wiki_conflict,
    save_wiki_page,
    update_knowledge_gap,
    update_review_item,
    update_wiki_page_status,
)
from app.config import get_settings
from app.eval import add_eval_question, compare_upgraded_eval, run_eval
from app.feedback import submit_feedback
from app.ingest import scan_sources
from app.migration import migrate_sqlite_to_postgres
from app.models import (
    AccountCreateRequest,
    AccountResponse,
    AccountUpdateRequest,
    AskRequest,
    AskResponse,
    AuthSessionResponse,
    BackupReleaseRequest,
    BackupReleaseResponse,
    CompileRequest,
    CompileResponse,
    ConflictResolveRequest,
    EvalQuestionRequest,
    EvalRunResponse,
    EntityAliasRequest,
    FeedbackRequest,
    FeedbackResponse,
    GapUpdateRequest,
    LoginRequest,
    ObsidianLinkResponse,
    ReviewUpdateRequest,
    ScanRequest,
    ScanResponse,
    SourcePreviewResponse,
    UpgradedEvalRunRequest,
    UploadResponse,
    VaultReconcileJobResponse,
    VaultStatusResponse,
    WikiConflictResponse,
    WikiMutationResponse,
    WikiPageContentResponse,
    WikiPageSaveRequest,
    WikiRevisionDetailResponse,
    WikiRevisionListResponse,
    WikiStatusUpdateRequest,
)
from app.projection_worker import (
    ProjectionJobNotFound,
    ProjectionJobStateConflict,
    list_projection_jobs,
    projection_health,
    retry_projection_job,
)
from app.obsidian import build_obsidian_uri
from app.search import ask, stream_ask_events
from app.vault import slugify
from app.vault_events import VaultEventStore
from app.vault_sync import VaultSyncStopping
from app.wiki import compile_wiki
from app.wiki_revisions import (
    InvalidWikiDocument,
    PageNotFound,
    PreconditionRequired,
    RevisionConflict,
)


router = APIRouter()


def current_user(request: Request) -> UserContext:
    return resolve_user_context(get_settings(), request)


def require_account_admin(user: UserContext) -> None:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要 admin 权限管理账户 ACL")


def require_editor(user: UserContext) -> None:
    if not user.is_admin and user.role != "editor":
        raise HTTPException(status_code=403, detail="需要 admin/editor 权限执行内部管理操作")


def _vault_reconcile_payload(job) -> dict:
    return {
        "job_id": job.id,
        "status": job.status,
        "result": job.result,
        "error_summary": job.error_summary,
    }


def _safe_asset_names(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    safe: list[str] = []
    for item in value:
        candidate = Path(str(item))
        if candidate.is_absolute() or ".." in candidate.parts:
            continue
        normalized = candidate.as_posix()
        if normalized and normalized != ".":
            safe.append(normalized)
    return sorted(dict.fromkeys(safe))


def _safe_vault_error(value: object, vault_path: Path) -> str | None:
    if value is None:
        return None
    summary = str(value)[:500]
    resolved = str(vault_path.resolve())
    candidates = {resolved, resolved.replace("\\", "/")}
    if vault_path.is_absolute():
        configured = str(vault_path)
        candidates.update({configured, configured.replace("\\", "/")})
    for candidate in sorted(candidates, key=len, reverse=True):
        path_pattern = re.compile(
            re.escape(candidate) + r"(?:(?:[\\/])[^;,\r\n]*)?",
            flags=re.IGNORECASE,
        )
        summary = path_pattern.sub("<vault>", summary)
    return summary


@router.post(
    "/vault/reconcile",
    response_model=VaultReconcileJobResponse,
    status_code=202,
)
async def request_vault_reconcile_endpoint(
    request: Request,
    user: UserContext = Depends(current_user),
) -> dict:
    require_account_admin(user)
    runtime = getattr(request.app.state, "vault_sync", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="vault sync runtime unavailable")
    try:
        job = runtime.request_reconcile(user.user_id)
    except VaultSyncStopping as exc:
        raise HTTPException(
            status_code=503,
            detail="vault sync runtime stopping",
        ) from exc
    return _vault_reconcile_payload(job)


@router.get(
    "/vault/reconcile/{job_id}",
    response_model=VaultReconcileJobResponse,
)
def get_vault_reconcile_endpoint(
    job_id: str,
    request: Request,
    user: UserContext = Depends(current_user),
) -> dict:
    require_account_admin(user)
    runtime = getattr(request.app.state, "vault_sync", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="vault sync runtime unavailable")
    job = runtime.events.get_reconcile(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="vault reconcile job not found")
    return _vault_reconcile_payload(job)


@router.get("/vault/status", response_model=VaultStatusResponse)
def vault_status_endpoint(
    request: Request,
    _user: UserContext = Depends(current_user),
) -> dict:
    settings = get_settings()
    runtime = getattr(request.app.state, "vault_sync", None)
    event_store = runtime.events if runtime is not None else VaultEventStore(settings)
    snapshot = {}
    if runtime is not None:
        try:
            snapshot = runtime.snapshot()
        except Exception:
            snapshot = {}
    else:
        try:
            snapshot = event_store.status_snapshot()
        except Exception:
            snapshot = {}

    counters = {
        key: int(snapshot.get(key) or 0)
        for key in (
            "pending_occurrences",
            "failed_occurrences",
            "pending_deletes",
            "open_issues",
            "invalid_pages",
        )
    }
    projections = projection_health(settings)
    enabled_targets = ["rag"]
    if settings.gbrain_enabled:
        enabled_targets.append("gbrain")
    projection_backlog = sum(
        int(projections[target][status])
        for target in enabled_targets
        for status in ("pending", "running")
    )
    projection_failed = sum(
        int(projections[target]["failed"])
        for target in enabled_targets
    )

    raw_obsidian = getattr(request.app.state, "obsidian_status", {})
    if not isinstance(raw_obsidian, dict):
        raw_obsidian = {}
    obsidian = {
        "installed": _safe_asset_names(raw_obsidian.get("installed")),
        "drifted": _safe_asset_names(raw_obsidian.get("drifted")),
        "vault_name": settings.effective_obsidian_vault_name,
    }

    reconcile = None
    active_reconcile = False
    reconcile_failed = False
    try:
        active = event_store.active_reconcile()
        job = active or event_store.latest_reconcile()
        if job is not None:
            reconcile = _vault_reconcile_payload(job)
            reconcile["error_summary"] = _safe_vault_error(
                reconcile["error_summary"],
                settings.vault_path,
            )
            active_reconcile = job.status in {"queued", "running"}
            reconcile_failed = job.status == "failed"
    except Exception:
        reconcile = None

    degraded = bool(
        counters["failed_occurrences"]
        or counters["open_issues"]
        or counters["invalid_pages"]
        or projection_failed
        or obsidian["drifted"]
        or reconcile_failed
    )
    busy = bool(
        counters["pending_occurrences"]
        or counters["pending_deletes"]
        or projection_backlog
        or active_reconcile
    )
    return {
        "configured": bool(settings.vault_watch_enabled),
        "running": bool(
            runtime is not None
            and getattr(runtime, "watcher_running", False)
        ),
        "clean": not degraded and not busy,
        "degraded": degraded,
        "last_event_at": snapshot.get("last_event_at"),
        "last_error": _safe_vault_error(
            snapshot.get("last_error"),
            settings.vault_path,
        ),
        **counters,
        "projection_backlog": projection_backlog,
        "projection": projections,
        "obsidian": obsidian,
        "reconcile": reconcile,
    }


def raise_wiki_http(exc: Exception) -> NoReturn:
    if isinstance(exc, PreconditionRequired):
        raise HTTPException(
            status_code=428,
            detail={"code": "expected_revision_required"},
        ) from exc
    if isinstance(exc, RevisionConflict):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "revision_conflict",
                "message": str(exc),
                "current_revision_id": exc.current_revision_id,
                "pending_intent_id": exc.pending_intent_id,
            },
        ) from exc
    if isinstance(exc, PageNotFound):
        raise HTTPException(
            status_code=404,
            detail={"code": "wiki_page_not_found"},
        ) from exc
    if isinstance(exc, InvalidWikiDocument):
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.error_code,
                "observation_id": exc.observation_id,
            },
        ) from exc
    raise HTTPException(
        status_code=400,
        detail={"code": "wiki_mutation_failed", "message": str(exc)},
    ) from exc


def bearer_from_request(request: Request) -> str | None:
    authorization = request.headers.get("authorization") or ""
    if not authorization.lower().startswith("bearer "):
        return None
    return authorization.split(" ", 1)[1].strip()


@router.post("/auth/login", response_model=AuthSessionResponse)
def login_endpoint(request: LoginRequest) -> AuthSessionResponse:
    try:
        token, user = authenticate_account(get_settings(), request.username, request.password)
        return AuthSessionResponse(token=token, user=user)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/auth/me")
def me_endpoint(user: UserContext = Depends(current_user)) -> dict:
    return user.to_public_dict()


@router.post("/auth/logout")
def logout_endpoint(request: Request, user: UserContext = Depends(current_user)) -> dict:
    revoke_session(get_settings(), bearer_from_request(request) or "", actor=user.user_id)
    return {"ok": True}


@router.get("/accounts", response_model=list[AccountResponse])
def list_accounts_endpoint(user: UserContext = Depends(current_user)) -> list[dict]:
    require_account_admin(user)
    try:
        return list_accounts(get_settings())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/accounts", response_model=AccountResponse)
def create_account_endpoint(request: AccountCreateRequest, user: UserContext = Depends(current_user)) -> dict:
    require_account_admin(user)
    try:
        return create_account(get_settings(), request, actor=user.user_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/accounts/{user_id}", response_model=AccountResponse)
def update_account_endpoint(user_id: str, request: AccountUpdateRequest, user: UserContext = Depends(current_user)) -> dict:
    require_account_admin(user)
    try:
        return update_account(get_settings(), user_id, request, actor=user.user_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sources")
def list_sources_endpoint(
    domain: str | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    user: UserContext = Depends(current_user),
) -> list[dict]:
    try:
        return list_sources(get_settings(), domain, include_deleted, user)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/sources/{source_id}")
def delete_source_endpoint(
    source_id: str,
    note: str | None = Query(default=None),
    user: UserContext = Depends(current_user),
) -> dict:
    require_editor(user)
    try:
        return delete_source(get_settings(), source_id, note)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/sources/{source_id}/preview", response_model=SourcePreviewResponse)
def source_preview_endpoint(
    source_id: str,
    max_chars: int = Query(default=8000, ge=200, le=50000),
    user: UserContext = Depends(current_user),
) -> SourcePreviewResponse:
    try:
        return SourcePreviewResponse(**read_source_preview(get_settings(), source_id, max_chars, user))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/ingest/reports")
def list_ingest_reports_endpoint(
    source_id: str | None = Query(default=None),
    user: UserContext = Depends(current_user),
) -> list[dict]:
    try:
        return list_ingest_reports(get_settings(), source_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/rag/status")
def rag_status_endpoint(user: UserContext = Depends(current_user)) -> dict:
    try:
        return rag_status(get_settings())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/projection-jobs")
def list_projection_jobs_endpoint(
    target: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1),
    user: UserContext = Depends(current_user),
) -> list[dict]:
    return list_projection_jobs(
        get_settings(),
        target=target,
        status=status,
        page_id=page_id,
        limit=limit,
    )


@router.post("/projection-jobs/{job_id}/retry")
def retry_projection_job_endpoint(
    job_id: str,
    user: UserContext = Depends(current_user),
) -> dict:
    require_editor(user)
    try:
        return retry_projection_job(get_settings(), job_id)
    except ProjectionJobNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProjectionJobStateConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "projection_job_state_conflict", "status": exc.status},
        ) from exc


@router.post("/rag/sync-postgres")
def sync_rag_to_postgres_endpoint(user: UserContext = Depends(current_user)) -> dict:
    require_editor(user)
    try:
        from app.pg_rag import sync_sqlite_chunks_to_pg

        synced = sync_sqlite_chunks_to_pg(get_settings())
        return {"synced_chunks": synced}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/database/migrate-sqlite-to-postgres")
def migrate_sqlite_to_postgres_endpoint(
    sqlite_path: str | None = Query(default=None),
    user: UserContext = Depends(current_user),
) -> dict:
    require_account_admin(user)
    try:
        return migrate_sqlite_to_postgres(get_settings(), Path(sqlite_path) if sqlite_path else None)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/wiki/revisions/{revision_id}",
    response_model=WikiRevisionDetailResponse,
)
def get_wiki_revision_endpoint(
    revision_id: str,
    user: UserContext = Depends(current_user),
) -> dict:
    try:
        return get_wiki_revision(
            get_settings(),
            revision_id,
            user_context=user,
        )
    except Exception as exc:
        raise_wiki_http(exc)


@router.post(
    "/wiki/conflicts/{review_id}/resolve",
    response_model=WikiMutationResponse,
)
def resolve_wiki_conflict_endpoint(
    review_id: str,
    request: ConflictResolveRequest,
    user: UserContext = Depends(current_user),
) -> dict:
    require_editor(user)
    try:
        return resolve_wiki_conflict(
            get_settings(),
            review_id,
            request,
            actor=user.user_id,
            user_context=user,
        )
    except Exception as exc:
        raise_wiki_http(exc)


@router.post(
    "/wiki/write-intents/{intent_id}/release-backup",
    response_model=BackupReleaseResponse,
)
def release_wiki_backup_endpoint(
    intent_id: str,
    request: BackupReleaseRequest,
    user: UserContext = Depends(current_user),
) -> dict:
    require_account_admin(user)
    try:
        return release_wiki_backup(
            get_settings(),
            intent_id,
            request,
            actor=user.user_id,
        )
    except Exception as exc:
        raise_wiki_http(exc)


@router.get("/wiki/pages")
def list_wiki_pages_endpoint(
    domain: str | None = Query(default=None),
    user: UserContext = Depends(current_user),
) -> list[dict]:
    try:
        return list_wiki_pages(
            get_settings(),
            domain,
            user_context=user,
        )
    except Exception as exc:
        raise_wiki_http(exc)


@router.get(
    "/wiki/pages/{page_path:path}/revisions",
    response_model=WikiRevisionListResponse,
)
def list_wiki_page_revisions_endpoint(
    page_path: str,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: UserContext = Depends(current_user),
) -> dict:
    try:
        return list_wiki_page_revisions(
            get_settings(),
            page_path,
            limit=limit,
            offset=offset,
            user_context=user,
        )
    except Exception as exc:
        raise_wiki_http(exc)


@router.get(
    "/wiki/pages/{page_path:path}/conflicts",
    response_model=list[WikiConflictResponse],
)
def list_wiki_page_conflicts_endpoint(
    page_path: str,
    user: UserContext = Depends(current_user),
) -> list[dict]:
    try:
        return list_wiki_page_conflicts(
            get_settings(),
            page_path,
            user_context=user,
        )
    except Exception as exc:
        raise_wiki_http(exc)


@router.patch(
    "/wiki/pages/{page_path:path}/status",
    response_model=WikiMutationResponse,
)
def update_wiki_status_endpoint(
    page_path: str,
    request: WikiStatusUpdateRequest,
    user: UserContext = Depends(current_user),
) -> dict:
    require_editor(user)
    try:
        return update_wiki_page_status(
            get_settings(),
            page_path,
            request,
            actor=user.user_id,
            user_context=user,
        )
    except Exception as exc:
        raise_wiki_http(exc)


@router.get(
    "/wiki/pages/{page_path:path}/obsidian-link",
    response_model=ObsidianLinkResponse,
)
def wiki_page_obsidian_link_endpoint(
    page_path: str,
    user: UserContext = Depends(current_user),
) -> dict:
    settings = get_settings()
    try:
        page = catalog.read_wiki_page(
            settings,
            page_path,
            user_context=user,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "wiki_page_not_found"},
        ) from exc
    except Exception as exc:
        raise_wiki_http(exc)
    return {
        "url": build_obsidian_uri(
            settings.effective_obsidian_vault_name,
            page["path"],
        )
    }


@router.get("/wiki/pages/{page_path:path}", response_model=WikiPageContentResponse)
def read_wiki_page_endpoint(
    page_path: str,
    user: UserContext = Depends(current_user),
) -> WikiPageContentResponse:
    try:
        return WikiPageContentResponse(
            **catalog.read_wiki_page(
                get_settings(),
                page_path,
                user_context=user,
            )
        )
    except Exception as exc:
        raise_wiki_http(exc)


@router.put(
    "/wiki/pages/{page_path:path}",
    response_model=WikiMutationResponse,
)
def save_wiki_page_endpoint(
    page_path: str,
    request: WikiPageSaveRequest,
    user: UserContext = Depends(current_user),
) -> dict:
    require_editor(user)
    try:
        return save_wiki_page(
            get_settings(),
            page_path,
            request,
            actor=user.user_id,
            user_context=user,
        )
    except Exception as exc:
        raise_wiki_http(exc)


@router.get("/gaps")
def list_gaps_endpoint(
    status: str | None = Query(default=None),
    user: UserContext = Depends(current_user),
) -> list[dict]:
    try:
        return list_knowledge_gaps(get_settings(), status)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/gaps/{gap_id}")
def update_gap_endpoint(
    gap_id: str,
    request: GapUpdateRequest,
    user: UserContext = Depends(current_user),
) -> dict:
    require_editor(user)
    try:
        return update_knowledge_gap(get_settings(), gap_id, request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/reviews")
def list_reviews_endpoint(
    status: str | None = Query(default=None),
    user: UserContext = Depends(current_user),
) -> list[dict]:
    try:
        return list_review_items(get_settings(), status)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/reviews/{review_id}")
def update_review_endpoint(
    review_id: str,
    request: ReviewUpdateRequest,
    user: UserContext = Depends(current_user),
) -> dict:
    require_editor(user)
    try:
        return update_review_item(get_settings(), review_id, request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sources/scan", response_model=ScanResponse)
def scan_endpoint(request: ScanRequest, user: UserContext = Depends(current_user)) -> ScanResponse:
    require_editor(user)
    try:
        return scan_sources(get_settings(), request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sources/upload", response_model=UploadResponse)
def upload_endpoint(
    files: list[UploadFile] = File(...),
    domain: str = Form("product"),
    owner: str | None = Form(None),
    acl_tags: str = Form("internal"),
    metadata_defaults: str = Form("{}"),
    user: UserContext = Depends(current_user),
) -> UploadResponse:
    require_editor(user)
    try:
        settings = get_settings()
        import json

        upload_dir = settings.upload_path / domain
        upload_dir.mkdir(parents=True, exist_ok=True)
        saved_files: list[str] = []
        for upload in files:
            suffix = Path(upload.filename or "upload").suffix
            stem = slugify(Path(upload.filename or "upload").stem, "upload")
            target = upload_dir / f"{stem}{suffix.lower()}"
            counter = 1
            while target.exists():
                target = upload_dir / f"{stem}-{counter}{suffix.lower()}"
                counter += 1
            with target.open("wb") as out:
                shutil.copyfileobj(upload.file, out)
            saved_files.append(str(target))

        scan = scan_sources(
            settings,
            ScanRequest(
                root_path=str(upload_dir),
                domain=domain,
                owner=owner,
                acl_tags=[tag.strip() for tag in acl_tags.split(",") if tag.strip()],
                metadata_defaults=json.loads(metadata_defaults or "{}"),
                force_reindex=True,
            ),
        )
        return UploadResponse(**scan.model_dump(), saved_files=saved_files)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/wiki/compile", response_model=CompileResponse)
def compile_endpoint(request: CompileRequest, user: UserContext = Depends(current_user)) -> CompileResponse:
    require_editor(user)
    try:
        return compile_wiki(get_settings(), request)
    except Exception as exc:
        raise_wiki_http(exc)


@router.post("/ask", response_model=AskResponse)
def ask_endpoint(request: AskRequest, user: UserContext = Depends(current_user)) -> AskResponse:
    try:
        return ask(get_settings(), request, user)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/ask/stream")
def ask_stream_endpoint(request: AskRequest, user: UserContext = Depends(current_user)) -> StreamingResponse:
    try:
        return StreamingResponse(
            stream_ask_events(get_settings(), request, user),
            media_type="application/x-ndjson",
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/feedback", response_model=FeedbackResponse)
def feedback_endpoint(request: FeedbackRequest, user: UserContext = Depends(current_user)) -> FeedbackResponse:
    try:
        return submit_feedback(get_settings(), request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/eval/questions")
def eval_question_endpoint(request: EvalQuestionRequest, user: UserContext = Depends(current_user)) -> dict[str, str]:
    require_editor(user)
    try:
        return add_eval_question(get_settings(), request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/eval/run", response_model=EvalRunResponse)
def eval_run_endpoint(
    domain: str | None = Query(default=None),
    user: UserContext = Depends(current_user),
) -> EvalRunResponse:
    require_editor(user)
    try:
        return run_eval(get_settings(), domain)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/eval/upgraded")
def upgraded_eval_endpoint(request: UpgradedEvalRunRequest, user: UserContext = Depends(current_user)) -> dict:
    require_editor(user)
    try:
        return compare_upgraded_eval(
            get_settings(),
            domain=request.domain,
            mode=request.gbrain_mode,
            pass_rate_threshold=request.pass_rate_threshold,
            citation_rate_threshold=request.citation_rate_threshold,
            p95_ms_threshold=request.p95_ms_threshold,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/aliases")
def list_aliases_endpoint(
    domain: str | None = Query(default=None),
    user: UserContext = Depends(current_user),
) -> list[dict]:
    try:
        return list_entity_aliases(get_settings(), domain)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/aliases")
def upsert_alias_endpoint(request: EntityAliasRequest, user: UserContext = Depends(current_user)) -> dict:
    try:
        if not user.is_admin and user.role not in {"editor"}:
            raise HTTPException(status_code=403, detail="需要 admin/editor 权限维护实体别名")
        return upsert_entity_alias(get_settings(), request, user.user_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/aliases/{alias_id}")
def delete_alias_endpoint(alias_id: str, user: UserContext = Depends(current_user)) -> dict:
    try:
        if not user.is_admin and user.role not in {"editor"}:
            raise HTTPException(status_code=403, detail="需要 admin/editor 权限维护实体别名")
        return delete_entity_alias(get_settings(), alias_id, user.user_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/aliases/seed")
def seed_aliases_endpoint(user: UserContext = Depends(current_user)) -> dict:
    try:
        if not user.is_admin and user.role not in {"editor"}:
            raise HTTPException(status_code=403, detail="需要 admin/editor 权限维护实体别名")
        seeded = seed_default_entity_aliases(get_settings(), actor=user.user_id)
        return {"seeded": seeded}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
