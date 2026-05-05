from __future__ import annotations

import urllib.parse

import feedparser
import httpx

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.utils.date_utils import parse_datetime
from app.utils.text_utils import normalize_ws


class NewsRssConnector:
    name = "News"

    async def enabled(self) -> bool:
        return True

    async def fetch(self, topic: str, *, limit: int) -> list[NormalizedRawItem]:
        items: list[NormalizedRawItem] = []
        feeds = settings.rss_feed_templates()

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            for tpl in feeds:
                url = tpl.format(query=urllib.parse.quote_plus(topic))
                try:
                    resp = await client.get(url, headers={"User-Agent": settings.reddit_user_agent})
                    resp.raise_for_status()
                except Exception:
                    continue

                parsed = feedparser.parse(resp.text)
                for e in parsed.entries[: max(0, limit - len(items))]:
                    source_url = getattr(e, "link", None) or getattr(e, "id", None) or url
                    title = normalize_ws(getattr(e, "title", "") or "")
                    summary = normalize_ws(getattr(e, "summary", "") or "")
                    published_at = parse_datetime(getattr(e, "published", None) or getattr(e, "updated", None))

                    text = summary if summary else title
                    if not text:
                        continue

                    items.append(
                        NormalizedRawItem(
                            source="News",
                            source_url=source_url,
                            topic=topic,
                            author=(getattr(e, "author", None) or None),
                            title=title or None,
                            text=text or None,
                            published_at=published_at,
                            engagement_count=None,
                            language=None,
                            external_id=None,
                        )
                    )

                if len(items) >= limit:
                    break

        return items[:limit]

