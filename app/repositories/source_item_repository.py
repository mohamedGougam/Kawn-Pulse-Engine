from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, case, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import SourceItem


def _dialect_insert(dialect_name: str):
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


class SourceItemRepository:
    async def bulk_upsert_ignore_duplicates(self, session: AsyncSession, items: list[SourceItem]) -> int:
        """Insert items, silently skipping any that already exist.

        Dedup is done by the database via INSERT .. ON CONFLICT DO NOTHING
        against the unique index (topic_id, source, source_url), instead of
        first loading every existing key for the topic into a Python set on
        every refresh -- that preload grew with the topic's row count and was
        a steady contributor to memory use late in the retention window.
        """
        if not items:
            return 0


        # topic_ids = {it.topic_id for it in items}
        # if not topic_ids:
        #     return 0

        # existing: set[tuple[str, str, str]] = set()
        # res = await session.execute(
        #     select(SourceItem.topic_id, SourceItem.source, SourceItem.source_url)
        #     .where(SourceItem.topic_id.in_(topic_ids))
        # )
        # for row in res.all():
        #     existing.add((row[0], row[1], row[2]))

        # seen_in_batch: set[tuple[str, str, str]] = set()
        # to_add: list[SourceItem] = []

        # Cheap in-batch dedup so one statement never carries the same key twice.
        seen: set[tuple[str, str, str]] = set()
        rows: list[dict] = []
        now = datetime.utcnow()

        for it in items:
            key = (it.topic_id, it.source, it.source_url)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "id": it.id,
                    "topic_id": it.topic_id,
                    "source": it.source,
                    "source_url": it.source_url,
                    "external_id": it.external_id,
                    "author": it.author,
                    "title": it.title,
                    "text": it.text,
                    "language": it.language,
                    "published_at": it.published_at,
                    "engagement_count": it.engagement_count,
                    "created_at": it.created_at or now,
                }
            )


        # if to_add:
        #     session.add_all(to_add)

        if not rows:
            return 0

        bind = session.get_bind()
        insert_fn = _dialect_insert(bind.dialect.name)


        inserted = 0
        # Chunked so a large batch stays well under driver bind-parameter limits.
        for i in range(0, len(rows), 200):
            chunk = rows[i : i + 200]
            stmt = insert_fn(SourceItem.__table__).values(chunk).on_conflict_do_nothing()
            res = await session.execute(stmt)
            rc = res.rowcount
            inserted += rc if rc is not None and rc >= 0 else 0

        await session.commit()
        return inserted

    async def list_for_topic(self, session: AsyncSession, topic_id: str, *, limit: int = 500) -> list[SourceItem]:
        res = await session.execute(
            select(SourceItem).where(SourceItem.topic_id == topic_id).order_by(SourceItem.created_at.desc()).limit(limit)
        )
        return list(res.scalars().all())

    async def list_balanced_for_topic(
        self,
        session: AsyncSession,
        topic_id: str,
        *,
        per_source_cap: int,
        news_outlet_names: set[str],
    ) -> list[SourceItem]:
        """Most recent `per_source_cap` items per connector family, newest first.

        Same result as loading a big recent pool and capping it in Python
        (see aggregation_service._balance_by_source), but the cap is applied
        inside the database with ROW_NUMBER() so only the rows that will
        actually be used are ever loaded. News outlets (BBC, CNN, ...) are
        collapsed into one "News" family, matching connector_family_for_source.
        """
        family = case(
            (func.lower(SourceItem.source).in_(list(news_outlet_names)), "News"),
            else_=SourceItem.source,
        )
        ranked = (
            select(
                SourceItem.id.label("item_id"),
                func.row_number()
                .over(partition_by=family, order_by=SourceItem.created_at.desc())
                .label("rn"),
            )
            .where(SourceItem.topic_id == topic_id)
            .subquery()
        )
        stmt = (
            select(SourceItem)
            .join(ranked, SourceItem.id == ranked.c.item_id)
            .where(ranked.c.rn <= per_source_cap)
            .order_by(SourceItem.created_at.desc())
        )
        res = await session.execute(stmt)
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
        return int(res.rowcount or 0)

    async def delete_older_than(self, session: AsyncSession, topic_id: str, cutoff: datetime) -> int:
        stmt = delete(SourceItem).where(
            SourceItem.topic_id == topic_id,
            or_(
                and_(SourceItem.published_at.is_not(None), SourceItem.published_at < cutoff),
                and_(SourceItem.published_at.is_(None), SourceItem.created_at < cutoff),
            ),
        )
        res = await session.execute(stmt)

        # return int(res.rowcount or 0)

        await session.commit()
        return int(res.rowcount or 0)

