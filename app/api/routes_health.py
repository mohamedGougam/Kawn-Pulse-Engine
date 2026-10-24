from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import settings

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/health/db")
async def health_db(request: Request) -> JSONResponse:
    """Database status without opening a log file: 200 when the service is
    fully started, 503 (with the reason) while the database is unavailable,
    e.g. a Neon project that is over its quota. /health stays DB-free so the
    host's health check never flaps because of the database."""
    state = request.app.state
    if getattr(state, "db_ready", False):
        return JSONResponse({"db": "ready"})
    return JSONResponse({"db": "unavailable", "error": getattr(state, "db_error", None)}, status_code=503)


@router.post("/internal/cron-tick")
async def cron_tick(
    background_tasks: BackgroundTasks,
    x_cron_secret: str | None = Header(default=None),
) -> dict:
    """Hit by an external cron pinger (e.g. cron-job.org) on a schedule.

    Free-tier dynos sleep after ~15 min idle, so the in-process APScheduler
    job cannot fire while asleep. This endpoint both wakes the dyno (the
    ping itself does that) and kicks off a refresh tick in the background,
    so scheduled background fetching works even if the process was just
    cold-started. Runs as a background task so the pinger gets an
    immediate 200 instead of waiting through a full Discover_subjects pass.
    """
    if not settings.cron_heartbeat_secret:
        raise HTTPException(status_code=503, detail="cron_heartbeat_secret not configured")
    if x_cron_secret != settings.cron_heartbeat_secret:
        raise HTTPException(status_code=401, detail="invalid cron secret")

    from app.main import scheduler_service

    background_tasks.add_task(scheduler_service._refresh_trending_topics_job)
    return {"status": "ok", "ticked": True}

