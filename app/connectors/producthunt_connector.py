from __future__ import annotations

import re

import httpx

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.utils.date_utils import parse_datetime
from app.utils.text_utils import normalize_ws

_NON_SLUG_CHARS_RE = re.compile(r"[^a-z0-9]+")

_QUERY = """
query TopicPosts($slug: String!, $first: Int!) {
  posts(topic: $slug, first: $first, order: VOTES) {
    edges {
      node {
        id
        name
        tagline
        url
        createdAt
        votesCount
      }
    }
  }
}
"""


def _topic_to_ph_slug(topic: str) -> str:
    return _NON_SLUG_CHARS_RE.sub("-", topic.lower()).strip("-")


class ProductHuntConnector:
    """Product Hunt's v2 API is GraphQL-only and always requires a Bearer
    token (a free, non-expiring "developer token" from
    https://www.producthunt.com/v2/oauth/applications works for read-only
    public access — no full OAuth flow needed).

    Important fit caveat: Product Hunt's `topic` filter matches its own
    fixed product-category slugs (e.g. "artificial-intelligence",
    "productivity", "developer-tools") — NOT a general full-text search.
    For topics like "football" or "world cup 2026" this will almost always
    return zero results, since those aren't Product Hunt categories. This
    connector is only useful for genuinely tech/product-shaped topics.
    """

    name = "ProductHunt"

    ENDPOINT = "https://api.producthunt.com/v2/api/graphql"

    async def enabled(self) -> bool:
        return bool(settings.producthunt_access_token) and not settings.enable_mock_data

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        slug = _topic_to_ph_slug(topic)
        if not slug:
            return []

        payload = {"query": _QUERY, "variables": {"slug": slug, "first": min(limit, 20)}}

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                self.ENDPOINT,
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.producthunt_access_token}",
                    "Content-Type": "application/json",
                    "User-Agent": settings.reddit_user_agent,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        edges = (((data or {}).get("data") or {}).get("posts") or {}).get("edges") or []
        items: list[NormalizedRawItem] = []

        for edge in edges[:limit]:
            node = edge.get("node") or {}
            title = normalize_ws(node.get("name") or "")
            tagline = normalize_ws(node.get("tagline") or "")
            text = tagline or title
            if not text:
                continue

            source_url = node.get("url") or ""
            if not source_url:
                continue

            post_id = node.get("id")

            items.append(
                NormalizedRawItem(
                    source="ProductHunt",
                    source_url=source_url,
                    topic=topic,
                    author=None,  # maker names are redacted in the current public API
                    title=title or None,
                    text=text,
                    published_at=parse_datetime(node.get("createdAt")),
                    engagement_count=int(node.get("votesCount") or 0) or None,
                    language=None,
                    external_id=str(post_id) if post_id is not None else None,
                )
            )

        return items[:limit]
