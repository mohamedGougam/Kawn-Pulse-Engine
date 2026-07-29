from __future__ import annotations

from collections import deque
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
    """

    def __init__(self, *, maxlen_per_topic: int) -> None:
        self._maxlen = maxlen_per_topic
        self._buckets: dict[tuple[str, str], _TopicBucket] = {}

    def _bucket(self, source: str, topic: str) -> _TopicBucket:
        key = (source, topic)
        b = self._buckets.get(key)
        if b is None:
            b = _TopicBucket(items=deque(maxlen=self._maxlen))
            self._buckets[key] = b
        return b

    def add(self, source: str, topic: str, item: NormalizedRawItem) -> None:
        b = self._bucket(source, topic)
        b.items.appendleft(item)
        b.dirty = True

    def get(self, source: str, topic: str, *, limit: int) -> list[NormalizedRawItem]:
        b = self._buckets.get((source, topic))
        if not b:
            return []
        return list(b.items)[:limit]

    def dirty_topics(self, source: str) -> list[str]:
        return [topic for (s, topic), b in self._buckets.items() if s == source and b.dirty and b.items]

    def mark_flushed(self, source: str, topic: str) -> None:
        b = self._buckets.get((source, topic))
        if b:
            b.dirty = False
