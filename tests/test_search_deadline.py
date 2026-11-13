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


async def test_deadline_returns_stored_cards_and_refresh_finishes_in_background(db, mock_mode, monkeypatch):
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


async def test_no_stored_data_waits_for_live(db, mock_mode, monkeypatch):
    q = f"dl-{uuid.uuid4().hex[:8]}"
    _slow(monkeypatch, 0.8)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await _post(c, query=q)
        assert r["meta"]["refreshed"] is True
        assert not r["meta"].get("pending_refresh")
        assert r["cards"]


async def test_deadline_zero_disables_cutoff(db, mock_mode, monkeypatch):
    monkeypatch.setattr(settings, "search_return_deadline_seconds", 0)
    q = f"dl-{uuid.uuid4().hex[:8]}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _post(c, query=q)
        _slow(monkeypatch, 0.6)
        r = await _post(c, query=q, language="fr")
        assert r["meta"]["refreshed"] is True


async def test_r2_backup_cards_get_real_sentiment(db, mock_mode, monkeypatch):
    from datetime import datetime
    from app.services.aggregation_service import PreviewCard

    def pc(text, url):
        return PreviewCard(topic="x", quote=text, source="News", source_url=url,
                           display_label="News", language="en", engagement_count=None,
                           published_at=datetime.utcnow())

    previews = [pc("I absolutely love this, it is wonderful and amazing!", "http://a/1"),
                pc("This is terrible, awful and a complete disaster.", "http://a/2"),
                pc("The meeting is on Tuesday at noon.", "http://a/3")]

    async def fake_previews(query, max_cards=8):
        return previews

    monkeypatch.setattr(type(settings), "r2_configured", lambda self: True)
    monkeypatch.setattr(routes_topics.aggregation, "build_cache_preview_cards", fake_previews)
    _slow(monkeypatch, 1.5)
    q = f"dl-{uuid.uuid4().hex[:8]}"   # brand-new topic: nothing in the database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await _post(c, query=q)
        assert r["meta"]["stored_from"] == "r2" and r["meta"]["pending_refresh"] is True
        by_url = {x["source_url"]: x["sentiment"] for x in r["cards"]}
        assert by_url["http://a/1"] == "positive"
        assert by_url["http://a/2"] == "negative"
        assert by_url["http://a/3"] == "neutral"
        b = r["summary"]["sentiment_breakdown"]
        assert b["positive"] > 0 and b["negative"] > 0
        await asyncio.sleep(1.8)   # let the background refresh finish before teardown


# ---------------- Explore / Following speed ----------------
from sqlalchemy import event
from app.database import engine as _engine


class _StmtCounter:
    def __init__(self):
        self.n = 0

    def __enter__(self):
        self._fn = lambda *a, **k: setattr(self, "n", self.n + 1)
        event.listen(_engine.sync_engine, "before_cursor_execute", self._fn)
        return self

    def __exit__(self, *a):
        event.remove(_engine.sync_engine, "before_cursor_execute", self._fn)


async def test_explore_feed_uses_a_constant_number_of_queries(db, mock_mode, monkeypatch):
    async def noop(q):
        return None
    monkeypatch.setattr(routes_topics, "Backgroundrefresh", noop)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/topics/explore-feed")   # first load: creates topics
        assert r.status_code == 200
        with _StmtCounter() as k:
            r = await c.get("/api/topics/explore-feed")   # warm load
        assert r.status_code == 200
        # was ~2 queries per subject (30+ for 15 subjects)
        assert k.n <= 6, k.n


async def test_explore_cold_previews_run_in_parallel(db, mock_mode, monkeypatch):
    from app.services.aggregation_service import PreviewCard
    calls = []

    async def slow_preview(query, max_cards=8):
        calls.append(query)
        await asyncio.sleep(0.4)
        return [PreviewCard(topic=query, quote=f"hello {query}", source="News", source_url=f"http://x/{query}",
                            display_label="News", language="en", engagement_count=None, published_at=None)]

    async def noop(q):
        return None

    monkeypatch.setattr(routes_topics, "Backgroundrefresh", noop)
    monkeypatch.setattr(settings, "Discover_subjects", [f"cold-{uuid.uuid4().hex[:6]}-{i}" for i in range(12)])
    monkeypatch.setattr(routes_topics.aggregation, "build_cache_preview_cards", slow_preview)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        t0 = time.monotonic()
        r = await c.get("/api/topics/explore-feed")
        took = time.monotonic() - t0
    assert r.status_code == 200
    assert len(calls) == 12
    assert took < 2.5, took          # sequential would be 12 x 0.4 = 4.8s
    assert len(r.json()) > 0


async def test_following_bulk_cards_is_one_topic_query(db, mock_mode):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        ids = []
        for i in range(6):
            r = await _post(c, query=f"fol-{uuid.uuid4().hex[:8]}")
            ids.append(r["topic"]["id"])
        with _StmtCounter() as k:
            r = await c.post("/api/topics/bulk-cards", json={"topic_ids": ids})
        assert r.status_code == 200 and len(r.json()) > 0
        assert k.n <= 3, k.n   # was 1 per id + 1
