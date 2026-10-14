from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.services.outlet_registry import outlet_for_article_url
from app.utils.date_utils import parse_datetime
from app.utils.http_errors import ConnectorUnavailable, scrub
from app.utils.text_utils import normalize_ws, significant_tokens

# GDELT DOC 2.0 API: free, no key, news from monitored outlets worldwide.
# Facts this relies on (blog.gdeltproject.org, "GDELT DOC 2.0 API Debuts"):
#   * search operators such as sourcelang: go INSIDE the query string, not
#     as separate URL parameters;
#   * you can search all of GDELT's translated languages with plain English
#     keywords, so an English topic still finds French/Arabic/... coverage;
#   * only roughly the most recent 3 months are searchable;
#   * the service asks for at most about one request every 5 seconds per IP.
# It returns headlines and metadata, not article text, so a card built from a
# GDELT item quotes the headline.
_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
_MAX_RECORDS = 75          # conservative: sources disagree on the cap (75 vs 250)
_MAX_QUERY_TOKENS = 5      # keep queries short; every token is ANDed
_MIN_TOKEN_LENGTH = 3      # GDELT rejects keywords shorter than this
_THROTTLE_COOLDOWN_SECONDS = 30.0
_CACHE_MAX_ENTRIES = 200

# ISO 639-1 -> the language name GDELT's sourcelang: operator accepts.
_LANG_NAMES = {
    "en": "english", "fr": "french", "ar": "arabic", "es": "spanish", "de": "german",
    "pt": "portuguese", "it": "italian", "ru": "russian", "tr": "turkish", "he": "hebrew",
    "hi": "hindi", "ja": "japanese", "ko": "korean", "zh": "chinese", "nl": "dutch",
}
_ISO_BY_NAME = {name: iso for iso, name in _LANG_NAMES.items()}

# --- module state: spacing between calls, and a result cache ---------------
_next_slot: float = 0.0   # time.monotonic() when the next request may start
_cache: dict[str, tuple[float, list[NormalizedRawItem]]] = {}


def reset_state() -> None:
    """Test helper."""
    global _next_slot
    _next_slot = 0.0
    _cache.clear()


def build_query(topic: str, language: str | None) -> str | None:
    """Turn a topic into a GDELT query, or None if nothing searchable is left.

    Keywords are ANDed. Anything under 3 characters is dropped because GDELT
    answers a query containing one with an error message instead of results.
    The language operator comes from the request's language when it names
    one, else from settings.gdelt_default_language (so an English reader isn't
    handed headlines in scripts they can't read).
    """
    tokens = [t for t in significant_tokens(topic) if len(t) >= _MIN_TOKEN_LENGTH]
    tokens = list(dict.fromkeys(tokens))[:_MAX_QUERY_TOKENS]
    if not tokens:
        return None

    query = " ".join(tokens)
    if language:
        lang_name = _LANG_NAMES.get(language.lower()[:2])  # unknown language: no filter
    else:
        lang_name = (settings.gdelt_default_language or "").strip().lower() or None
    if lang_name:
        query += f" sourcelang:{lang_name}"
    return query


def _reserve_slot() -> float:
    """Claim the next permitted request time and return how long to wait for
    it. Raises instead of waiting if the queue of other GDELT calls ahead is
    longer than the connector could ever wait out (it is cancelled at
    connector_timeout_seconds), which turns "silently times out" into a
    visible, explained skip. No await between read and write, so concurrent
    callers each get a distinct slot."""
    global _next_slot
    now = time.monotonic()
    start_at = max(now, _next_slot)
    wait = start_at - now
    if wait > settings.gdelt_max_wait_seconds:
        raise ConnectorUnavailable(
            "GDELT allows about one request every 5 seconds per IP and other GDELT searches are queued "
            f"ahead of this one (would wait {wait:.0f}s); skipped this time"
        )
    _next_slot = start_at + settings.gdelt_min_interval_seconds
    return wait


def _cache_get(key: str):
    hit = _cache.get(key)
    if hit and time.monotonic() < hit[0]:
        return hit[1]
    return None


def _cache_put(key: str, items: list[NormalizedRawItem]) -> None:
    ttl = settings.gdelt_cache_ttl_seconds
    if ttl <= 0:
        return
    if len(_cache) >= _CACHE_MAX_ENTRIES and key not in _cache:
        _cache.pop(min(_cache, key=lambda k: _cache[k][0]), None)
    _cache[key] = (time.monotonic() + ttl, items)


_NON_WORD_RE = re.compile(r"\W+", re.UNICODE)


class GdeltConnector:
    name = "GDELT"

    async def enabled(self) -> bool:
        return settings.gdelt_enabled and (not settings.enable_mock_data)

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        global _next_slot

        query = build_query(topic, language)
        if query is None:
            return []

        cached = _cache_get(query)
        if cached is not None:
            return cached[:limit]

        wait = _reserve_slot()
        if wait > 0:
            await asyncio.sleep(wait)

        params = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": _MAX_RECORDS,
            "sort": "datedesc",
            "timespan": settings.gdelt_timespan,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.get(_URL, params=params, headers={"User-Agent": settings.reddit_user_agent})
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    _next_slot = max(_next_slot, time.monotonic() + _THROTTLE_COOLDOWN_SECONDS)
                raise

        text = resp.text.strip()
        if not text:
            return []  # GDELT answers a query with no matches with an empty body

        if "limit requests" in text.lower():
            # GDELT's throttle message is returned as plain text with HTTP 200.
            _next_slot = max(_next_slot, time.monotonic() + _THROTTLE_COOLDOWN_SECONDS)
            raise ConnectorUnavailable("GDELT throttled this IP (asked for at most one request per 5 seconds); backing off 30s")

        try:
            data = resp.json()
        except ValueError:
            # GDELT reports a rejected query (keyword too short, malformed
            # operator, ...) as plain text, not JSON. Surface its own words.
            raise ValueError(f"GDELT did not return JSON: {scrub(text, max_len=140)}") from None

        articles = data.get("articles") if isinstance(data, dict) else None
        if not articles:
            _cache_put(query, [])
            return []

        items = self._to_items(articles, topic)
        _cache_put(query, items)
        return items[:limit]

    @staticmethod
    def _to_items(articles: list[dict], topic: str) -> list[NormalizedRawItem]:
        items: list[NormalizedRawItem] = []
        seen_urls: set[str] = set()
        seen_titles: set[str] = set()

        for a in articles:
            if not isinstance(a, dict):
                continue
            url = (a.get("url") or "").strip()
            title = normalize_ws(a.get("title") or "")
            if not url or not title or url in seen_urls:
                continue

            # The same wire story is syndicated by dozens of outlets with an
            # identical headline; one copy is enough.
            title_key = _NON_WORD_RE.sub(" ", title.lower()).strip()
            if title_key in seen_titles:
                continue
            seen_urls.add(url)
            seen_titles.add(title_key)

            host = (a.get("domain") or urlparse(url).hostname or "").lower() or None
            # A recognised outlet keeps its own name, which is what makes the
            # same visibility rules, labels and per-source caps apply as for
            # that outlet's RSS items (a France24 article found through GDELT
            # is still France24-only). Anything else is generic "News".
            outlet = outlet_for_article_url(url) or "News"

            items.append(
                NormalizedRawItem(
                    source=outlet,
                    source_url=url,
                    topic=topic,
                    author=host,
                    title=title,
                    text=title,
                    published_at=parse_datetime(a.get("seendate")),
                    engagement_count=None,
                    language=_ISO_BY_NAME.get((a.get("language") or "").strip().lower()),
                    external_id=None,
                )
            )
        return items
