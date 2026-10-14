"""What actually happened the last time each source was called.

Until now a failing source left exactly one trace: a `logger.warning` line.
Nothing the API exposed could tell "Reddit returned nothing because there
is no Reddit post about this topic" apart from "Reddit's credentials were
rejected an hour ago and every call since has failed". This keeps a small
in-memory record per source, fed by AggregationService._safe_fetch on every
real fetch (so it costs no extra requests and no API quota), and read by
GET /api/sources/health.

In-memory on purpose: it is operational telemetry, not data. It resets on
restart, which on a free-tier dyno is fine - the point is "what is going on
right now", and the first few searches repopulate it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import settings
from app.utils.http_errors import ErrorInfo

# statuses
OK = "ok"
EMPTY = "empty"          # call succeeded, nothing matched (not a failure)
ERROR = "error"
TIMEOUT = "timeout"
DISABLED = "disabled"
MOCK = "mock"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SourceHealth:
    source: str
    last_status: str | None = None
    last_checked_at: str | None = None
    last_success_at: str | None = None
    last_item_count: int | None = None
    last_latency_ms: int | None = None
    last_error: dict | None = None
    last_error_at: str | None = None
    consecutive_failures: int = 0
    total_calls: int = 0
    total_failures: int = 0

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "last_status": self.last_status,
            "last_checked_at": self.last_checked_at,
            "last_success_at": self.last_success_at,
            "last_item_count": self.last_item_count,
            "last_latency_ms": self.last_latency_ms,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "consecutive_failures": self.consecutive_failures,
            "total_calls": self.total_calls,
            "total_failures": self.total_failures,
        }


_health: dict[str, SourceHealth] = {}


def _entry(source: str) -> SourceHealth:
    entry = _health.get(source)
    if entry is None:
        entry = _health[source] = SourceHealth(source=source)
    return entry


def record_success(source: str, item_count: int, started_monotonic: float) -> None:
    e = _entry(source)
    e.total_calls += 1
    e.last_status = OK if item_count else EMPTY
    e.last_checked_at = _now_iso()
    e.last_success_at = e.last_checked_at
    e.last_item_count = item_count
    e.last_latency_ms = int((time.monotonic() - started_monotonic) * 1000)
    e.consecutive_failures = 0


def record_failure(source: str, error: ErrorInfo, started_monotonic: float, *, timed_out: bool = False) -> None:
    e = _entry(source)
    e.total_calls += 1
    e.total_failures += 1
    e.consecutive_failures += 1
    e.last_status = TIMEOUT if timed_out else ERROR
    e.last_checked_at = _now_iso()
    e.last_item_count = 0
    e.last_latency_ms = int((time.monotonic() - started_monotonic) * 1000)
    e.last_error = error.as_dict()
    e.last_error_at = e.last_checked_at


def snapshot() -> dict[str, dict]:
    return {name: h.as_dict() for name, h in sorted(_health.items())}


def reset() -> None:
    """Test helper."""
    _health.clear()


# --- why is a source off? --------------------------------------------------

def explain_disabled(source: str) -> str | None:
    """A human reason a source's enabled() is False, or None if there's no
    specific reason we can name. Kept next to the health data (not inside
    each connector) so one place answers "why is X off" for the status and
    diagnostics endpoints."""
    if settings.enable_mock_data:
        return "ENABLE_MOCK_DATA is true - every live connector is switched off and only mock data is served"

    s = source.lower()
    if s == "reddit" and not settings.reddit_configured():
        return "REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are not set"
    if s == "youtube" and not settings.youtube_configured():
        return "YOUTUBE_API_KEY is not set"
    if s == "bluesky" and not settings.bluesky_configured():
        return "BLUESKY_HANDLE / BLUESKY_APP_PASSWORD are not set (Bluesky post search requires a logged-in session)"
    if s == "producthunt" and not settings.producthunt_configured():
        return "PRODUCTHUNT_ACCESS_TOKEN is not set"
    if s == "news" and not settings.news_enabled:
        return "NEWS_ENABLED is false"
    if s == "gdelt" and not settings.gdelt_enabled:
        return "GDELT_ENABLED is false"
    return None
