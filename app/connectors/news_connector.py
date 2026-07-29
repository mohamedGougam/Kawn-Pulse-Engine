from __future__ import annotations

import urllib.parse

import feedparser
import httpx

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.utils.date_utils import parse_datetime
from app.utils.text_utils import normalize_ws, topic_matches


_LANG_REGION = {
    "en": ("en", "US"),
    "ar": ("ar", "SA"),
    "fr": ("fr", "FR"),
    "es": ("es", "ES"),
    "de": ("de", "DE"),
    "pt": ("pt", "BR"),
    "it": ("it", "IT"),
    "ru": ("ru", "RU"),
    "he": ("he", "IL"),
    "hi": ("hi", "IN"),
    "ja": ("ja", "JP"),
    "ko": ("ko", "KR"),
    "zh": ("zh", "CN"),
    "tr": ("tr", "TR"),
}


class NewsRssConnector:
    name = "News"

    async def enabled(self) -> bool:
        return True

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        items: list[NormalizedRawItem] = []
        hl, gl = _LANG_REGION.get((language or "").lower(), (None, None))

        # Query-templated feeds: Google News search + the Reuters workaround
        # (Reuters has published no public RSS since 2020, so this scopes a
        # Google News search to reuters.com instead of a dead feed URL).
        # Every entry here is already query-relevant, no client-side
        # filtering needed.
        templated_feeds = settings.rss_feed_templates() + [settings.reuters_rss_workaround_template]

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            for tpl in templated_feeds:
                if len(items) >= limit:
                    break
                url = tpl.format(query=urllib.parse.quote_plus(topic))
                if hl and gl and "news.google.com" in url and "hl=" not in url:
                    sep = "&" if "?" in url else "?"
                    url = f"{url}{sep}hl={hl}&gl={gl}&ceid={gl}:{hl}"
                await self._fetch_and_append(client, url, topic, language, items, limit, require_match=False)

            # Fixed-URL outlet feeds (CNN/BBC/NYT/Al Jazeera): these publish
            # whole sections, not per-query search results, so every entry
            # has to be matched against the topic ourselves.
            for url in settings.major_outlet_feeds():
                if len(items) >= limit:
                    break
                await self._fetch_and_append(client, url, topic, language, items, limit, require_match=True)

        return items[:limit]

    async def _fetch_and_append(
        self,
        client: httpx.AsyncClient,
        url: str,
        topic: str,
        language: str | None,
        items: list[NormalizedRawItem],
        limit: int,
        *,
        require_match: bool,
    ) -> None:
        try:
            resp = await client.get(url, headers={"User-Agent": settings.reddit_user_agent})
            resp.raise_for_status()
        except Exception:
            return

        parsed = feedparser.parse(resp.text)
        for e in parsed.entries:
            if len(items) >= limit:
                break

            source_url = getattr(e, "link", None) or getattr(e, "id", None) or url
            title = normalize_ws(getattr(e, "title", "") or "")
            summary = normalize_ws(getattr(e, "summary", "") or "")
            published_at = parse_datetime(getattr(e, "published", None) or getattr(e, "updated", None))

            text = summary if summary else title
            if not text:
                continue

            if require_match and not (topic_matches(topic, text) or topic_matches(topic, title)):
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
                    language=(language or None),
                    external_id=None,
                )
            )

