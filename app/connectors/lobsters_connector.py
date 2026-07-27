from __future__ import annotations

import re

import httpx

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.utils.date_utils import parse_datetime
from app.utils.text_utils import normalize_ws

_NON_TAG_CHARS_RE = re.compile(r"[^a-z0-9]+")


def _topic_to_lobsters_tag(topic: str) -> str:
    return _NON_TAG_CHARS_RE.sub("", topic.lower())


class LobstersConnector:
    """Lobsters has no official, documented full-text search API — a core
    maintainer has publicly confirmed "no authenticated API, just JSON
    representations of various pages." This uses its tag page JSON view
    (https://lobste.rs/t/{tag}.json), following the same officially-known
    pattern as its confirmed /hottest.json and /newest.json endpoints.
    This is a best-effort exact-tag match, not a stable guaranteed search —
    treat it as lower-confidence than the other connectors here.
    """

    name = "Lobsters"

    BASE_URL = "https://lobste.rs"

    async def enabled(self) -> bool:
        return not settings.enable_mock_data

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        tag = _topic_to_lobsters_tag(topic)
        if not tag:
            return []

        url = f"{self.BASE_URL}/t/{tag}.json"

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={"User-Agent": settings.reddit_user_agent})
            if resp.status_code == 404:
                return []  # unknown/unused tag — not an error
            resp.raise_for_status()
            stories = resp.json()

        items: list[NormalizedRawItem] = []

        for s in (stories or [])[:limit]:
            title = normalize_ws(s.get("title") or "")
            description = normalize_ws(s.get("description_plain_text") or s.get("description") or "")
            text = description or title
            if not text:
                continue

            source_url = s.get("url") or s.get("comments_url") or s.get("short_id_url") or ""
            if not source_url:
                continue

            engagement = int(s.get("score") or 0) + int(s.get("comment_count") or 0)
            short_id = s.get("short_id")

            submitter = s.get("submitter_user")
            if isinstance(submitter, dict):
                author = submitter.get("username") or submitter.get("name")
            else:
                author = submitter or None

            items.append(
                NormalizedRawItem(
                    source="Lobsters",
                    source_url=source_url,
                    topic=topic,
                    author=author,
                    title=title or None,
                    text=text,
                    published_at=parse_datetime(s.get("created_at")),
                    engagement_count=engagement or None,
                    language=None,
                    external_id=short_id,
                )
            )

        return items[:limit]
