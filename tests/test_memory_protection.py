"""Protection against the instance running out of memory.

Covers: one shared R2 client with capped concurrency, the process-wide limit
on concurrent refreshes, de-duplicated background refreshes, the memory guard,
and a regression test for the scheduler's broken refresh call.
"""
import asyncio
import logging
import os
import signal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.models.db_models  # noqa: F401  (registers the tables before the db fixture runs create_all)
from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.services import memory_guard
from app.services.refresh_gate import RefreshBusy, gate
from app.storage.object_store import ObjectStore


@pytest.fixture(autouse=True)
def fresh_gate(monkeypatch):
    # the gate is process-wide; every test starts from a clean one
    gate._loop = None
    gate.bg_last_attempt.clear()
    monkeypatch.setattr(settings, "enable_mock_data", False)
    yield
    gate._loop = None


# ------------------------------------------------------------------ R2 client
class _Body:
    def __init__(self, data: bytes):
        self._d = data

    async def read(self):
        return self._d


class _FakeS3:
    def __init__(self):
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = 0

    async def get_object(self, Bucket, Key):
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        return {"Body": _Body(b'{"items": []}')}

    async def put_object(self, **kw):
        await asyncio.sleep(0.01)


class _FakeSession:
    """Stands in for aioboto3.Session; counts how many clients get built/closed."""

    def __init__(self):
        self.created = 0
        self.closed = 0
        self.s3 = _FakeS3()

    def client(self, *a, **kw):
        outer = self

        class _CM:
            async def __aenter__(self_inner):
                outer.created += 1
                return outer.s3

            async def __aexit__(self_inner, *exc):
                outer.closed += 1

        return _CM()


def _configured_store(monkeypatch):
    monkeypatch.setattr(settings, "r2_account_id", "acct")
    monkeypatch.setattr(settings, "r2_access_key_id", "k")
    monkeypatch.setattr(settings, "r2_secret_access_key", "s")
    monkeypatch.setattr(settings, "r2_max_concurrency", 3)
    store = ObjectStore()
    store._session = _FakeSession()
    return store


async def test_r2_builds_one_client_for_many_calls(monkeypatch):
    store = _configured_store(monkeypatch)
    await asyncio.gather(*(store.get_json(f"S{i}", "t") for i in range(40)),
                         *(store.put_json(f"S{i}", "t", []) for i in range(40)))
    assert store._session.created == 1            # was: one client per call (80)
    await store.close()
    assert store._session.closed == 1


async def test_r2_in_flight_requests_are_capped(monkeypatch):
    store = _configured_store(monkeypatch)
    await asyncio.gather(*(store.get_json(f"S{i}", "t") for i in range(30)))
    assert store._session.s3.calls == 30
    assert store._session.s3.max_in_flight <= 3   # r2_max_concurrency


async def test_r2_unconfigured_raises_unavailable(monkeypatch):
    from app.storage.object_store import ObjectStoreUnavailable
    monkeypatch.setattr(settings, "r2_account_id", None)
    with pytest.raises(ObjectStoreUnavailable):
        await ObjectStore().get_json("News", "t")


# ------------------------------------------------------------ user-slot limit
async def test_user_refreshes_are_capped(monkeypatch):
    monkeypatch.setattr(settings, "max_concurrent_refreshes", 2)
    running = {"now": 0, "max": 0}

    async def work():
        async with gate.user_slot():
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
            await asyncio.sleep(0.02)
            running["now"] -= 1

    await asyncio.gather(*(work() for _ in range(8)))
    assert running["max"] == 2
    assert gate.active == 0


async def test_user_refresh_gets_busy_error_when_queue_wait_is_exceeded(monkeypatch):
    monkeypatch.setattr(settings, "max_concurrent_refreshes", 1)
    monkeypatch.setattr(settings, "refresh_queue_timeout_seconds", 0.05)
    release = asyncio.Event()

    async def holder():
        async with gate.user_slot():
            await release.wait()

    t = asyncio.create_task(holder())
    await asyncio.sleep(0.01)
    with pytest.raises(RefreshBusy):
        async with gate.user_slot():
            pass
    release.set()
    await t
    async with gate.user_slot():       # slot is free again afterwards
        pass


# ------------------------------------------------------- background de-dupe
async def test_background_refresh_dedupe_cooldown_and_memory_pressure(monkeypatch):
    monkeypatch.setattr(memory_guard, "over_soft_limit", lambda: False)
    monkeypatch.setattr(settings, "background_retry_cooldown_seconds", 300.0)
    gate._bind()

    assert gate.try_begin_background("Creed 3") is None
    assert gate.try_begin_background("  creed 3 ") == "already refreshing"   # same topic, normalised
    assert gate.try_begin_background("World Cup") is None                    # other topics unaffected

    gate.end_background("Creed 3")
    assert gate.try_begin_background("Creed 3") == "attempted recently"     # cool-down after a try

    monkeypatch.setattr(settings, "background_retry_cooldown_seconds", 0.0)
    assert gate.try_begin_background("Creed 3") is None
    gate.end_background("Creed 3")
    gate.end_background("World Cup")

    monkeypatch.setattr(memory_guard, "over_soft_limit", lambda: True)
    assert gate.try_begin_background("Anything") == "memory pressure"
    assert "anything" not in gate.bg_inflight                               # a skip must not leave the topic "in flight"


