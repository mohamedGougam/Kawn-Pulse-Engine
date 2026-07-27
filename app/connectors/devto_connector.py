from __future__ import annotations

import re

import httpx

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.utils.date_utils import parse_datetime
from app.utils.text_utils import normalize_ws

_NON_TAG_CHARS_RE = re.compile(r"[^a-z0-9]+")


def _topic_to_devto_tag(topic: str) -> str:
    """Dev.to tags are lowercase, no spaces/punctuation (e.g. "webdev")."""
    return _NON_TAG_CHARS_RE.sub("", topic.lower())


class DevToConnector:
    """Dev.to's public articles API needs no authentication for reads."""

    name = "DevTo"

    BASE_URL = "https://dev.to/api/articles"

    async def enabled(self) -> bool:
        return not settings.enable_mock_data

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        tag = _topic_to_devto_tag(topic)
        if not tag:
            return []

        url = f"{self.BASE_URL}?tag={tag}&per_page={min(limit, 100)}"

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={"User-Agent": settings.reddit_user_agent})
            resp.raise_for_status()
            articles = resp.json()

        items: list[NormalizedRawItem] = []

        for a in (articles or [])[:limit]:
            title = normalize_ws(a.get("title") or "")
            description = normalize_ws(a.get("description") or "")
            text = description or title
            if not text:
                continue

            source_url = a.get("url") or ""
            if not source_url:
                continue

            user = a.get("user") or {}
            author = user.get("name") or user.get("username") or None

            engagement = int(a.get("positive_reactions_count") or 0) + int(a.get("comments_count") or 0)
            article_id = a.get("id")

            items.append(
                NormalizedRawItem(
                    source="DevTo",
                    source_url=source_url,
                    topic=topic,
                    author=author,
                    title=title or None,
                    text=text,
                    published_at=parse_datetime(a.get("published_at")),
                    engagement_count=engagement or None,
                    language=None,
                    external_id=str(article_id) if article_id is not None else None,
                )
            )

        return items[:limit]
