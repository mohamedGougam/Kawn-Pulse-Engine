from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.connectors.bluesky_connector import BlueskyConnector
from app.connectors.devto_connector import DevToConnector
from app.connectors.discourse_connector import DiscourseConnector
from app.connectors.lobsters_connector import LobstersConnector
from app.connectors.mastodon_connector import MastodonConnector
from app.connectors.mock_connector import MockConnector
from app.connectors.wikipedia_connector import WikipediaConnector

# Disabled -- code kept below, uncomment import + __init__ line + fetch_plan
# entry to re-enable:
# from app.connectors.hackernews_connector import HackerNewsConnector
# from app.connectors.hashnode_connector import HashnodeConnector
# from app.connectors.lemmy_connector import LemmyConnector
# from app.connectors.peertube_connector import PeerTubeConnector
# from app.connectors.producthunt_connector import ProductHuntConnector
# from app.connectors.news_connector import NewsRssConnector
# from app.connectors.reddit_connector import RedditConnector
# from app.connectors.youtube_connector import YouTubeConnector
from app.models.db_models import PulseCard, SourceBreakdown, SourceItem, TopicSummary
from app.repositories.pulse_card_repository import PulseCardRepository
from app.repositories.source_item_repository import SourceItemRepository
from app.repositories.topic_repository import TopicRepository
from app.services.ai_service import AIService
from app.services.cleaning_service import CleaningService
from app.services.normalization_service import NormalizationService
from app.services.pulse_card_service import PulseCardService


