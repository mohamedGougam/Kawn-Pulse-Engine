from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes_health import router as health_router
from app.api.routes_sources import router as sources_router
from app.api.routes_topics import router as topics_router
from app.config import settings
from app.database import init_db
from app.services import memory_guard
from app.services.scheduler_service import SchedulerService
from app.storage.object_store import object_store
from app.streaming.bluesky_firehose import bluesky_firehose_service
from app.streaming.reddit_stream import reddit_stream_service
from pathlib import Path


logger = logging.getLogger("kawn.main")

scheduler_service = SchedulerService()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A database outage (e.g. Neon quota exceeded) must not kill the whole
    # API: start anyway so /health and the UI respond. Routes that need the
    # database will fail until it is reachable again.
    try:
        await init_db()
    except Exception:
        logger.error("init_db failed; starting without a working database", exc_info=True)
    scheduler_service.start()
    # Both no-op unless their *_enabled flag is set — see config.py for
    # why they're off by default (need an always-on process, not a
    # free-tier dyno that spins down on idle).
    bluesky_firehose_service.start()
    reddit_stream_service.start()
    guard_task = asyncio.create_task(memory_guard.run())
    yield
    guard_task.cancel()
    scheduler_service.shutdown()
    await bluesky_firehose_service.shutdown()
    await reddit_stream_service.shutdown()
    await object_store.close()


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


@app.middleware("http")
async def legacy_host_redirect(request, call_next):
    """Send visitors of the old deployment's URL to the new one."""
    old_hosts = {h.strip().lower() for h in settings.redirect_old_hosts.split(",") if h.strip()}
    target = settings.redirect_target_url.rstrip("/")
    if old_hosts and target and request.url.path != "/health":
        host = (request.headers.get("host") or "").split(":")[0].lower()
        if host in old_hosts:
            url = target + request.url.path
            if request.url.query:
                url += "?" + request.url.query
            # 307 keeps the HTTP method (POST stays POST) for API clients.
            return RedirectResponse(url, status_code=307)
    return await call_next(request)


app.include_router(health_router)
app.include_router(topics_router)
app.include_router(sources_router)

