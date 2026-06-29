from __future__ import annotations

from pathlib import Path
import shutil

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi import Depends, Request
from fastapi.responses import StreamingResponse

from app.aliases import delete_entity_alias, list_entity_aliases, upsert_entity_alias
from app.auth import UserContext, resolve_user_context
from app.catalog import (
    delete_source,
    list_knowledge_gaps,
    list_ingest_reports,
    list_review_items,
    list_sources,
    list_wiki_pages,
    rag_status,
    read_source_preview,
    read_wiki_page,
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
    AskRequest,
    AskResponse,
    CompileRequest,
    CompileResponse,
    EvalQuestionRequest,
    EvalRunResponse,
    EntityAliasRequest,
    FeedbackRequest,
    FeedbackResponse,
    GapUpdateRequest,
    ReviewUpdateRequest,
    ScanRequest,
    ScanResponse,
    SourcePreviewResponse,
    UpgradedEvalRunRequest,
    UploadResponse,
    WikiPageContentResponse,
    WikiPageSaveRequest,
    WikiStatusUpdateRequest,
)
from app.search import ask, stream_ask_events
from app.vault import slugify
from app.wiki import compile_wiki


router = APIRouter()


def current_user(request: Request) -> UserContext:
    return resolve_user_context(get_settings(), request)


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
def delete_source_endpoint(source_id: str, note: str | None = Query(default=None)) -> dict:
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
def list_ingest_reports_endpoint(source_id: str | None = Query(default=None)) -> list[dict]:
    try:
        return list_ingest_reports(get_settings(), source_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/rag/status")
def rag_status_endpoint() -> dict:
    try:
        return rag_status(get_settings())
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/rag/sync-postgres")
def sync_rag_to_postgres_endpoint() -> dict:
    try:
        from app.pg_rag import sync_sqlite_chunks_to_pg

        synced = sync_sqlite_chunks_to_pg(get_settings())
        return {"synced_chunks": synced}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/database/migrate-sqlite-to-postgres")
def migrate_sqlite_to_postgres_endpoint(sqlite_path: str | None = Query(default=None)) -> dict:
    try:
        return migrate_sqlite_to_postgres(get_settings(), Path(sqlite_path) if sqlite_path else None)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/wiki/pages")
def list_wiki_pages_endpoint(domain: str | None = Query(default=None)) -> list[dict]:
    try:
        return list_wiki_pages(get_settings(), domain)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/wiki/pages/{page_path:path}/status")
def update_wiki_status_endpoint(page_path: str, request: WikiStatusUpdateRequest) -> dict:
    try:
        return update_wiki_page_status(get_settings(), page_path, request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/wiki/pages/{page_path:path}", response_model=WikiPageContentResponse)
def read_wiki_page_endpoint(page_path: str) -> WikiPageContentResponse:
    try:
        return WikiPageContentResponse(**read_wiki_page(get_settings(), page_path))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/wiki/pages/{page_path:path}")
def save_wiki_page_endpoint(page_path: str, request: WikiPageSaveRequest) -> dict:
    try:
        return save_wiki_page(get_settings(), page_path, request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/gaps")
def list_gaps_endpoint(status: str | None = Query(default=None)) -> list[dict]:
    try:
        return list_knowledge_gaps(get_settings(), status)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/gaps/{gap_id}")
def update_gap_endpoint(gap_id: str, request: GapUpdateRequest) -> dict:
    try:
        return update_knowledge_gap(get_settings(), gap_id, request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/reviews")
def list_reviews_endpoint(status: str | None = Query(default=None)) -> list[dict]:
    try:
        return list_review_items(get_settings(), status)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/reviews/{review_id}")
def update_review_endpoint(review_id: str, request: ReviewUpdateRequest) -> dict:
    try:
        return update_review_item(get_settings(), review_id, request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/sources/scan", response_model=ScanResponse)
def scan_endpoint(request: ScanRequest) -> ScanResponse:
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
) -> UploadResponse:
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
def compile_endpoint(request: CompileRequest) -> CompileResponse:
    try:
        return compile_wiki(get_settings(), request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
def feedback_endpoint(request: FeedbackRequest) -> FeedbackResponse:
    try:
        return submit_feedback(get_settings(), request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/eval/questions")
def eval_question_endpoint(request: EvalQuestionRequest) -> dict[str, str]:
    try:
        return add_eval_question(get_settings(), request)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/eval/run", response_model=EvalRunResponse)
def eval_run_endpoint(domain: str | None = Query(default=None)) -> EvalRunResponse:
    try:
        return run_eval(get_settings(), domain)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/eval/upgraded")
def upgraded_eval_endpoint(request: UpgradedEvalRunRequest) -> dict:
    try:
        return compare_upgraded_eval(get_settings(), domain=request.domain, mode=request.gbrain_mode)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/aliases")
def list_aliases_endpoint(domain: str | None = Query(default=None)) -> list[dict]:
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
