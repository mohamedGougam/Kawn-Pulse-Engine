from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.db_models import Topic as TopicDB
from app.models.db_models import TopicSummary as TopicSummaryDB
from app.models.schemas import (
    PulseCard as PulseCardSchema,
    SearchRequest,
    SearchResponse,
    SentimentBreakdown,
    SourceBreakdownItem,
    Topic as TopicSchema,
    TopicSummary as TopicSummarySchema,
)
from app.repositories.pulse_card_repository import PulseCardRepository
from app.services.category_service import annotate_and_filter
from app.services.feed_diversity import mix_feed
from app.services.outlet_registry import filter_visible_cards
from app.repositories.topic_repository import TopicRepository
from app.services.aggregation_service import AggregationService
from app.services.refresh_gate import RefreshBusy
from app.services.ai_service import AIService

router = APIRouter(prefix="/api/topics", tags=["topics"])

topic_repo = TopicRepository()
card_repo = PulseCardRepository()
aggregation = AggregationService()
ai = AIService()


async def _session_dep() -> AsyncSession:
    async with get_session() as s:
        yield s


def _topic_schema(t: TopicDB) -> TopicSchema:
    return TopicSchema(id=t.id, query=t.query, created_at=t.created_at, updated_at=t.updated_at)


async def _get_summary(session: AsyncSession, topic_id: str) -> TopicSummaryDB | None:
    from sqlalchemy import select

    res = await session.execute(select(TopicSummaryDB).where(TopicSummaryDB.topic_id == topic_id))
    return res.scalar_one_or_none()


async def _get_source_breakdown(session: AsyncSession, topic_id: str) -> list[SourceBreakdownItem]:
    from sqlalchemy import select

    from app.models.db_models import SourceBreakdown

    res = await session.execute(select(SourceBreakdown).where(SourceBreakdown.topic_id == topic_id))
    rows = list(res.scalars().all())
    return [SourceBreakdownItem(source=r.source, item_count=r.item_count, card_count=r.card_count) for r in rows]


def _summary_schema(s: TopicSummaryDB) -> TopicSummarySchema:
    themes = ai.loads_themes(s.themes_json)
    breakdown = SentimentBreakdown(
        positive=float(s.sentiment_positive or 0.0),
        neutral=float(s.sentiment_neutral or 0.0),
        negative=float(s.sentiment_negative or 0.0),
    )
    return TopicSummarySchema(
        topic_id=s.topic_id,
        summary_text=s.summary_text,
        sentiment_label=s.sentiment_label,  # type: ignore[arg-type]
        sentiment_score=float(s.sentiment_score or 0.0),
        sentiment_breakdown=breakdown,
        themes=themes,
        updated_at=s.updated_at,
    )


_query_locks: dict[str, asyncio.Lock] = {}


def _query_lock(query: str) -> asyncio.Lock:
    """One lock per normalized query, so identical searches arriving together
    (double click, a retry after a slow response) run one live refresh and the
    rest are answered from its result instead of each doing a full fetch."""
    key = query.strip().lower()
    lock = _query_locks.get(key)
    if lock is None:
        if len(_query_locks) > 500:  # bounded: drop locks nobody holds
            for k in [k for k, v in _query_locks.items() if not v.locked()]:
                _query_locks.pop(k, None)
        lock = _query_locks[key] = asyncio.Lock()
    return lock


async def _search_response(
    session: AsyncSession, req: SearchRequest, topic: TopicDB, summary: TopicSummaryDB, meta: dict
) -> SearchResponse:
    cards_db = await card_repo.list_for_topic(session, topic.id, page=1, page_size=50)
    cards = [
        PulseCardSchema(
            id=c.id,
            topic=topic.query,
            quote=c.quote,
            source=c.source,
            sentiment=c.sentiment,  # type: ignore[arg-type]
            theme=c.theme,
            language=c.language,
            source_url=c.source_url,
            engagement_count=c.engagement_count,
            published_at=c.published_at,
            display_label=c.display_label,
            created_at=c.created_at,
        )
        for c in cards_db
    ]
    cards = annotate_and_filter(filter_visible_cards(cards, req.country), req.country, req.category)
    breakdown = await _get_source_breakdown(session, topic.id)
    return SearchResponse(
        topic=_topic_schema(topic),
        summary=_summary_schema(summary),
        source_breakdown=breakdown,
        cards=cards,
        meta=meta,
    )


