"""GDELT connector and registry-driven regional RSS outlets.

HTTP is replaced by httpx's MockTransport returning what GDELT / the outlets
are documented to send. These prove the connector and gating logic; whether
GDELT or a given outlet's feed is reachable from your host is what
GET /api/sources/health and /api/sources/diagnostics report once deployed.
"""
import time

import httpx
import pytest

from app.config import settings
from app.connectors import gdelt_connector
from app.connectors.gdelt_connector import GdeltConnector, build_query
from app.connectors.news_connector import NewsRssConnector
from app.services import connector_health
from app.services.outlet_registry import (
    FRANCE_AND_NEIGHBOURS, FRANCOPHONE, REGIONAL_OUTLET_FEEDS, is_outlet_visible,
    news_outlet_names, outlet_for_article_url, regional_outlet_feeds,
)
from app.services.pulse_card_service import _NEWS_OUTLET_NAMES
from app.utils.http_errors import BAD_RESPONSE, NOT_FOUND, ConnectorUnavailable, describe_error


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(settings, "enable_mock_data", False)
    monkeypatch.setattr(settings, "gdelt_min_interval_seconds", 0.0)
    gdelt_connector.reset_state()
    connector_health.reset()
    yield
    gdelt_connector.reset_state()
    connector_health.reset()


@pytest.fixture
def http(monkeypatch):
    real = httpx.AsyncClient
    state = {"handler": lambda request: httpx.Response(500), "calls": []}

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)

        def handler(request):
            state["calls"].append(request)
            return state["handler"](request)

        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return state


def article(url, title, **kw):
    return {"url": url, "title": title, "seendate": "20260919T083000Z", "domain": url.split("/")[2],
            "language": "French", "sourcecountry": "France", **kw}


# --- GDELT: query building ----------------------------------------------------

class TestBuildQuery:
    def test_keywords_are_anded_and_short_ones_dropped(self):
        assert build_query("Smart AI Assistants", None) == "smart assistants sourcelang:english"

    def test_request_language_becomes_a_sourcelang_operator_inside_the_query(self):
        assert build_query("Coupe du monde", "fr") == "coupe monde sourcelang:french"
        assert build_query("Coupe du monde", "FR-ca") == "coupe monde sourcelang:french"

    def test_an_unmapped_language_means_no_filter_not_the_default(self):
        assert build_query("Smart Assistants", "sw") == "smart assistants"

    def test_default_language_can_be_switched_off(self, monkeypatch):
        monkeypatch.setattr(settings, "gdelt_default_language", "")
        assert build_query("Bitcoin", None) == "bitcoin"

    def test_a_topic_with_nothing_searchable_returns_none(self):
        assert build_query("AI", None) is None
        assert build_query("  ", None) is None

    def test_query_length_is_bounded(self):
        q = build_query("alpha bravo charlie delta echo foxtrot golf", None)
        assert len([w for w in q.split() if not w.startswith("sourcelang")]) <= 5


# --- GDELT: fetching ----------------------------------------------------------

