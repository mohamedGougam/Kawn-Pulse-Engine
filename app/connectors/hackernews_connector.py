from __future__ import annotations

import urllib.parse

import httpx

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.utils.date_utils import parse_datetime
from app.utils.text_utils import normalize_ws


class HackerNewsConnector:
    name = "HackerNews"

    BASE_URL = "https://hn.algolia.com/api/v1/search"

    async def enabled(self) -> bool:
        return not settings.enable_mock_data

    async def fetch(self, topic: str, *, limit: int) -> list[NormalizedRawItem]:
        q = urllib.parse.quote_plus(topic)
        url = f"{self.BASE_URL}?query={q}&tags=(story,comment)&hitsPerPage={min(limit, 50)}"

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={"User-Agent": settings.reddit_user_agent})
            resp.raise_for_status()
            data = resp.json()

        hits = data.get("hits") or []
        items: list[NormalizedRawItem] = []

        for h in hits[:limit]:
            obj_id = h.get("objectID")
            tags = h.get("_tags") or []

            if "comment" in tags:
                text = normalize_ws(h.get("comment_text") or "")
                title = None
                source_url = f"https://news.ycombinator.com/item?id={obj_id}" if obj_id else ""
            else:
                title = normalize_ws(h.get("title") or h.get("story_title") or "")
                text = normalize_ws(h.get("story_text") or "") or title
                story_url = h.get("url")
                source_url = story_url or (f"https://news.ycombinator.com/item?id={obj_id}" if obj_id else "")

            if not text and not title:
                continue
            if not source_url:
                continue

            engagement = h.get("points") or h.get("num_comments") or 0
            published_at = parse_datetime(h.get("created_at"))
            author = h.get("author") or None

            items.append(
                NormalizedRawItem(
                    source="HackerNews",
                    source_url=source_url,
                    topic=topic,
                    author=author,
                    title=title,
                    text=text or title,
                    published_at=published_at,
                    engagement_count=int(engagement) if engagement else None,
                    language=None,
                    external_id=str(obj_id) if obj_id else None,
                )
            )

        return items[:limit]
