from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes_health import router as health_router
from app.api.routes_sources import router as sources_router
from app.api.routes_topics import router as topics_router
from app.config import settings
from app.database import init_db
from app.services.scheduler_service import SchedulerService
from pathlib import Path


scheduler_service = SchedulerService()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    scheduler_service.start()
    yield
    scheduler_service.shutdown()


app = FastAPI(
    title="Kawn Pulse Engine",
    version="0.1.0",
    description="Topic pulse backend: reactions, quote cards, sentiment, themes, summaries.",
    lifespan=lifespan,
)

BASE_DIR = Path(__file__).resolve().parent
UI_DIR = BASE_DIR / "ui"
ASSETS_DIR = UI_DIR / "assets"
if ASSETS_DIR.exists():
    app.mount("/ui/assets", StaticFiles(directory=str(ASSETS_DIR)), name="ui-assets")


@app.get("/")
async def root() -> dict:
    return {"service": "kawn-pulse-engine", "status": "ok", "mock_mode": settings.enable_mock_data}


@app.get("/ui")
async def ui() -> FileResponse:
    index = UI_DIR / "index.html"
    return FileResponse(str(index))


@app.get("/UI")
async def ui_uppercase() -> RedirectResponse:
    return RedirectResponse(url="/ui", status_code=307)


app.include_router(health_router)
app.include_router(topics_router)
app.include_router(sources_router)