class AggregationService:
    def __init__(self) -> None:
        self.topic_repo = TopicRepository()
        self.source_repo = SourceItemRepository()
        self.card_repo = PulseCardRepository()

        self.normalizer = NormalizationService()
        self.cleaner = CleaningService()
        self.ai = AIService()
        self.card_builder = PulseCardService(self.ai)

        self.mock = MockConnector()
        self.bluesky = BlueskyConnector()
        self.mastodon = MastodonConnector()
        self.devto = DevToConnector()
        self.lobsters = LobstersConnector()
        self.wikipedia = WikipediaConnector()
        self.discourse = DiscourseConnector()

        # self.reddit = RedditConnector()
        # self.youtube = YouTubeConnector()
        # self.news = NewsRssConnector()
        # self.hackernews = HackerNewsConnector()
        # self.lemmy = LemmyConnector()
        # self.hashnode = HashnodeConnector()
        # self.peertube = PeerTubeConnector()
        # self.producthunt = ProductHuntConnector()

    async def refresh_topic(self, session: AsyncSession, query: str, *, language: str | None = None) -> str:
        topic = await self.topic_repo.upsert(session, query)

        # Fetch from enabled connectors concurrently (but capped), falling back to
        # mock per-connector if disabled or fails. These used to run one-at-a-time
        # (14 sequential awaits), so total time was the SUM of every connector's
        # latency. A semaphore caps how many run at once — enough to be much
        # faster than fully sequential, without firing all 14 network requests
        # in one burst, which can starve other apps/services on the same machine.
        per = max(5, settings.max_source_items_per_connector)
        semaphore = asyncio.Semaphore(settings.max_concurrent_connectors)

        async def _bounded_fetch(connector, fallback_source: str):
            async with semaphore:
                return await self._safe_fetch(connector, query, per, fallback_source=fallback_source, language=language)

        fetch_plan = [
            (self.bluesky, "Bluesky"),
            (self.mastodon, "Mastodon"),
            (self.devto, "DevTo"),
            (self.lobsters, "Lobsters"),
            (self.wikipedia, "Wikipedia"),
            (self.discourse, "Discourse"),
            # (self.reddit, "Reddit"),
            # (self.youtube, "YouTube"),
            # (self.news, "News"),
            # (self.hackernews, "HackerNews"),
            # (self.lemmy, "Lemmy"),
            # (self.hashnode, "Hashnode"),
            # (self.peertube, "PeerTube"),
            # (self.producthunt, "ProductHunt"),
        ]

        results = await asyncio.gather(
            *(_bounded_fetch(connector, fallback_source) for connector, fallback_source in fetch_plan)
        )
        raw_items: list = [item for batch in results for item in batch]

        normalized = [self.normalizer.normalize(r) for r in raw_items]
        normalized_items = [n for n in normalized if n is not None]
        cleaned = self.cleaner.clean(normalized_items)

        # Store source items (ignore duplicates).
        db_items = [
            SourceItem(
                topic_id=topic.id,
                source=it.source,
                source_url=it.source_url,
                external_id=it.external_id,
                author=it.author,
                title=it.title,
                text=it.text,
                language=it.language,
                published_at=it.published_at,
                engagement_count=it.engagement_count,
            )
            for it in cleaned
        ]
        await self.source_repo.bulk_upsert_ignore_duplicates(session, db_items)

        # Prune anything older than the freshness window so cards are only
        # ever built from roughly "this month" of content, instead of every
        # item ever fetched for this topic since it was first searched.
        cutoff = datetime.utcnow() - timedelta(days=settings.source_freshness_days)
        await self.source_repo.delete_older_than(session, topic.id, cutoff)

        # Reload a larger raw pool ordered by recency, then cap how many items
        # any single source can contribute. Without this, a source that keeps
        # producing genuinely new items on every refresh (News) keeps pushing
        # fresh rows to the top of "most recently inserted", which over many
        # refreshes crowds sources with a fixed, static result set (Wikipedia,
        # Discourse, ProductHunt, etc.) out of the window entirely — even
        # though those items are still well within the freshness window.
        raw_pool = await self.source_repo.list_for_topic(session, topic.id, limit=2000)
        all_items = _balance_by_source(raw_pool, per_source_cap=settings.max_source_items_per_connector)
        ai_texts = [(i.text or i.title or "") for i in all_items if (i.text or i.title)]

        ai_res = self.ai.analyze_topic(topic.query, ai_texts)

        # Upsert TopicSummary
        themes_json = self.ai.dumps_themes(ai_res.themes)
        summary = await _upsert_summary(
            session,
            topic_id=topic.id,
            summary_text=ai_res.summary_text,
            sentiment_label=ai_res.sentiment_label,
            sentiment_score=ai_res.sentiment_score,
            breakdown=ai_res.breakdown,
            themes_json=themes_json,
        )

        # Rebuild pulse cards for a clean MVP experience.
        await self.card_repo.delete_for_topic(session, topic.id)

        # Map items into NormalizedItem-like shape for card builder.
        from app.services.normalization_service import NormalizedItem

        normalized_for_cards = [
            NormalizedItem(
                source=i.source,
                source_url=i.source_url,
                topic=topic.query,
                author=i.author,
                text=i.text,
                title=i.title,
                published_at=i.published_at,
                engagement_count=i.engagement_count,
                language=i.language,
                external_id=i.external_id,
            )
            for i in all_items
        ]

        drafts = self.card_builder.build_cards(topic.query, normalized_for_cards, max_cards=settings.pulse_cards_per_topic)

        cards: list[PulseCard] = []
        for d in drafts:
            if d.source_index < 0 or d.source_index >= len(all_items):
                continue
            src = all_items[d.source_index]
            cards.append(
                PulseCard(
                    topic_id=topic.id,
                    source_item_id=src.id,
                    quote=d.quote,
                    source=src.source,
                    theme=d.theme,
                    sentiment=d.sentiment,
                    language=src.language,
                    source_url=src.source_url,
                    engagement_count=src.engagement_count,
                    published_at=src.published_at,
                    display_label=d.display_label,
                )
            )

        await self.card_repo.bulk_create(session, cards)

        # Upsert per-source breakdown
        item_counts = await self.source_repo.count_by_source(session, topic.id)
        card_counts = await self.card_repo.count_by_source(session, topic.id)
        await _upsert_source_breakdown(session, topic.id, item_counts, card_counts)

        # Touch topic timestamp
        topic.updated_at = datetime.utcnow()
        session.add(topic)
        await session.commit()

        return topic.id

    async def _safe_fetch(self, connector, topic: str, limit: int, *, fallback_source: str, language: str | None = None) -> list:
        if settings.enable_mock_data:
            return [i for i in (await self.mock.fetch(topic, limit=limit, language=language)) if i.source == fallback_source][:limit]

        if not (await connector.enabled()):
            return []

        try:
            items = await connector.fetch(topic, limit=limit, language=language)
            return items[:limit] if items else []
        except Exception:
            return []


