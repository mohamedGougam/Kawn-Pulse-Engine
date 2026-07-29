from __future__ import annotations

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.connectors.reddit_connector import RedditConnector
from app.streaming.reddit_stream import buffer as reddit_buffer
from app.streaming.watchlist import watchlist


class RedditStreamConnector:
    """Same `fetch()` contract as RedditConnector, backed by the
    submissions+comments stream buffer when it's running, with the
    original OAuth search connector kept as a fallback — see
    BlueskyFirehoseConnector's docstring for why the fallback is needed
    even when the stream is healthy (cold-start topics)."""

    name = "Reddit"

    def __init__(self) -> None:
        self._poll_fallback = RedditConnector()

    async def enabled(self) -> bool:
        return await self._poll_fallback.enabled()

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        watchlist.register(topic)

        if settings.reddit_stream_enabled:
            buffered = reddit_buffer.get("Reddit", topic, limit=limit)
            if buffered:
                return buffered

        return await self._poll_fallback.fetch(topic, limit=limit, language=language)
