import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import routes_topics
from app.config import settings
from app.main import app


@pytest.fixture
def mock_mode(monkeypatch):
    monkeypatch.setattr(settings, "enable_mock_data", True)


async def _post(client, **body):
    r = await client.post("/api/topics/search", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def test_repeat_search_is_answered_from_database(mock_mode):
    q = f"intel-{uuid.uuid4().hex[:8]}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        first = await _post(c, query=q)
        assert first["meta"]["refreshed"] is True and first["cards"]

        second = await _post(c, query=q)
        assert second["meta"]["instant_cache"] is True
        assert second["meta"]["refreshed"] is False
        assert len(second["cards"]) == len(first["cards"])
        assert second["summary"]["summary_text"] == first["summary"]["summary_text"]


async def test_named_language_always_searches_live(mock_mode):
    q = f"intel-{uuid.uuid4().hex[:8]}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _post(c, query=q)
        again = await _post(c, query=q, language="fr")
        assert again["meta"]["refreshed"] is True


async def test_fast_path_can_be_disabled(mock_mode, monkeypatch):
    monkeypatch.setattr(settings, "search_instant_cache_enabled", False)
    q = f"intel-{uuid.uuid4().hex[:8]}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _post(c, query=q)
        again = await _post(c, query=q)
        assert again["meta"]["refreshed"] is True


async def test_identical_concurrent_searches_run_one_refresh(mock_mode, monkeypatch):
    calls = 0
    real = routes_topics.aggregation.refresh_topic

    async def counting(*a, **kw):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.2)
        return await real(*a, **kw)

    monkeypatch.setattr(routes_topics.aggregation, "refresh_topic", counting)
    q = f"intel-{uuid.uuid4().hex[:8]}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        a, b = await asyncio.gather(_post(c, query=q), _post(c, query=q))
    assert calls == 1
    assert {a["meta"]["refreshed"], b["meta"]["refreshed"]} == {True, False}


async def test_stale_topic_is_returned_now_and_refreshed_in_background(mock_mode, monkeypatch):
    queued = []

    async def fake_bg(query):
        queued.append(query)

    monkeypatch.setattr(routes_topics, "Backgroundrefresh", fake_bg)
    monkeypatch.setattr(settings, "search_cache_fresh_seconds", -1)  # everything counts as stale
    q = f"intel-{uuid.uuid4().hex[:8]}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _post(c, query=q)
        again = await _post(c, query=q)
    assert again["meta"]["instant_cache"] is True
    assert queued == [q]
