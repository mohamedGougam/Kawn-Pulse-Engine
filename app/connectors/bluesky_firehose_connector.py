from __future__ import annotations

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.connectors.bluesky_connector import BlueskyConnector
from app.streaming.bluesky_firehose import buffer as firehose_buffer
from app.streaming.watchlist import watchlist


class BlueskyFirehoseConnector:
    """Same `fetch()` contract as BlueskyConnector, backed by the Jetstream
    firehose buffer when it's running, with the original poll-based search
    connector kept as a fallback.

    The fallback matters for two cases: (1) the firehose consumer is
    disabled or not yet running (bluesky_firehose_enabled=False, or the
    always-on process it needs isn't set up yet), and (2) a topic just
    got its very first search — the firehose only has data for topics it
    was already watching, so a brand-new topic legitimately has nothing
    buffered yet regardless of whether the firehose is healthy.
    """

    name = "Bluesky"

    def __init__(self) -> None:
        self._poll_fallback = BlueskyConnector()

    async def enabled(self) -> bool:
        return await self._poll_fallback.enabled()

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        watchlist.register(topic)

        if settings.bluesky_firehose_enabled:
            buffered = firehose_buffer.get("Bluesky", topic, limit=limit)
            if buffered:
                return buffered

        return await self._poll_fallback.fetch(topic, limit=limit, language=language)
