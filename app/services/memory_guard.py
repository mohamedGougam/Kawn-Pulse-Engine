"""Watches this process's resident memory and acts before the host does.

Render kills an instance that crosses its memory limit (512 MB on the free
plan) and the service is unavailable while it restarts. Python rarely hands
memory back to the OS after a burst of work, so usage only ratchets up. Two
cheap defences:

  * over memory_soft_limit_mb: background refreshes are skipped (see
    refresh_gate) - live searches keep working;
  * over memory_recycle_mb with no refresh running: the gunicorn worker exits
    gracefully (SIGTERM) and the master immediately starts a fresh one. That
    is a sub-second blip for the next request instead of an OOM kill.

Resident memory comes from /proc/self/status, so this is a no-op on platforms
without it (the reading is None and nothing is limited). The recycle only
fires under gunicorn, which sets SERVER_SOFTWARE and respawns workers; under
plain uvicorn (local dev, run.bat) nothing would restart the process, so it
never self-terminates there.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time

from app.config import settings

logger = logging.getLogger("kawn.memory")

_STARTED = time.monotonic()


def current_rss_mb() -> float | None:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0  # kB -> MB
    except (OSError, ValueError, IndexError):
        pass
    return None


def over_soft_limit() -> bool:
    limit = settings.memory_soft_limit_mb
    if limit <= 0:
        return False
    rss = current_rss_mb()
    return rss is not None and rss > limit


def _under_gunicorn() -> bool:
    return "gunicorn" in os.environ.get("SERVER_SOFTWARE", "").lower()


def should_recycle(active_refreshes: int) -> bool:
    limit = settings.memory_recycle_mb
    if limit <= 0 or active_refreshes > 0 or not _under_gunicorn():
        return False
    if time.monotonic() - _STARTED < settings.memory_recycle_min_uptime_seconds:
        return False
    rss = current_rss_mb()
    return rss is not None and rss > limit


async def run() -> None:
    """Background loop started from the app lifespan."""
    from app.services.refresh_gate import gate  # local import: gate imports this module

    while True:
        try:
            await asyncio.sleep(max(5.0, settings.memory_check_interval_seconds))
            if should_recycle(gate.active):
                logger.warning(
                    "resident memory %.0f MB is over the %d MB recycle limit and no refresh is running; "
                    "asking gunicorn to replace this worker",
                    current_rss_mb() or 0.0, settings.memory_recycle_mb,
                )
                os.kill(os.getpid(), signal.SIGTERM)
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("memory guard check failed", exc_info=True)
