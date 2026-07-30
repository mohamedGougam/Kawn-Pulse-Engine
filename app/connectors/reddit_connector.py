from __future__ import annotations

import asyncio
import base64
import time
import urllib.parse

import httpx

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.utils.date_utils import utcnow
from app.utils.text_utils import normalize_ws

# Reddit's client_credentials tokens are typically valid for ~1 hour, but
# fetching a brand new one on every single search was doing two sequential
# network round-trips (token endpoint, then the search itself) inside the
# 3-second per-connector budget in config.py -- more than enough for one of
# those two calls alone to blow the whole timeout on a slow/rate-limited
# response, which is exactly what made Reddit look "broken" even with valid
# credentials. Caching the token in-process and only refreshing it shortly
# before it actually expires turns almost every search into a single round
# trip instead of two.
_token_lock = asyncio.Lock()
_cached_token: str | None = None
_cached_token_expires_at: float = 0.0
_TOKEN_REFRESH_SLACK_SECONDS = 60.0


class RedditConnector:
    name = "Reddit"

    async def enabled(self) -> bool:
        return settings.reddit_configured() and (not settings.enable_mock_data)

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        # Uses Reddit OAuth2 client credentials for basic search.
        # If this fails (rate-limit or invalid), the caller will fallback to mock.
        token = await _get_token()
        if not token:
            return []

        q = urllib.parse.quote_plus(topic)
        url = f"https://oauth.reddit.com/search?q={q}&sort=relevance&t=week&limit={min(limit, 50)}"

        headers = {
            "Authorization": f"bearer {token}",
            "User-Agent": settings.reddit_user_agent,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        children = (((data or {}).get("data") or {}).get("children") or [])
        items: list[NormalizedRawItem] = []

        for c in children[:limit]:
            post = (c or {}).get("data") or {}
            permalink = post.get("permalink")
            source_url = f"https://www.reddit.com{permalink}" if permalink else (post.get("url") or "")
            if not source_url:
                continue

            title = normalize_ws(post.get("title") or "")
            selftext = normalize_ws(post.get("selftext") or "")
            text = selftext or title
            if not text:
                continue

            created_utc = post.get("created_utc")
            published_at = utcnow()
            try:
                if created_utc:
                    from datetime import datetime

                    published_at = datetime.utcfromtimestamp(float(created_utc))
            except Exception:
                published_at = utcnow()

            items.append(
                NormalizedRawItem(
                    source="Reddit",
                    source_url=source_url,
                    topic=topic,
                    author=post.get("author") or None,
                    title=title or None,
                    text=text or None,
                    published_at=published_at,
                    engagement_count=int(post.get("score") or 0),
                    language=None,
                    external_id=str(post.get("id") or "") or None,
                )
            )

        return items[:limit]


async def _get_token() -> str | None:
    global _cached_token, _cached_token_expires_at

    if not settings.reddit_configured():
        return None

    # Fast path: reuse the cached token if it's not close to expiring yet.
    # No lock needed for the read -- worst case under a race is one extra
    # refresh, which is harmless.
    if _cached_token and time.monotonic() < _cached_token_expires_at:
        return _cached_token

    async with _token_lock:
        # Re-check after acquiring the lock in case a concurrent request
        # already refreshed it while we were waiting.
        if _cached_token and time.monotonic() < _cached_token_expires_at:
            return _cached_token

        auth = f"{settings.reddit_client_id}:{settings.reddit_client_secret}".encode("utf-8")
        basic = base64.b64encode(auth).decode("ascii")
        headers = {
            "Authorization": f"Basic {basic}",
            "User-Agent": settings.reddit_user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {"grant_type": "client_credentials"}

        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.post("https://www.reddit.com/api/v1/access_token", headers=headers, data=data)
                resp.raise_for_status()
                payload = resp.json()
                token = payload.get("access_token")
                expires_in = float(payload.get("expires_in") or 0)
                if token:
                    _cached_token = token
                    _cached_token_expires_at = time.monotonic() + max(0.0, expires_in - _TOKEN_REFRESH_SLACK_SECONDS)
                return token
            except Exception:
                _cached_token = None
                _cached_token_expires_at = 0.0
                return None

