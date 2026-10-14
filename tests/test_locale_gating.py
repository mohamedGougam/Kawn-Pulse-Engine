"""Phase 3: regional/global outlet gating.

Two layers get tested:
  - app.services.outlet_registry: the pure visibility rule.
  - The four routes that return card lists (search, explore-feed,
    bulk-cards, {topic_id}/cards): each must apply that rule to what it
    returns, including to cards that were already sitting in the DB before
    this feature existed (the "serve-time, not fetch-time" requirement).
"""
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.db_models import PulseCard, SourceBreakdown, Topic, TopicSummary
from app.services.aggregation_service import RefreshResult
from app.services.outlet_registry import is_outlet_visible, filter_visible_cards


# --- pure registry logic -----------------------------------------------------

@pytest.mark.parametrize("country", [None, "US", "DE", "fr", "FR"])
def test_global_outlets_visible_everywhere(country):
    for name in ("BBC", "CNN", "NYT", "Al Jazeera", "Al Arabiya", "Euronews", "Reuters"):
        assert is_outlet_visible(name, country) is True


def test_france24_only_in_its_countries():
    assert is_outlet_visible("France24", "FR") is True
    assert is_outlet_visible("France24", "fr") is True  # case-insensitive
    assert is_outlet_visible("France24", "BE") is True
    assert is_outlet_visible("France24", "US") is False
    assert is_outlet_visible("France24", None) is False


def test_non_curated_sources_are_never_gated():
    for name in ("Reddit", "Hacker News", "News", "Mastodon", "Bluesky", ""):
        assert is_outlet_visible(name, None) is True
        assert is_outlet_visible(name, "US") is True


class _Card:
    def __init__(self, source):
        self.source = source


def test_filter_visible_cards_keeps_order_and_drops_only_gated_ones():
    cards = [_Card("BBC"), _Card("France24"), _Card("Reddit"), _Card("France24")]
    assert [c.source for c in filter_visible_cards(cards, "US")] == ["BBC", "Reddit"]
    assert [c.source for c in filter_visible_cards(cards, "FR")] == ["BBC", "France24", "Reddit", "France24"]
    assert filter_visible_cards([], "FR") == []


# --- routes: DB already has both global and regional cards for a topic -----

async def _seed_topic_with_cards(session, query="World News"):
    topic = Topic(query=query)
    session.add(topic)
    await session.flush()
    session.add(TopicSummary(
        topic_id=topic.id, summary_text="s", sentiment_label="neutral", sentiment_score=0.0,
        sentiment_positive=0.0, sentiment_neutral=1.0, sentiment_negative=0.0, themes_json="[]",
    ))
    bbc = PulseCard(topic_id=topic.id, source_item_id="s1", quote="q1", source="BBC",
                     source_url="https://bbc.com/a", display_label="BBC quote")
    fr = PulseCard(topic_id=topic.id, source_item_id="s2", quote="q2", source="France24",
                    source_url="https://france24.com/a", display_label="France24 quote")
    session.add(bbc)
    session.add(fr)
    session.add(SourceBreakdown(topic_id=topic.id, source="BBC", item_count=1, card_count=1))
    session.add(SourceBreakdown(topic_id=topic.id, source="France24", item_count=1, card_count=1))
    await session.commit()
    return topic


@pytest.mark.asyncio
async def test_search_hides_france24_for_non_french_but_shows_for_french(db):
    topic = await _seed_topic_with_cards(db)

    fake_result = RefreshResult(
        topic_id=topic.id, missing_sources=[], cached_sources=[], source_freshness={}, disabled_sources=[],
    )
    with patch("app.api.routes_topics.aggregation.refresh_topic", new=AsyncMock(return_value=fake_result)):
        with TestClient(app) as client:
            us = client.post("/api/topics/search", json={"query": topic.query, "country": "US"})
            fr = client.post("/api/topics/search", json={"query": topic.query, "country": "FR"})
            none = client.post("/api/topics/search", json={"query": topic.query})

    assert us.status_code == 200, us.text
    us_sources = {c["source"] for c in us.json()["cards"]}
    fr_sources = {c["source"] for c in fr.json()["cards"]}
    none_sources = {c["source"] for c in none.json()["cards"]}

    assert us_sources == {"BBC"}
    assert fr_sources == {"BBC", "France24"}
    assert none_sources == {"BBC"}  # no country given -> regional outlets stay hidden, not shown by default


@pytest.mark.asyncio
async def test_get_cards_endpoint_applies_same_gating_to_already_persisted_cards(db):
    """This is the case that matters most: cards already sitting in the DB
    from BEFORE locale gating existed (or fetched by a different user's
    request) must still be filtered per-viewer at read time."""
    topic = await _seed_topic_with_cards(db)

    with TestClient(app) as client:
        us = client.get(f"/api/topics/{topic.id}/cards", params={"country": "US"})
        fr = client.get(f"/api/topics/{topic.id}/cards", params={"country": "FR"})

    assert {c["source"] for c in us.json()} == {"BBC"}
    assert {c["source"] for c in fr.json()} == {"BBC", "France24"}


@pytest.mark.asyncio
async def test_bulk_cards_applies_gating(db):
    topic = await _seed_topic_with_cards(db)

    with TestClient(app) as client:
        us = client.post("/api/topics/bulk-cards", json={"topic_ids": [topic.id]}, params={"country": "US"})
        fr = client.post("/api/topics/bulk-cards", json={"topic_ids": [topic.id]}, params={"country": "FR"})

    assert {c["source"] for c in us.json()} == {"BBC"}
    assert {c["source"] for c in fr.json()} == {"BBC", "France24"}


