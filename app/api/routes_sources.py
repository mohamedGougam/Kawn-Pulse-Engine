from __future__ import annotations

import asyncio
import re
import time

from fastapi import APIRouter, Header, HTTPException, Query

from app.config import settings
from app.services import connector_health
from app.services.connector_health import DISABLED, EMPTY, ERROR, OK, TIMEOUT, explain_disabled
from app.utils.http_errors import (
    AUTH_REJECTED,
    NETWORK,
    QUOTA_EXCEEDED,
    RATE_LIMITED,
    SERVER_ERROR,
    UNAVAILABLE,
    describe_error,
)

router = APIRouter(prefix="/api/sources", tags=["sources"])

# Reddit asks API clients for "<platform>:<app ID>:<version> (by /u/<username>)"
# and throttles generic/shared User-Agents harder.
_REDDIT_UA_RE = re.compile(r"^[^:\s]+:[^:\s]+:\S+ \(by /u/[^)\s]+\)$")

# What to do about a failure, by (source, kind), falling back to (kind). These
# are the causes that actually apply to these APIs as of 2026, not generic
# advice - see the connector modules for the reasoning.
_HINTS: dict[tuple[str | None, str], str] = {
    ("Reddit", AUTH_REJECTED): (
        "Reddit rejected the request. Check REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET. New Reddit API apps need "
        "approval under the Responsible Builder Policy before they work; apps created earlier still do. A 403 "
        "with valid credentials can also mean a generic User-Agent - see warnings."
    ),
    ("Bluesky", AUTH_REJECTED): (
        "Bluesky rejected the login or token. Check BLUESKY_HANDLE and that BLUESKY_APP_PASSWORD is an *app "
        "password* (Settings > App Passwords), not the account password."
    ),
    ("YouTube", AUTH_REJECTED): "The YouTube API key is invalid, restricted, or the YouTube Data API v3 isn't enabled for it.",
    ("YouTube", QUOTA_EXCEEDED): (
        "YouTube's daily quota is used up (a search costs 100 of 10,000 units). It resets at midnight Pacific. "
        "Raise the quota in Google Cloud, or lengthen YOUTUBE_CACHE_TTL_SECONDS."
    ),
    ("Mastodon", AUTH_REJECTED): (
        "This instance requires login even for hashtag timelines (public preview disabled). Point "
        "MASTODON_INSTANCE_URL at an instance that allows it."
    ),
    (None, RATE_LIMITED): "Rate limited by the source. Reduce how often it's called or wait before retrying.",
    (None, SERVER_ERROR): "The source's own servers are failing; nothing to fix on this side.",
    (None, NETWORK): "Couldn't reach the source from this host (DNS/connection). Check the service's outbound network.",
    (None, UNAVAILABLE): "The connector knows it would fail right now and is not sending requests; see the message.",
}


def _hint(source: str, kind: str) -> str | None:
    return _HINTS.get((source, kind)) or _HINTS.get((None, kind))


def _require_secret(x_cron_secret: str | None) -> None:
    # Same shared secret as /internal/cron-tick. These endpoints reveal
    # operational detail and the diagnostics one spends real API quota (a
    # YouTube probe is 100 units), so they must not be public.
    if not settings.cron_heartbeat_secret:
        raise HTTPException(status_code=503, detail="cron_heartbeat_secret not configured")
    if x_cron_secret != settings.cron_heartbeat_secret:
        raise HTTPException(status_code=401, detail="invalid cron secret")


def _is_configured(name: str) -> bool:
    n = name.lower()
    if n == "reddit":
        return settings.reddit_configured()
    if n == "youtube":
        return settings.youtube_configured()
    if n == "bluesky":
        return settings.bluesky_configured()
    if n == "producthunt":
        return settings.producthunt_configured()
    return True


def _plan():
    # Imported lazily: routes_topics builds the AggregationService at import.
    from app.api.routes_topics import aggregation

    return aggregation.fetch_plan


@router.get("/status")
async def sources_status() -> dict:
    """Configuration view. Built from the connectors the aggregator actually
    calls (its fetch_plan), so it can no longer list a source as enabled
    while nothing is calling it. For what is *really happening*, see
    /api/sources/health."""
    connectors: dict[str, dict] = {}
    for connector, name in _plan():
        try:
            enabled = bool(await connector.enabled())
        except Exception:
            enabled = False
        connectors[name] = {
            "enabled": enabled,
            "configured": _is_configured(name),
            "disabled_reason": None if enabled else explain_disabled(name),
            "in_fetch_plan": True,
        }
    return {"enable_mock_data": settings.enable_mock_data, "connectors": connectors}


