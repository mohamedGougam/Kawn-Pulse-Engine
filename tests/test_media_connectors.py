"""Social/media connectors: the failure modes that made them look "not working",
and the visibility added so they can be diagnosed.

Nothing here talks to the real services. HTTP is replaced by httpx's
MockTransport returning the responses those APIs are documented to send
(including their error responses), so these prove the connector logic and the
error handling, NOT that Reddit/Bluesky/etc. are reachable from your host -
that's what GET /api/sources/diagnostics is for once deployed.
"""
import asyncio
import base64
import json
import logging
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.connectors import bluesky_connector, reddit_connector, youtube_connector
from app.connectors.base import NormalizedRawItem
from app.connectors.bluesky_connector import BlueskyConnector
from app.connectors.lemmy_connector import LemmyConnector
from app.connectors.mastodon_connector import MastodonConnector, candidate_hashtags
from app.connectors.reddit_connector import RedditConnector
from app.connectors.youtube_connector import YouTubeConnector
from app.main import app
from app.services import connector_health
from app.utils.http_errors import (
    AUTH_REJECTED, BAD_RESPONSE, NETWORK, NOT_FOUND, QUOTA_EXCEEDED, RATE_LIMITED, SERVER_ERROR,
    TIMEOUT, UNAVAILABLE, ConnectorUnavailable, describe_error, scrub,
)

SECRET = "AIzaSySECRETKEY123"


# --- fixtures / helpers -------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(settings, "enable_mock_data", False)
    bluesky_connector.reset_session_cache()
    reddit_connector.reset_token_cache()
    youtube_connector.reset_state()
    connector_health.reset()
    yield
    bluesky_connector.reset_session_cache()
    reddit_connector.reset_token_cache()
    youtube_connector.reset_state()
    connector_health.reset()


@pytest.fixture
def http(monkeypatch):
    """Route every httpx.AsyncClient through a handler the test sets."""
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


def status_error(status, *, url=f"https://www.googleapis.com/youtube/v3/search?q=x&key={SECRET}", body=None):
    request = httpx.Request("GET", url)
    response = httpx.Response(status, json=body, request=request) if body is not None else httpx.Response(status, request=request)
    # httpx puts the full URL (query string included) in this message.
    return httpx.HTTPStatusError(f"Client error '{status}' for url '{url}'", request=request, response=response)


def jwt(exp_in_seconds):
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64({'exp': time.time() + exp_in_seconds})}.sig"


def raw_item(source="X", url="https://example.com/1"):
    return NormalizedRawItem(source=source, source_url=url, topic="t", text="some text about the topic")


# --- error classification and secret safety -----------------------------------

class TestDescribeError:
    def test_quota_exceeded_is_recognised_and_the_key_never_appears(self):
        body = {"error": {"errors": [{"reason": "quotaExceeded"}], "message": "quota"}}
        info = describe_error(status_error(403, body=body))
        assert info.kind == QUOTA_EXCEEDED
        assert SECRET not in info.message
        assert "key=" not in info.message and "?" not in info.message
        assert "www.googleapis.com/youtube/v3/search" in info.message

    @pytest.mark.parametrize("status,kind", [
        (401, AUTH_REJECTED), (403, AUTH_REJECTED), (429, RATE_LIMITED), (404, NOT_FOUND), (503, SERVER_ERROR),
    ])
    def test_status_classification(self, status, kind):
        assert describe_error(status_error(status)).kind == kind

    def test_403_without_a_quota_reason_is_an_auth_problem_not_quota(self):
        assert describe_error(status_error(403, body={"error": "Forbidden"})).kind == AUTH_REJECTED

    def test_timeouts_and_network_errors(self):
        assert describe_error(httpx.ReadTimeout("slow")).kind == TIMEOUT
        assert describe_error(asyncio.TimeoutError()).kind == TIMEOUT
        assert describe_error(httpx.ConnectError("dns")).kind == NETWORK

    def test_parse_errors_and_connector_unavailable(self):
        assert describe_error(ValueError("bad shape")).kind == BAD_RESPONSE
        assert describe_error(ConnectorUnavailable("quota is out")).kind == UNAVAILABLE

    def test_scrub_removes_query_strings_and_credentials(self):
        text = "failed https://x.test/a?key=SECRET&q=1 with Authorization: Bearer abc.def.ghi and token=zzz"
        out = scrub(text)
        for leaked in ("SECRET", "abc.def.ghi", "zzz", "q=1"):
            assert leaked not in out


