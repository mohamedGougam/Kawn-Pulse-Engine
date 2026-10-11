from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.utils.date_utils import parse_datetime
from app.utils.http_errors import ConnectorUnavailable
from app.utils.text_utils import normalize_ws

# Why this logs in: Bluesky's public AppView (public.api.bsky.app) answers
# 403 to unauthenticated app.bsky.feed.searchPosts calls, which is why this
# connector had to be pulled out of the fetch plan. Post search works with a
# logged-in session, so the engine signs in with an app password
# (BLUESKY_HANDLE / BLUESKY_APP_PASSWORD) and sends searchPosts to the
# account's PDS, which proxies app.bsky.* calls to the AppView.
#
# Sessions are cached: createSession is rate-limited, and logging in on every
# search would also add a full extra round trip inside the per-connector
# timeout. A rejected login is remembered briefly so a wrong password isn't
# retried on every single search.

_session_lock = asyncio.Lock()
_access_jwt: str | None = None
_access_expires_at: float = 0.0          # time.monotonic()
_login_failed_until: float = 0.0
_login_failed_message: str = ""

_SESSION_SLACK_SECONDS = 120.0
_FALLBACK_SESSION_SECONDS = 1800.0
_LOGIN_RETRY_COOLDOWN_SECONDS = 60.0


def reset_session_cache() -> None:
    """Test helper."""
    global _access_jwt, _access_expires_at, _login_failed_until, _login_failed_message
    _access_jwt, _access_expires_at = None, 0.0
    _login_failed_until, _login_failed_message = 0.0, ""


def _jwt_seconds_remaining(token: str) -> float | None:
    """Read the `exp` claim without verifying the signature - we only want to
    know when to refresh our own token, not to trust it."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) - time.time() if exp else None
    except Exception:
        return None


async def _get_token(client: httpx.AsyncClient, *, force: bool = False) -> str:
    global _access_jwt, _access_expires_at, _login_failed_until, _login_failed_message

    if not force and _access_jwt and time.monotonic() < _access_expires_at:
        return _access_jwt

    async with _session_lock:
        if not force and _access_jwt and time.monotonic() < _access_expires_at:
            return _access_jwt

        if time.monotonic() < _login_failed_until:
            raise ConnectorUnavailable(_login_failed_message)

        base = settings.bluesky_service_url.rstrip("/")
        try:
            resp = await client.post(
                f"{base}/xrpc/com.atproto.server.createSession",
                json={"identifier": settings.bluesky_handle, "password": settings.bluesky_app_password},
            )
            resp.raise_for_status()
            token = (resp.json() or {}).get("accessJwt")
            if not token:
                raise ValueError("createSession response had no accessJwt")
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (400, 401, 403):
                _login_failed_until = time.monotonic() + _LOGIN_RETRY_COOLDOWN_SECONDS
                _login_failed_message = (
                    f"Bluesky login was rejected (HTTP {e.response.status_code}); check BLUESKY_HANDLE and "
                    "that BLUESKY_APP_PASSWORD is an app password. Not retrying for a minute."
                )
            raise

        ttl = _jwt_seconds_remaining(token) or _FALLBACK_SESSION_SECONDS
        _access_jwt = token
        _access_expires_at = time.monotonic() + max(0.0, ttl - _SESSION_SLACK_SECONDS)
        return token


def _token_expired(resp: httpx.Response) -> bool:
    if resp.status_code == 401:
        return True
    if resp.status_code == 400:
        try:
            return (resp.json() or {}).get("error") in {"ExpiredToken", "InvalidToken"}
        except ValueError:
            return False
    return False


class BlueskyConnector:
    name = "Bluesky"

    async def enabled(self) -> bool:
        return settings.bluesky_configured() and (not settings.enable_mock_data)

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        base = settings.bluesky_service_url.rstrip("/")
        url = f"{base}/xrpc/app.bsky.feed.searchPosts"
        params: dict[str, str | int] = {"q": topic, "limit": min(limit, 50)}
        if language:
            params["lang"] = language

        async with httpx.AsyncClient(timeout=15.0) as client:
            token = await _get_token(client)
            resp = await client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
            if _token_expired(resp):
                # Our cached token died early (revoked, clock skew). Log in
                # once more and retry once; a second failure is a real one.
                token = await _get_token(client, force=True)
                resp = await client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
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
