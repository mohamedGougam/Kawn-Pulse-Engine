from __future__ import annotations

import re
import urllib.parse

import httpx

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.utils.date_utils import parse_datetime
from app.utils.text_utils import normalize_ws

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_NON_TAG_CHARS_RE = re.compile(r"[^a-z0-9]+")


def _topic_to_hashtag(topic: str) -> str:
    return _NON_TAG_CHARS_RE.sub("", topic.lower())


class MastodonConnector:
    """Mastodon's full-text status search (/api/v2/search) requires a
    logged-in user access token on almost every instance. Without one, the
    only public, keyless way to find topical posts is the hashtag timeline
    (/api/v1/timelines/tag/:hashtag), so this connector turns the topic into
    a hashtag. That means it only surfaces posts that were actually tagged
    with that hashtag — not a general full-text match — so results for
    multi-word or conversational topics may be sparse.
    """

    name = "Mastodon"

    async def enabled(self) -> bool:
        return not settings.enable_mock_data

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        instance = settings.mastodon_instance_url.rstrip("/")
        tag = _topic_to_hashtag(topic)
        if not tag:
            return []

        url = f"{instance}/api/v1/timelines/tag/{urllib.parse.quote(tag)}?limit={min(limit, 40)}"

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={"User-Agent": settings.reddit_user_agent})
            resp.raise_for_status()
            statuses = resp.json()

        items: list[NormalizedRawItem] = []

        for s in (statuses or [])[:limit]:
            html_content = s.get("content") or ""
            text = normalize_ws(_HTML_TAG_RE.sub(" ", html_content))
            if not text:
                continue

            source_url = s.get("url") or s.get("uri") or ""
            if not source_url:
                continue

            account = s.get("account") or {}
            author = account.get("acct")
            status_id = s.get("id")
            engagement = int(s.get("reblogs_count") or 0) + int(s.get("favourites_count") or 0)

            items.append(
                NormalizedRawItem(
                    source="Mastodon",
                    source_url=source_url,
                    topic=topic,
                    author=author,
                    title=None,
                    text=text,
                    published_at=parse_datetime(s.get("created_at")),
                    engagement_count=engagement or None,
                    language=s.get("language"),
                    external_id=str(status_id) if status_id is not None else None,
                )
            )

        return items[:limit]
