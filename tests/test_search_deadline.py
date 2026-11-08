import asyncio
import time
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import routes_topics
from app.config import settings
from app.main import app


@pytest.fixture
def mock_mode(monkeypatch):
    monkeypatch.setattr(settings, "enable_mock_data", True)
    monkeypatch.setattr(settings, "search_return_deadline_seconds", 0.3)


def _slow(monkeypatch, delay):
    real = routes_topics.aggregation.refresh_topic

    async def slow(*a, **kw):
        await asyncio.sleep(delay)
        return await real(*a, **kw)

    monkeypatch.setattr(routes_topics.aggregation, "refresh_topic", slow)


async def _post(c, **body):
    r = await c.post("/api/topics/search", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def test_deadline_returns_stored_cards_and_refresh_finishes_in_background(mock_mode, monkeypatch):
    q = f"dl-{uuid.uuid4().hex[:8]}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _post(c, query=q)                      # populate the database
        _slow(monkeypatch, 1.5)
        t0 = time.monotonic()
        early = await _post(c, query=q, language="fr")   # language => goes live
        assert time.monotonic() - t0 < 1.2
        assert early["meta"]["pending_refresh"] is True
        assert early["meta"]["stored_from"] == "database"
        assert early["cards"]

        st = (await c.get("/api/topics/search-status", params={"query": q, "language": "fr"})).json()
        assert st["pending"] is True
        for _ in range(40):
            await asyncio.sleep(0.1)
            if not (await c.get("/api/topics/search-status", params={"query": q, "language": "fr"})).json()["pending"]:
                break
        # live finished in the background -> the same search is now instant
        again = await _post(c, query=q, language="fr")
        assert again["meta"]["instant_cache"] is True


async def test_no_stored_data_waits_for_live(mock_mode, monkeypatch):
    q = f"dl-{uuid.uuid4().hex[:8]}"
    _slow(monkeypatch, 0.8)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await _post(c, query=q)
        assert r["meta"]["refreshed"] is True
        assert not r["meta"].get("pending_refresh")
        assert r["cards"]


async def test_deadline_zero_disables_cutoff(mock_mode, monkeypatch):
    monkeypatch.setattr(settings, "search_return_deadline_seconds", 0)
    q = f"dl-{uuid.uuid4().hex[:8]}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _post(c, query=q)
        _slow(monkeypatch, 0.6)
        r = await _post(c, query=q, language="fr")
        assert r["meta"]["refreshed"] is True