def _balance_by_source(items: list, *, per_source_cap: int) -> list:
    """Cap how many items any single source contributes, keeping each
    source's most recent items, then merge back together sorted by recency.
    Prevents a high-volume source (e.g. News, which finds new unique items
    on every refresh) from crowding lower-volume sources out entirely.
    """
    by_source: dict[str, list] = {}
    for it in items:
        by_source.setdefault(it.source, []).append(it)

    capped: list = []
    for src_items in by_source.values():
        capped.extend(src_items[:per_source_cap])

    capped.sort(key=lambda i: i.created_at, reverse=True)
    return capped


async def _upsert_summary(
    session: AsyncSession,
    *,
    topic_id: str,
    summary_text: str,
    sentiment_label: str,
    sentiment_score: float,
    breakdown: dict[str, float],
    themes_json: str,
) -> TopicSummary:
    from sqlalchemy import select

    res = await session.execute(select(TopicSummary).where(TopicSummary.topic_id == topic_id))
    existing = res.scalar_one_or_none()
    now = datetime.utcnow()
    if existing:
        existing.summary_text = summary_text
        existing.sentiment_label = sentiment_label
        existing.sentiment_score = sentiment_score
        existing.sentiment_positive = float(breakdown.get("positive", 0.0))
        existing.sentiment_neutral = float(breakdown.get("neutral", 0.0))
        existing.sentiment_negative = float(breakdown.get("negative", 0.0))
        existing.themes_json = themes_json
        existing.updated_at = now
        session.add(existing)
        await session.commit()
        await session.refresh(existing)
        return existing

    ts = TopicSummary(
        topic_id=topic_id,
        summary_text=summary_text,
        sentiment_label=sentiment_label,
        sentiment_score=sentiment_score,
        sentiment_positive=float(breakdown.get("positive", 0.0)),
        sentiment_neutral=float(breakdown.get("neutral", 0.0)),
        sentiment_negative=float(breakdown.get("negative", 0.0)),
        themes_json=themes_json,
        updated_at=now,
    )
    session.add(ts)
    await session.commit()
    await session.refresh(ts)
    return ts


async def _upsert_source_breakdown(
    session: AsyncSession,
    topic_id: str,
    item_counts: dict[str, int],
    card_counts: dict[str, int],
) -> None:
    from sqlalchemy import select

    sources = set(item_counts.keys()) | set(card_counts.keys()) | {
        "Reddit", "YouTube", "News", "Bluesky", "HackerNews", "Lemmy", "Mastodon",
        "DevTo", "Hashnode", "Lobsters", "PeerTube", "ProductHunt", "Wikipedia", "Discourse",
    }
    now = datetime.utcnow()
    for src in sources:
        res = await session.execute(
            select(SourceBreakdown).where(SourceBreakdown.topic_id == topic_id).where(SourceBreakdown.source == src)
        )
        existing = res.scalar_one_or_none()
        if existing:
            existing.item_count = int(item_counts.get(src, 0))
            existing.card_count = int(card_counts.get(src, 0))
            existing.updated_at = now
            session.add(existing)
            continue

        session.add(
            SourceBreakdown(
                topic_id=topic_id,
                source=src,
                item_count=int(item_counts.get(src, 0)),
                card_count=int(card_counts.get(src, 0)),
                updated_at=now,
            )
        )

    await session.commit()