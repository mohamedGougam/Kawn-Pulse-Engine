from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


SentimentLabel = Literal["positive", "neutral", "negative"]


class Theme(BaseModel):
    name: str
    score: float = 0.0


class SentimentBreakdown(BaseModel):
    positive: float = Field(ge=0.0, le=1.0)
    neutral: float = Field(ge=0.0, le=1.0)
    negative: float = Field(ge=0.0, le=1.0)


class SourceBreakdownItem(BaseModel):
    source: str
    item_count: int
    card_count: int


class Topic(BaseModel):
    id: str
    query: str
    created_at: datetime
    updated_at: datetime


class SourceItem(BaseModel):
    id: str
    topic_id: str
    source: str
    source_url: str
    external_id: Optional[str] = None
    author: Optional[str] = None
    title: Optional[str] = None
    text: Optional[str] = None
    language: Optional[str] = None
    published_at: Optional[datetime] = None
    engagement_count: Optional[int] = None
    created_at: datetime


class PulseCard(BaseModel):
    id: str
    topic: str
    quote: str
    source: str
    sentiment: SentimentLabel
    theme: Optional[str] = None
    language: Optional[str] = None
    source_url: str
    engagement_count: Optional[int] = None
    published_at: Optional[datetime] = None
    display_label: str
    created_at: datetime
    # True only for explorer's cold-start preview cards (built straight
    # from cache, no AI) — see AggregationService.build_cache_preview_cards.
    # Absent/False for every normal DB-backed card.
    is_preview: bool = False


class TopicSummary(BaseModel):
    topic_id: str
    summary_text: str
    sentiment_label: SentimentLabel
    sentiment_score: float
    sentiment_breakdown: SentimentBreakdown
    themes: list[str]
    updated_at: datetime


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=160)
    language: Optional[str] = Field(default=None, max_length=8)


class SearchResponse(BaseModel):
    topic: Topic
    summary: TopicSummary
    source_breakdown: list[SourceBreakdownItem]
    cards: list[PulseCard]
    meta: dict[str, Any] = Field(default_factory=dict)

