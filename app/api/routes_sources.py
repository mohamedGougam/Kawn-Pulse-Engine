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
            # -- fast tier --
            "Bluesky": {"enabled": live, "configured": True},
            "Reddit": {"enabled": live and settings.reddit_configured(), "configured": settings.reddit_configured()},
            "Mastodon": {"enabled": live, "configured": True},
            "YouTube": {"enabled": live and settings.youtube_configured(), "configured": settings.youtube_configured()},
            "HackerNews": {"enabled": live, "configured": True},
            "Wikipedia": {"enabled": False, "configured": True},
            # -- heavy tier --
            "DevTo": {"enabled": live, "configured": True},
            "News": {"enabled": False, "configured": True},
            "Hashnode": {"enabled": False, "configured": True},
            # -- not on the requested lists, disabled --
            "Discourse": {"enabled": live, "configured": True},
            "Lobsters": {"enabled": False, "configured": True},
            "Lemmy": {"enabled": live, "configured": True},
            "PeerTube": {"enabled": live, "configured": True},
            "ProductHunt": {"enabled": False, "configured": settings.producthunt_configured()},
        },
    }

