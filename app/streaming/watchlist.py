from __future__ import annotations

import threading
from collections import OrderedDict

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

    Bounded LRU, capped at settings.watchlist_max_topics: register() used
    to add to a plain, never-shrinking set, so every distinct query ever
    searched (real user searches, not just Discover_subjects) stayed
    resident in memory for the life of the process. On a process that
    stays up for a while against real traffic, that's an unbounded leak —
    it doesn't crash on any single request, it crashes hours/days in once
    enough distinct queries have piled up, which made it look like a new
    bug after every deploy rather than the same root cause recurring.
    Evicting the least-recently-registered topic once the cap is hit
    keeps memory flat regardless of how long the process runs or how much
    distinct search traffic it sees, while still favoring whatever's
    actually been searched/refreshed recently. Discover_subjects are
    re-registered whenever they're touched (scheduler ticks hit them
    every pass), so they naturally stay warm and are never the ones
    evicted.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._topics: "OrderedDict[str, None]" = OrderedDict(
            (t.strip(), None) for t in settings.Discover_subjects if t.strip()
        )

    def register(self, topic: str) -> None:
        t = (topic or "").strip()
        if not t:
            return
        with self._lock:
            # Re-inserting moves it to the most-recently-used end.
            self._topics.pop(t, None)
            self._topics[t] = None
            max_topics = settings.watchlist_max_topics
            while len(self._topics) > max_topics:
                self._topics.popitem(last=False)  # evict least-recently-used

    def all(self) -> list[str]:
        with self._lock:
            return list(self._topics)


watchlist = TopicWatchlist()
