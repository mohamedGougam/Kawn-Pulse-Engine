from __future__ import annotations

import urllib.parse

import httpx

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.utils.date_utils import parse_datetime
from app.utils.text_utils import normalize_ws


class BlueskyConnector:
    name = "Bluesky"

    BASE_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"

    async def enabled(self) -> bool:
        return not settings.enable_mock_data

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        q = urllib.parse.quote_plus(topic)
        url = f"{self.BASE_URL}?q={q}&limit={min(limit, 50)}"
        if language:
            url += f"&lang={urllib.parse.quote_plus(language)}"

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={"User-Agent": settings.reddit_user_agent})
            resp.raise_for_status()
            data = resp.json()

        posts = data.get("posts") or []
        items: list[NormalizedRawItem] = []

        for p in posts[:limit]:
            record = p.get("record") or {}
            text = normalize_ws(record.get("text") or "")
            if not text:
                continue

            author = (p.get("author") or {}).get("handle") or None
            uri = p.get("uri") or ""
            cid = p.get("cid") or ""
            source_url = self._uri_to_url(uri, author)

            published_at = parse_datetime(record.get("createdAt"))

            engagement = (p.get("likeCount") or 0) + (p.get("repostCount") or 0) + (p.get("replyCount") or 0)

            items.append(
                NormalizedRawItem(
                    source="Bluesky",
                    source_url=source_url or "https://bsky.app",
                    topic=topic,
                    author=author,
                    title=None,
                    text=text,
                    published_at=published_at,
                    engagement_count=int(engagement) if engagement else None,
                    language=(record.get("langs") or [None])[0],
                    external_id=cid or None,
                )
            )

        return items[:limit]

    @staticmethod
    def _uri_to_url(uri: str, handle: str | None) -> str:
        if not uri or not handle:
            return ""
        if uri.startswith("at://"):
            try:
                rkey = uri.rstrip("/").split("/")[-1]
                return f"https://bsky.app/profile/{handle}/post/{rkey}"
            except Exception:
                return ""
        return uri
