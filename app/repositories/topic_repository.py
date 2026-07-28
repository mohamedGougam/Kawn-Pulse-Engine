from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import Topic


class TopicRepository:
    async def get_by_id(self, session: AsyncSession, topic_id: str) -> Topic | None:
        res = await session.execute(select(Topic).where(Topic.id == topic_id))
        return res.scalar_one_or_none()

    async def get_by_query(self, session: AsyncSession, query: str) -> Topic | None:
        res = await session.execute(select(Topic).where(Topic.query == query))
        return res.scalar_one_or_none()

    async def upsert(self, session: AsyncSession, query: str) -> Topic:
        existing = await self.get_by_query(session, query)
        now = datetime.utcnow()
        if existing:
            existing.updated_at = now
            session.add(existing)
            await session.commit()
            await session.refresh(existing)
            return existing

        topic = Topic(query=query, created_at=now, updated_at=now)
        session.add(topic)
        await session.commit()
        await session.refresh(topic)
        return topic

    async def list_trending(self, session: AsyncSession, *, limit: int = 20) -> list[Topic]:
        res = await session.execute(select(Topic).order_by(Topic.updated_at.desc()).limit(limit))
        return list(res.scalars().all())

    async def record_search(self, session: AsyncSession, topic: Topic) -> Topic:
        """Bump the real-user-interest signal. Called only from paths a
        person actually triggered (search, manual refresh) — never from the
        background worker's own refreshes, or every topic's interest score
        would just track how often it happens to get auto-refreshed."""
        topic.search_count = (topic.search_count or 0) + 1
        topic.last_searched_at = datetime.utcnow()
        session.add(topic)
        await session.commit()
        await session.refresh(topic)
        return topic

    async def list_by_search_interest(self, session: AsyncSession, *, limit: int = 50) -> list[Topic]:
        """Topics with at least one real user search, most-searched first —
        candidates for the background worker's priority queue beyond the
        fixed Discover_subjects list."""
        res = await session.execute(
            select(Topic).where(Topic.search_count > 0).order_by(Topic.search_count.desc()).limit(limit)
        )
        return list(res.scalars().all())

