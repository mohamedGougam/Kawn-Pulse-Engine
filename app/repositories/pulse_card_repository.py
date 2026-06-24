from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import PulseCard


class PulseCardRepository:
    async def bulk_create(self, session: AsyncSession, cards: list[PulseCard]) -> int:
        for c in cards:
            session.add(c)
        await session.commit()
        return len(cards)

    async def list_for_topic(
        self,
        session: AsyncSession,
        topic_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> list[PulseCard]:
        page = max(1, int(page))
        page_size = min(100, max(1, int(page_size)))
        offset = (page - 1) * page_size

        res = await session.execute(
            select(PulseCard)
            .where(PulseCard.topic_id == topic_id)
            .order_by(PulseCard.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
        return list(res.scalars().all())

    async def count_for_topic(self, session: AsyncSession, topic_id: str) -> int:
        res = await session.execute(select(func.count(PulseCard.id)).where(PulseCard.topic_id == topic_id))
        return int(res.scalar_one() or 0)

    async def count_by_source(self, session: AsyncSession, topic_id: str) -> dict[str, int]:
        res = await session.execute(
            select(PulseCard.source, func.count(PulseCard.id))
            .where(PulseCard.topic_id == topic_id)
            .group_by(PulseCard.source)
        )
        return {row[0]: int(row[1]) for row in res.all()}

    async def delete_for_topic(self, session: AsyncSession, topic_id: str) -> int:
        res = await session.execute(delete(PulseCard).where(PulseCard.topic_id == topic_id))
        await session.commit()
        return int(res.rowcount or 0)

    async def get_by_id(self, session: AsyncSession, card_id: str) -> PulseCard | None:
        res = await session.execute(select(PulseCard).where(PulseCard.id == card_id))
        return res.scalar_one_or_none()

    async def Listcardsformultipletopics(
        self,
        session: AsyncSession,
        topic_ids: list[str],
        *,
        limit_per_topic: int = 5,
    ) -> list[PulseCard]:
        from sqlalchemy import func
        from sqlalchemy.orm import aliased

        subq = (
            select(
                PulseCard,
                func.row_number().over(
                    partition_by=PulseCard.topic_id,
                    order_by=PulseCard.created_at.desc()
                ).label("rn")
            )
            .where(PulseCard.topic_id.in_(topic_ids))
            .subquery()
        )

        card_alias = aliased(PulseCard, subq)
        res = await session.execute(
            select(card_alias)
            .where(subq.c.rn <= limit_per_topic)
        )
        return list(res.scalars().all())


