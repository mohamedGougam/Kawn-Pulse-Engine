from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.utils.date_utils import parse_datetime
from app.utils.http_errors import QUOTA_EXCEEDED, ConnectorUnavailable, describe_error
from app.utils.text_utils import normalize_ws

_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

# --- quota -----------------------------------------------------------------
# search.list costs 100 units and the default daily quota is 10,000, so the
# whole project gets ~100 searches a day. Background refreshes re-search the
# same topics all day, which exhausts that within hours; after that YouTube
# returns 403 quotaExceeded until midnight Pacific and the source silently
# contributed nothing. Two defences:
#   * a TTL cache per topic, so repeat refreshes don't spend quota;
#   * remembering that quota is exhausted, so we stop sending requests that
#     are certain to fail and say *why* (visible in /api/sources/health).
_cache: dict[tuple[str, str], tuple[float, list[NormalizedRawItem]]] = {}
_CACHE_MAX_ENTRIES = 200
_quota_exhausted_until: float = 0.0  # wall-clock epoch seconds


def reset_state() -> None:
    """Test helper."""
    global _quota_exhausted_until
    _cache.clear()
    _quota_exhausted_until = 0.0


def _next_quota_reset_epoch() -> float:
    """YouTube Data API quota resets at midnight Pacific Time."""
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("America/Los_Angeles"))
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return nxt.timestamp()
    except Exception:  # no tz database available: fall back to a short pause
        return time.time() + 6 * 3600


def _cache_get(key, *, allow_stale: bool = False):
    hit = _cache.get(key)
    if hit is None:
        return None
    expires_at, items = hit
    if allow_stale or time.monotonic() < expires_at:
        return items
    return None


def _cache_put(key, items: list[NormalizedRawItem]) -> None:
    ttl = settings.youtube_cache_ttl_seconds
    if ttl <= 0:
        return
    if len(_cache) >= _CACHE_MAX_ENTRIES and key not in _cache:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
    _cache[key] = (time.monotonic() + ttl, items)


class YouTubeConnector:
    name = "YouTube"

    async def enabled(self) -> bool:
        return settings.youtube_configured() and (not settings.enable_mock_data)

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        global _quota_exhausted_until

        # Uses YouTube Data API v3 search endpoint.
        if not settings.youtube_api_key:
            return []

        key = (topic.strip().lower(), language or "")

        fresh = _cache_get(key)
        if fresh is not None:
            return fresh[:limit]

        if time.time() < _quota_exhausted_until:
            stale = _cache_get(key, allow_stale=True)
            if stale is not None:
                return stale[:limit]  # old results beat none while quota is out
            resume = datetime.fromtimestamp(_quota_exhausted_until, timezone.utc).isoformat(timespec="minutes")
            raise ConnectorUnavailable(
                f"YouTube daily quota is exhausted (each search costs 100 of 10,000 units); resumes about {resume}"
            )

        params: dict[str, str | int] = {
            "part": "snippet",
            "type": "video",
            "maxResults": min(limit, 50),
            "q": topic,
        }
        if language:
            params["relevanceLanguage"] = language

        # The key goes in a header, not the query string: httpx puts the full
        # URL in its error messages, so a key in the URL ended up in the logs
        # on every failed call.
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(_SEARCH_URL, params=params, headers={"x-goog-api-key": settings.youtube_api_key})
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                if describe_error(e).kind == QUOTA_EXCEEDED:
                    _quota_exhausted_until = _next_quota_reset_epoch()
                raise
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

        out = out[:limit]
        _cache_put(key, out)
        return out
