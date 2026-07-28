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
    connector_timeout_seconds: float = 3.0
    # Overall budget for a live/search-triggered refresh. Once this elapses,
    # aggregation returns whatever connectors have finished instead of waiting
    # for stragglers. With every connector now firing in a single wave
    # (no more queueing through a concurrency-3 semaphore), this no longer
    # needs slack for multiple waves — set to roughly double
    # connector_timeout_seconds as a backstop for event-loop scheduling
    # delays, not because any connector should legitimately take this long.
    search_fetch_budget_seconds: float = 6.0

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

    news_rss_feeds: str = "https://news.google.com/rss/search?q={query}"

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

    def reddit_configured(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret and self.reddit_user_agent)

    def youtube_configured(self) -> bool:
        return bool(self.youtube_api_key)

    def producthunt_configured(self) -> bool:
        return bool(self.producthunt_access_token)


settings = Settings()

