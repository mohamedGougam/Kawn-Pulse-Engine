from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import SourceItem


class SourceItemRepository:
    async def bulk_upsert_ignore_duplicates(self, session: AsyncSession, items: list[SourceItem]) -> int:
        # Rely on unique index (topic_id, source, source_url) and ignore duplicates.
        created = 0
        for it in items:
            session.add(it)
            try:
                await session.flush()
                created += 1
            except Exception:
                await session.rollback()
        await session.commit()
        return created

    async def list_for_topic(self, session: AsyncSession, topic_id: str, *, limit: int = 500) -> list[SourceItem]:
        res = await session.execute(
            select(SourceItem).where(SourceItem.topic_id == topic_id).order_by(SourceItem.created_at.desc()).limit(limit)
        )
        return list(res.scalars().all())

    async def count_by_source(self, session: AsyncSession, topic_id: str) -> dict[str, int]:
        res = await session.execute(
            select(SourceItem.source, func.count(SourceItem.id))
            .where(SourceItem.topic_id == topic_id)
            .group_by(SourceItem.source)
        )
        return {row[0]: int(row[1]) for row in res.all()}

    async def delete_for_topic(self, session: AsyncSession, topic_id: str) -> int:
        res = await session.execute(delete(SourceItem).where(SourceItem.topic_id == topic_id))
        await session.commit()
        return int(res.rowcount or 0)