@pytest.mark.asyncio
async def test_single_card_deep_link_is_not_locale_gated(db):
    """A direct share link to one specific card should keep working
    regardless of the opener's country - only listing/browsing endpoints
    are gated, not a link someone was explicitly sent."""
    topic = await _seed_topic_with_cards(db)
    from app.repositories.pulse_card_repository import PulseCardRepository

    cards = await PulseCardRepository().list_for_topic(db, topic.id, page=1, page_size=10)
    france24_card = next(c for c in cards if c.source == "France24")

    with TestClient(app) as client:
        r = client.get(f"/api/topics/cards/{france24_card.id}")
    assert r.status_code == 200
    assert r.json()["source"] == "France24"


@pytest.mark.asyncio
async def test_explore_feed_gates_france24_and_country_param_is_plumbed(db):
    from app.repositories.topic_repository import TopicRepository
    from unittest.mock import patch as _patch

    subject = "Smart AI Assistants"
    topic = await TopicRepository().upsert(db, subject)
    await db.commit()
    bbc = PulseCard(topic_id=topic.id, source_item_id="s1", quote="q1", source="BBC",
                     source_url="https://bbc.com/a", display_label="BBC quote")
    fr = PulseCard(topic_id=topic.id, source_item_id="s2", quote="q2", source="France24",
                    source_url="https://france24.com/a", display_label="France24 quote")
    db.add(bbc)
    db.add(fr)
    await db.commit()

    with _patch("app.config.settings.Discover_subjects", [subject]):
        with TestClient(app) as client:
            us = client.get("/api/topics/explore-feed", params={"country": "US"})
            fr_resp = client.get("/api/topics/explore-feed", params={"country": "FR"})

    assert us.status_code == 200, us.text
    assert {c["source"] for c in us.json()} == {"BBC"}
    assert {c["source"] for c in fr_resp.json()} == {"BBC", "France24"}


# --- Merge fix: France24 must share the "News" capping family -------------

@pytest.mark.asyncio
async def test_france24_shares_the_news_capping_family_with_bbc(db):
    """Regression test for the render-cap + Phase 3 merge: without
    'france24' in _NEWS_OUTLET_NAMES, France24 would get its own separate
    per-connector allotment instead of competing for the same shared 'News'
    pool as BBC/CNN/etc - inconsistent with how it's actually fetched
    (same NewsRssConnector, same major_outlet_rss_feeds list)."""
    from datetime import datetime, timedelta

    from app.models.db_models import SourceItem
    from app.repositories.source_item_repository import SourceItemRepository
    from app.repositories.topic_repository import TopicRepository
    from app.services.pulse_card_service import _NEWS_OUTLET_NAMES

    assert "france24" in _NEWS_OUTLET_NAMES

    topic = await TopicRepository().upsert(db, "Capping test topic")
    await db.commit()

    now = datetime.utcnow()
    items = []
    # 5 BBC + 5 France24 = 10 items total sharing one "News" family.
    for i in range(5):
        items.append(SourceItem(
            topic_id=topic.id, source="BBC", source_url=f"https://bbc.com/{i}",
            external_id=f"bbc-{i}", created_at=now - timedelta(minutes=i),
        ))
        items.append(SourceItem(
            topic_id=topic.id, source="France24", source_url=f"https://france24.com/{i}",
            external_id=f"fr24-{i}", created_at=now - timedelta(minutes=i),
        ))
    for item in items:
        db.add(item)
    await db.commit()

    balanced = await SourceItemRepository().list_balanced_for_topic(
        db, topic.id, per_source_cap=3, news_outlet_names=_NEWS_OUTLET_NAMES,
    )

    # Capped at 3 total for the COMBINED News family, not 3 each -
    # proves BBC and France24 are genuinely sharing one pool.
    assert len(balanced) == 3
    sources = {item.source for item in balanced}
    assert sources <= {"BBC", "France24"}


@pytest.mark.asyncio
async def test_bulk_upsert_ignores_duplicates_against_existing_and_within_batch(db):
    """The new DB-native ON CONFLICT DO NOTHING upsert (replacing the old
    Python-side preload-and-dedup) must still behave identically: existing
    rows are skipped, and duplicates within the same incoming batch are
    skipped too."""
    from app.models.db_models import SourceItem
    from app.repositories.source_item_repository import SourceItemRepository
    from app.repositories.topic_repository import TopicRepository

    topic = await TopicRepository().upsert(db, "Upsert test topic")
    await db.commit()
    repo = SourceItemRepository()

    existing = SourceItem(topic_id=topic.id, source="BBC", source_url="https://bbc.com/a", external_id="a")
    db.add(existing)
    await db.commit()

    batch = [
        SourceItem(topic_id=topic.id, source="BBC", source_url="https://bbc.com/a", external_id="a"),  # already exists
        SourceItem(topic_id=topic.id, source="BBC", source_url="https://bbc.com/b", external_id="b"),  # new
        SourceItem(topic_id=topic.id, source="BBC", source_url="https://bbc.com/b", external_id="b"),  # dup within batch
    ]
    inserted = await repo.bulk_upsert_ignore_duplicates(db, batch)

    assert inserted == 1
    from sqlalchemy import select
    result = await db.execute(select(SourceItem).where(SourceItem.topic_id == topic.id))
    urls = {row[0].source_url for row in result.all()}
    assert urls == {"https://bbc.com/a", "https://bbc.com/b"}

    assert await repo.bulk_upsert_ignore_duplicates(db, []) == 0
