from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import SourceItem


class SourceItemRepository:
    async def bulk_upsert_ignore_duplicates(self, session: AsyncSession, items: list[SourceItem]) -> int:
        if not items:
            return 0

        topic_ids = {it.topic_id for it in items}
        if not topic_ids:
            return 0

        existing: set[tuple[str, str, str]] = set()
        for tid in topic_ids:
            res = await session.execute(
                select(SourceItem.topic_id, SourceItem.source, SourceItem.source_url)
                .where(SourceItem.topic_id == tid)
            )
            for row in res.all():
                existing.add((row[0], row[1], row[2]))

        seen_in_batch: set[tuple[str, str, str]] = set()
        to_add: list[SourceItem] = []
        for it in items:
            key = (it.topic_id, it.source, it.source_url)
            if key in existing or key in seen_in_batch:
                continue
            seen_in_batch.add(key)
            to_add.append(it)

        for it in to_add:
            session.add(it)

        if to_add:
            await session.commit()

        return len(to_add)

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
