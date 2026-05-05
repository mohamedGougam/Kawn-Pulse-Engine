from __future__ import annotations

import asyncio
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_session
from app.repositories.topic_repository import TopicRepository
from app.services.aggregation_service import AggregationService


class SchedulerService:
    def __init__(self) -> None:
        self.scheduler: AsyncIOScheduler | None = None
        self.aggregation = AggregationService()
        self.topic_repo = TopicRepository()

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
        # Refresh most recently updated topics to keep pulse warm.
        async with get_session() as session:
            session: AsyncSession
            topics = await self.topic_repo.list_trending(session, limit=10)

        for t in topics:
            try:
                async with get_session() as s2:
                    await self.aggregation.refresh_topic(s2, t.query)
            except Exception:
                continue

    def shutdown(self) -> None:
        if self.scheduler is None:
            return
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        self.scheduler = None