class TestGdeltFetch:
    async def test_request_shape(self, http):
        http["handler"] = lambda r: httpx.Response(200, json={"articles": []})
        await GdeltConnector().fetch("Smart Assistants", limit=10, language="fr")
        req = http["calls"][0]
        assert req.url.host == "api.gdeltproject.org" and req.url.path == "/api/v2/doc/doc"
        q = dict(req.url.params)
        assert q["query"] == "smart assistants sourcelang:french"       # operator INSIDE the query
        assert "sourcelang" not in q and "sourcecountry" not in q       # never as URL params
        assert q["mode"] == "artlist" and q["format"] == "json" and q["timespan"] == "7d"

    async def test_articles_become_items_with_outlet_attribution(self, http):
        http["handler"] = lambda r: httpx.Response(200, json={"articles": [
            article("https://www.lemonde.fr/a/1", "Les assistants intelligents progressent"),
            article("https://www.bbc.co.uk/news/2", "Smart assistants advance", language="English"),
            article("https://blog.example.org/3", "Un blog parle des assistants"),
        ]})
        items = await GdeltConnector().fetch("assistants", limit=10)
        by_url = {i.source_url: i for i in items}

        assert by_url["https://www.lemonde.fr/a/1"].source == "Le Monde"
        assert by_url["https://www.bbc.co.uk/news/2"].source == "BBC"
        assert by_url["https://blog.example.org/3"].source == "News"      # unknown publisher: generic
        lm = by_url["https://www.lemonde.fr/a/1"]
        assert lm.text == lm.title == "Les assistants intelligents progressent"
        assert lm.author == "www.lemonde.fr" and lm.language == "fr"
        assert lm.published_at is not None and lm.published_at.year == 2026

    async def test_a_regional_outlets_article_stays_gated_to_its_region(self, http):
        http["handler"] = lambda r: httpx.Response(200, json={"articles": [article("https://www.france24.com/fr/x", "Titre")]})
        item = (await GdeltConnector().fetch("titre", limit=5))[0]
        assert item.source == "France24"
        assert is_outlet_visible(item.source, "FR") and not is_outlet_visible(item.source, "US")

    async def test_syndicated_duplicates_are_collapsed(self, http):
        http["handler"] = lambda r: httpx.Response(200, json={"articles": [
            article("https://a.example/1", "Same Wire Story!"),
            article("https://b.example/2", "same wire story"),
            article("https://a.example/1", "Same Wire Story!"),
            article("https://c.example/3", "A different story"),
        ]})
        assert len(await GdeltConnector().fetch("story", limit=10)) == 2

    async def test_limit_is_respected(self, http):
        http["handler"] = lambda r: httpx.Response(200, json={"articles": [article(f"https://x.example/{i}", f"Title number {i}") for i in range(20)]})
        assert len(await GdeltConnector().fetch("title", limit=5)) == 5

    @pytest.mark.parametrize("body", ["", "   ", "{}", '{"articles": []}', '{"articles": null}'])
    async def test_no_matches_is_an_empty_result_not_an_error(self, http, body):
        http["handler"] = lambda r: httpx.Response(200, text=body)
        assert await GdeltConnector().fetch("nothing", limit=5) == []

    async def test_a_rejected_query_surfaces_gdeltss_own_message(self, http):
        http["handler"] = lambda r: httpx.Response(200, text="Query was too short or too long.")
        with pytest.raises(ValueError) as exc:
            await GdeltConnector().fetch("something", limit=5)
        assert "too short" in str(exc.value)
        assert describe_error(exc.value).kind == BAD_RESPONSE

    async def test_nothing_searchable_makes_no_request(self, http):
        assert await GdeltConnector().fetch("AI", limit=5) == []
        assert http["calls"] == []

    async def test_enabled_follows_the_setting_and_mock_mode(self, monkeypatch):
        assert await GdeltConnector().enabled() is True
        monkeypatch.setattr(settings, "gdelt_enabled", False)
        assert await GdeltConnector().enabled() is False
        monkeypatch.setattr(settings, "gdelt_enabled", True)
        monkeypatch.setattr(settings, "enable_mock_data", True)
        assert await GdeltConnector().enabled() is False


