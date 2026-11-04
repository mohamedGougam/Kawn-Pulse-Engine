from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
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


_legacy_client: httpx.AsyncClient | None = None
_HOP_BY_HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "upgrade", "te"}
_DROP_RESPONSE = {"content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive"}


def _get_legacy_client() -> httpx.AsyncClient:
    # One shared client for every forwarded request (a new one per request
    # would waste memory on the 512 MB free instance).
    global _legacy_client
    if _legacy_client is None or _legacy_client.is_closed:
        _legacy_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=False)
    return _legacy_client


@app.middleware("http")
async def legacy_host_redirect(request, call_next):
    """Old deployment's URL -> new deployment (proxy or 307, see redirect_mode)."""
    old_hosts = {h.strip().lower() for h in settings.redirect_old_hosts.split(",") if h.strip()}
    target = settings.redirect_target_url.rstrip("/")
    if not (old_hosts and target) or request.url.path == "/health":
        return await call_next(request)
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host not in old_hosts:
        return await call_next(request)

    url = target + request.url.path
    if request.url.query:
        url += "?" + request.url.query

    if settings.redirect_mode != "proxy":
        # 307 keeps the HTTP method (POST stays POST) for clients that follow it.
        return RedirectResponse(url, status_code=307)

    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    headers["x-forwarded-host"] = host
    try:
        upstream = await _get_legacy_client().request(
            request.method, url, headers=headers, content=await request.body()
        )
    except httpx.HTTPError as e:
        logging.getLogger(__name__).warning("legacy proxy to %s failed: %s", url, e)
        return JSONResponse({"detail": "upstream unavailable"}, status_code=502)
    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _DROP_RESPONSE}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=out_headers)


app.include_router(health_router)
app.include_router(topics_router)
app.include_router(sources_router)

