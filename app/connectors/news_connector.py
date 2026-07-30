from __future__ import annotations

import asyncio
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


# Maps a distinctive fragment of each fixed outlet feed's URL (see
# settings.major_outlet_rss_feeds) to the publisher's display name, so
# items pulled from these feeds are attributed to the actual outlet
# ("BBC", "CNN", "Al Arabiya", "Euronews", ...) instead of the generic
# connector name "News". Checked with simple substring matching against
# the feed URL, not the entry/article URL, since outlets sometimes proxy
# article links through other domains.
_OUTLET_NAME_BY_URL_FRAGMENT: list[tuple[str, str]] = [
    ("bbci.co.uk", "BBC"),
    ("cnn.com", "CNN"),
    ("nytimes.com", "NYT"),
    ("aljazeera.com", "Al Jazeera"),
    ("alarabiya.net", "Al Arabiya"),
    ("euronews", "Euronews"),
    ("reuters.com", "Reuters"),
]


def _outlet_name_for_feed_url(feed_url: str) -> str | None:
    for fragment, outlet_name in _OUTLET_NAME_BY_URL_FRAGMENT:
        if fragment in feed_url:
            return outlet_name
    return None


class NewsRssConnector:
    name = "News"

    async def enabled(self) -> bool:
        return settings.news_enabled

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        hl, gl = _LANG_REGION.get((language or "").lower(), (None, None))

        # Query-templated feeds: Google News search + the Reuters workaround
        # (Reuters has published no public RSS since 2020, so this scopes a
        # Google News search to reuters.com instead of a dead feed URL).
        # Every entry here is already query-relevant, no client-side
        # filtering needed.
        templated_feeds = settings.rss_feed_templates() + [settings.reuters_rss_workaround_template]

        fetch_jobs: list[tuple[str, bool]] = []
        for tpl in templated_feeds:
            url = tpl.format(query=urllib.parse.quote_plus(topic))
            if hl and gl and "news.google.com" in url and "hl=" not in url:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}hl={hl}&gl={gl}&ceid={gl}:{hl}"
            fetch_jobs.append((url, False))

        # Fixed-URL outlet feeds (CNN/BBC/NYT/Al Jazeera): these publish
        # whole sections, not per-query search results, so every entry has
        # to be matched against the topic ourselves.
        for url in settings.major_outlet_feeds():
            fetch_jobs.append((url, True))

        # Fetch every feed concurrently rather than one at a time. With up
        # to 6 feed URLs here, awaiting them sequentially inside the shared
        # per-connector timeout (connector_timeout_seconds, currently 3s)
        # meant only the first feed or two ever had a real chance to finish
        # before the whole connector got cancelled -- firing them all at
        # once lets every feed race the same budget independently instead
        # of queueing behind each other.
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            results = await asyncio.gather(
                *(
                    self._fetch_feed(client, url, topic, language, require_match=require_match)
                    for url, require_match in fetch_jobs
                ),
                return_exceptions=True,
            )

        items: list[NormalizedRawItem] = []
        for res in results:
            if isinstance(res, Exception):
                continue
            items.extend(res)

        return items[:limit]

    async def _fetch_feed(
        self,
        client: httpx.AsyncClient,
        url: str,
        topic: str,
        language: str | None,
        *,
        require_match: bool,
    ) -> list[NormalizedRawItem]:
        try:
            resp = await client.get(url, headers={"User-Agent": settings.reddit_user_agent})
            resp.raise_for_status()
        except Exception:
            return []

        # Resolve the actual publisher from the feed URL itself where we
        # can (fixed outlet feeds, plus the Reuters-scoped Google News
        # workaround). Google News' own search feeds embed the originating
        # publisher per-entry (<source>), so fall back to that when the
        # feed URL itself doesn't identify a single outlet -- e.g. the
        # generic query-templated Google News search covers many
        # publishers at once and has nothing to key off of otherwise.
        feed_outlet_name = _outlet_name_for_feed_url(url)

        parsed = feedparser.parse(resp.text)
        items: list[NormalizedRawItem] = []
        for e in parsed.entries:
            source_url = getattr(e, "link", None) or getattr(e, "id", None) or url
            title = normalize_ws(getattr(e, "title", "") or "")
            summary = normalize_ws(getattr(e, "summary", "") or "")
            published_at = parse_datetime(getattr(e, "published", None) or getattr(e, "updated", None))

            text = summary if summary else title
            if not text:
                continue

            if require_match and not (topic_matches(topic, text) or topic_matches(topic, title)):
                continue

            entry_source = getattr(e, "source", None)
            entry_outlet_name = normalize_ws(getattr(entry_source, "title", "") or "") if entry_source else ""
            outlet_name = (feed_outlet_name or entry_outlet_name or "News")[:40]

            items.append(
                NormalizedRawItem(
                    source=outlet_name,
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

        return items

