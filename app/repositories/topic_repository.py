from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import Topic

logger = logging.getLogger("kawn.topics")


class TopicRepository:
    async def get_by_id(self, session: AsyncSession, topic_id: str) -> Topic | None:
        res = await session.execute(select(Topic).where(Topic.id == topic_id))
        return res.scalar_one_or_none()

    async def get_by_query(self, session: AsyncSession, query: str) -> Topic | None:
        res = await session.execute(select(Topic).where(Topic.query == query))
        return res.scalar_one_or_none()

    async def get_by_ids(self, session: AsyncSession, topic_ids: list[str]) -> list[Topic]:
        """All the topics in ONE query (the callers used to run one per id)."""
        if not topic_ids:
            return []
        res = await session.execute(select(Topic).where(Topic.id.in_(topic_ids)))
        return list(res.scalars().all())

    async def get_or_create_many(self, session: AsyncSession, queries: list[str]) -> list[Topic]:
        """One SELECT for the existing topics, one INSERT batch + ONE commit for
        the missing ones, returned in the same order as the given queries. Replaces a
        SELECT + SELECT + INSERT + COMMIT round-trip chain per query."""
        if not queries:
            return []
        res = await session.execute(select(Topic).where(Topic.query.in_(queries)))
        by_query: dict[str, Topic] = {}
        for t in res.scalars().all():
            by_query.setdefault(t.query, t)
        missing = [q for q in queries if q not in by_query]
        if missing:
            now = datetime.utcnow()
            new = [Topic(query=q, created_at=now, updated_at=now) for q in missing]
            try:
                session.add_all(new)
                await session.commit()
                for t in new:
                    by_query[t.query] = t
            except Exception:
                await session.rollback()
                logger.warning("Batch topic create failed; re-reading", exc_info=True)
                res = await session.execute(select(Topic).where(Topic.query.in_(queries)))
                for t in res.scalars().all():
                    by_query.setdefault(t.query, t)
        return [by_query[q] for q in queries if q in by_query]

    async def upsert(self, session: AsyncSession, query: str) -> Topic:
        existing = await self.get_by_query(session, query)
        now = datetime.utcnow()
        if existing:
            existing.updated_at = now
            session.add(existing)
            await session.commit()
            return existing

        topic = Topic(query=query, created_at=now, updated_at=now)
        session.add(topic)
        await session.commit()
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
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            logger.warning("Could not record search_count for topic=%r; skipped", topic.query, exc_info=True)
        return topic

    async def list_by_search_interest(self, session: AsyncSession, *, limit: int = 50) -> list[Topic]:
        """Topics with at least one real user search, most-searched first —
        candidates for the background worker's priority queue beyond the
        fixed Discover_subjects list."""
        res = await session.execute(
            select(Topic).where(Topic.search_count > 0).order_by(Topic.search_count.desc()).limit(limit)
        )
        return list(res.scalars().all())

