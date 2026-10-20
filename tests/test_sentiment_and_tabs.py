"""Neutral sentiment (lexicon fallback) and the All / Near you / Newsletters /
Social media tabs."""
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.db_models import PulseCard, SourceBreakdown, Topic, TopicSummary
from app.models.schemas import PulseCard as PulseCardSchema
from app.services.category_service import (
    annotate_and_filter, category_for_source, is_near_you, normalize_tab,
)
from app.services.sentiment_service import SentimentService, lexicon_sentiment


# --- sentiment ---------------------------------------------------------------

def test_clearly_happy_text_is_positive_not_random_negative():
    # The exact sentence the app showed as "Negative".
    r = lexicon_sentiment("Have a great day with my amazing client all the way from NewYork")
    assert r.label == "positive"


def test_clearly_bad_text_is_negative():
    assert lexicon_sentiment("This is terrible, the update ruined everything and I hate it").label == "negative"


def test_plain_factual_text_is_neutral():
    for t in ("The committee will meet on Tuesday to review the proposal",
              "Git is a distributed version control system"):
        assert lexicon_sentiment(t).label == "neutral"


def test_non_english_is_neutral_rather_than_guessed():
    assert lexicon_sentiment("Le gouvernement a annoncé une nouvelle réforme des retraites hier soir").label == "neutral"
    assert lexicon_sentiment("").label == "neutral"


def test_result_is_deterministic():
    t = "What a wonderful result for the team"
    assert {lexicon_sentiment(t).label for _ in range(20)} == {"positive"}


def test_service_uses_lexicon_when_transformers_missing():
    svc = SentimentService()
    svc._ready, svc._pipeline = True, None
    labels = [r.label for r in svc.analyze(["I love this, it is fantastic", "It is Tuesday", "This is awful and I hate it"])]
    assert labels == ["positive", "neutral", "negative"]


# --- categories --------------------------------------------------------------

@pytest.mark.parametrize("source,expected", [
    ("Reddit", "social"), ("Bluesky", "social"), ("Mastodon", "social"), ("YouTube", "social"),
    ("HackerNews", "social"), ("BBC", "newsletter"), ("Le Monde", "newsletter"),
    ("News", "newsletter"), ("DevTo", "newsletter"), ("Hashnode", "newsletter"),
    ("Wikipedia", "other"), ("Mock", "other"), ("", "other"), (None, "other"),
])
def test_category_for_source(source, expected):
    assert category_for_source(source) == expected


def test_near_you_only_for_regional_outlets_in_the_viewers_country():
    assert is_near_you("Le Monde", "FR") is True
    assert is_near_you("Le Monde", "fr") is True
    assert is_near_you("Le Monde", "US") is False
    assert is_near_you("Le Monde", None) is False
    assert is_near_you("BBC", "FR") is False       # global outlet: not "near"
    assert is_near_you("Reddit", "FR") is False


def _card(source):
    return PulseCardSchema(id=source, topic="t", quote="q", source=source, sentiment="neutral",
                           source_url=f"https://x/{source}", display_label="l", created_at=datetime.utcnow())


def test_annotate_and_filter_tabs():
    cards = [_card(s) for s in ("Le Monde", "BBC", "Reddit", "Wikipedia")]
    ids = lambda tab: [c.id for c in annotate_and_filter([_card(s) for s in ("Le Monde", "BBC", "Reddit", "Wikipedia")], "FR", tab)]
    assert ids(None) == ids("all") == ["Le Monde", "BBC", "Reddit", "Wikipedia"]
    assert ids("near_you") == ["Le Monde"]
    assert ids("newsletters") == ["Le Monde", "BBC"]
    assert ids("social_media") == ["Reddit"]
    assert normalize_tab("nonsense") == "all"
    # annotation happens even on "all"
    annotated = annotate_and_filter(cards, "FR", "all")
    assert annotated[0].near_you is True and annotated[0].category == "newsletter"


# --- routes (serve-time, per-viewer) ----------------------------------------

async def _seed(session):
    topic = Topic(query="Tabs Topic")
    session.add(topic)
    await session.flush()
    session.add(TopicSummary(topic_id=topic.id, summary_text="s", sentiment_label="neutral", sentiment_score=0.0,
                             sentiment_positive=0.0, sentiment_neutral=1.0, sentiment_negative=0.0, themes_json="[]"))
    for i, src in enumerate(("Le Monde", "BBC", "Reddit")):
        session.add(PulseCard(topic_id=topic.id, source_item_id=f"s{i}", quote=f"q{i}", source=src,
                              source_url=f"https://x/{i}", display_label=f"{src} quote"))
        session.add(SourceBreakdown(topic_id=topic.id, source=src, item_count=1, card_count=1))
    await session.commit()
    return topic


@pytest.mark.asyncio
async def test_topic_cards_route_filters_by_tab_and_is_per_country(db):
    topic = await _seed(db)
    client = TestClient(app)
    get = lambda **p: [c["source"] for c in client.get(f"/api/topics/{topic.id}/cards", params=p).json()]

    assert set(get(country="FR")) == {"Le Monde", "BBC", "Reddit"}
    assert get(country="FR", category="near_you") == ["Le Monde"]
    assert set(get(country="FR", category="newsletters")) == {"Le Monde", "BBC"}
    assert get(country="FR", category="social_media") == ["Reddit"]
    # A US viewer has no "near you" cards, and never sees France-only ones at all.
    assert get(country="US", category="near_you") == []
    assert "Le Monde" not in get(country="US")
    # Fields are on the payload for the app.
    card = client.get(f"/api/topics/{topic.id}/cards", params={"country": "FR"}).json()[0]
    assert "category" in card and "near_you" in card
