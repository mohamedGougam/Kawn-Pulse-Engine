from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
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
from app.repositories.topic_repository import TopicRepository
from app.services.aggregation_service import AggregationService
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


@router.post("/search", response_model=SearchResponse)
async def search_topic(req: SearchRequest, session: AsyncSession = Depends(_session_dep)) -> SearchResponse:
    import logging
    import traceback
    logger = logging.getLogger("kawn.search")

    try:
        topic_id = await aggregation.refresh_topic(session, req.query, language=(req.language or None))
    except Exception as e:
        logger.exception("refresh_topic failed for query=%r language=%r", req.query, req.language)
        raise HTTPException(status_code=500, detail=f"refresh_topic failed: {type(e).__name__}: {e}")

    topic = await topic_repo.get_by_id(session, topic_id)
    if not topic:
        raise HTTPException(status_code=500, detail="Topic not created")

    summary = await _get_summary(session, topic_id)
    if not summary:
        raise HTTPException(status_code=500, detail="Summary not created")

    cards_db = await card_repo.list_for_topic(session, topic_id, page=1, page_size=50)
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

    breakdown = await _get_source_breakdown(session, topic_id)

    return SearchResponse(
        topic=_topic_schema(topic),
        summary=_summary_schema(summary),
        source_breakdown=breakdown,
        cards=cards,
        meta={"refreshed": True, "language": (req.language or None)},
    )


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


@router.get("/{topic_id}/cards", response_model=list[PulseCardSchema])
async def get_cards(
    topic_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(_session_dep),
) -> list[PulseCardSchema]:
    topic = await topic_repo.get_by_id(session, topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")

    cards_db = await card_repo.list_for_topic(session, topic_id, page=page, page_size=page_size)
    return [
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


@router.post("/{topic_id}/refresh")
async def refresh(topic_id: str, session: AsyncSession = Depends(_session_dep)) -> dict:
    topic = await topic_repo.get_by_id(session, topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail="Topic not found")

    await aggregation.refresh_topic(session, topic.query)
    return {"status": "ok", "topic_id": topic_id, "refreshed": True}