class TestGdeltRateLimiting:
    async def test_repeat_searches_are_served_from_cache(self, http):
        http["handler"] = lambda r: httpx.Response(200, json={"articles": [article("https://x.example/1", "A title here")]})
        await GdeltConnector().fetch("Smart Assistants", limit=5)
        await GdeltConnector().fetch("  smart   assistants ", limit=5)
        assert len(http["calls"]) == 1

    async def test_empty_results_are_cached_too(self, http):
        http["handler"] = lambda r: httpx.Response(200, json={})
        await GdeltConnector().fetch("nothing here", limit=5)
        await GdeltConnector().fetch("nothing here", limit=5)
        assert len(http["calls"]) == 1

    async def test_calls_are_spaced_by_the_minimum_interval(self, http, monkeypatch):
        monkeypatch.setattr(settings, "gdelt_min_interval_seconds", 0.3)
        monkeypatch.setattr(settings, "gdelt_max_wait_seconds", 5.0)
        http["handler"] = lambda r: httpx.Response(200, json={})
        t0 = time.monotonic()
        await GdeltConnector().fetch("first topic", limit=5)
        await GdeltConnector().fetch("second topic", limit=5)
        assert time.monotonic() - t0 >= 0.28

    async def test_a_call_that_would_wait_too_long_is_skipped_with_a_reason_and_no_request(self, http, monkeypatch):
        monkeypatch.setattr(settings, "gdelt_min_interval_seconds", 50.0)
        monkeypatch.setattr(settings, "gdelt_max_wait_seconds", 1.0)
        http["handler"] = lambda r: httpx.Response(200, json={})
        await GdeltConnector().fetch("first topic", limit=5)
        with pytest.raises(ConnectorUnavailable) as exc:
            await GdeltConnector().fetch("second topic", limit=5)
        assert len(http["calls"]) == 1
        assert "queued" in str(exc.value)

    async def test_gdeltss_plain_text_throttle_message_triggers_a_backoff(self, http):
        http["handler"] = lambda r: httpx.Response(200, text="Please limit requests to one every 5 seconds.")
        with pytest.raises(ConnectorUnavailable):
            await GdeltConnector().fetch("first topic", limit=5)
        with pytest.raises(ConnectorUnavailable):   # backing off: no new request
            await GdeltConnector().fetch("other topic", limit=5)
        assert len(http["calls"]) == 1

    async def test_http_429_triggers_a_backoff(self, http):
        http["handler"] = lambda r: httpx.Response(429)
        with pytest.raises(httpx.HTTPStatusError):
            await GdeltConnector().fetch("first topic", limit=5)
        with pytest.raises(ConnectorUnavailable):
            await GdeltConnector().fetch("other topic", limit=5)
        assert len(http["calls"]) == 1


# --- regional RSS: the registry ------------------------------------------------

class TestRegistry:
    def test_every_regional_outlet_is_well_formed_and_unique(self):
        names = [f.name for f in REGIONAL_OUTLET_FEEDS]
        urls = [f.feed_url for f in REGIONAL_OUTLET_FEEDS]
        assert len(set(names)) == len(names) and len(set(urls)) == len(urls)
        for f in REGIONAL_OUTLET_FEEDS:
            assert f.feed_url.startswith("https://") and f.domains and f.countries
            assert len(f.name) <= 40                       # SourceItem.source column

    def test_visibility_follows_the_declared_countries(self):
        assert is_outlet_visible("Le Figaro", "FR") and is_outlet_visible("Le Figaro", "be")
        assert not is_outlet_visible("Le Figaro", "US") and not is_outlet_visible("Le Figaro", None)
        assert not is_outlet_visible("Le Figaro", "CA")            # domestic press: narrower than francophone
        assert is_outlet_visible("RFI", "CA") and is_outlet_visible("RFI", "SN")   # international broadcaster
        assert FRANCE_AND_NEIGHBOURS < FRANCOPHONE

    def test_global_outlets_and_unlisted_sources_are_unaffected(self):
        for name in ("BBC", "CNN", "Reuters", "Reddit", "News", "HackerNews"):
            assert is_outlet_visible(name, None) and is_outlet_visible(name, "US")

    def test_regional_feeds_can_be_switched_off(self, monkeypatch):
        assert len(regional_outlet_feeds()) == len(REGIONAL_OUTLET_FEEDS)
        monkeypatch.setattr(settings, "regional_outlets_enabled", False)
        assert regional_outlet_feeds() == []

    def test_every_outlet_shares_the_news_capping_family(self):
        for f in REGIONAL_OUTLET_FEEDS:
            assert f.name.lower() in _NEWS_OUTLET_NAMES
        assert "bbc" in _NEWS_OUTLET_NAMES and "news" in _NEWS_OUTLET_NAMES
        assert news_outlet_names() == _NEWS_OUTLET_NAMES

    @pytest.mark.parametrize("url,expected", [
        ("https://www.lemonde.fr/politique/article/1.html", "Le Monde"),
        ("https://lemonde.fr/x", "Le Monde"),
        ("https://www.bbc.com/news/1", "BBC"),
        ("https://www.bbc.co.uk/news/1", "BBC"),
        ("https://www.rfi.fr/fr/afrique/1", "RFI"),
        ("https://notlemonde.fr/x", None),          # whole-host-suffix match only
        ("https://lemonde.fr.evil.example/x", None),
        ("not a url", None),
        ("", None),
    ])
    def test_article_url_attribution(self, url, expected):
        assert outlet_for_article_url(url) == expected


