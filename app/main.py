from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import router
from app.config import get_settings
from app.db import init_app_db
from app.vault import ensure_vault


settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_vault(settings.vault_path)
    init_app_db(settings)
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "app": settings.app_name}


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
