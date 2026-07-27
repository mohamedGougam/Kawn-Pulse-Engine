from __future__ import annotations

import urllib.parse

import httpx

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.utils.date_utils import parse_datetime
from app.utils.text_utils import normalize_ws, strip_html


class WikipediaConnector:
    """Note on scope: this deliberately searches Wikipedia's *project*
    namespace (ns=4 — "Wikipedia:" pages, which is where community
    noticeboards like the Administrators' Noticeboard and Village Pump
    live), NOT general encyclopedia articles. Content here is editorial/
    procedural discussion among Wikipedia editors, not public sentiment
    about a topic — a genuinely different character than the other
    connectors. It will only surface something when a topic happens to be
    actively discussed *by Wikipedia editors themselves* (e.g. disputes
    over how an article should be written), which is comparatively rare.
    Uses Wikipedia's public search API, no authentication needed.
    """

    name = "Wikipedia"

    BASE_URL = "https://en.wikipedia.org/w/api.php"
    PROJECT_NAMESPACE = 4  # "Wikipedia:" — where noticeboards live

    async def enabled(self) -> bool:
        return not settings.enable_mock_data

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        params = {
            "action": "query",
            "list": "search",
            "srsearch": topic,
            "srnamespace": str(self.PROJECT_NAMESPACE),
            "srlimit": str(min(limit, 50)),
            "format": "json",
        }
        url = f"{self.BASE_URL}?{urllib.parse.urlencode(params)}"

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={"User-Agent": settings.reddit_user_agent})
            resp.raise_for_status()
            data = resp.json()

        results = ((data or {}).get("query") or {}).get("search") or []
        items: list[NormalizedRawItem] = []

        for r in results[:limit]:
            title = normalize_ws(r.get("title") or "")
            snippet = normalize_ws(strip_html(r.get("snippet") or ""))
            text = snippet or title
            if not text or not title:
                continue

            page_id = r.get("pageid")
            source_url = (
                f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
            )

            items.append(
                NormalizedRawItem(
                    source="Wikipedia",
                    source_url=source_url,
                    topic=topic,
                    author=None,  # noticeboard pages are multi-editor, no single "author"
                    title=title,
                    text=text,
                    published_at=parse_datetime(r.get("timestamp")),
                    engagement_count=None,
                    language="en",
                    external_id=str(page_id) if page_id is not None else None,
                )
            )

        return items[:limit]