class _Fake:
    def __init__(self, name):
        self.name = name
        self.calls = 0

    async def enabled(self):
        return True

    async def fetch(self, topic, *, limit, language=None):
        self.calls += 1
        await asyncio.sleep(0.02)
        return [NormalizedRawItem(source=self.name, source_url=f"https://{self.name}.example/{topic}/{i}", topic=topic,
                                  title="Great debate", text=f"{topic} fans are talking about the new release number {i}",
                                  engagement_count=i) for i in range(5)]


def _fake_agg():
    from app.services.aggregation_service import AggregationService

    agg = AggregationService()
    fakes = [_Fake(n) for n in ("Reddit", "HackerNews", "Wikipedia")]
    agg.fetch_plan = [(f, f.name) for f in fakes]

    async def nocache(*a, **k):
        return None

    agg._read_cached_source = nocache
    agg._persist_heavy_cache = nocache
    return agg, fakes


async def test_concurrent_background_requests_for_one_topic_refresh_it_once(db, monkeypatch):
    monkeypatch.setattr(memory_guard, "over_soft_limit", lambda: False)
    agg, fakes = _fake_agg()
    results = await asyncio.gather(*(agg.refresh_in_background("dedupe topic") for _ in range(6)))
    assert sum(r is not None for r in results) == 1    # one did the work, five were skipped
    assert fakes[0].calls == 1                         # and only one fetch wave happened


async def test_scheduler_pass_really_refreshes_topics(db, monkeypatch, caplog):
    """Regression: the scheduler called refresh_topic(query=...) without the
    required `session`, so every background refresh died with a TypeError that
    was caught and logged. Nothing was ever refreshed in the background."""
    from app.database import get_session
    from app.repositories.pulse_card_repository import PulseCardRepository
    from app.repositories.topic_repository import TopicRepository
    from app.services.scheduler_service import SchedulerService

    monkeypatch.setattr(memory_guard, "over_soft_limit", lambda: False)
    monkeypatch.setattr(settings, "Discover_subjects", ["sched subject a", "sched subject b"])
    # large batch: other tests leave searched topics in the shared DB that
    # outrank these two in the priority queue, and must not crowd them out
    monkeypatch.setattr(settings, "background_batch_size", 500)

    sched = SchedulerService()
    sched.aggregation, _ = _fake_agg()

    with caplog.at_level(logging.WARNING):
        await sched._run_refresh_pass()

    assert "background refresh failed" not in caplog.text
    async with get_session() as s:
        for q in ("sched subject a", "sched subject b"):
            topic = await TopicRepository().get_by_query(s, q)
            assert await PulseCardRepository().count_for_topic(s, topic.id) > 0


# ---------------------------------------------------------------- memory guard
def test_current_rss_is_reported_on_linux():
    rss = memory_guard.current_rss_mb()
    assert rss is None or rss > 10


@pytest.mark.parametrize("rss, active, gunicorn, limit, expected", [
    (500.0, 0, True, 420, True),     # over the limit, idle, under gunicorn -> recycle
    (500.0, 2, True, 420, False),    # refreshes running: never cut them off
    (500.0, 0, False, 420, False),   # plain uvicorn/dev: nothing would restart us
    (500.0, 0, True, 0, False),      # recycling switched off
    (300.0, 0, True, 420, False),    # below the limit
    (None, 0, True, 420, False),     # can't read memory (non-Linux)
])
def test_should_recycle_rules(monkeypatch, rss, active, gunicorn, limit, expected):
    monkeypatch.setattr(memory_guard, "current_rss_mb", lambda: rss)
    monkeypatch.setattr(settings, "memory_recycle_mb", limit)
    monkeypatch.setattr(settings, "memory_recycle_min_uptime_seconds", 0.0)
    monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/22.0.0" if gunicorn else "")
    assert memory_guard.should_recycle(active) is expected


def test_a_young_worker_is_never_recycled_so_a_bad_limit_cannot_restart_loop(monkeypatch):
    monkeypatch.setattr(memory_guard, "current_rss_mb", lambda: 999.0)
    monkeypatch.setattr(settings, "memory_recycle_mb", 1)            # absurdly low limit
    monkeypatch.setattr(settings, "memory_recycle_min_uptime_seconds", 600.0)
    monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/22.0.0")
    assert memory_guard.should_recycle(0) is False                   # just started: leave it alone
    monkeypatch.setattr(memory_guard, "_STARTED", memory_guard._STARTED - 601)
    assert memory_guard.should_recycle(0) is True                    # old enough now


async def test_guard_loop_asks_the_worker_to_exit_once_when_over_limit(monkeypatch):
    killed = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(memory_guard, "current_rss_mb", lambda: 999.0)
    monkeypatch.setattr(settings, "memory_recycle_mb", 420)
    monkeypatch.setattr(settings, "memory_recycle_min_uptime_seconds", 0.0)
    monkeypatch.setenv("SERVER_SOFTWARE", "gunicorn/22.0.0")

    real_sleep = asyncio.sleep

    async def fast_sleep(_):
        await real_sleep(0)

    monkeypatch.setattr(memory_guard.asyncio, "sleep", fast_sleep)
    await asyncio.wait_for(memory_guard.run(), timeout=2)
    assert killed == [(os.getpid(), signal.SIGTERM)]


# ----------------------------------------------------------- overload response
def test_search_returns_503_with_retry_after_when_busy(monkeypatch):
    from app.api import routes_topics

    async def busy(*a, **k):
        raise RefreshBusy("too many")

    monkeypatch.setattr(routes_topics.aggregation, "refresh_topic", busy)
    app = FastAPI()
    app.include_router(routes_topics.router)
    r = TestClient(app).post("/api/topics/search", json={"query": "anything"})
    assert r.status_code == 503
    assert r.headers["retry-after"] == "10"
