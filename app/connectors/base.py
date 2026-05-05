from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol


@dataclass
class NormalizedRawItem:
    source: str
    source_url: str
    topic: str
    author: Optional[str] = None
    text: Optional[str] = None
    title: Optional[str] = None
    published_at: Optional[datetime] = None
    engagement_count: Optional[int] = None
    language: Optional[str] = None
    external_id: Optional[str] = None


class SourceConnector(Protocol):
    name: str

    async def enabled(self) -> bool: ...

    async def fetch(self, topic: str, *, limit: int) -> list[NormalizedRawItem]: ...

