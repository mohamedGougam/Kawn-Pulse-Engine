from __future__ import annotations

import urllib.parse

import httpx

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.utils.date_utils import parse_datetime
from app.utils.text_utils import normalize_ws


class PeerTubeConnector:
    """Federated video platform. Its "platform search" mode (searching what
    this instance already knows about) needs no authentication — only the
    optional "global index search" and remote-URI lookup modes need auth,
    neither of which this uses.
    """

    name = "PeerTube"

    async def enabled(self) -> bool:
        return not settings.enable_mock_data

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        instance = settings.peertube_instance_url.rstrip("/")
        q = urllib.parse.quote_plus(topic)
        url = f"{instance}/api/v1/search/videos?search={q}&count={min(limit, 25)}&sort=-publishedAt"

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={"User-Agent": settings.reddit_user_agent})
            resp.raise_for_status()
            data = resp.json()

        videos = data.get("data") or []
        items: list[NormalizedRawItem] = []

        for v in videos[:limit]:
            title = normalize_ws(v.get("name") or "")
            description = normalize_ws(v.get("description") or "")
            text = description or title
            if not text:
                continue

            uuid = v.get("uuid")
            if not uuid:
                continue
            # Canonical watch-page URL, per PeerTube's own docs example.
            source_url = f"{instance}/videos/watch/{uuid}"

            account = v.get("account") or {}
            author = account.get("displayName") or account.get("name") or None

            engagement = int(v.get("likes") or 0) - int(v.get("dislikes") or 0)

            items.append(
                NormalizedRawItem(
                    source="PeerTube",
                    source_url=source_url,
                    topic=topic,
                    author=author,
                    title=title or None,
                    text=text,
                    published_at=parse_datetime(v.get("publishedAt")),
                    engagement_count=engagement if engagement else None,
                    language=(v.get("language") or {}).get("id") if isinstance(v.get("language"), dict) else None,
                    external_id=str(uuid),
                )
            )

        return items[:limit]
