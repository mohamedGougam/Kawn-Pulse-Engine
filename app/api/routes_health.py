from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException

from app.config import settings

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


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