# --- Bluesky ------------------------------------------------------------------

BSKY_POST = {
    "uri": "at://did:plc:abc/app.bsky.feed.post/3kxyz", "cid": "bafy1",
    "author": {"handle": "alice.bsky.social"},
    "record": {"text": "Smart assistants are everywhere now", "createdAt": "2026-10-01T10:00:00Z", "langs": ["en"]},
    "likeCount": 3, "repostCount": 1, "replyCount": 0,
}


@pytest.fixture
def bsky_creds(monkeypatch):
    monkeypatch.setattr(settings, "bluesky_handle", "kawn.bsky.social")
    monkeypatch.setattr(settings, "bluesky_app_password", "abcd-efgh-ijkl-mnop")


def bsky_handler(token=None, *, search_status=200, search_error=None):
    token = token or jwt(7200)

    def handler(request):
        if request.url.path.endswith("com.atproto.server.createSession"):
            return httpx.Response(200, json={"accessJwt": token})
        if search_error and "bearer" in request.headers.get("authorization", "").lower() and handler.fail_next:
            handler.fail_next = False
            return httpx.Response(search_status, json={"error": search_error})
        return httpx.Response(200, json={"posts": [BSKY_POST]})

    handler.fail_next = True
    return handler


class TestBluesky:
    async def test_disabled_without_credentials_and_enabled_with_them(self, monkeypatch, bsky_creds):
        assert await BlueskyConnector().enabled() is True
        monkeypatch.setattr(settings, "bluesky_app_password", None)
        assert await BlueskyConnector().enabled() is False

    async def test_logs_in_then_searches_the_pds_with_a_bearer_token(self, http, bsky_creds):
        http["handler"] = bsky_handler()
        items = await BlueskyConnector().fetch("smart assistants", limit=5)

        assert [i.source_url for i in items] == ["https://bsky.app/profile/alice.bsky.social/post/3kxyz"]
        assert items[0].engagement_count == 4
        search = http["calls"][-1]
        assert search.url.host == "bsky.social" and search.url.path.endswith("app.bsky.feed.searchPosts")
        assert search.headers["authorization"].startswith("Bearer ")
        assert all(c.url.host != "public.api.bsky.app" for c in http["calls"])  # the endpoint that 403s

    async def test_the_session_is_reused_not_recreated_per_search(self, http, bsky_creds):
        http["handler"] = bsky_handler()
        await BlueskyConnector().fetch("a", limit=5)
        await BlueskyConnector().fetch("b", limit=5)
        logins = [c for c in http["calls"] if c.url.path.endswith("createSession")]
        assert len(logins) == 1

    async def test_a_nearly_expired_token_is_not_reused(self, http, bsky_creds):
        http["handler"] = bsky_handler(token=jwt(30))  # inside the refresh slack
        await BlueskyConnector().fetch("a", limit=5)
        await BlueskyConnector().fetch("b", limit=5)
        assert len([c for c in http["calls"] if c.url.path.endswith("createSession")]) == 2

    async def test_an_expired_token_triggers_one_relogin_and_retry(self, http, bsky_creds):
        http["handler"] = bsky_handler(search_status=400, search_error="ExpiredToken")
        items = await BlueskyConnector().fetch("a", limit=5)
        assert len(items) == 1
        assert len([c for c in http["calls"] if c.url.path.endswith("createSession")]) == 2

    async def test_rejected_login_raises_then_backs_off_instead_of_retrying_each_search(self, http, bsky_creds):
        http["handler"] = lambda request: httpx.Response(401, json={"error": "AuthenticationRequired"})
        with pytest.raises(httpx.HTTPStatusError):
            await BlueskyConnector().fetch("a", limit=5)
        calls_after_first = len(http["calls"])
        with pytest.raises(ConnectorUnavailable) as exc:
            await BlueskyConnector().fetch("a", limit=5)
        assert len(http["calls"]) == calls_after_first  # no new request
        assert "app password" in str(exc.value)


