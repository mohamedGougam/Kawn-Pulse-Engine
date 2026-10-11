from __future__ import annotations

import asyncio
import re
import urllib.parse

import httpx

from app.config import settings
from app.connectors.base import NormalizedRawItem
from app.utils.date_utils import parse_datetime
from app.utils.text_utils import normalize_ws, significant_tokens, topic_matches

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_NON_TAG_CHARS_RE = re.compile(r"[^a-z0-9]+")
_WORD_RE = re.compile(r"\w+", re.UNICODE)

# Each hashtag is one request to the instance. Bounded so a many-word topic
# can't fan out into a burst against a shared public instance's rate limit.
_MAX_TAG_TIMELINES = 4


def _topic_to_hashtag(topic: str) -> str:
    return _NON_TAG_CHARS_RE.sub("", topic.lower())


def candidate_hashtags(topic: str) -> tuple[str | None, list[str]]:
    """(exact_tag, keyword_tags) for a topic.

    exact_tag is the whole topic as one hashtag ("world cup" -> "worldcup").
    keyword_tags are its individual significant words, longest first.

    The old behaviour only ever tried the exact tag, so a topic like "Smart
    AI Assistants" searched for #smartaiassistants - a hashtag essentially
    nobody uses - and came back empty almost every time. Single-word topics
    still work the same way, since their exact tag is the word itself.
    """
    # significant_tokens() ignores anything under 3 characters, which would
    # throw away "AI" and "6G" - often the single most specific words in a
    # topic and very commonly used hashtags. Keep 2-character terms that are
    # written as acronyms or contain a digit.
    significant = set(significant_tokens(topic))
    words = []
    for raw in _WORD_RE.findall(topic):
        low = raw.lower()
        is_short_acronym = len(low) == 2 and (raw.isupper() or any(c.isdigit() for c in raw))
        if low in significant or is_short_acronym:
            cleaned = _NON_TAG_CHARS_RE.sub("", low)
            if cleaned:
                words.append(cleaned)
    words = list(dict.fromkeys(words))  # dedupe, keep order

    exact = "".join(words) or _topic_to_hashtag(topic) or None
    keyword_tags: list[str] = []
    if len(words) > 1:
        keyword_tags = sorted(words, key=len, reverse=True)

    budget = _MAX_TAG_TIMELINES - (1 if exact else 0)
    return exact, [t for t in keyword_tags if t != exact][:max(0, budget)]


def _keyword_post_is_relevant(topic: str, tag: str, text: str) -> bool:
    others = [t for t in significant_tokens(topic) if _NON_TAG_CHARS_RE.sub("", t.lower()) != tag]
    if not others:
        return True  # single-word topic: the tag is the whole topic
    lowered = text.lower()
    return any(t.lower() in lowered for t in others) and topic_matches(topic, text)


class MastodonConnector:
    """Mastodon's full-text status search (/api/v2/search) needs a logged-in
    user token on almost every instance, but hashtag timelines
    (/api/v1/timelines/tag/:hashtag) are public unless the instance admin has
    turned public previews off (docs.joinmastodon.org: "Requires app token +
    read:statuses if the instance has disabled public preview"). So this
    connector searches by hashtag.

    Posts under the topic's exact hashtag are on-topic by definition. Posts
    found through a single keyword's hashtag (#assistants) are noisy, so
    those are only kept when the post ALSO mentions another word of the
    topic ("smart"). A post under #assistants trivially contains
    "assistants", so without that extra requirement the relevance check
    would never reject anything.
    """

    name = "Mastodon"

    async def enabled(self) -> bool:
        return not settings.enable_mock_data

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        instance = settings.mastodon_instance_url.rstrip("/")
        exact_tag, keyword_tags = candidate_hashtags(topic)
        tags = ([exact_tag] if exact_tag else []) + keyword_tags
        if not tags:
            return []

        async with httpx.AsyncClient(timeout=15.0) as client:
            results = await asyncio.gather(
                *(self._fetch_tag(client, instance, tag, limit) for tag in tags),
                return_exceptions=True,
            )

        failures = [r for r in results if isinstance(r, BaseException)]
        if failures and len(failures) == len(results):
            # Every timeline failed: that's an outage/denial, not "no posts".
            raise failures[0]

        items: list[NormalizedRawItem] = []
        seen: set[str] = set()
        for tag, statuses in zip(tags, results):
            if isinstance(statuses, BaseException):
                continue
            for status in statuses:
                item = self._to_item(status, topic)
                if item is None or item.source_url in seen:
                    continue
                if tag != exact_tag and not _keyword_post_is_relevant(topic, tag, item.text or ""):
                    continue
                seen.add(item.source_url)
                items.append(item)

        return items[:limit]

    @staticmethod
    async def _fetch_tag(client: httpx.AsyncClient, instance: str, tag: str, limit: int) -> list[dict]:
        url = f"{instance}/api/v1/timelines/tag/{urllib.parse.quote(tag)}"
        resp = await client.get(
            url,
            params={"limit": min(limit, 40)},
            headers={"User-Agent": settings.reddit_user_agent},
        )
        resp.raise_for_status()
        statuses = resp.json()
        return statuses if isinstance(statuses, list) else []

    @staticmethod
    def _to_item(s: dict, topic: str) -> NormalizedRawItem | None:
        html_content = s.get("content") or ""
        text = normalize_ws(_HTML_TAG_RE.sub(" ", html_content))
        if not text:
            return None

        source_url = s.get("url") or s.get("uri") or ""
        if not source_url:
            return None

        account = s.get("account") or {}
        status_id = s.get("id")
        engagement = int(s.get("reblogs_count") or 0) + int(s.get("favourites_count") or 0)

        return NormalizedRawItem(
            source="Mastodon",
            source_url=source_url,
            topic=topic,
            author=account.get("acct"),
            title=None,
            text=text,
            published_at=parse_datetime(s.get("created_at")),
            engagement_count=engagement or None,
            language=s.get("language"),
            external_id=str(status_id) if status_id is not None else None,
        )
