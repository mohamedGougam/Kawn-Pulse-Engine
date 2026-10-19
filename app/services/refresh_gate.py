"""Process-wide limits on how many topic refreshes run at once.

Why this exists: one refresh fans out to every connector plus R2 cache
traffic, so memory grows with the number of refreshes in flight. Nothing used
to bound that number. Several users searching at once, and the explore feed
re-queuing a background refresh for every cold Discover subject on every
single feed load, could stack up dozens of refreshes and push the instance
past Render's 512 MB limit.

State is module-level on purpose: the web routes and the scheduler each build
their own AggregationService, so per-instance limits would not be shared.

asyncio primitives bind to the loop that first uses them, so they are created
lazily and rebuilt if the running loop changes (only happens under test
runners that create one loop per test).
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from app.config import settings
from app.services import memory_guard

logger = logging.getLogger("kawn.refresh_gate")


class RefreshBusy(Exception):
    """Too many refreshes in flight and none freed up within the queue timeout."""


class _Gate:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._user: asyncio.Semaphore | None = None
        self._bg: asyncio.Semaphore | None = None
        self.active = 0                                 # refreshes running right now (user + background)
        self.bg_inflight: set[str] = set()              # normalized queries being refreshed in background
        self.bg_last_attempt: dict[str, float] = {}     # normalized query -> time.monotonic()

    def _bind(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return
        self._loop = loop
        self._user = asyncio.Semaphore(max(1, settings.max_concurrent_refreshes))
        self._bg = asyncio.Semaphore(max(1, settings.max_concurrent_background_refreshes))
        self.active = 0
        self.bg_inflight.clear()

    @asynccontextmanager
    async def user_slot(self):
        """Wait for a free slot for a user-triggered refresh, or raise RefreshBusy."""
        self._bind()
        try:
            await asyncio.wait_for(self._user.acquire(), timeout=settings.refresh_queue_timeout_seconds)
        except asyncio.TimeoutError:
            raise RefreshBusy(
                f"{settings.max_concurrent_refreshes} refreshes already running; "
                f"none finished within {settings.refresh_queue_timeout_seconds:.0f}s"
            ) from None
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1
            self._user.release()

    def try_begin_background(self, query: str) -> str | None:
        """Claim a background refresh for `query`.

        Returns None if it may proceed (and marks it in flight), otherwise a
        short reason it was skipped: already running, tried too recently, or
        the process is under memory pressure. Skipping is always safe: the
        scheduler picks the topic up again on a later tick.
        """
        self._bind()
        key = query.strip().lower()
        if key in self.bg_inflight:
            return "already refreshing"
        last = self.bg_last_attempt.get(key)
        if last is not None and time.monotonic() - last < settings.background_retry_cooldown_seconds:
            return "attempted recently"
        if memory_guard.over_soft_limit():
            return "memory pressure"
        self.bg_inflight.add(key)
        self.bg_last_attempt[key] = time.monotonic()
        if len(self.bg_last_attempt) > 500:  # bounded: drop the oldest half
            for k, _ in sorted(self.bg_last_attempt.items(), key=lambda kv: kv[1])[:250]:
                self.bg_last_attempt.pop(k, None)
        return None

    def end_background(self, query: str) -> None:
        self.bg_inflight.discard(query.strip().lower())

    @asynccontextmanager
    async def background_slot(self):
        self._bind()
        async with self._bg:
            self.active += 1
            try:
                yield
            finally:
                self.active -= 1


gate = _Gate()