# --- regional RSS: fetched through the News connector --------------------------

def rss(*entries):
    items = "".join(f"<item><title>{t}</title><link>{l}</link><description>{t}</description></item>" for t, l in entries)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{items}</channel></rss>'


class TestRegionalFeedsThroughNewsConnector:
    @pytest.fixture(autouse=True)
    def only_registry_feeds(self, monkeypatch):
        # Isolate the regional outlets from the global ones and the search feeds.
        monkeypatch.setattr(settings, "major_outlet_rss_feeds", "")
        monkeypatch.setattr(settings, "news_rss_feeds", "")
        monkeypatch.setattr(settings, "reuters_rss_workaround_template", "https://reuters.invalid/?q={query}")

    def _handler(self, overrides=None):
        overrides = overrides or {}

        def handler(request):
            url = str(request.url)
            if url in overrides:
                return overrides[url]
            if "reuters.invalid" in url:
                return httpx.Response(200, text=rss())
            return httpx.Response(200, text=rss(("Assistants intelligents au Salon", f"{url}/article-1")))
        return handler

    async def test_every_regional_feed_is_fetched_and_attributed_to_its_outlet(self, http):
        http["handler"] = self._handler()
        items = await NewsRssConnector().fetch("assistants intelligents", limit=50)
        requested = {str(c.url) for c in http["calls"]}
        assert {f.feed_url for f in REGIONAL_OUTLET_FEEDS} <= requested
        assert {i.source for i in items} == {f.name for f in REGIONAL_OUTLET_FEEDS}

    async def test_a_url_in_both_the_env_list_and_the_registry_is_fetched_once(self, http, monkeypatch):
        france24 = "https://www.france24.com/fr/rss"
        monkeypatch.setattr(settings, "major_outlet_rss_feeds", france24)
        http["handler"] = self._handler()
        await NewsRssConnector().fetch("assistants intelligents", limit=50)
        assert [str(c.url) for c in http["calls"]].count(france24) == 1

    async def test_switching_regional_outlets_off_stops_fetching_them(self, http, monkeypatch):
        monkeypatch.setattr(settings, "regional_outlets_enabled", False)
        http["handler"] = self._handler()
        await NewsRssConnector().fetch("assistants", limit=50)
        assert not ({f.feed_url for f in REGIONAL_OUTLET_FEEDS} & {str(c.url) for c in http["calls"]})

    async def test_items_are_filtered_by_the_topic(self, http):
        http["handler"] = self._handler()
        assert await NewsRssConnector().fetch("cryptomonnaie bitcoin", limit=50) == []

    async def test_a_dead_feed_is_reported_by_outlet_name_and_does_not_break_the_others(self, http):
        broken = next(f for f in REGIONAL_OUTLET_FEEDS if f.name == "Le Figaro")
        http["handler"] = self._handler({broken.feed_url: httpx.Response(404)})
        items = await NewsRssConnector().fetch("assistants intelligents", limit=50)

        assert "Le Figaro" not in {i.source for i in items}
        assert len(items) == len(REGIONAL_OUTLET_FEEDS) - 1
        health = connector_health.snapshot()
        assert health["Le Figaro"]["last_status"] == "error"
        assert health["Le Figaro"]["last_error"]["kind"] == NOT_FOUND
        assert health["Le Monde"]["last_status"] == "ok"

    async def test_an_html_page_served_where_a_feed_used_to_be_is_an_error_not_silence(self, http):
        moved = next(f for f in REGIONAL_OUTLET_FEEDS if f.name == "RFI")
        http["handler"] = self._handler({moved.feed_url: httpx.Response(200, text="<html><body>Page moved</body></html>")})
        await NewsRssConnector().fetch("assistants intelligents", limit=50)
        err = connector_health.snapshot()["RFI"]["last_error"]
        assert err["kind"] == BAD_RESPONSE and "RSS" in err["message"]

    async def test_a_valid_but_empty_feed_is_not_mistaken_for_a_broken_one(self, http):
        empty = next(f for f in REGIONAL_OUTLET_FEEDS if f.name == "Le Monde")
        http["handler"] = self._handler({empty.feed_url: httpx.Response(200, text=rss())})
        await NewsRssConnector().fetch("assistants intelligents", limit=50)
        assert connector_health.snapshot()["Le Monde"]["last_status"] == "empty"

    async def test_a_healthy_feed_with_no_matching_entries_is_empty_not_an_error(self, http):
        http["handler"] = self._handler()
        await NewsRssConnector().fetch("cryptomonnaie bitcoin", limit=50)
        health = connector_health.snapshot()
        assert health["Le Monde"]["last_status"] == "empty" and health["Le Monde"]["consecutive_failures"] == 0


