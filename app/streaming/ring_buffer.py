from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field

from app.connectors.base import NormalizedRawItem


@dataclass
class _TopicBucket:
    items: deque = field(default_factory=lambda: deque(maxlen=200))
    dirty: bool = False  # has anything been added since the last R2 flush?


class StreamingRingBuffer:
    """Holds recent matched items per (source, topic), most-recent-first.

    Single-process, in-memory, not persisted — that's what the periodic R2
    flush in each consumer is for. Sized per topic via maxlen so one very
    chatty topic can't starve memory for the others.

    Bounded LRU over the *set of buckets*, capped at max_topics: each
    bucket's own deque was already capped, but the dict of buckets itself
    had no cap — a (source, topic) entry, once created, stuck around
    forever even after TopicWatchlist stopped watching that topic. That's
    the same unbounded-growth shape as the watchlist bug one layer up:
    with streaming enabled and enough topic churn over a long enough
    uptime, this dict grows without bound regardless of how tightly the
    watchlist itself is capped. Evicting the least-recently-touched bucket
    once the cap is hit keeps this in step with the watchlist instead of
    quietly outliving it.
    """

    def __init__(self, *, maxlen_per_topic: int, max_topics: int = 500) -> None:
        self._maxlen = maxlen_per_topic
        self._max_topics = max_topics
        self._buckets: "OrderedDict[tuple[str, str], _TopicBucket]" = OrderedDict()

    def _bucket(self, source: str, topic: str) -> _TopicBucket:
        key = (source, topic)
        b = self._buckets.get(key)
        if b is None:
            b = _TopicBucket(items=deque(maxlen=self._maxlen))
            self._buckets[key] = b
            while len(self._buckets) > self._max_topics:
                self._buckets.popitem(last=False)  # evict least-recently-touched
        else:
            self._buckets.move_to_end(key)
        return b

    def add(self, source: str, topic: str, item: NormalizedRawItem) -> None:
        b = self._bucket(source, topic)
        b.items.appendleft(item)
        b.dirty = True

    def get(self, source: str, topic: str, *, limit: int) -> list[NormalizedRawItem]:
        key = (source, topic)
        b = self._buckets.get(key)
        if not b:
            return []
        self._buckets.move_to_end(key)
        return list(b.items)[:limit]

    def dirty_topics(self, source: str) -> list[str]:
        return [topic for (s, topic), b in self._buckets.items() if s == source and b.dirty and b.items]

    def mark_flushed(self, source: str, topic: str) -> None:
        b = self._buckets.get((source, topic))
        if b:
            b.dirty = False
