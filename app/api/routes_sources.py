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
            "Reddit": {
                "enabled": (settings.reddit_configured() and live),
                "configured": settings.reddit_configured(),
            },
            "YouTube": {
                "enabled": (settings.youtube_configured() and live),
                "configured": settings.youtube_configured(),
            },
            "News": {"enabled": True, "configured": True},
            "Bluesky": {"enabled": live, "configured": True},
            "HackerNews": {"enabled": live, "configured": True},
        },
    }

