from __future__ import annotations

import asyncio
import json
import logging

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.streaming.ring_buffer import StreamingRingBuffer
from app.streaming.watchlist import watchlist
from app.storage.object_store import ObjectStoreUnavailable, object_store
from app.utils.date_utils import parse_datetime
from app.utils.text_utils import normalize_ws, topic_matches

logger = logging.getLogger("kawn.streaming.bluesky")

SOURCE = "Bluesky"

buffer = StreamingRingBuffer(maxlen_per_topic=settings.firehose_buffer_size_per_topic)


def _uri_to_url(did: str, rkey: str) -> str:
    return f"https://bsky.app/profile/{did}/post/{rkey}"


def _handle_event(raw: str) -> None:
    """Parse one Jetstream JSON message and, if it's a new post matching a
    watched topic, add it to the buffer. Jetstream sends JSON directly
    (unlike the raw CBOR/CAR firehose), so no binary decoding needed."""
    try:
        evt = json.loads(raw)
    except Exception:
        return

    if evt.get("kind") != "commit":
        return
    commit = evt.get("commit") or {}
    if commit.get("operation") != "create" or commit.get("collection") != "app.bsky.feed.post":
        return

    record = commit.get("record") or {}
    text = normalize_ws(record.get("text") or "")
    if not text:
        return

    did = evt.get("did") or ""
    rkey = commit.get("rkey") or ""
    cid = commit.get("cid") or ""
    if not did or not rkey:
        return

    matched_topics = [t for t in watchlist.all() if topic_matches(t, text)]
    if not matched_topics:
        return

    item = NormalizedRawItem(
        source=SOURCE,
        source_url=_uri_to_url(did, rkey),
        topic="",  # set per-topic below
        # Jetstream gives us the author's DID, not their handle — resolving
        # it to a handle costs an extra API call per post, which doesn't
        # scale against firehose volume. DID is still a stable, dereferenceable
        # identifier, just less friendly to display than "@handle".
        author=did,
        title=None,
        text=text,
        published_at=parse_datetime(record.get("createdAt")),
        # The firehose only carries the post itself, not like/repost
        # counts — those live on a separate, non-streamed part of the API.
        engagement_count=None,
        language=(record.get("langs") or [None])[0],
        external_id=cid or None,
    )

    for topic in matched_topics:
        buffer.add(SOURCE, topic, NormalizedRawItem(**{**item.__dict__, "topic": topic}))


async def _consume_forever(stop_event: asyncio.Event) -> None:
    import websockets

    backoff = 2.0
    while not stop_event.is_set():
        try:
            async with websockets.connect(settings.bluesky_jetstream_url, ping_interval=20, ping_timeout=20) as ws:
                logger.info("Bluesky Jetstream connected")
                backoff = 2.0
                while not stop_event.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    _handle_event(raw)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Bluesky Jetstream connection error, reconnecting in %.0fs: %s", backoff, e)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60.0)


async def _flush_forever(stop_event: asyncio.Event) -> None:
    """Periodically persist dirty topic buffers to R2 so a restart doesn't
    lose everything the firehose has seen — see firehose_flush_interval_seconds
    for the batching rationale (bounding R2 write volume, not latency)."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.firehose_flush_interval_seconds)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        if not settings.r2_configured():
            continue

        for topic in buffer.dirty_topics(SOURCE):
            items = buffer.get(SOURCE, topic, limit=settings.firehose_buffer_size_per_topic)
            payload = [
                {
                    "source_url": i.source_url,
                    "author": i.author,
                    "text": i.text,
                    "title": i.title,
                    "published_at": i.published_at.isoformat() if i.published_at else None,
                    "engagement_count": i.engagement_count,
                    "language": i.language,
                    "external_id": i.external_id,
                }
                for i in items
            ]
            try:
                await object_store.put_json(SOURCE, topic, payload)
                buffer.mark_flushed(SOURCE, topic)
            except ObjectStoreUnavailable:
                pass
            except Exception as e:
                logger.warning("Bluesky firehose R2 flush failed for topic=%r: %s", topic, e)


class BlueskyFirehoseService:
    def __init__(self) -> None:
        self._stop_event: asyncio.Event | None = None
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        if not settings.bluesky_firehose_enabled:
            return
        try:
            import websockets  # noqa: F401
        except ImportError:
            logger.warning(
                "bluesky_firehose_enabled is True but the 'websockets' package is not installed "
                "(pip install -r requirements.txt); skipping the firehose consumer"
            )
            return
        self._stop_event = asyncio.Event()
        self._tasks = [
            asyncio.create_task(_consume_forever(self._stop_event)),
            asyncio.create_task(_flush_forever(self._stop_event)),
        ]
        logger.info("Bluesky firehose service started")

    async def shutdown(self) -> None:
        if not self._stop_event:
            return
        self._stop_event.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


bluesky_firehose_service = BlueskyFirehoseService()