# --- Reddit -------------------------------------------------------------------

REDDIT_LISTING = {"data": {"children": [{"data": {
    "id": "abc", "title": "Smart assistants thread", "selftext": "", "permalink": "/r/tech/comments/abc/x/",
    "author": "bob", "score": 42, "created_utc": 1790000000,
}}]}}


@pytest.fixture
def reddit_creds(monkeypatch):
    monkeypatch.setattr(settings, "reddit_client_id", "cid")
    monkeypatch.setattr(settings, "reddit_client_secret", "csecret")


class TestReddit:
    async def test_happy_path_uses_oauth_and_the_configured_user_agent(self, http, reddit_creds, monkeypatch):
        monkeypatch.setattr(settings, "reddit_user_agent", "web:kawn:1.0 (by /u/kawnbot)")

        def handler(request):
            if request.url.path.endswith("access_token"):
                return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
            return httpx.Response(200, json=REDDIT_LISTING)

        http["handler"] = handler
        items = await RedditConnector().fetch("smart assistants", limit=5)

        assert items[0].engagement_count == 42
        search = http["calls"][-1]
        assert search.url.host == "oauth.reddit.com"  # not the blocked unauthenticated www host
        assert search.headers["user-agent"] == "web:kawn:1.0 (by /u/kawnbot)"

    async def test_rejected_credentials_now_raise_instead_of_silently_returning_nothing(self, http, reddit_creds):
        http["handler"] = lambda request: httpx.Response(401, json={"message": "Unauthorized", "error": 401})
        with pytest.raises(httpx.HTTPStatusError) as exc:
            await RedditConnector().fetch("x", limit=5)
        assert describe_error(exc.value).kind == AUTH_REJECTED

    async def test_bad_credentials_are_not_resent_on_every_search(self, http, reddit_creds):
        http["handler"] = lambda request: httpx.Response(401)
        with pytest.raises(httpx.HTTPStatusError):
            await RedditConnector().fetch("x", limit=5)
        n = len(http["calls"])
        with pytest.raises(ConnectorUnavailable) as exc:
            await RedditConnector().fetch("x", limit=5)
        assert len(http["calls"]) == n
        assert "Responsible Builder Policy" in str(exc.value)

    async def test_disabled_without_credentials(self):
        assert await RedditConnector().enabled() is False


# --- Mastodon -----------------------------------------------------------------

def toot(url, text, **kw):
    return {"id": url[-3:], "url": url, "content": f"<p>{text}</p>", "created_at": "2026-10-01T10:00:00Z",
            "account": {"acct": "carol"}, "reblogs_count": 1, "favourites_count": 2, **kw}


