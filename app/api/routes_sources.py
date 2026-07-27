from __future__ import annotations

from fastapi import APIRouter

from app.config import settings

router = APIRouter(prefix="/api/sources", tags=["sources"])


@router.get("/status")
async def sources_status() -> dict:
    live = not settings.enable_mock_data
    return {
        "enable_mock_data": settings.enable_mock_data,
        "connectors": {
            "Bluesky": {"enabled": live, "configured": True},
            "Mastodon": {"enabled": live, "configured": True},
            "DevTo": {"enabled": live, "configured": True},
            "Lobsters": {"enabled": live, "configured": True},
            "Wikipedia": {"enabled": live, "configured": True},
            "Discourse": {"enabled": live, "configured": True},
            "Reddit": {"enabled": False, "configured": settings.reddit_configured()},
            "YouTube": {"enabled": False, "configured": settings.youtube_configured()},
            "News": {"enabled": False, "configured": True},
            "HackerNews": {"enabled": False, "configured": True},
            "Lemmy": {"enabled": False, "configured": True},
            "Hashnode": {"enabled": False, "configured": True},
            "PeerTube": {"enabled": False, "configured": True},
            "ProductHunt": {"enabled": False, "configured": settings.producthunt_configured()},
        },
    }

