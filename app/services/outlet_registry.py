"""Which curated news outlets exist, where their feeds are, and who may see them.

One entry here defines a regional outlet completely: its feed URL (fetched by
NewsRssConnector), the publisher domains used to recognise its articles when
they arrive through another source (GDELT), and the countries allowed to see
it. Adding a region is adding entries to REGIONAL_OUTLET_FEEDS - nothing else.

Visibility is keyed by the outlet display name every card carries
(PulseCard.source, e.g. "BBC", "Le Monde") and is enforced when cards leave
the engine, because cards are stored once per topic and shared by every
viewer: filtering only at fetch time would let one country's articles leak to
another through the stored copy.

The global outlets keep their feed URLs in settings.major_outlet_rss_feeds
(which can be overridden per environment); they are listed here only for
visibility and for recognising their article URLs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.config import settings

GLOBAL = "global"
REGIONAL = "regional"


@dataclass(frozen=True)
class OutletScope:
    scope: str
    countries: frozenset[str] = field(default_factory=frozenset)

    def visible_to(self, country: str | None) -> bool:
        if self.scope == GLOBAL:
            return True
        return bool(country) and country.upper() in self.countries


@dataclass(frozen=True)
class OutletFeed:
    name: str                      # display name, <= 40 chars (SourceItem.source column)
    feed_url: str
    domains: tuple[str, ...]       # publisher hosts, for recognising article URLs
    countries: frozenset[str]      # who may see it (ISO 3166-1 alpha-2)


# --- regions -----------------------------------------------------------------
# French domestic press: France and its immediate neighbours that read it.
FRANCE_AND_NEIGHBOURS = frozenset({"FR", "BE", "CH", "LU", "MC"})
# International francophone broadcasters: the wider francophone world.
FRANCOPHONE = frozenset({"FR", "BE", "CH", "MC", "LU", "CA", "SN", "CI", "MA", "DZ", "TN", "LB"})

# The feed URLs below are the outlets' published RSS addresses but have not
# been fetched from this engine's host: check GET /api/sources/health after
# deploying - each outlet is reported by name, with the HTTP error if its URL
# has moved.
REGIONAL_OUTLET_FEEDS: tuple[OutletFeed, ...] = (
    OutletFeed("France24", "https://www.france24.com/fr/rss", ("france24.com",), FRANCOPHONE),
    OutletFeed("RFI", "https://www.rfi.fr/fr/rss", ("rfi.fr",), FRANCOPHONE),
    OutletFeed("Le Monde", "https://www.lemonde.fr/rss/une.xml", ("lemonde.fr",), FRANCE_AND_NEIGHBOURS),
    OutletFeed("franceinfo", "https://www.francetvinfo.fr/titres.rss", ("francetvinfo.fr", "franceinfo.fr"), FRANCE_AND_NEIGHBOURS),
    OutletFeed("Le Figaro", "https://www.lefigaro.fr/rss/figaro_actualites.xml", ("lefigaro.fr",), FRANCE_AND_NEIGHBOURS),
    OutletFeed("20 Minutes", "https://www.20minutes.fr/feeds/rss-une.xml", ("20minutes.fr",), FRANCE_AND_NEIGHBOURS),
)

# Global outlets: visible everywhere. Domains are for recognising their
# *article* URLs (a feed host such as feeds.bbci.co.uk differs from article
# hosts such as www.bbc.com).
_GLOBAL_OUTLET_DOMAINS: dict[str, tuple[str, ...]] = {
    "BBC": ("bbc.com", "bbc.co.uk"),
    "CNN": ("cnn.com",),
    "NYT": ("nytimes.com",),
    "Al Jazeera": ("aljazeera.com",),
    "Al Arabiya": ("alarabiya.net",),
    "Euronews": ("euronews.com",),
    "Reuters": ("reuters.com",),
}

OUTLET_SCOPES: dict[str, OutletScope] = {
    **{name: OutletScope(GLOBAL) for name in _GLOBAL_OUTLET_DOMAINS},
    **{f.name: OutletScope(REGIONAL, f.countries) for f in REGIONAL_OUTLET_FEEDS},
}


def regional_outlet_feeds() -> list[OutletFeed]:
    """The regional feeds to fetch (empty when REGIONAL_OUTLETS_ENABLED=false)."""
    return list(REGIONAL_OUTLET_FEEDS) if settings.regional_outlets_enabled else []


def news_outlet_names() -> set[str]:
    """Lower-cased display names that belong to the shared "News" family for
    per-source capping and round-robin (see pulse_card_service)."""
    return {"news"} | {n.lower() for n in OUTLET_SCOPES}


def outlet_name_for_feed_url(feed_url: str) -> str | None:
    for f in REGIONAL_OUTLET_FEEDS:
        if feed_url == f.feed_url:
            return f.name
    return None


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def outlet_for_article_url(url: str) -> str | None:
    """Recognise a known outlet from an article URL, so an article that
    arrives through GDELT is labelled (and therefore gated and capped) exactly
    like the same outlet's RSS items. Matches whole host suffixes, so
    "notlemonde.fr" is not "Le Monde"."""
    host = _host(url)
    if not host:
        return None
    candidates = [(f.name, f.domains) for f in REGIONAL_OUTLET_FEEDS] + list(_GLOBAL_OUTLET_DOMAINS.items())
    for name, domains in candidates:
        for d in domains:
            if host == d or host.endswith("." + d):
                return name
    return None


def is_outlet_visible(source_name: str, country: str | None) -> bool:
    """True for anything NOT in the registry (Reddit, Hacker News, the
    generic "News" source, ...) - this only gates the curated outlet layer,
    not the whole aggregator."""
    scope = OUTLET_SCOPES.get(source_name)
    if scope is None:
        return True
    return scope.visible_to(country)


def filter_visible_cards(cards, country: str | None):
    """Applied at every point cards leave the engine as a browsable list.
    Persisted DB cards are shared across all future requesters of a topic
    regardless of who originally triggered the fetch, so filtering has to
    happen here (serve time), not just at fetch time."""
    return [c for c in cards if is_outlet_visible(c.source, country)]
