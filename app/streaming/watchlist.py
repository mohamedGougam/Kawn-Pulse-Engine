from __future__ import annotations

from app.config import settings


class TopicWatchlist:
    """What the firehose/stream consumers filter incoming events against.

    A stream can't be queried on demand the way a search API can — it can
    only match events against topics it already knows to look for. Seeded
    with Discover_subjects (so the explorer feed is covered from the
    start), and grown by register() whenever a connector's fetch() is
    called for a topic that isn't already watched, so repeat/tracked
    searches gradually pick up real-time coverage too.

    A brand-new topic's very first search still won't have any buffered
    matches — the stream only sees events from the moment it starts
    watching a topic onward, it can't retroactively search history. That's
    an inherent trade-off of stream-based fetching, not a bug: the
    poll-based fallback each connector keeps internally covers exactly
    this cold-start case.
    """

    def __init__(self) -> None:
        self._topics: set[str] = {t.strip() for t in settings.Discover_subjects if t.strip()}

    def register(self, topic: str) -> None:
        t = (topic or "").strip()
        if t:
            self._topics.add(t)

    def all(self) -> list[str]:
        return list(self._topics)


watchlist = TopicWatchlist()
