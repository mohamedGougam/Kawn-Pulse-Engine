from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import Column, DateTime, Index, String, Text, func
from sqlmodel import Field, SQLModel


def _uuid() -> str:
    return str(uuid4())


class Topic(SQLModel, table=True):
    __tablename__ = "topics"

    id: str = Field(default_factory=_uuid, primary_key=True)
    query: str = Field(index=True, min_length=1)
    created_at: datetime = Field(default_factory=datetime.utcnow, sa_column=Column(DateTime(timezone=False)))
    updated_at: datetime = Field(default_factory=datetime.utcnow, sa_column=Column(DateTime(timezone=False)))


class SourceItem(SQLModel, table=True):
    __tablename__ = "source_items"

    id: str = Field(default_factory=_uuid, primary_key=True)
    topic_id: str = Field(index=True)

    source: str = Field(sa_column=Column(String(40)))
    source_url: str = Field(sa_column=Column(String(1024)))
    external_id: Optional[str] = Field(default=None, sa_column=Column(String(128)))

    author: Optional[str] = Field(default=None, sa_column=Column(String(256)))
    title: Optional[str] = Field(default=None, sa_column=Column(String(512)))
    text: Optional[str] = Field(default=None, sa_column=Column(Text))
    language: Optional[str] = Field(default=None, sa_column=Column(String(16)))

    published_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=False)))
    engagement_count: Optional[int] = Field(default=None)

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=False), server_default=func.now()),
    )


Index("ix_source_items_topic_source_url", SourceItem.topic_id, SourceItem.source, SourceItem.source_url, unique=True)
Index("ix_source_items_source", SourceItem.source)
Index("ix_source_items_source_url", SourceItem.source_url)
Index("ix_source_items_external_id", SourceItem.external_id)


class TopicSummary(SQLModel, table=True):
    __tablename__ = "topic_summaries"

    id: str = Field(default_factory=_uuid, primary_key=True)
    topic_id: str = Field(index=True, unique=True)

    summary_text: str = Field(default="", sa_column=Column(Text))
    sentiment_label: str = Field(default="neutral", sa_column=Column(String(16)))
    sentiment_score: float = Field(default=0.0)

    sentiment_positive: float = Field(default=0.0)
    sentiment_neutral: float = Field(default=0.0)
    sentiment_negative: float = Field(default=0.0)

    themes_json: str = Field(default="[]", sa_column=Column(Text))  # list[str]

    updated_at: datetime = Field(default_factory=datetime.utcnow, sa_column=Column(DateTime(timezone=False)))


class PulseCard(SQLModel, table=True):
    __tablename__ = "pulse_cards"

    id: str = Field(default_factory=_uuid, primary_key=True)
    topic_id: str = Field(index=True)
    source_item_id: str = Field(index=True)

    quote: str = Field(sa_column=Column(Text))
    source: str = Field(sa_column=Column(String(40)))
    theme: Optional[str] = Field(default=None, sa_column=Column(String(120)))
    sentiment: str = Field(default="neutral", sa_column=Column(String(16)))
    language: Optional[str] = Field(default=None, sa_column=Column(String(8)))

    source_url: str = Field(sa_column=Column(String(1024)))
    engagement_count: Optional[int] = Field(default=None)
    published_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=False)))

    display_label: str = Field(default="Reaction", sa_column=Column(String(80)))
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=False), server_default=func.now()),
    )


class SourceBreakdown(SQLModel, table=True):
    __tablename__ = "source_breakdown"

    id: str = Field(default_factory=_uuid, primary_key=True)
    topic_id: str = Field(index=True)
    source: str = Field(sa_column=Column(String(40)))
    item_count: int = Field(default=0)
    card_count: int = Field(default=0)
    updated_at: datetime = Field(default_factory=datetime.utcnow, sa_column=Column(DateTime(timezone=False)))


Index("ix_source_breakdown_topic_source", SourceBreakdown.topic_id, SourceBreakdown.source, unique=True)
Index("ix_pulse_cards_source", PulseCard.source)