class TestMastodon:
    def test_hashtags_for_a_multi_word_topic_include_the_keywords_and_acronyms(self):
        exact, keywords = candidate_hashtags("Smart AI Assistants")
        assert exact == "smartaiassistants"
        assert set(keywords) == {"assistants", "smart", "ai"}
        assert candidate_hashtags("6G Mobile Networks")[1].count("6g") == 1
        assert candidate_hashtags("Bitcoin") == ("bitcoin", [])

    def test_request_count_is_bounded(self):
        exact, keywords = candidate_hashtags("one two three four five six seven")
        assert 1 + len(keywords) <= 4

    async def test_keyword_hashtag_posts_are_filtered_for_relevance_but_the_exact_tag_is_trusted(self, http):
        def handler(request):
            tag = request.url.path.rsplit("/", 1)[-1]
            if tag == "smartassistants":
                return httpx.Response(200, json=[toot("https://m.test/@a/111", "totally different words #smartassistants")])
            if tag == "assistants":
                return httpx.Response(200, json=[
                    toot("https://m.test/@b/222", "smart assistants are getting better"),
                    toot("https://m.test/@c/333", "our customer assistants are off today"),  # has the tag word, not "smart"
                ])
            return httpx.Response(200, json=[])

        http["handler"] = handler
        items = await MastodonConnector().fetch("Smart Assistants", limit=10)
        urls = {i.source_url for i in items}
        assert "https://m.test/@a/111" in urls          # exact tag: kept as-is
        assert "https://m.test/@b/222" in urls          # keyword tag + matches the topic
        assert "https://m.test/@c/333" not in urls      # keyword tag, but never mentions "smart"

    async def test_the_same_post_found_under_two_tags_is_returned_once(self, http):
        http["handler"] = lambda request: httpx.Response(200, json=[toot("https://m.test/@a/111", "smart assistants rock")])
        items = await MastodonConnector().fetch("Smart Assistants", limit=10)
        assert len(items) == 1

    async def test_one_failing_timeline_does_not_lose_the_others(self, http):
        def handler(request):
            if request.url.path.endswith("/smartassistants"):
                return httpx.Response(500)
            return httpx.Response(200, json=[toot("https://m.test/@b/222", "smart assistants everywhere")])

        http["handler"] = handler
        assert len(await MastodonConnector().fetch("Smart Assistants", limit=10)) == 1

    async def test_every_timeline_failing_is_an_error_not_an_empty_result(self, http):
        http["handler"] = lambda request: httpx.Response(401, json={"error": "This API requires an authenticated user"})
        with pytest.raises(httpx.HTTPStatusError) as exc:
            await MastodonConnector().fetch("Smart Assistants", limit=10)
        assert describe_error(exc.value).kind == AUTH_REJECTED


# --- YouTube ------------------------------------------------------------------

YT_RESPONSE = {"items": [{"id": {"videoId": "vid1"}, "snippet": {
    "title": "Smart assistants explained", "description": "A video", "channelTitle": "Chan", "publishedAt": "2026-10-01T10:00:00Z"}}]}
YT_QUOTA = {"error": {"code": 403, "message": "quota", "errors": [{"reason": "quotaExceeded"}]}}


@pytest.fixture
def yt_key(monkeypatch):
    monkeypatch.setattr(settings, "youtube_api_key", SECRET)


