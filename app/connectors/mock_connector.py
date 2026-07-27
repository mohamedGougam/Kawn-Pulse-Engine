from __future__ import annotations

import random
from datetime import datetime, timedelta

from app.connectors.base import NormalizedRawItem


class MockConnector:
    name = "Mock"

    async def enabled(self) -> bool:
        return True

    async def fetch(self, topic: str, *, limit: int, language: str | None = None) -> list[NormalizedRawItem]:
        seed = abs(hash(topic)) % (2**32)
        rnd = random.Random(seed)

        now = datetime.utcnow()
        templates = _mock_templates(topic)
        items: list[NormalizedRawItem] = []

        for i in range(limit):
            t = rnd.choice(templates)
            source = t["source"]
            source_url = t["source_url_template"].format(topic=_slug(topic), i=i, seed=seed)
            published_at = now - timedelta(hours=rnd.randint(1, 96))
            engagement = max(0, int(rnd.gauss(120, 80)))

            items.append(
                NormalizedRawItem(
                    source=source,
                    source_url=source_url,
                    topic=topic,
                    author=rnd.choice(t["authors"]),
                    title=rnd.choice(t["titles"]),
                    text=rnd.choice(t["texts"]),
                    published_at=published_at,
                    engagement_count=engagement,
                    language="en",
                    external_id=f"{source.lower()}_{seed}_{i}",
                )
            )

        return items


def _slug(topic: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in topic).strip("-")


