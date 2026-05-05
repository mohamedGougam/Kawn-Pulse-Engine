from __future__ import annotations

import urllib.parse

import httpx

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.utils.date_utils import parse_datetime
from app.utils.text_utils import normalize_ws


class YouTubeConnector:
    name = "YouTube"

    async def enabled(self) -> bool:
        return settings.youtube_configured() and (not settings.enable_mock_data)

    async def fetch(self, topic: str, *, limit: int) -> list[NormalizedRawItem]:
        # Uses YouTube Data API v3 search endpoint.
        if not settings.youtube_api_key:
            return []

        q = urllib.parse.quote_plus(topic)
        url = (
            "https://www.googleapis.com/youtube/v3/search"
            f"?part=snippet&type=video&maxResults={min(limit, 50)}&q={q}&key={settings.youtube_api_key}"
        )

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        items = data.get("items") or []
        out: list[NormalizedRawItem] = []

        for it in items[:limit]:
            id_ = ((it.get("id") or {}).get("videoId")) or None
            snippet = it.get("snippet") or {}
            title = normalize_ws(snippet.get("title") or "")
            desc = normalize_ws(snippet.get("description") or "")
            published_at = parse_datetime(snippet.get("publishedAt") or None)
            channel = snippet.get("channelTitle") or None

            if not id_:
                continue
            source_url = f"https://www.youtube.com/watch?v={id_}"
            text = desc or title
            if not text:
                continue

            out.append(
                NormalizedRawItem(
                    source="YouTube",
                    source_url=source_url,
                    topic=topic,
                    author=channel,
                    title=title or None,
                    text=text or None,
                    published_at=published_at,
                    engagement_count=None,
                    language=None,
                    external_id=id_,
                )
            )

        return out[:limit]

