from __future__ import annotations

import re

import httpx

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.utils.date_utils import parse_datetime
from app.utils.text_utils import normalize_ws

_NON_SLUG_CHARS_RE = re.compile(r"[^a-z0-9]+")

_QUERY = """
query TagPosts($slug: String!, $first: Int!) {
  tag(slug: $slug) {
    posts(first: $first) {
      edges {
        node {
          title
          brief
          slug
          url
          publishedAt
          author { name username }
        }
      }
    }
  }
}
"""


def _topic_to_hashnode_slug(topic: str) -> str:
    """Hashnode tag slugs are hyphenated (e.g. "web-development"), unlike
    Mastodon's no-separator hashtags.
    """
    return _NON_SLUG_CHARS_RE.sub("-", topic.lower()).strip("-")


class HashnodeConnector:
    """Uses Hashnode's public GraphQL API (gql.hashnode.com), which needs no
    authentication for reading public tag/post data.
    """

    name = "Hashnode"

    ENDPOINT = "https://gql.hashnode.com"

    async def enabled(self) -> bool:
        return not settings.enable_mock_data

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        slug = _topic_to_hashnode_slug(topic)
        if not slug:
            return []

        payload = {
            "query": _QUERY,
            "variables": {"slug": slug, "first": min(limit, 20)},
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                self.ENDPOINT,
                json=payload,
                headers={"User-Agent": settings.reddit_user_agent, "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        tag = ((data or {}).get("data") or {}).get("tag")
        if not tag:
            return []  # unknown tag slug — Hashnode returns null rather than an error

        edges = ((tag.get("posts") or {}).get("edges")) or []
        items: list[NormalizedRawItem] = []

        for edge in edges[:limit]:
            node = edge.get("node") or {}
            title = normalize_ws(node.get("title") or "")
            brief = normalize_ws(node.get("brief") or "")
            text = brief or title
            if not text:
                continue

            source_url = node.get("url") or ""
            if not source_url:
                continue

            author = node.get("author") or {}

            items.append(
                NormalizedRawItem(
                    source="Hashnode",
                    source_url=source_url,
                    topic=topic,
                    author=author.get("name") or author.get("username") or None,
                    title=title or None,
                    text=text,
                    published_at=parse_datetime(node.get("publishedAt")),
                    engagement_count=None,  # not exposed by this query shape
                    language=None,
                    external_id=node.get("slug") or None,
                )
            )

        return items[:limit]
