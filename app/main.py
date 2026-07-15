import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import router
from app.config import get_settings
from app.db import init_app_db
from app.gbrain import _gbrain_circuit_is_open
from app.obsidian import ensure_obsidian_vault
from app.projection_worker import ProjectionWorker, projection_health
from app.vault import ensure_vault
from app.vault_sync import VaultSyncService
from app.vault_writer import IntentExecutor


settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    vault_sync: VaultSyncService | None = None
    projection_worker: ProjectionWorker | None = None
    obsidian_status: dict | None = None
    try:
        ensure_vault(settings.vault_path)
        init_app_db(settings)
        obsidian_result = ensure_obsidian_vault(settings.vault_path)
        intent_executor = IntentExecutor(settings)
        vault_sync = VaultSyncService(
            settings,
            intent_executor=intent_executor,
        )
        app.state.vault_sync = vault_sync
        projection_worker = ProjectionWorker(settings)
        app.state.projection_worker = projection_worker
        obsidian_status = {
            "installed": list(obsidian_result.installed),
            "drifted": list(obsidian_result.drifted),
            "vault_name": settings.effective_obsidian_vault_name,
        }
        app.state.obsidian_status = obsidian_status

        await asyncio.to_thread(intent_executor.reconcile_all)
        await vault_sync.reconcile_before_watcher_start()
        if settings.projection_worker_enabled:
            await projection_worker.start()
        if settings.vault_watch_enabled:
            await vault_sync.start()
        yield
    finally:
        active_error = sys.exc_info()[1]
        cleanup_error: BaseException | None = None
        if vault_sync is not None:
            try:
                await vault_sync.stop()
            except BaseException as exc:
                cleanup_error = exc
        if projection_worker is not None:
            try:
                await projection_worker.stop()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc

        owned_state = {
            "vault_sync": vault_sync,
            "projection_worker": projection_worker,
            "obsidian_status": obsidian_status,
        }
        for name, owner in owned_state.items():
            if owner is not None and app.state._state.get(name) is owner:
                delattr(app.state, name)
        if active_error is None and cleanup_error is not None:
            raise cleanup_error


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    projections = projection_health(settings)
    circuit_open = _gbrain_circuit_is_open(settings)
    query_available = bool(
        settings.gbrain_enabled
        and settings.gbrain_endpoint
        and not circuit_open
    )
    return {
        "status": "ok",
        "app": settings.app_name,
        "gbrain": {
            "query": {
                "available": query_available,
                "circuit_open": circuit_open,
            },
            "projection": projections["gbrain"],
        },
        "rag": {"projection": projections["rag"]},
    }


app.include_router(router, prefix="/api/internal", tags=["internal"])

static_dir = Path(__file__).resolve().parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

frontend_dist_dir = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if frontend_dist_dir.exists():
    app.mount("/console-static", StaticFiles(directory=frontend_dist_dir), name="console-static")


@app.get("/console", include_in_schema=False)
def console() -> FileResponse:
    frontend_index = frontend_dist_dir / "index.html"
    if frontend_index.exists():
        return FileResponse(frontend_index)
    return FileResponse(static_dir / "console.html")
