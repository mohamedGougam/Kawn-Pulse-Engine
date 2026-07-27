from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.connectors.bluesky_connector import BlueskyConnector
from app.connectors.devto_connector import DevToConnector
from app.connectors.discourse_connector import DiscourseConnector
from app.connectors.hackernews_connector import HackerNewsConnector
from app.connectors.hashnode_connector import HashnodeConnector
from app.connectors.lemmy_connector import LemmyConnector
from app.connectors.lobsters_connector import LobstersConnector
from app.connectors.mastodon_connector import MastodonConnector
from app.connectors.mock_connector import MockConnector
from app.connectors.peertube_connector import PeerTubeConnector
from app.connectors.producthunt_connector import ProductHuntConnector
from app.connectors.wikipedia_connector import WikipediaConnector
from app.connectors.news_connector import NewsRssConnector
from app.connectors.reddit_connector import RedditConnector
from app.connectors.youtube_connector import YouTubeConnector
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
        self.reddit = RedditConnector()
        self.youtube = YouTubeConnector()
        self.news = NewsRssConnector()
        self.bluesky = BlueskyConnector()
        self.hackernews = HackerNewsConnector()
        self.lemmy = LemmyConnector()
        self.mastodon = MastodonConnector()
        self.devto = DevToConnector()
        self.hashnode = HashnodeConnector()
        self.lobsters = LobstersConnector()
        self.peertube = PeerTubeConnector()
        self.producthunt = ProductHuntConnector()
        self.wikipedia = WikipediaConnector()
        self.discourse = DiscourseConnector()

    async def refresh_topic(self, session: AsyncSession, query: str, *, language: str | None = None) -> str:
        topic = await self.topic_repo.upsert(session, query)

        # Fetch from enabled connectors; fallback to mock per-connector if disabled or fails.
        raw_items = []
        per = max(5, settings.max_source_items_per_connector)

        raw_items += await self._safe_fetch(self.reddit, query, per, fallback_source="Reddit", language=language)
        raw_items += await self._safe_fetch(self.youtube, query, per, fallback_source="YouTube", language=language)
        raw_items += await self._safe_fetch(self.news, query, per, fallback_source="News", language=language)
        raw_items += await self._safe_fetch(self.bluesky, query, per, fallback_source="Bluesky", language=language)
        raw_items += await self._safe_fetch(self.hackernews, query, per, fallback_source="HackerNews", language=language)
        raw_items += await self._safe_fetch(self.lemmy, query, per, fallback_source="Lemmy", language=language)
        raw_items += await self._safe_fetch(self.mastodon, query, per, fallback_source="Mastodon", language=language)
        raw_items += await self._safe_fetch(self.devto, query, per, fallback_source="DevTo", language=language)
        raw_items += await self._safe_fetch(self.hashnode, query, per, fallback_source="Hashnode", language=language)
        raw_items += await self._safe_fetch(self.lobsters, query, per, fallback_source="Lobsters", language=language)
        raw_items += await self._safe_fetch(self.peertube, query, per, fallback_source="PeerTube", language=language)
        raw_items += await self._safe_fetch(self.producthunt, query, per, fallback_source="ProductHunt", language=language)
        raw_items += await self._safe_fetch(self.wikipedia, query, per, fallback_source="Wikipedia", language=language)
        raw_items += await self._safe_fetch(self.discourse, query, per, fallback_source="Discourse", language=language)

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

        # Reload a bounded set for AI & cards (including past items).
        all_items = await self.source_repo.list_for_topic(session, topic.id, limit=600)
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