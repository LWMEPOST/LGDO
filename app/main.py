from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import router
from app.config import get_settings
from app.db import init_app_db
from app.gbrain import _gbrain_circuit_is_open
from app.projection_worker import ProjectionWorker, projection_health
from app.vault import ensure_vault


settings = get_settings()


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
