from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.connectors.bluesky_connector import BlueskyConnector
from app.connectors.devto_connector import DevToConnector
from app.connectors.hackernews_connector import HackerNewsConnector
from app.connectors.hashnode_connector import HashnodeConnector
from app.connectors.mastodon_connector import MastodonConnector
from app.connectors.mock_connector import MockConnector
from app.connectors.news_connector import NewsRssConnector
from app.connectors.reddit_connector import RedditConnector
from app.connectors.wikipedia_connector import WikipediaConnector
from app.connectors.youtube_connector import YouTubeConnector

# Active set matches the fast/heavy connector lists as specified:
#   fast:  Bluesky, Reddit, Mastodon, YouTube, HackerNews, Wikipedia
#          (X/Twitter has no connector -- no free streaming API available)
#   heavy: Dev.to, News (RSS -- Google News/Bing/Al Jazeera feed templates
#          via NEWS_RSS_FEEDS), Hashnode
#          (LinkedIn has no connector -- no free API available)
#
# Disabled -- code kept below, uncomment import + __init__ line + fetch_plan
# entry to re-enable (not on either requested list, kept as optional extras):
# from app.connectors.discourse_connector import DiscourseConnector
# from app.connectors.lobsters_connector import LobstersConnector
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
from app.services.normalization_service import NormalizationService, NormalizedItem
from app.services.pulse_card_service import PulseCardService, display_label_for_source
from app.storage.object_store import ObjectStoreUnavailable, object_store
from app.utils.date_utils import parse_datetime
from app.utils.text_utils import best_sentence, clean_text, is_low_quality, trim_text


logger = logging.getLogger("kawn.aggregation")


@dataclass
class RefreshResult:
    topic_id: str
    # Sources with no live data AND no cache fallback this refresh — the
    # visible signal for what used to be a silently-incomplete card set.
    missing_sources: list[str]
    # Sources where live fetch failed/timed out/returned nothing, but a
    # cached copy from a previous heavy fetch was used to backfill instead.
    cached_sources: list[str]
    # source -> ISO timestamp the cached copy was written (only present for
    # entries in `cached_sources` — live sources are fresh as of this
    # request, so there's nothing meaningful to timestamp for them). Lets a
    # client show e.g. "Bluesky results are from 4 hours ago" instead of
    # just a generic "may be stale" note.
    source_freshness: dict[str, str]

    @property
    def partial(self) -> bool:
        return bool(self.missing_sources)


@dataclass
class PreviewCard:
    """A card built entirely from cached data, no live fetch or AI
    inference — used only to give a cold-start explorer topic something
    to render on first paint while a real refresh happens in the
    background. Deliberately not persisted to the DB: once the background
    refresh completes, the real (AI-scored) cards take over on the next
    load. `sentiment` is always "neutral" here since it's a placeholder,
    not a model output — set `is_preview=True` downstream so clients don't
    mistake it for real sentiment."""
    topic: str
    quote: str
    source: str
    source_url: str
    display_label: str
    language: str | None
    engagement_count: int | None
    published_at: datetime | None


def _normalized_items_from_cache_payload(
    source: str, topic_query: str, cache_payload: dict | None, *, limit: int
) -> list[NormalizedItem]:
    """Turn a raw heavy-fetch cache payload (`{"items": [...]}`) into
    NormalizedItems for one source. Shared by the search-refresh cache
    merge and the explorer cold-start preview path so both interpret the
    cache file format the same way."""
    cache_items = (cache_payload or {}).get("items") or []
    out: list[NormalizedItem] = []
    for raw in cache_items[:limit]:
        source_url = raw.get("source_url") or ""
        if not source_url:
            continue
        out.append(
            NormalizedItem(
                source=source,
                source_url=source_url,
                topic=topic_query,
                author=raw.get("author"),
                text=raw.get("text"),
                title=raw.get("title"),
                published_at=_parse_iso(raw.get("published_at")),
                engagement_count=raw.get("engagement_count"),
                language=raw.get("language"),
                external_id=raw.get("external_id"),
            )
        )
    return out