class TestYouTube:
    async def test_the_api_key_goes_in_a_header_never_the_url(self, http, yt_key):
        http["handler"] = lambda request: httpx.Response(200, json=YT_RESPONSE)
        await YouTubeConnector().fetch("smart assistants", limit=5)
        request = http["calls"][0]
        assert SECRET not in str(request.url) and "key=" not in str(request.url)
        assert request.headers["x-goog-api-key"] == SECRET

    async def test_a_failed_call_cannot_leak_the_key_through_its_error(self, http, yt_key):
        http["handler"] = lambda request: httpx.Response(403, json=YT_QUOTA)
        with pytest.raises(httpx.HTTPStatusError) as exc:
            await YouTubeConnector().fetch("a", limit=5)
        assert SECRET not in str(exc.value)
        assert SECRET not in describe_error(exc.value).message

    async def test_repeat_searches_for_a_topic_spend_no_quota(self, http, yt_key):
        http["handler"] = lambda request: httpx.Response(200, json=YT_RESPONSE)
        a = await YouTubeConnector().fetch("Smart Assistants", limit=5)
        b = await YouTubeConnector().fetch("  smart assistants ", limit=5)
        assert len(http["calls"]) == 1 and a[0].external_id == b[0].external_id == "vid1"

    async def test_cache_can_be_turned_off(self, http, yt_key, monkeypatch):
        monkeypatch.setattr(settings, "youtube_cache_ttl_seconds", 0)
        http["handler"] = lambda request: httpx.Response(200, json=YT_RESPONSE)
        await YouTubeConnector().fetch("a", limit=5)
        await YouTubeConnector().fetch("a", limit=5)
        assert len(http["calls"]) == 2

    async def test_exhausted_quota_is_remembered_so_no_doomed_requests_are_sent(self, http, yt_key):
        http["handler"] = lambda request: httpx.Response(403, json=YT_QUOTA)
        with pytest.raises(httpx.HTTPStatusError):
            await YouTubeConnector().fetch("topic one", limit=5)
        sent = len(http["calls"])
        with pytest.raises(ConnectorUnavailable) as exc:
            await YouTubeConnector().fetch("topic two", limit=5)
        assert len(http["calls"]) == sent
        assert "quota" in str(exc.value).lower()

    async def test_old_results_are_served_when_quota_is_out(self, http, yt_key):
        http["handler"] = lambda request: httpx.Response(200, json=YT_RESPONSE)
        await YouTubeConnector().fetch("Smart Assistants", limit=5)
        # expire the cached copy, then run out of quota on a different topic
        for k, (_, items) in list(youtube_connector._cache.items()):
            youtube_connector._cache[k] = (0.0, items)
        http["handler"] = lambda request: httpx.Response(403, json=YT_QUOTA)
        with pytest.raises(httpx.HTTPStatusError):
            await YouTubeConnector().fetch("other topic", limit=5)
        stale = await YouTubeConnector().fetch("Smart Assistants", limit=5)
        assert stale and stale[0].external_id == "vid1"

    async def test_a_plain_403_does_not_mark_quota_as_exhausted(self, http, yt_key):
        http["handler"] = lambda request: httpx.Response(403, json={"error": {"errors": [{"reason": "keyInvalid"}]}})
        with pytest.raises(httpx.HTTPStatusError):
            await YouTubeConnector().fetch("a", limit=5)
        with pytest.raises(httpx.HTTPStatusError):   # tries again: not treated as a quota outage
            await YouTubeConnector().fetch("b", limit=5)


# --- Lemmy --------------------------------------------------------------------

LEMMY_POST = {"id": 7, "name": "Smart assistants discussion", "body": "", "ap_id": "https://lemmy.test/post/7", "published": "2026-10-01T10:00:00Z"}


class TestLemmy:
    async def test_lemmy_0_19_shape(self, http):
        http["handler"] = lambda request: httpx.Response(200, json={"posts": [{"post": LEMMY_POST, "creator": {"name": "dan"}, "counts": {"score": 9}}]})
        items = await LemmyConnector().fetch("smart assistants", limit=5)
        assert items[0].source_url == "https://lemmy.test/post/7" and items[0].engagement_count == 9

    async def test_lemmy_1_0_combined_results_shape(self, http):
        http["handler"] = lambda request: httpx.Response(200, json={"results": [
            {"type_": "post", "post": {**LEMMY_POST, "score": 5}, "creator": {"name": "dan"}},
            {"type_": "community", "community": {"name": "x"}},
        ]})
        items = await LemmyConnector().fetch("smart assistants", limit=5)
        assert len(items) == 1 and items[0].engagement_count == 5

    async def test_an_unknown_response_shape_is_an_error_not_a_silent_empty(self, http):
        http["handler"] = lambda request: httpx.Response(200, json={"something_new": []})
        with pytest.raises(ValueError) as exc:
            await LemmyConnector().fetch("x", limit=5)
        assert "something_new" in str(exc.value)

    async def test_a_genuinely_empty_search_is_still_just_empty(self, http):
        http["handler"] = lambda request: httpx.Response(200, json={"posts": []})
        assert await LemmyConnector().fetch("x", limit=5) == []


# --- _safe_fetch records health and never leaks -------------------------------