# --- end to end: GDELT -> pipeline -> stored cards -> country gating -----------

class TestGdeltThroughTheWholePipeline:
    def _search(self, client, country, query="assistants intelligents"):
        # Cards are stored per topic and shared across searches, so tests that
        # must not see each other's stored cards use different topic strings.
        r = client.post("/api/topics/search", json={"query": query, "country": country, "language": "fr"})
        assert r.status_code == 200, r.text
        return {c["source"] for c in r.json()["cards"]}

    def test_headline_only_gdelt_items_become_cards_and_are_gated_by_country(self, http, monkeypatch):
        from datetime import datetime, timezone

        from fastapi.testclient import TestClient

        from app.api.routes_topics import aggregation
        from app.main import app

        # Dated today: the aggregator drops items older than source_freshness_days.
        today = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        monkeypatch.setattr(aggregation, "fetch_plan", [(GdeltConnector(), "GDELT")])
        http["handler"] = lambda r: httpx.Response(200, json={"articles": [
            article("https://www.france24.com/fr/europe/1", "Les assistants intelligents transforment le quotidien des Français", seendate=today),
            article("https://www.lemonde.fr/tech/2", "Assistants intelligents : la bataille des géants du numérique s'intensifie", seendate=today),
            article("https://www.bbc.com/news/3", "Smart assistants are changing how people search online", seendate=today, language="English"),
            article("https://blog.example.org/4", "Pourquoi les assistants intelligents divisent les experts du secteur", seendate=today),
        ]})

        with TestClient(app) as client:
            fr_sources = self._search(client, "FR")
            us_sources = self._search(client, "US")

        assert {"France24", "Le Monde"} <= fr_sources            # regional outlets reach French viewers...
        assert not ({"France24", "Le Monde"} & us_sources)       # ...and only them
        assert "BBC" in us_sources and "News" in us_sources      # global + unknown publishers reach everyone
        assert "GDELT" not in fr_sources | us_sources            # the connector name never leaks as a card source

    def test_items_older_than_the_freshness_window_are_discarded_by_the_pipeline(self, http, monkeypatch):
        # Documents WHY gdelt_timespan should not exceed source_freshness_days.
        from fastapi.testclient import TestClient

        from app.api.routes_topics import aggregation
        from app.main import app

        monkeypatch.setattr(aggregation, "fetch_plan", [(GdeltConnector(), "GDELT")])
        http["handler"] = lambda r: httpx.Response(200, json={"articles": [
            article("https://www.bbc.com/news/old", "Smart assistants are changing how people search online (old story)",
                    seendate="20200101T000000Z", language="English"),
        ]})
        with TestClient(app) as client:
            assert self._search(client, "FR", query="old story smart assistants") == set()
