from __future__ import annotations

import urllib.parse

import httpx

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.utils.date_utils import parse_datetime
from app.utils.text_utils import normalize_ws


class LemmyConnector:
    """Lemmy is federated, but each instance only knows about content it has
    already federated with — there's no single global search across every
    Lemmy instance. This connector targets one configured instance's public
    /api/v3/search endpoint (no auth needed for public communities).
    """

    name = "Lemmy"

    async def enabled(self) -> bool:
        return not settings.enable_mock_data

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        instance = settings.lemmy_instance_url.rstrip("/")
        q = urllib.parse.quote_plus(topic)
        url = f"{instance}/api/v3/search?q={q}&type_=Posts&sort=New&limit={min(limit, 50)}"

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={"User-Agent": settings.reddit_user_agent})
            resp.raise_for_status()
            data = resp.json()

        # Lemmy 0.19 returns {"posts": [...]}. Lemmy 1.0 reworked search into
        # a combined result list ("results") and its release notes say the
        # v3 search endpoint is not fully backwards compatible. Accept both,
        # and if it's neither, fail loudly - quietly returning [] here would
        # make a response-format change look like "no posts about this topic".
        posts = data.get("posts")
        if posts is None:
            results = data.get("results")
            if not isinstance(results, list):
                raise ValueError(f"unrecognized Lemmy search response; top-level keys: {sorted(data)[:8]}")
            posts = [r for r in results if isinstance(r, dict) and isinstance(r.get("post"), dict)]
        items: list[NormalizedRawItem] = []

        for p in posts[:limit]:
            post = p.get("post") or {}
            title = normalize_ws(post.get("name") or "")
            body = normalize_ws(post.get("body") or "")
            text = body or title
            if not text:
                continue

            source_url = post.get("ap_id") or ""
            if not source_url:
                continue

            post_id = post.get("id")
            creator = (p.get("creator") or {}).get("name")
            counts = p.get("counts") or {}

            items.append(
                NormalizedRawItem(
                    source="Lemmy",
                    source_url=source_url,
                    topic=topic,
                    author=creator,
                    title=title or None,
                    text=text,
                    published_at=parse_datetime(post.get("published")),
                    engagement_count=int(counts.get("score") or post.get("score") or 0) or None,
                    language=None,
                    external_id=str(post_id) if post_id is not None else None,
                )
            )

        return items[:limit]