class FakeConnector:
    def __init__(self, *, enabled=True, items=None, exc=None, delay=0.0):
        self._enabled, self.items, self.exc, self.delay = enabled, items or [], exc, delay

    async def enabled(self):
        return self._enabled

    async def fetch(self, topic, *, limit, language=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.items


@pytest.fixture
def aggregation():
    from app.api.routes_topics import aggregation as agg

    return agg


class TestSafeFetchHealth:
    async def test_success_and_empty_are_recorded(self, aggregation):
        await aggregation._safe_fetch(FakeConnector(items=[raw_item()]), "t", 5, fallback_source="A")
        await aggregation._safe_fetch(FakeConnector(items=[]), "t", 5, fallback_source="B")
        snap = connector_health.snapshot()
        assert snap["A"]["last_status"] == "ok" and snap["A"]["last_item_count"] == 1
        assert snap["B"]["last_status"] == "empty" and snap["B"]["consecutive_failures"] == 0

    async def test_failure_is_recorded_classified_and_counted(self, aggregation):
        exc = status_error(403, body={"error": {"errors": [{"reason": "quotaExceeded"}]}})
        for _ in range(3):
            assert await aggregation._safe_fetch(FakeConnector(exc=exc), "t", 5, fallback_source="YouTube") == []
        e = connector_health.snapshot()["YouTube"]
        assert e["last_status"] == "error" and e["consecutive_failures"] == 3
        assert e["last_error"]["kind"] == QUOTA_EXCEEDED

    async def test_the_logs_and_health_never_contain_the_credential(self, aggregation, caplog):
        caplog.set_level(logging.WARNING)
        await aggregation._safe_fetch(FakeConnector(exc=status_error(403)), "t", 5, fallback_source="YouTube")
        assert SECRET not in caplog.text
        assert SECRET not in json.dumps(connector_health.snapshot())
        assert "YouTube fetch failed" in caplog.text

    async def test_success_after_failure_resets_the_failure_streak(self, aggregation):
        await aggregation._safe_fetch(FakeConnector(exc=status_error(500)), "t", 5, fallback_source="S")
        await aggregation._safe_fetch(FakeConnector(items=[raw_item()]), "t", 5, fallback_source="S")
        e = connector_health.snapshot()["S"]
        assert e["consecutive_failures"] == 0 and e["last_success_at"] and e["total_failures"] == 1

    async def test_timeouts_are_recorded_as_timeouts(self, aggregation, monkeypatch):
        monkeypatch.setattr(settings, "connector_timeout_seconds", 0.05)
        await aggregation._safe_fetch(FakeConnector(delay=1.0), "t", 5, fallback_source="Slow")
        assert connector_health.snapshot()["Slow"]["last_status"] == "timeout"

    async def test_disabled_connectors_record_nothing(self, aggregation):
        await aggregation._safe_fetch(FakeConnector(enabled=False), "t", 5, fallback_source="Off")
        assert "Off" not in connector_health.snapshot()

    def test_bluesky_is_back_in_the_plan_and_inert_without_credentials(self, aggregation):
        assert "Bluesky" in [name for _, name in aggregation.fetch_plan]


# --- endpoints ----------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "cron_heartbeat_secret", "s3cret")
    with TestClient(app) as c:
        yield c


AUTH = {"X-Cron-Secret": "s3cret"}


class TestStatusEndpoint:
    def test_status_explains_why_each_source_is_off(self, client, monkeypatch):
        monkeypatch.setattr(settings, "reddit_client_id", None)
        monkeypatch.setattr(settings, "bluesky_handle", None)
        body = client.get("/api/sources/status").json()
        assert body["connectors"]["Reddit"]["enabled"] is False
        assert "REDDIT_CLIENT_ID" in body["connectors"]["Reddit"]["disabled_reason"]
        assert "BLUESKY_HANDLE" in body["connectors"]["Bluesky"]["disabled_reason"]
        assert body["connectors"]["Mastodon"]["enabled"] is True

    def test_in_mock_mode_the_reason_says_so(self, client, monkeypatch):
        monkeypatch.setattr(settings, "enable_mock_data", True)
        body = client.get("/api/sources/status").json()
        assert "ENABLE_MOCK_DATA" in body["connectors"]["Mastodon"]["disabled_reason"]


class TestHealthAndDiagnosticsAuth:
    @pytest.mark.parametrize("path", ["/api/sources/health", "/api/sources/diagnostics"])
    def test_requires_the_secret(self, client, path):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"X-Cron-Secret": "wrong"}).status_code == 401
        assert client.get(path, headers=AUTH).status_code == 200

    @pytest.mark.parametrize("path", ["/api/sources/health", "/api/sources/diagnostics"])
    def test_refuses_when_no_secret_is_configured(self, path, monkeypatch):
        monkeypatch.setattr(settings, "cron_heartbeat_secret", None)
        with TestClient(app) as c:
            assert c.get(path, headers=AUTH).status_code == 503

    def test_health_reports_what_real_traffic_recorded(self, client):
        connector_health.record_failure("Reddit", describe_error(status_error(401)), time.monotonic())
        body = client.get("/api/sources/health", headers=AUTH).json()
        assert body["sources"]["Reddit"]["last_error"]["kind"] == AUTH_REJECTED


