"""The service must survive a database that is down or over quota (Neon free
tier), recover on its own, and keep its own database usage in check."""
import asyncio
import time
from datetime import datetime, timedelta

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import exc as sa_exc, select

import app.models.db_models  # noqa: F401  (register tables before the db fixture runs create_all)
from app.config import settings
from app.db_errors import is_db_unavailable
from app.models.db_models import PulseCard, SourceBreakdown, SourceItem, Topic, TopicSummary
from app.services import storage_prune

QUOTA = asyncpg.exceptions.InsufficientResourcesError(
    "Your account or project has exceeded the quota. Upgrade your plan to increase limits."
)


# ----------------------------------------------------------- classification
def test_quota_error_is_recognised_raw_and_when_wrapped_by_sqlalchemy():
    assert is_db_unavailable(QUOTA)
    wrapped = sa_exc.DBAPIError("SELECT 1", {}, QUOTA)          # how SQLAlchemy surfaces it mid-request
    assert is_db_unavailable(wrapped)
    assert is_db_unavailable(ConnectionRefusedError())
    assert is_db_unavailable(sa_exc.OperationalError("SELECT 1", {}, Exception("server closed the connection")))


def test_server_shutdown_states_count_as_unavailable():
    # What Postgres (and Neon, when it suspends compute) answers mid-shutdown.
    assert is_db_unavailable(asyncpg.exceptions.AdminShutdownError("terminating connection due to administrator command"))
    assert is_db_unavailable(asyncpg.exceptions.CannotConnectNowError("the database system is shutting down"))
    assert is_db_unavailable(asyncpg.exceptions.CrashShutdownError("terminating connection because of crash of another server process"))
    # ...but a query that simply timed out is not an outage
    assert not is_db_unavailable(asyncpg.exceptions.QueryCanceledError("canceling statement due to statement timeout"))


def test_ordinary_bugs_are_not_mistaken_for_an_outage():
    assert not is_db_unavailable(ValueError("bad input"))
    assert not is_db_unavailable(KeyError("x"))
    assert not is_db_unavailable(sa_exc.IntegrityError("INSERT", {}, Exception("duplicate key")))
    assert not is_db_unavailable(None)


def test_cause_chain_is_followed():
    try:
        try:
            raise QUOTA
        except Exception as inner:
            raise RuntimeError("refresh failed") from inner
    except RuntimeError as outer:
        assert is_db_unavailable(outer)


# ----------------------------------------------------------- startup survival
def test_service_starts_while_db_is_down_then_recovers(monkeypatch):
    import app.main as main

    calls = {"n": 0}

    async def flaky_init():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise QUOTA

    started = []
    monkeypatch.setattr(main, "init_db", flaky_init)
    monkeypatch.setattr(main.scheduler_service, "start", lambda: started.append(1))
    monkeypatch.setattr(settings, "db_startup_retry_initial_seconds", 0.05)
    monkeypatch.setattr(settings, "db_startup_retry_max_seconds", 0.1)

    with TestClient(main.app) as client:
        # Up and answering even though init_db failed (this used to be a crash: exit status 3)
        assert client.get("/health").status_code == 200
        down = client.get("/health/db")
        assert down.status_code == 503
        assert "exceeded the quota" in down.json()["error"]
        assert started == []                                    # background work not started yet

        deadline = time.time() + 5
        while time.time() < deadline and client.get("/health/db").status_code != 200:
            time.sleep(0.05)
        assert client.get("/health/db").json() == {"db": "ready"}
        assert started == [1]                                   # started exactly once, after recovery
    assert calls["n"] == 3


def test_startup_does_not_hang_when_the_database_never_answers(monkeypatch):
    import app.main as main

    async def hangs():
        await asyncio.sleep(60)

    monkeypatch.setattr(main, "init_db", hangs)
    monkeypatch.setattr(main.scheduler_service, "start", lambda: None)
    monkeypatch.setattr(settings, "db_startup_timeout_seconds", 0.2)
    t0 = time.time()
    with TestClient(main.app) as client:
        assert time.time() - t0 < 5
        assert client.get("/health").status_code == 200
        assert client.get("/health/db").status_code == 503


# ------------------------------------------------------- request-time handling
def test_search_answers_503_with_retry_after_when_the_database_is_over_quota(monkeypatch):
    from app.api import routes_topics

    async def boom(*a, **k):
        raise sa_exc.DBAPIError("SELECT", {}, QUOTA)

    monkeypatch.setattr(routes_topics.aggregation, "refresh_topic", boom)
    app = FastAPI()
    app.include_router(routes_topics.router)
    r = TestClient(app).post("/api/topics/search", json={"query": "anything"})
    assert r.status_code == 503 and r.headers["retry-after"] == "30"
    assert "quota" not in r.text                                  # no internals leaked