@router.post("/search", response_model=SearchResponse)
async def search_topic(
    req: SearchRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(_session_dep),
) -> SearchResponse:
    import logging

    from app.config import settings

    logger = logging.getLogger("kawn.search")
    query = req.query.strip()
    language = req.language or None

    async with _query_lock(query):
        # Fast path: a topic that already has cards is answered from the
        # database right away. A live refresh fans out to ~15 sources and takes
        # 8-10s, which is what made every repeat search feel broken. If the
        # stored result is older than search_cache_fresh_seconds it is still
        # returned now, and refreshed in the background for next time. The
        # same filters (country gating, tab) are applied as on the live path.
        # A search that names a language always goes live, since language
        # changes what the news sources return.
        if settings.search_instant_cache_enabled and not language:
            existing = await topic_repo.get_by_query(session, query)
            if existing:
                summary = await _get_summary(session, existing.id)
                if summary and await card_repo.count_for_topic(session, existing.id) > 0:
                    age = (datetime.utcnow() - existing.updated_at).total_seconds() if existing.updated_at else 1e9
                    if age > settings.search_cache_fresh_seconds:
                        background_tasks.add_task(Backgroundrefresh, query)
                    await topic_repo.record_search(session, existing)
                    return await _search_response(
                        session, req, existing, summary,
                        {"refreshed": False, "instant_cache": True, "age_seconds": round(age, 1), "language": None},
                    )

        try:
            refresh_result = await aggregation.refresh_topic(session, query, language=language)
        except RefreshBusy:
            raise HTTPException(
                status_code=503,
                detail="The server is busy refreshing other topics; please retry in a few seconds.",
                headers={"Retry-After": "10"},
            )
        except Exception as e:
            logger.exception("refresh_topic failed for query=%r language=%r", query, language)
            raise HTTPException(status_code=500, detail=f"refresh_topic failed: {type(e).__name__}: {e}")

        topic = await topic_repo.get_by_id(session, refresh_result.topic_id)
        if not topic:
            raise HTTPException(status_code=500, detail="Topic not created")
        summary = await _get_summary(session, refresh_result.topic_id)
        if not summary:
            raise HTTPException(status_code=500, detail="Summary not created")

        return await _search_response(
            session, req, topic, summary,
            {
                "refreshed": True,
                "instant_cache": False,
                "language": language,
                "partial": refresh_result.partial,
                "missing_sources": refresh_result.missing_sources,
                "cached_sources": refresh_result.cached_sources,
                "source_freshness": refresh_result.source_freshness,
                "disabled_sources": refresh_result.disabled_sources,
                "timings": refresh_result.timings,
            },
        )


from pydantic import BaseModel

class Bulkcardsrequest(BaseModel):
    topic_ids: list[str]


async def Backgroundrefresh(query: str):
    # Every explore-feed request queues this for each Discover subject that
    # still has no cards. refresh_in_background skips a subject that is
    # already being refreshed or was tried within the last few minutes, so a
    # busy feed (or a subject that keeps failing and so keeps showing zero
    # cards) can no longer stack up dozens of full 15-source refreshes.
    await aggregation.refresh_in_background(query)


@router.get("/explore-feed", response_model=list[PulseCardSchema])
async def Getexplorefeed(
    background_tasks: BackgroundTasks,
    country: str | None = Query(default=None, max_length=2),
    category: str | None = Query(default=None, max_length=20, description="all | near_you | newsletters | social_media"),
    session: AsyncSession = Depends(_session_dep)
) -> list[PulseCardSchema]:
    from app.config import settings

    subjects = settings.Discover_subjects
    topics_db = []
    cold_start_subjects: list[str] = []
    for sub in subjects:
        topic = await topic_repo.get_by_query(session, sub)
        if not topic:
            try:
                topic = await topic_repo.upsert(session, sub)
                background_tasks.add_task(Backgroundrefresh, sub)
                cold_start_subjects.append(sub)
            except Exception:
                pass
        else:
            count = await card_repo.count_for_topic(session, topic.id)
            if count == 0:
                background_tasks.add_task(Backgroundrefresh, sub)
                cold_start_subjects.append(sub)
        if topic:
            topics_db.append(topic)

    if not topics_db:
        return []

    topic_map = {t.id: t.query for t in topics_db}
    topic_ids = list(topic_map.keys())

    cards_db = await card_repo.Listcardsformultipletopics(session, topic_ids, limit_per_topic=8)

    # Cold-start instant paint: for any Discover subject with 0 DB cards
    # right now (background refresh just kicked off above but hasn't
    # landed yet), pull whatever's already sitting in the R2 heavy-fetch
    # cache so the feed isn't empty for that subject on first load. These
    # are cheap, AI-free preview cards — real cards replace them once the
    # background refresh above completes.
    preview_results: list[PulseCardSchema] = []
    for sub in cold_start_subjects:
        try:
            previews = await aggregation.build_cache_preview_cards(sub, max_cards=8)
        except Exception:
            previews = []
        for p in previews:
            preview_results.append(
                PulseCardSchema(
                    id=f"preview:{p.source}:{p.source_url}",
                    topic=p.topic,
                    quote=p.quote,
                    source=p.source,
                    sentiment="neutral",
                    theme=None,
                    language=p.language,
                    source_url=p.source_url,
                    engagement_count=p.engagement_count,
                    published_at=p.published_at,
                    display_label=p.display_label,
                    created_at=p.published_at or datetime.utcnow(),
                    is_preview=True,
                )
            )

    results = [
        PulseCardSchema(
            id=c.id,
            topic=topic_map.get(c.topic_id, "General"),
            quote=c.quote,
            source=c.source,
            sentiment=c.sentiment,
            theme=c.theme,
            language=c.language,
            source_url=c.source_url,
            engagement_count=c.engagement_count,
            published_at=c.published_at,
            display_label=c.display_label,
            created_at=c.created_at,
        )
        for c in cards_db
    ]
    results.extend(preview_results)
    results = filter_visible_cards(results, country)
    results = annotate_and_filter(results, country, category)
    # Mix AFTER the tab filter so every tab is varied on its own, not just "All".
    # Replaces a plain random.shuffle, which let one source fill the feed.
    return mix_feed(results)


