from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    log_level: str = "INFO"

    # Storage
    database_url: str = "sqlite:///./kawn_pulse.db"

    # Source configuration
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str = "KawnPulseEngine/1.0"

    youtube_api_key: str | None = None

    news_rss_feeds: str = "https://news.google.com/rss/search?q={query}"

    enable_mock_data: bool = True

    # Pipeline sizing
    max_source_items_per_connector: int = 35
    pulse_cards_per_topic: int = 20

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


settings = Settings()