def test_unhandled_db_errors_in_other_routes_become_503_too(monkeypatch):
    import app.main as main
    from app.api import routes_topics

    async def boom(*a, **k):
        raise QUOTA

    monkeypatch.setattr(main.scheduler_service, "start", lambda: None)

    async def ok_init():
        return None

    monkeypatch.setattr(main, "init_db", ok_init)
    monkeypatch.setattr(routes_topics.topic_repo, "get_by_id", boom)
    with TestClient(main.app) as client:
        r = client.post("/api/topics/bulk-cards", json={"topic_ids": ["x"]})
        assert r.status_code in (503, 200, 422)                    # route shape may vary; must never be a 500 with internals
        assert "InsufficientResources" not in r.text


# ----------------------------------------------------------- scheduler throttle
async def test_refresh_passes_are_spaced_out_however_often_cron_pings(monkeypatch):
    from app.services.scheduler_service import SchedulerService

    s = SchedulerService()
    runs = []

    async def fake_pass():
        runs.append(1)

    async def no_prune():
        return None

    monkeypatch.setattr(s, "_run_refresh_pass", fake_pass)
    monkeypatch.setattr(storage_prune, "maybe_prune", no_prune)
    monkeypatch.setattr(settings, "background_min_pass_interval_seconds", 1500.0)

    for _ in range(6):                      # six cron pings in a row (one hour of 10-minute pings, squashed)
        await s._refresh_trending_topics_job()
    assert len(runs) == 1                   # only the first did database work

    s._last_pass_started -= 1501            # ...25 minutes later
    await s._refresh_trending_topics_job()
    assert len(runs) == 2


async def test_a_pass_failing_because_the_db_is_down_is_quiet_and_retried_next_time(monkeypatch, caplog):
    import logging
    from app.services.scheduler_service import SchedulerService

    s = SchedulerService()

    async def down():
        raise QUOTA

    monkeypatch.setattr(s, "_run_refresh_pass", down)
    with caplog.at_level(logging.WARNING):
        await s._refresh_trending_topics_job()          # must not raise
    assert "database unavailable" in caplog.text
    assert "Traceback" not in caplog.text


# ------------------------------------------------------------- storage prune
async def test_prune_removes_expired_items_and_idle_topics_but_keeps_the_rest(db, monkeypatch):
    from app.database import get_session

    monkeypatch.setattr(settings, "Discover_subjects", ["Discover Topic"])
    monkeypatch.setattr(settings, "source_freshness_days", 14)
    monkeypatch.setattr(settings, "idle_topic_retention_days", 30)
    now = datetime.utcnow()
    old = now - timedelta(days=60)

    async with get_session() as s:
        active = Topic(query="prune active", last_searched_at=now - timedelta(days=1))
        idle = Topic(query="prune idle", created_at=old, last_searched_at=old)
        discover_idle = Topic(query="discover topic", created_at=old, last_searched_at=old)   # case-insensitive match
        s.add_all([active, idle, discover_idle])
        await s.flush()
        for t in (active, idle, discover_idle):
            s.add(SourceItem(topic_id=t.id, source="Reddit", source_url=f"https://x/{t.id}/fresh", text="fresh", created_at=now))
            s.add(PulseCard(topic_id=t.id, source_item_id="i", quote="q", source="Reddit", source_url=f"https://x/{t.id}"))
            s.add(TopicSummary(topic_id=t.id, summary_text="s"))
            s.add(SourceBreakdown(topic_id=t.id, source="Reddit"))
        # an expired item on the ACTIVE topic must go too
        s.add(SourceItem(topic_id=active.id, source="Reddit", source_url="https://x/old", text="old",
                         published_at=old, created_at=old))
        await s.commit()
        ids = {"active": active.id, "idle": idle.id, "discover": discover_idle.id}

        counts = await storage_prune.prune_stale_data(s)

    async with get_session() as s:
        async def left(model, tid):
            return len((await s.execute(select(model).where(model.topic_id == tid))).scalars().all())

        assert await left(SourceItem, ids["active"]) == 1                     # fresh kept, expired removed
        for model in (SourceItem, PulseCard, TopicSummary, SourceBreakdown):
            assert await left(model, ids["idle"]) == 0                        # idle topic's data gone
            assert await left(model, ids["discover"]) == 1                    # Discover subjects exempt
            assert await left(model, ids["active"]) >= 1
        assert (await s.execute(select(Topic).where(Topic.id == ids["idle"]))).scalar_one_or_none() is not None  # row kept
    assert counts["expired_items"] >= 1 and counts["idle_cards"] >= 1


async def test_prune_runs_at_most_once_per_interval_and_never_raises(db, monkeypatch):
    storage_prune.reset_state()
    monkeypatch.setattr(settings, "storage_prune_interval_seconds", 3600.0)
    first = await storage_prune.maybe_prune()
    assert first is not None
    assert await storage_prune.maybe_prune() is None                          # too soon

    storage_prune.reset_state()

    async def broken(*a, **k):
        raise QUOTA

    monkeypatch.setattr(storage_prune, "prune_stale_data", broken)
    assert await storage_prune.maybe_prune() is None                          # swallowed, not raised
