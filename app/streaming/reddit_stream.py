from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.streaming.ring_buffer import StreamingRingBuffer
from app.streaming.watchlist import watchlist
from app.storage.object_store import ObjectStoreUnavailable, object_store
from app.utils.text_utils import normalize_ws, topic_matches

logger = logging.getLogger("kawn.streaming.reddit")

SOURCE = "Reddit"

buffer = StreamingRingBuffer(
    maxlen_per_topic=settings.firehose_buffer_size_per_topic,
    max_topics=settings.watchlist_max_topics,
)


def _match_and_buffer(*, source_url: str, author: str | None, title: str | None,
                       text: str, published_at: datetime | None,
                       engagement_count: int | None, external_id: str | None) -> None:
    text = normalize_ws(text or "")
    if not text:
        return

    for topic in watchlist.all():
        if not (topic_matches(topic, text) or (title and topic_matches(topic, title))):
            continue
        buffer.add(
            SOURCE,
            topic,
            NormalizedRawItem(
                source=SOURCE,
                source_url=source_url,
                topic=topic,
                author=author,
                title=title,
                text=text,
                published_at=published_at,
                engagement_count=engagement_count,
                language=None,
                external_id=external_id,
            ),
        )


async def _run_submission_stream(reddit, subreddit_name: str, stop_event: asyncio.Event) -> None:
    subreddit = await reddit.subreddit(subreddit_name)
    async for submission in subreddit.stream.submissions(skip_existing=True):
        if stop_event.is_set():
            break
        title = normalize_ws(submission.title or "")
        selftext = normalize_ws(getattr(submission, "selftext", "") or "")
        author = str(submission.author) if submission.author else None
        published_at = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc) if submission.created_utc else None
        _match_and_buffer(
            source_url=f"https://www.reddit.com{submission.permalink}",
            author=author,
            title=title or None,
            text=selftext or title,
            published_at=published_at,
            engagement_count=int(submission.score or 0),
            external_id=submission.id,
        )


async def _run_comment_stream(reddit, subreddit_name: str, stop_event: asyncio.Event) -> None:
    subreddit = await reddit.subreddit(subreddit_name)
    async for comment in subreddit.stream.comments(skip_existing=True):
        if stop_event.is_set():
            break
        author = str(comment.author) if comment.author else None
        published_at = datetime.fromtimestamp(comment.created_utc, tz=timezone.utc) if comment.created_utc else None
        _match_and_buffer(
            source_url=f"https://www.reddit.com{comment.permalink}",
            author=author,
            title=None,
            text=comment.body or "",
            published_at=published_at,
            engagement_count=int(comment.score or 0),
            external_id=comment.id,
        )


async def _consume_forever(stop_event: asyncio.Event) -> None:
    import asyncpraw

    subreddit_name = "+".join(settings.reddit_stream_subreddit_list())
    backoff = 2.0

    while not stop_event.is_set():
        reddit = asyncpraw.Reddit(
            client_id=settings.reddit_client_id,
            client_secret=settings.reddit_client_secret,
            user_agent=settings.reddit_user_agent,
        )
        try:
            logger.info("Reddit stream connecting: r/%s", subreddit_name)
            await asyncio.gather(
                _run_submission_stream(reddit, subreddit_name, stop_event),
                _run_comment_stream(reddit, subreddit_name, stop_event),
            )
            backoff = 2.0
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Reddit stream error, reconnecting in %.0fs: %s", backoff, e)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60.0)
        finally:
            try:
                await reddit.close()
            except Exception:
                pass


async def _flush_forever(stop_event: asyncio.Event) -> None:
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
                logger.warning("Reddit stream R2 flush failed for topic=%r: %s", topic, e)


class RedditStreamService:
    def __init__(self) -> None:
        self._stop_event: asyncio.Event | None = None
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        if not settings.reddit_stream_enabled:
            return
        if not settings.reddit_configured():
            logger.warning("reddit_stream_enabled is True but Reddit API credentials are not configured; skipping")
            return
        try:
            import asyncpraw  # noqa: F401
        except ImportError:
            logger.warning(
                "reddit_stream_enabled is True but the 'asyncpraw' package is not installed "
                "(pip install -r requirements.txt); skipping the Reddit stream consumer"
            )
            return
        self._stop_event = asyncio.Event()
        self._tasks = [
            asyncio.create_task(_consume_forever(self._stop_event)),
            asyncio.create_task(_flush_forever(self._stop_event)),
        ]
        logger.info("Reddit stream service started")

    async def shutdown(self) -> None:
        if not self._stop_event:
            return
        self._stop_event.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


reddit_stream_service = RedditStreamService()
