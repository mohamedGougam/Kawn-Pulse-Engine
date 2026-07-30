from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    log_level: str = "INFO"

    # Storage (relational — topics, cards, summaries)
    # Local dev: sqlite:///./kawn_pulse.db
    # Prod (Render free tier has no persistent disk): a Postgres URL, e.g. Neon.
    #   postgresql+asyncpg://user:pass@host/dbname
    database_url: str = "sqlite:///./kawn_pulse.db"

    # Object storage (heavy-fetch JSON cache) — Cloudflare R2, S3-compatible.
    # Free tier: 10GB storage, 1M writes/mo, 10M reads/mo, no egress fees.
    r2_account_id: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    r2_bucket_name: str = "kawn-pulse-cache"
    # Key layout: sources/<source_name>/<topic_slug>.json
    r2_key_prefix: str = "sources"

    def r2_configured(self) -> bool:
        return bool(self.r2_account_id and self.r2_access_key_id and self.r2_secret_access_key)

    def r2_endpoint_url(self) -> str | None:
        if not self.r2_account_id:
            return None
        return f"https://{self.r2_account_id}.r2.cloudflarestorage.com"

    # Fetch timeout budget
    # Per-connector hard timeout — a single slow source can no longer stall
    # the whole refresh past this, no matter what its own http client timeout is.
    # Shortened from 5.0: the fast-fetch wave no longer queues connectors
    # behind a concurrency cap (all of fetch_plan fires at once now), so a
    # tighter per-connector timeout is what actually bounds total search
    # latency — and it means a stuck source gets replaced by its cache
    # fallback sooner instead of eating the whole budget.
    # Raised from 3.0: in production (extra network hops, cold TLS handshake
    # per connector since each fetch() opens its own httpx.AsyncClient, no
    # keep-alive reuse across calls) most real connectors — especially News,
    # which has to wait on all 8 of its RSS feeds inside this same budget —
    # routinely ran past 3s and got cancelled outright by asyncio.wait_for,
    # leaving only the single lightweight Lemmy endpoint reliably finishing
    # in time. Can still be overridden via CONNECTOR_TIMEOUT_SECONDS.
    connector_timeout_seconds: float = 8.0
    # Overall budget for a live/search-triggered refresh. Once this elapses,
    # aggregation returns whatever connectors have finished instead of waiting
    # for stragglers. Kept at roughly 1.25x connector_timeout_seconds as a
    # backstop for event-loop scheduling delays, not because any connector
    # should legitimately take this long. Can be overridden via
    # SEARCH_FETCH_BUDGET_SECONDS.
    search_fetch_budget_seconds: float = 10.0

    # Per-source read timeout against the R2 heavy-fetch cache. These reads
    # run in parallel with the live fetch wave (not after it), so this only
    # needs to be generous enough that a slow R2 read doesn't itself become
    # the bottleneck — it does not add to the live-fetch budget above.
    # Shortened from 4.0 so a slow cache read doesn't sit around after the
    # (now-faster) live fetch has already come back — a fast miss here just
    # means that source shows up in missing_sources instead of cached_sources.
    heavy_cache_read_timeout_seconds: float = 2.0

    # Shared secret for the external cron heartbeat (cron-job.org etc.) that
    # wakes the free-tier dyno and drives scheduled refreshes, since an
    # in-process APScheduler job cannot fire while the instance is asleep.
    cron_heartbeat_secret: str | None = None

    # Source configuration
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str = "KawnPulseEngine/1.0"

    youtube_api_key: str | None = None

    # Per-connector kill switch for News, read by NewsRssConnector.enabled().
    # This field was previously missing entirely -- settings.news_enabled
    # raised AttributeError on every call, which got silently swallowed as
    # "News returned nothing this round" instead of actually disabling it,
    # and meant a disabled News kept getting backfilled from whatever was
    # already sitting in the R2 heavy-fetch cache from before. Set
    # NEWS_ENABLED=false to genuinely turn it off.
    news_enabled: bool = True

    news_rss_feeds: str = "https://news.google.com/rss/search?q={query}"

    # Fixed-URL outlet feeds (CNN/BBC/NYT/Al Jazeera/Al Arabiya/Euronews) —
    # these publish section feeds, not per-query search feeds, so unlike
    # news_rss_feeds above there's no {query} to substitute. NewsRssConnector
    # pulls all of them on every call and filters entries by
    # topic_matches() instead. Each item's actual outlet is resolved from
    # its feed URL (see _OUTLET_NAME_BY_URL_FRAGMENT in news_connector.py)
    # so cards show "BBC" / "CNN" / "Al Arabiya" etc. instead of the
    # generic connector name "News".
    # Reuters discontinued its public RSS in 2020 and still has none as of
    # 2026 — routed through Reuters-scoped Google News search instead of a
    # dead feed URL.
    major_outlet_rss_feeds: str = (
        "https://feeds.bbci.co.uk/news/world/rss.xml,"
        "http://rss.cnn.com/rss/edition_world.rss,"
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml,"
        "https://www.aljazeera.com/xml/rss/all.xml,"
        "https://english.alarabiya.net/feed/flipboard/en.xml,"
        "https://feeds.feedburner.com/euronews/en/home/"
    )
    reuters_rss_workaround_template: str = "https://news.google.com/rss/search?q={query}+site:reuters.com"

    # --- Bluesky firehose (Jetstream) ---
    # Off by default: a firehose consumer is a long-lived connection, which
    # only makes sense on an always-on process. On a free-tier web dyno
    # that spins down after ~15 min idle, this dies with the dyno just like
    # the in-process scheduler does — see cron-tick's docstring for the
    # same issue. Turn on once this runs somewhere always-on (a paid
    # instance, or a separate worker process).
    bluesky_firehose_enabled: bool = False
    bluesky_jetstream_url: str = (
        "wss://jetstream2.us-east.bsky.network/subscribe?wantedCollections=app.bsky.feed.post"
    )
    # How many recent matched posts to keep per topic in memory. This is
    # the *in-process* buffer only — see streaming/README notes: matches
    # are also periodically flushed to the same R2 heavy-fetch cache
    # everything else uses, so a restart doesn't lose everything, only
    # whatever hadn't been flushed yet.
    firehose_buffer_size_per_topic: int = 60
    # How often matched-but-unflushed topics get written to R2. Kept well
    # above a few seconds on purpose — R2's free tier caps at 1M writes/mo,
    # and a global firehose can accumulate matches across many topics
    # quickly; batching per topic keeps write volume proportional to
    # distinct active topics, not to raw event volume.
    firehose_flush_interval_seconds: float = 45.0

    # --- Reddit stream (submissions + comments via asyncpraw) ---
    # Same always-on caveat as Bluesky above.
    reddit_stream_enabled: bool = False
    # Comma-separated subreddits to stream, e.g. "all" for r/all (broadest
    # coverage, highest volume) or a curated list like
    # "technology,worldnews,science" (lower volume, more relevant to
    # Discover_subjects' actual topics, cheaper on a constrained instance).
    reddit_stream_subreddits: str = "all"

    producthunt_access_token: str | None = None
    discourse_instance_url: str = "https://meta.discourse.org"
    peertube_instance_url: str = "https://framatube.org"
    lemmy_instance_url: str = "https://lemmy.world"
    mastodon_instance_url: str = "https://mastodon.social"

    enable_mock_data: bool = True

    # Pipeline sizing
    max_source_items_per_connector: int = 35
    pulse_cards_per_topic: int = 20

    # Background worker (scheduler / cron-tick) tuning. A single topic
    # refresh now fires every connector concurrently (no per-connector cap
    # any more — see AggregationService.refresh_topic), so this semaphore is
    # what keeps a burst of background topic refreshes from piling on top of
    # a live user search's own connector burst at the same time.
    background_worker_concurrency: int = 2
    # How many topics get refreshed per scheduler/cron-tick pass, taken from
    # the top of the priority-scored candidate list (see
    # SchedulerService._priority_score). Deliberately not "all of them" —
    # with real connector + AI latency per topic, an unbounded pass can run
    # long enough to overlap the next tick even with the tick-lock in place.
    background_batch_size: int = 6

    source_freshness_days: int = 31  ## condition for fetching only new info (31days cap) 


    # AI
    ai_sentiment_model: str = "cardiffnlp/twitter-roberta-base-sentiment-latest"
    ai_summary_model: str = "sshleifer/distilbart-cnn-12-6"

   
    # Scheduler
    scheduler_enabled: bool = True
    scheduler_refresh_minutes: int = 30

    Discover_subjects: list[str] = [
        "Smart AI Assistants",
        "Brain-Based Learning",
        "Eco-Friendly Electronics",
        "Flood-Proof Cities",
        "Super-Secure Coding",
        "High-Level Part-Time Jobs",
        "AI Virus Tracking",
        "Real-Life Content Verification",
        "Human Relationship Skills",
        "Space Junk Cleanup",
        "Human-Like Robots (Physical AI)",
        "6G Mobile Networks",
        "Gene-Editing Medicine (CRISPR)",
        "Ocean-Powered Energy",
        "Addiction-Free Painkillers",
    ]

    def rss_feed_templates(self) -> list[str]:
        return [s.strip() for s in self.news_rss_feeds.split(",") if s.strip()]

    def major_outlet_feeds(self) -> list[str]:
        return [s.strip() for s in self.major_outlet_rss_feeds.split(",") if s.strip()]

    def reddit_stream_subreddit_list(self) -> list[str]:
        return [s.strip() for s in self.reddit_stream_subreddits.split(",") if s.strip()] or ["all"]

    def reddit_configured(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret and self.reddit_user_agent)

    def youtube_configured(self) -> bool:
        return bool(self.youtube_api_key)

    def producthunt_configured(self) -> bool:
        return bool(self.producthunt_access_token)


settings = Settings()

