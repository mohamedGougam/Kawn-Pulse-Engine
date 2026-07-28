from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.models.db_models import Topic
from app.repositories.topic_repository import TopicRepository
from app.services.aggregation_service import AggregationService

logger = logging.getLogger("kawn.scheduler")

# How long a topic can go unrefreshed before staleness stops adding to its
# priority score — without a cap, a topic nobody has touched in months would
# permanently outrank everything else the moment it's noticed, crowding out
# genuinely current interest.
_MAX_STALENESS_HOURS = 24 * 7
# Extra priority for anything in Discover_subjects, so the explore feed's
# fixed subject list keeps getting refreshed even with zero recorded search
# interest — without this, a niche user-searched topic with a handful of
# searches could permanently outrank a discover subject nobody has searched
# but everybody browsing the feed sees.
_DISCOVER_FLOOR = 12.0


def _priority_score(topic: Topic, *, is_discover: bool) -> float:
    now = datetime.utcnow()

    hours_stale = max(0.0, (now - topic.updated_at).total_seconds() / 3600.0)
    staleness_score = min(hours_stale, _MAX_STALENESS_HOURS)

    search_score = min(topic.search_count or 0, 50) * 2.0

    recency_bonus = 0.0
    if topic.last_searched_at:
        hours_since_search = max(0.0, (now - topic.last_searched_at).total_seconds() / 3600.0)
        # Up to +24 for a search in the last hour, decaying to 0 after a day.
        recency_bonus = max(0.0, 24.0 - hours_since_search)

    discover_bonus = _DISCOVER_FLOOR if is_discover else 0.0

    return staleness_score + search_score + recency_bonus + discover_bonus


class SchedulerService:
    def __init__(self) -> None:
        self.scheduler: AsyncIOScheduler | None = None
        self.aggregation = AggregationService()
        self.topic_repo = TopicRepository()
        self._tick_lock = asyncio.Lock()

    def start(self) -> None:
        if not settings.scheduler_enabled:
            return
        if self.scheduler is not None:
            return

        scheduler = AsyncIOScheduler()
        scheduler.add_job(self._refresh_trending_topics_job, "interval", minutes=settings.scheduler_refresh_minutes)
        scheduler.start()
        self.scheduler = scheduler

    async def _refresh_trending_topics_job(self) -> None:
        if self._tick_lock.locked():
            # A cron ping (or the interval job) arrived while a previous
            # pass was still running. Skip rather than run two passes
            # concurrently against the same topics — the next tick will
            # pick up where this leaves off, and the priority scoring means
            # nothing important gets starved by being skipped once.
            return

        async with self._tick_lock:
            await self._run_refresh_pass()

    async def _run_refresh_pass(self) -> None:
        candidates: dict[str, Topic] = {}
        is_discover: dict[str, bool] = {}

        async with get_session() as session:
            session: AsyncSession
            for sub in settings.Discover_subjects:
                topic = await self.topic_repo.get_by_query(session, sub)
                if not topic:
                    # Cold-start row so it has something to score — this is
                    # bookkeeping, not a real search.
                    topic = await self.topic_repo.upsert(session, sub)
                candidates[topic.id] = topic
                is_discover[topic.id] = True

            for t in await self.topic_repo.list_by_search_interest(session, limit=50):
                candidates.setdefault(t.id, t)
                is_discover.setdefault(t.id, False)

        if not candidates:
            return

        ranked = sorted(
            candidates.values(),
            key=lambda t: _priority_score(t, is_discover=is_discover[t.id]),
            reverse=True,
        )
        batch = ranked[: settings.background_batch_size]

        # A single topic refresh fires every connector concurrently (no
        # per-connector cap any more), so this semaphore caps how many
        # *topics* the background worker refreshes at once — otherwise a
        # burst of background work could pile a lot of simultaneous
        # connections on top of whatever a live user search is doing.
        semaphore = asyncio.Semaphore(settings.background_worker_concurrency)

        async def _refresh_one(topic: Topic) -> None:
            async with semaphore:
                try:
                    async with get_session() as s2:
                        # Background-driven refreshes never count toward the
                        # search-interest signal that feeds this same
                        # priority queue.
                        await self.aggregation.refresh_topic(s2, topic.query, record_search=False)
                except Exception:
                    logger.warning("background refresh failed for topic=%r", topic.query, exc_info=True)

        await asyncio.gather(*(_refresh_one(t) for t in batch))

    def shutdown(self) -> None:
        if self.scheduler is None:
            return
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        self.scheduler = None