def _mock_templates(topic: str) -> list[dict]:
    # Keep these realistic and UI-friendly for quote cards.
    common = [
        "I didn’t expect it to hit that hard.",
        "The takes are wild, but some points are actually fair.",
        "This is going to be a long conversation online.",
        "It’s not perfect, but it’s definitely memorable.",
        "The highlight for me was the pacing and payoff.",
        "People are missing the context here.",
        "That one moment is going to be quoted everywhere.",
    ]

    topic_specific: dict[str, list[str]] = {
        "creed 3": [
            "The final fight scene gave me chills.",
            "Jonathan Majors was intense—every scene felt like pressure.",
            "The soundtrack + crowd energy made it feel like an event.",
            "It’s a sports movie, but it’s really about friendship and pride.",
        ],
        "real madrid": [
            "The midfield control is unreal this season.",
            "They look calm even when they’re behind—classic Madrid.",
            "The young players are stepping up at the perfect time.",
            "The finish was clinical; that’s the difference at the top.",
        ],
        "ai tools": [
            "This tool saves me hours, but the output still needs a human pass.",
            "The UI is great, but pricing is starting to creep up.",
            "It’s the workflow integration that makes it sticky.",
            "Hallucinations are down, but you still have to verify facts.",
        ],
        "algeria football": [
            "The team’s intensity is back—pressing looks coordinated.",
            "Set pieces are improving; you can see the training work.",
            "The crowd atmosphere is a massive advantage.",
            "They need more consistency in the final third.",
        ],
        "world cup 2026": [
            "The expanded format changes everything—more upsets incoming.",
            "Travel and scheduling will be a bigger story than people think.",
            "Some teams will benefit from deeper squads and rotation.",
            "The host cities are going to create very different atmospheres.",
        ],
    }

    ts = topic_specific.get(topic.lower(), [])
    pool = ts + common

    return [
        {
            "source": "Reddit",
            "source_url_template": "https://reddit.com/r/all/search?q={topic}&i={i}&seed={seed}",
            "authors": ["throwawayfan", "matchday_mind", "cinephile99", "toolmaker", "north_africa_sports"],
            "titles": [
                f"Hot takes on {topic}",
                f"Why everyone is talking about {topic}",
                f"One thing people miss about {topic}",
                f"Best reactions to {topic} so far",
            ],
            "texts": pool,
        },
        {
            "source": "YouTube",
            "source_url_template": "https://youtube.com/watch?v=mock_{seed}_{i}",
            "authors": ["PulseClips", "DailyRecap", "SportsTalk", "NewsNow", "ExplainerHub"],
            "titles": [
                f"{topic} explained in 5 minutes",
                f"The moment that changed {topic}",
                f"Top reactions: {topic}",
                f"Breaking down {topic}",
            ],
            "texts": pool,
        },
        {
            "source": "News",
            "source_url_template": "https://example.com/news/{topic}/{seed}/{i}",
            "authors": ["Reuters-style desk", "Local reporter", "Analyst column", "Editorial team"],
            "titles": [
                f"{topic}: what happened and why it matters",
                f"Public reaction grows around {topic}",
                f"Key context behind {topic}",
            ],
            "texts": pool,
        },
        {
            "source": "Bluesky",
            "source_url_template": "https://bsky.app/profile/mock.bsky.social/post/{seed}{i}",
            "authors": ["skywatcher.bsky.social", "curiousmind.bsky.social", "dailytake.bsky.social"],
            "titles": [f"Thread on {topic}"],
            "texts": pool,
        },
        {
            "source": "HackerNews",
            "source_url_template": "https://news.ycombinator.com/item?id=mock{seed}{i}",
            "authors": ["hn_lurker", "sre_person", "compiler_nerd", "startup_founder"],
            "titles": [
                f"Show HN: thoughts on {topic}",
                f"Ask HN: what's the deal with {topic}?",
                f"{topic} discussion",
            ],
            "texts": pool,
        },
        {
            "source": "Lemmy",
            "source_url_template": "https://lemmy.world/post/mock{seed}{i}",
            "authors": ["fedi_poster", "instance_regular", "commie_cat"],
            "titles": [f"{topic}", f"Anyone else following {topic}?"],
            "texts": pool,
        },
        {
            "source": "Mastodon",
            "source_url_template": "https://mastodon.social/@mockuser/{seed}{i}",
            "authors": ["fedi.mockuser@mastodon.social", "opinionhaver@mastodon.social"],
            "titles": [f"Toot about {topic}"],
            "texts": pool,
        },
        {
            "source": "DevTo",
            "source_url_template": "https://dev.to/mock/{topic}-{seed}{i}",
            "authors": ["devto_writer", "code_with_me", "fullstack_fan"],
            "titles": [
                f"What I learned building with {topic}",
                f"{topic}: a practical guide",
                f"Why developers are talking about {topic}",
            ],
            "texts": pool,
        },
        {
            "source": "Hashnode",
            "source_url_template": "https://hashnode.com/post/{topic}-{seed}{i}",
            "authors": ["hashnode_blogger", "techwriter_dz", "buildinpublic"],
            "titles": [
                f"Deep dive: {topic}",
                f"{topic} explained for developers",
            ],
            "texts": pool,
        },
        {
            "source": "Lobsters",
            "source_url_template": "https://lobste.rs/s/mock{seed}/{topic}_{i}",
            "authors": ["greybeard", "ml_curious", "sysadmin_life"],
            "titles": [f"{topic}", f"On {topic}"],
            "texts": pool,
        },
        {
            "source": "PeerTube",
            "source_url_template": "https://framatube.org/videos/watch/mock-{seed}-{i}",
            "authors": ["indie_channel", "open_video_maker", "commons_creator"],
            "titles": [
                f"{topic} - full explainer",
                f"Everything about {topic} in one video",
            ],
            "texts": pool,
        },
        {
            "source": "ProductHunt",
            "source_url_template": "https://www.producthunt.com/posts/mock-{topic}-{seed}{i}",
            "authors": ["maker_hub", "indie_maker"],
            "titles": [
                f"{topic}: a new take",
                f"Launching something for {topic}",
            ],
            "texts": pool,
        },
        {
            "source": "Wikipedia",
            "source_url_template": "https://en.wikipedia.org/wiki/Talk:{topic}",
            "authors": ["wiki_editor", "talk_page_regular"],
            "titles": [f"Talk: {topic}", f"Editing notes on {topic}"],
            "texts": pool,
        },
        {
            "source": "Discourse",
            "source_url_template": "https://meta.discourse.org/t/{topic}/{seed}{i}",
            "authors": ["forum_regular", "community_mod", "longtime_member"],
            "titles": [f"Discussion: {topic}", f"{topic} - thread"],
            "texts": pool,
        },
    ]

