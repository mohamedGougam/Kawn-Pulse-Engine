from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.connectors.base import NormalizedRawItem
from app.utils.text_utils import normalize_ws


@dataclass
class NormalizedItem:
    source: str
    source_url: str
    topic: str
    author: Optional[str]
    text: Optional[str]
    title: Optional[str]
    published_at: Optional[datetime]
    engagement_count: Optional[int]
    language: Optional[str]
    external_id: Optional[str]


class NormalizationService:
    def normalize(self, raw: NormalizedRawItem) -> NormalizedItem | None:
        source = normalize_ws(raw.source or "")
        source_url = normalize_ws(raw.source_url or "")
        topic = normalize_ws(raw.topic or "")

        if not source or not source_url or not topic:
            return None

        title = normalize_ws(raw.title or "") or None
        text = normalize_ws(raw.text or "") or None
        author = normalize_ws(raw.author or "") or None

        return NormalizedItem(
            source=source,
            source_url=source_url,
            topic=topic,
            author=author,
            text=text,
            title=title,
            published_at=raw.published_at,
            engagement_count=raw.engagement_count,
            language=raw.language,
            external_id=raw.external_id,
        )