@router.get("/health")
async def sources_health(x_cron_secret: str | None = Header(default=None)) -> dict:
    """What happened the last time each source was actually called by a real
    search or background refresh. Costs nothing: it only reads what the
    aggregator already recorded. Empty until sources have been called since
    the last restart."""
    _require_secret(x_cron_secret)
    return {"sources": connector_health.snapshot()}


async def _probe(connector, name: str, topic: str) -> dict:
    result: dict = {"source": name}
    try:
        enabled = bool(await connector.enabled())
    except Exception as e:
        info = describe_error(e)
        return {**result, "status": ERROR, "error": info.as_dict()}

    if not enabled:
        return {
            **result,
            "status": DISABLED,
            "reason": explain_disabled(name) or "switched off by the connector's own enabled() check",
        }

    started = time.monotonic()
    try:
        items = await asyncio.wait_for(
            connector.fetch(topic, limit=5, language=None), timeout=settings.connector_timeout_seconds
        )
    except asyncio.TimeoutError:
        info = describe_error(asyncio.TimeoutError())
        connector_health.record_failure(name, info, started, timed_out=True)
        return {**result, "status": TIMEOUT, "latency_ms": int((time.monotonic() - started) * 1000), "error": info.as_dict()}
    except Exception as e:
        info = describe_error(e)
        connector_health.record_failure(name, info, started)
        out = {**result, "status": ERROR, "latency_ms": int((time.monotonic() - started) * 1000), "error": info.as_dict()}
        hint = _hint(name, info.kind)
        if hint:
            out["hint"] = hint
        return out

    connector_health.record_success(name, len(items), started)
    out = {**result, "status": OK if items else EMPTY, "items": len(items), "latency_ms": int((time.monotonic() - started) * 1000)}
    if items:
        first = items[0]
        out["sample"] = {"url": first.source_url, "text": (first.text or first.title or "")[:100]}
    else:
        out["hint"] = "The call succeeded but nothing matched this topic. Try a broader topic before concluding it's broken."
    return out


@router.get("/diagnostics")
async def sources_diagnostics(
    topic: str = Query("artificial intelligence", min_length=1, max_length=160),
    only: str | None = Query(default=None, description="Comma-separated source names to probe, e.g. Reddit,Bluesky"),
    x_cron_secret: str | None = Header(default=None),
) -> dict:
    """Actively calls each source once with a small query and reports, per
    source: disabled (and why), ok, empty, timeout, or error (with a safe,
    classified message and a hint).

    This SPENDS quota: a YouTube probe costs 100 units, and a paid source
    would bill. Use `only=` to probe just the one you're debugging, and use
    /api/sources/health for routine monitoring.
    """
    _require_secret(x_cron_secret)

    wanted = {s.strip().lower() for s in only.split(",") if s.strip()} if only else None
    plan = [(c, n) for c, n in _plan() if wanted is None or n.lower() in wanted]
    if wanted and not plan:
        raise HTTPException(status_code=404, detail=f"no such source in the fetch plan: {only}")

    results = await asyncio.gather(*(_probe(c, n, topic) for c, n in plan))

    warnings: list[str] = []
    if settings.enable_mock_data:
        warnings.append("ENABLE_MOCK_DATA is true: live connectors are switched off and searches return mock data.")
    if settings.reddit_configured() and not _REDDIT_UA_RE.match(settings.reddit_user_agent or ""):
        warnings.append(
            "REDDIT_USER_AGENT doesn't follow Reddit's required format "
            "'<platform>:<app ID>:<version> (by /u/<username>)'. Generic User-Agents are throttled or blocked harder."
        )
    if not settings.r2_configured():
        warnings.append("R2 is not configured: the heavy-source fallback cache is off, so a failed live fetch has no cached copy to fall back on.")

    summary = {s: sum(1 for r in results if r["status"] == s) for s in (OK, EMPTY, DISABLED, ERROR, TIMEOUT)}
    return {"topic": topic, "summary": summary, "warnings": warnings, "results": results}
