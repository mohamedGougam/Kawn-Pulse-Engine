from __future__ import annotations

import urllib.parse

import httpx

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.utils.date_utils import parse_datetime
from app.utils.text_utils import normalize_ws


class DiscourseConnector:
    """Unlike Lemmy/Mastodon/PeerTube, Discourse is NOT federated — every
    community runs its own separate, unconnected forum (e.g.
    meta.discourse.org, community.openai.com, forum.unity.com). There is no
    single "Discourse network" to search across. This connector targets
    exactly one configured instance via `discourse_instance_url` — point it
    at whichever specific community forum is actually relevant to your
    topics. The default (Discourse's own meta-forum, about the Discourse
    software itself) is only a safe placeholder, not a topically useful
    default — change it.

    Uses the instance's public /search.json endpoint, no auth needed on
    forums with public read access (the common case).
    """

    name = "Discourse"

    async def enabled(self) -> bool:
        return not settings.enable_mock_data

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        instance = settings.discourse_instance_url.rstrip("/")
        q = urllib.parse.quote_plus(topic)
        url = f"{instance}/search.json?q={q}"

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={"User-Agent": settings.reddit_user_agent})
            resp.raise_for_status()
            data = resp.json()

        posts = data.get("posts") or []
        topics_by_id = {t.get("id"): t for t in (data.get("topics") or [])}
        users_by_username = {u.get("username"): u for u in (data.get("users") or [])}

        items: list[NormalizedRawItem] = []

        for p in posts[:limit]:
            text = normalize_ws(p.get("blurb") or "")
            if not text:
                continue

            topic_id = p.get("topic_id")
            post_number = p.get("post_number") or 1
            topic_slug = (topics_by_id.get(topic_id) or {}).get("slug")
            title = normalize_ws((topics_by_id.get(topic_id) or {}).get("title") or "")

            if topic_slug and topic_id:
                source_url = f"{instance}/t/{topic_slug}/{topic_id}/{post_number}"
            elif topic_id:
                source_url = f"{instance}/t/{topic_id}/{post_number}"
            else:
                continue

            username = p.get("username")
            author = (users_by_username.get(username) or {}).get("name") or username

            like_count = p.get("like_count")
            post_id = p.get("id")

            items.append(
                NormalizedRawItem(
                    source="Discourse",
                    source_url=source_url,
                    topic=topic,
                    author=author,
                    title=title or None,
                    text=text,
                    published_at=parse_datetime(p.get("created_at")),
                    engagement_count=int(like_count) if like_count else None,
                    language=None,
                    external_id=str(post_id) if post_id is not None else None,
                )
            )

        return items[:limit]