@router.post("/bulk-cards", response_model=list[PulseCardSchema])
async def Getbulkcards(
    req: Bulkcardsrequest,
    country: str | None = Query(default=None, max_length=2),
    category: str | None = Query(default=None, max_length=20),
    session: AsyncSession = Depends(_session_dep),
) -> list[PulseCardSchema]:
    if not req.topic_ids:
        return []

    topics_db = []
    for tid in req.topic_ids:
        topic = await topic_repo.get_by_id(session, tid)
        if topic:
            topics_db.append(topic)

    topic_map = {t.id: t.query for t in topics_db}
    cards_db = await card_repo.Listcardsformultipletopics(session, req.topic_ids, limit_per_topic=5)

    cards = [
        PulseCardSchema(
            id=c.id,
            topic=topic_map.get(c.topic_id, "General"),
            quote=c.quote,
            source=c.source,
            sentiment=c.sentiment,
            theme=c.theme,
            language=c.language,
            source_url=c.source_url,
            engagement_count=c.engagement_count,
            published_at=c.published_at,
            display_label=c.display_label,
            created_at=c.created_at,
        )
        for c in cards_db
    ]
    return annotate_and_filter(filter_visible_cards(cards, country), country, category)


@router.get("/{topic_id}/cards", response_model=list[PulseCardSchema])
async def get_cards(
    topic_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    country: str | None = Query(default=None, max_length=2),
    category: str | None = Query(default=None, max_length=20),
    session: AsyncSession = Depends(_session_dep),
) -> list[PulseCardSchema]:
    topic = await topic_repo.get_by_id(session, topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")

    cards_db = await card_repo.list_for_topic(session, topic_id, page=page, page_size=page_size)
    cards = [
        PulseCardSchema(
            id=c.id,
            topic=topic.query,
            quote=c.quote,
            source=c.source,
            sentiment=c.sentiment,  # type: ignore[arg-type]
            theme=c.theme,
            language=c.language,
            source_url=c.source_url,
            engagement_count=c.engagement_count,
            published_at=c.published_at,
            display_label=c.display_label,
            created_at=c.created_at,
        )
        for c in cards_db
    ]
    return annotate_and_filter(filter_visible_cards(cards, country), country, category)


@router.get("/trending", response_model=list[TopicSchema])
async def trending(session: AsyncSession = Depends(_session_dep)) -> list[TopicSchema]:
    topics = await topic_repo.list_trending(session, limit=25)
    return [_topic_schema(t) for t in topics]


@router.get("/{topic_id}", response_model=TopicSchema)
async def get_topic(topic_id: str, session: AsyncSession = Depends(_session_dep)) -> TopicSchema:
    topic = await topic_repo.get_by_id(session, topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")
    return _topic_schema(topic)


@router.post("/{topic_id}/refresh")
async def refresh(topic_id: str, session: AsyncSession = Depends(_session_dep)) -> dict:
    topic = await topic_repo.get_by_id(session, topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")

    try:
        await aggregation.refresh_topic(session, topic.query)
    except RefreshBusy:
        raise HTTPException(
            status_code=503,
            detail="The server is busy refreshing other topics; please retry in a few seconds.",
            headers={"Retry-After": "10"},
        )
    return {"status": "ok", "topic_id": topic_id, "refreshed": True}


@router.get("/cards/{card_id}", response_model=PulseCardSchema)
async def get_single_card(
    card_id: str,
    session: AsyncSession = Depends(_session_dep),
) -> PulseCardSchema:
    card = await card_repo.get_by_id(session, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    topic = await topic_repo.get_by_id(session, card.topic_id)
    topic_query = topic.query if topic else "General"

    single = PulseCardSchema(
        id=card.id,
        topic=topic_query,
        quote=card.quote,
        source=card.source,
        sentiment=card.sentiment,  # type: ignore[arg-type]
        theme=card.theme,
        language=card.language,
        source_url=card.source_url,
        engagement_count=card.engagement_count,
        published_at=card.published_at,
        display_label=card.display_label,
        created_at=card.created_at,
    )
    return annotate_and_filter([single], None)[0]