class TestDiagnostics:
    @pytest.fixture
    def plan(self, aggregation, monkeypatch):
        plan = [
            (FakeConnector(enabled=False), "Reddit"),
            (FakeConnector(items=[raw_item("Mastodon", "https://m.test/1")]), "Mastodon"),
            (FakeConnector(items=[]), "Lemmy"),
            (FakeConnector(exc=status_error(403, body={"error": {"errors": [{"reason": "quotaExceeded"}]}})), "YouTube"),
            (FakeConnector(delay=2.0), "Slow"),
        ]
        monkeypatch.setattr(aggregation, "fetch_plan", plan)
        monkeypatch.setattr(settings, "connector_timeout_seconds", 0.2)
        return plan

    def test_reports_every_state_with_reasons_and_hints(self, client, plan):
        body = client.get("/api/sources/diagnostics", headers=AUTH).json()
        by = {r["source"]: r for r in body["results"]}

        assert by["Reddit"]["status"] == "disabled" and by["Reddit"]["reason"]
        assert by["Mastodon"]["status"] == "ok" and by["Mastodon"]["sample"]["url"] == "https://m.test/1"
        assert by["Lemmy"]["status"] == "empty"
        assert by["YouTube"]["status"] == "error" and by["YouTube"]["error"]["kind"] == QUOTA_EXCEEDED
        assert "midnight Pacific" in by["YouTube"]["hint"]
        assert by["Slow"]["status"] == "timeout"
        assert body["summary"] == {"ok": 1, "empty": 1, "disabled": 1, "error": 1, "timeout": 1}

    def test_the_response_never_contains_a_credential(self, client, plan):
        assert SECRET not in client.get("/api/sources/diagnostics", headers=AUTH).text

    def test_only_probes_the_named_sources(self, client, plan):
        body = client.get("/api/sources/diagnostics?only=mastodon,lemmy", headers=AUTH).json()
        assert [r["source"] for r in body["results"]] == ["Mastodon", "Lemmy"]

    def test_an_unknown_source_name_is_a_404_not_an_empty_success(self, client, plan):
        assert client.get("/api/sources/diagnostics?only=Nope", headers=AUTH).status_code == 404

    def test_probes_feed_the_passive_health_record_too(self, client, plan):
        client.get("/api/sources/diagnostics", headers=AUTH)
        assert connector_health.snapshot()["YouTube"]["last_status"] == "error"

    def test_warns_about_a_generic_reddit_user_agent(self, client, plan, monkeypatch):
        monkeypatch.setattr(settings, "reddit_client_id", "cid")
        monkeypatch.setattr(settings, "reddit_client_secret", "sec")
        monkeypatch.setattr(settings, "reddit_user_agent", "KawnPulseEngine/1.0")
        warnings = client.get("/api/sources/diagnostics?only=Mastodon", headers=AUTH).json()["warnings"]
        assert any("REDDIT_USER_AGENT" in w for w in warnings)

        monkeypatch.setattr(settings, "reddit_user_agent", "web:kawn-pulse-engine:1.0 (by /u/kawnbot)")
        warnings = client.get("/api/sources/diagnostics?only=Mastodon", headers=AUTH).json()["warnings"]
        assert not any("REDDIT_USER_AGENT" in w for w in warnings)
