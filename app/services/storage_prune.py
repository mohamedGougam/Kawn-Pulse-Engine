"""Keeps the database from growing without bound.

Rows used to be pruned only when their own topic was refreshed, so every
one-off search ("some topic nobody looked at again") left its stored items,
cards, summary and breakdown behind permanently. On a small hosted Postgres
(Neon's free plan has a storage cap) that is a slow leak that ends in
"exceeded the quota".

prune_stale_data() deletes:
  * source items past the retention window, for every topic;
  * all stored items/cards/summaries/breakdowns of topics nobody has searched
    for idle_topic_retention_days (Discover subjects are exempt).

The Topic row itself is kept (it is tiny), so a pruned topic is simply rebuilt
on its next search. maybe_prune() rate-limits this to once per
storage_prune_interval_seconds and never raises.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select

from app.config import settings
from app.database import get_session
from app.models.db_models import PulseCard, SourceBreakdown, SourceItem, Topic, TopicSummary

logger = logging.getLogger("kawn.prune")

_last_run: float | None = None


def reset_state() -> None:  # tests
    global _last_run
    _last_run = None


async def prune_stale_data(session, *, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.utcnow()
    item_cutoff = now - timedelta(days=settings.source_freshness_days)
    idle_cutoff = now - timedelta(days=settings.idle_topic_retention_days)
    discover = [s.strip().lower() for s in settings.Discover_subjects]

    idle_topics = select(Topic.id).where(
        func.coalesce(Topic.last_searched_at, Topic.created_at) < idle_cutoff,
        func.lower(Topic.query).not_in(discover) if discover else True,
    )

    counts: dict[str, int] = {}

    res = await session.execute(
        delete(SourceItem).where(
            or_(
                and_(SourceItem.published_at.is_not(None), SourceItem.published_at < item_cutoff),
                and_(SourceItem.published_at.is_(None), SourceItem.created_at < item_cutoff),
            )
        )
    )
    counts["expired_items"] = int(res.rowcount or 0)

    for name, model in (
        ("idle_items", SourceItem),
        ("idle_cards", PulseCard),
        ("idle_summaries", TopicSummary),
        ("idle_breakdowns", SourceBreakdown),
    ):
        res = await session.execute(delete(model).where(model.topic_id.in_(idle_topics)))
        counts[name] = int(res.rowcount or 0)

    await session.commit()
    return counts


async def maybe_prune() -> dict[str, int] | None:
    """Run the prune if it has not run recently. Returns the counts, or None
    if it was skipped or failed (a failure is logged, never raised: pruning
    is housekeeping and must not break the refresh pass that calls it)."""
    global _last_run
    now = time.monotonic()
    if _last_run is not None and now - _last_run < settings.storage_prune_interval_seconds:
        return None
    _last_run = now
    try:
        async with get_session() as session:
            counts = await prune_stale_data(session)
        if any(counts.values()):
            logger.info("pruned stale data: %s", counts)
        return counts
    except Exception as e:
        logger.warning("storage prune failed: %s: %s", type(e).__name__, e)
        return None