def _dedup_key(item: NormalizedItem) -> tuple[str, str]:
    """Stable identity for an item, used to merge live + cached results
    without double-counting the same post/article fetched from both.
    Prefers the connector's own external_id; falls back to the URL, which
    every connector guarantees is present (normalize() rejects items without
    one)."""
    if item.external_id:
        return (item.source, item.external_id)
    return (item.source, item.source_url)


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
        self.wikipedia = WikipediaConnector()
        self.reddit = RedditConnector()
        self.youtube = YouTubeConnector()
        self.news = NewsRssConnector()
        self.hackernews = HackerNewsConnector()
        self.hashnode = HashnodeConnector()

        # Not on either requested connector list -- kept disabled, code
        # available if you want to re-add them as extras later.
        # self.discourse = DiscourseConnector()
        # self.lobsters = LobstersConnector()
        # self.lemmy = LemmyConnector()
        # self.peertube = PeerTubeConnector()
        # self.producthunt = ProductHuntConnector()

        # Single source of truth for which connectors are active, shared by
        # refresh_topic's live-fetch wave and build_cache_preview_cards'
        # cache-only read (the latter only needs the source names, not the
        # connector objects, but keeping one list means re-enabling a
        # connector here automatically covers both call sites).
        self.fetch_plan = [
            # -- fast tier --
            (self.bluesky, "Bluesky"),
            (self.reddit, "Reddit"),
            (self.mastodon, "Mastodon"),
            (self.youtube, "YouTube"),
            (self.hackernews, "HackerNews"),
            (self.wikipedia, "Wikipedia"),
            # -- heavy tier --
            (self.devto, "DevTo"),
            (self.news, "News"),
            (self.hashnode, "Hashnode"),
            # (self.discourse, "Discourse"),
            # (self.lobsters, "Lobsters"),
            # (self.lemmy, "Lemmy"),
            # (self.peertube, "PeerTube"),
            # (self.producthunt, "ProductHunt"),
        ]

    async def refresh_topic(
        self,
        session: AsyncSession,
        query: str,
        *,
        language: str | None = None,
        record_search: bool = True,
    ) -> RefreshResult:
        topic = await self.topic_repo.upsert(session, query)
        if record_search:
            # Only real user-triggered refreshes (search, manual refresh)
            # count toward the priority-queue interest signal — see
            # TopicRepository.record_search.
            await self.topic_repo.record_search(session, topic)

        # Fetch from enabled connectors fully concurrently — no semaphore cap.
        # These used to be capped at max_concurrent_connectors (3) at a time,
        # so with 9 connectors in fetch_plan a search could spend two extra
        # queueing waves behind that cap before ever reaching the slowest
        # source. Firing every connector at once instead means every source
        # gets a chance to answer within the same window, which is what
        # actually gives a search request variety across sources — the
        # trade-off is a bigger simultaneous request burst, offset below by
        # a shorter per-connector timeout so a stalled connector can't hold
        # up the batch for long.
        per = max(5, settings.max_source_items_per_connector)

        async def _bounded_fetch(connector, fallback_source: str):
            return await self._safe_fetch(connector, query, per, fallback_source=fallback_source, language=language)

        # Overall budget for this refresh's live-fetch stage. asyncio.gather
        # itself has no timeout, so without this a single connector whose own
        # http client is misbehaving (or the network being flaky) can stall
        # the whole search past _safe_fetch's per-connector timeout. wait_for
        # here is a backstop on top of the per-connector timeout below, not a
        # replacement for it.

        fetch_plan = self.fetch_plan

        task_by_source: dict[str, asyncio.Task] = {
            fallback_source: asyncio.create_task(_bounded_fetch(connector, fallback_source))
            for connector, fallback_source in fetch_plan
        }

        # Heavy-fetch cache reads run *in parallel* with the live fetch wave,
        # not after it — they're cheap R2 GETs and shouldn't add to the
        # live-fetch budget. Read for every source up front; only the ones
        # whose live fetch comes back empty actually get used below, but
        # kicking them all off now means the fallback is ready the instant
        # we know it's needed instead of paying R2 latency serially after.
        cache_task_by_source: dict[str, asyncio.Task] = {
            fallback_source: asyncio.create_task(self._read_cached_source(fallback_source, topic.query))
            for _, fallback_source in fetch_plan
        }

        done, pending = await asyncio.wait(list(task_by_source.values()), timeout=settings.search_fetch_budget_seconds)
        for t in pending:
            t.cancel()
        if pending:
            names = [source for source, t in task_by_source.items() if t in pending]
            logger.warning(
                "refresh_topic budget (%ss) exceeded for query=%r, skipping still-pending sources: %s",
                settings.search_fetch_budget_seconds, query, names,
            )

        live_items_by_source: dict[str, list] = {}
        for source, t in task_by_source.items():
            if t in pending:
                live_items_by_source[source] = []
                continue
            try:
                live_items_by_source[source] = t.result()
            except Exception:
                live_items_by_source[source] = []

        raw_items: list = [item for batch in live_items_by_source.values() for item in batch]

        normalized = [self.normalizer.normalize(r) for r in raw_items]
        normalized_items = [n for n in normalized if n is not None]
        live_cleaned = self.cleaner.clean(normalized_items)

        # Heavy-fetch cache: persist exactly what live fetch produced, one
        # JSON file per source, to R2. Fire-and-forget — a slow or failed
        # cache write must never slow down or fail the search itself.
        # Deliberately only the *live* items, not cache-fallback items added
        # below — otherwise a stale cache entry would keep re-writing itself
        # with a fresh `fetched_at`, making it look newer than it is.
        asyncio.create_task(self._persist_heavy_cache(topic.query, live_cleaned))

        # Merge in cached data for any source whose live fetch came back
        # empty (disabled, timed out, errored, or genuinely had nothing).
        # This is the fix for "cards sometimes incomplete": a connector
        # hiccup no longer means that source silently vanishes from the
        # card set this round, as long as *some* previous heavy fetch has
        # data for it. Sources still end up in `missing_sources` when even
        # the cache has nothing, so incompleteness is visible instead of
        # silent either way.
        cleaned = list(live_cleaned)
        seen_keys = {_dedup_key(it) for it in cleaned}
        empty_live_sources = [s for s, items in live_items_by_source.items() if not items]

        cached_sources: list[str] = []
        missing_sources: list[str] = []
        source_freshness: dict[str, str] = {}
        for source in empty_live_sources:
            cache_task = cache_task_by_source.get(source)
            cache_payload = None
            if cache_task is not None:
                try:
                    cache_payload = await asyncio.wait_for(cache_task, timeout=settings.heavy_cache_read_timeout_seconds)
                except Exception as e:
                    logger.warning("heavy cache read failed for source=%s topic=%r: %s", source, query, e)

            cache_items = _normalized_items_from_cache_payload(source, topic.query, cache_payload, limit=per)
            added_any = False
            for item in cache_items:
                key = _dedup_key(item)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                cleaned.append(item)
                added_any = True

            if added_any:
                cached_sources.append(source)
                fetched_at = (cache_payload or {}).get("fetched_at")
                if fetched_at:
                    source_freshness[source] = fetched_at
            else:
                missing_sources.append(source)

        # Any cache reads for sources that turned out not to need them
        # (live succeeded) are still running in the background — let them
        # finish naturally rather than cancelling mid-flight; they're cheap
        # and cancelling a completed-or-nearly-complete GET buys nothing.

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

        # analyze_topic runs real model inference (sentiment over up to 60
        # texts, summarization over up to 40) when the transformers models
        # are loaded — synchronous, CPU-bound calls. Run off the event loop
        # so a slow/heavy search doesn't freeze the whole process for every
        # other concurrent request (including unrelated health checks and
        # the cron-tick endpoint) for the duration of inference.
        ai_res = await asyncio.to_thread(self.ai.analyze_topic, topic.query, ai_texts)

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

        # build_cards also runs blocking sentiment inference internally (per
        # candidate card, up to max_cards*2 texts) — same reasoning as above.
        drafts = await asyncio.to_thread(
            self.card_builder.build_cards,
            topic.query,
            normalized_for_cards,
            max_cards=settings.pulse_cards_per_topic,
        )

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

        return RefreshResult(
            topic_id=topic.id,
            missing_sources=missing_sources,
            cached_sources=cached_sources,
            source_freshness=source_freshness,
        )

    async def _read_cached_source(self, source: str, topic: str) -> dict | None:
        """Read one source's heavy-fetch cache file. Returns the raw payload
        dict (`source`, `topic`, `fetched_at`, `items`) or None on any miss
        or failure — callers treat that the same as "nothing cached"."""
        try:
            return await object_store.get_json(source, topic)
        except ObjectStoreUnavailable:
            return None

    async def build_cache_preview_cards(self, topic_query: str, *, max_cards: int = 8) -> list[PreviewCard]:
        """Explorer's cold-start instant-paint: for a topic with no cards
        in Postgres yet, build a preview card set straight from whatever's
        already sitting in the R2 heavy-fetch cache — no live fetch, no AI
        inference, so this is cheap enough to run inline in a GET request.
        Returns [] immediately if R2 isn't configured or nothing's cached
        yet, which is exactly today's cold-start behavior (empty this
        round, populated by the background refresh for next time) — so
        this is a pure addition with no regression risk for that case.

        Deliberately separate from refresh_topic's cache-merge path: that
        one merges cache into a *live* fetch and needs the full dedup /
        missing-sources bookkeeping; this one has no live data to merge
        against and skips AI, so it doesn't fit that method's shape.
        """
        if not settings.r2_configured():
            return []

        source_names = [name for _, name in self.fetch_plan]
        read_tasks = {name: asyncio.create_task(self._read_cached_source(name, topic_query)) for name in source_names}

        # Bounded the same way the search merge path bounds a cache read —
        # these are cheap R2 GETs, but explorer is a GET the client expects
        # to be fast, so a slow/hanging R2 must not block the feed.
        done, pending = await asyncio.wait(list(read_tasks.values()), timeout=settings.heavy_cache_read_timeout_seconds)
        for t in pending:
            t.cancel()

        items: list[NormalizedItem] = []
        seen_keys: set[tuple[str, str]] = set()
        for source, task in read_tasks.items():
            if task in pending:
                continue
            try:
                payload = task.result()
            except Exception:
                continue
            for item in _normalized_items_from_cache_payload(source, topic_query, payload, limit=max_cards * 2):
                key = _dedup_key(item)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                items.append(item)

        if not items:
            return []

        # Same round-robin-across-sources shape as PulseCardService.build_cards,
        # just without the AI sentiment pass — quote quality filtering reuses
        # the same text_utils helpers so preview cards read the same as real
        # ones, only the sentiment badge is a placeholder.
        by_source: dict[str, list[NormalizedItem]] = {}
        for it in items:
            by_source.setdefault(it.source, []).append(it)

        ordered_sources = sorted(by_source.keys(), key=lambda s: -len(by_source[s]))
        cursors = {s: 0 for s in ordered_sources}
        picked: list[NormalizedItem] = []
        while len(picked) < max_cards * 2:
            progressed = False
            for src in ordered_sources:
                if cursors[src] < len(by_source[src]):
                    picked.append(by_source[src][cursors[src]])
                    cursors[src] += 1
                    progressed = True
                    if len(picked) >= max_cards * 2:
                        break
            if not progressed:
                break

        drafts: list[PreviewCard] = []
        for it in picked:
            raw = it.text or it.title or ""
            cleaned = clean_text(raw)
            quote = best_sentence(cleaned, max_len=220) if cleaned else ""

            if not quote or is_low_quality(quote, min_len=12, min_words=2):
                title_clean = clean_text(it.title or "")
                if title_clean and not is_low_quality(title_clean, min_len=12, min_words=2):
                    quote = title_clean
                else:
                    continue

            quote = trim_text(quote, max_len=220)

            drafts.append(
                PreviewCard(
                    topic=topic_query,
                    quote=quote,
                    source=it.source,
                    source_url=it.source_url,
                    display_label=display_label_for_source(it.source),
                    language=it.language,
                    engagement_count=it.engagement_count,
                    published_at=it.published_at,
                )
            )
            if len(drafts) >= max_cards:
                break

        return drafts

    async def _persist_heavy_cache(self, topic: str, items: list) -> None:
        if not settings.r2_configured():
            return

        by_source: dict[str, list] = {}
        for it in items:
            by_source.setdefault(it.source, []).append(it)

        async def _write_source(source: str, source_items: list) -> None:
            payload = [
                {
                    "source_url": i.source_url,
                    "author": i.author,
                    "text": i.text,
                    "title": i.title,
                    "published_at": i.published_at.isoformat() if i.published_at else None,
                    "engagement_count": i.engagement_count,
                    "language": i.language,
                    "external_id": i.external_id,
                }
                for i in source_items
            ]
            try:
                await object_store.put_json(source, topic, payload)
            except ObjectStoreUnavailable:
                pass
            except Exception as e:
                logger.warning("heavy cache write failed for source=%s topic=%r: %s", source, topic, e)

        await asyncio.gather(*(_write_source(s, its) for s, its in by_source.items()), return_exceptions=True)

    async def _safe_fetch(self, connector, topic: str, limit: int, *, fallback_source: str, language: str | None = None) -> list:
        if settings.enable_mock_data:
            return [i for i in (await self.mock.fetch(topic, limit=limit, language=language)) if i.source == fallback_source][:limit]

        if not (await connector.enabled()):
            return []

        try:
            items = await asyncio.wait_for(
                connector.fetch(topic, limit=limit, language=language),
                timeout=settings.connector_timeout_seconds,
            )
            return items[:limit] if items else []
        except asyncio.TimeoutError:
            logger.warning(
                "%s timed out after %ss for query=%r",
                fallback_source, settings.connector_timeout_seconds, topic,
            )
            return []
        except Exception as e:
            logger.warning("%s fetch failed for query=%r: %s", fallback_source, topic, e)
            return []


def _parse_iso(value: str | None) -> datetime | None:
    # Delegates to the codebase's shared parser (app.utils.date_utils),
    # which normalizes any tz-aware input to naive UTC. Cached items'
    # published_at strings can carry a timezone offset (real connectors
    # produce tz-aware datetimes; _persist_heavy_cache writes whatever
    # they gave us via .isoformat()), and mixing a tz-aware value here
    # with the naive `cutoff` in SourceItemRepository.delete_older_than
    # raises "can't compare offset-naive and offset-aware datetimes" the
    # moment SQLAlchemy evaluates that delete in-Python against a
    # pending object in the same session — this bit only with real
    # tz-aware data, not the naive mock-data timestamps used to smoke
    # test this path before.
    return parse_datetime(value)


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