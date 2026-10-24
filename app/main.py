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
from app.db_errors import db_exception_handler, describe
from app.services import memory_guard
from app.services.scheduler_service import SchedulerService
from app.storage.object_store import object_store
from app.streaming.bluesky_firehose import bluesky_firehose_service
from app.streaming.reddit_stream import reddit_stream_service
from pathlib import Path


logger = logging.getLogger("kawn.main")

scheduler_service = SchedulerService()


async def _init_db_until_ready(app: FastAPI) -> None:
    """Keep trying to initialise the database until it works.

    Only used when the first attempt at startup failed. Backs off up to
    db_startup_retry_max_seconds so a database that is over quota (or
    suspended) is probed gently instead of hammered.
    """
    delay = settings.db_startup_retry_initial_seconds
    while True:
        await asyncio.sleep(delay)
        try:
            await init_db()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            app.state.db_error = describe(e)
            logger.warning("database still unavailable (%s); next attempt in %.0fs", app.state.db_error, min(delay * 2, settings.db_startup_retry_max_seconds))
            delay = min(delay * 2, settings.db_startup_retry_max_seconds)
            continue
        app.state.db_ready = True
        app.state.db_error = None
        logger.info("database is available; service fully started")
        scheduler_service.start()
        return


@asynccontextmanager
async def lifespan(app: FastAPI):
    # A database that is down or over quota (Neon free tier) used to make
    # init_db() raise here, which killed the whole process at boot
    # ("Application startup failed" / exit status 3) and left nothing running
    # to even report the problem. Now the service comes up regardless, serves
    # /health and /health/db, answers database-backed routes with 503, and
    # recovers by itself when the database is back.
    app.state.db_ready = False
    app.state.db_error = None
    init_task: asyncio.Task | None = None
    try:
        await asyncio.wait_for(init_db(), timeout=settings.db_startup_timeout_seconds)
        app.state.db_ready = True
        scheduler_service.start()
    except Exception as e:
        app.state.db_error = describe(e) if str(e) else f"{type(e).__name__} (timed out after {settings.db_startup_timeout_seconds:.0f}s)"
        logger.error(
            "database unavailable at startup (%s). Starting anyway; retrying in the background. "
            "If this is Neon, check the project's quota / usage page.", app.state.db_error,
        )
        init_task = asyncio.create_task(_init_db_until_ready(app))
    # Both no-op unless their *_enabled flag is set — see config.py for
    # why they're off by default (need an always-on process, not a
    # free-tier dyno that spins down on idle).
    bluesky_firehose_service.start()
    reddit_stream_service.start()
    guard_task = asyncio.create_task(memory_guard.run())
    yield
    guard_task.cancel()
    if init_task is not None:
        init_task.cancel()
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

# Database errors raised inside a request: a clean 503 (retry later) when the
# database is unreachable or over quota, instead of an opaque 500.
import asyncpg  # noqa: E402
from sqlalchemy import exc as _sa_exc  # noqa: E402

for _exc_type in (_sa_exc.DBAPIError, _sa_exc.TimeoutError, asyncpg.exceptions.PostgresError, ConnectionError):
    app.add_exception_handler(_exc_type, db_exception_handler)

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

