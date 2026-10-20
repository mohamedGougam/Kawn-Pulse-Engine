"""Which feed tab a card belongs to: All / Near you / Newsletters / Social media.

Everything here is computed at SERVE time from the card's source name and the
viewer's country, never stored. Cards are persisted once per topic and shared by
every viewer (see outlet_registry), so "near you" - which depends on who is
looking - must not be baked into the stored copy.

Tabs are filters, not buckets: a Le Monde card is both "Near you" (for a viewer
in France) and "Newsletters".
"""
from __future__ import annotations

from app.services.outlet_registry import OUTLET_SCOPES, REGIONAL

CATEGORY_NEWSLETTER = "newsletter"
CATEGORY_SOCIAL = "social"
CATEGORY_OTHER = "other"

# Query-string values accepted by the feed endpoints.
TAB_ALL = "all"
TAB_NEAR_YOU = "near_you"
TAB_NEWSLETTERS = "newsletters"
TAB_SOCIAL = "social_media"
VALID_TABS = {TAB_ALL, TAB_NEAR_YOU, TAB_NEWSLETTERS, TAB_SOCIAL}

# Community / social platforms.
_SOCIAL_SOURCES = {
    "reddit", "bluesky", "mastodon", "lemmy", "youtube", "peertube",
    "hackernews", "lobsters", "discourse", "producthunt",
}
# Editorial publications: every curated outlet (BBC, Le Monde, ...), the generic
# "News" source (RSS / GDELT), and blogging platforms.
_NEWSLETTER_SOURCES = {"news", "devto", "hashnode"} | {n.lower() for n in OUTLET_SCOPES}


def category_for_source(source: str | None) -> str:
    s = (source or "").strip().lower()
    if s in _SOCIAL_SOURCES:
        return CATEGORY_SOCIAL
    if s in _NEWSLETTER_SOURCES:
        return CATEGORY_NEWSLETTER
    return CATEGORY_OTHER  # Wikipedia, mock data, unknown: only shows under "All"


def is_near_you(source: str | None, country: str | None) -> bool:
    """A regional outlet that is meant for this viewer's country."""
    scope = OUTLET_SCOPES.get(source or "")
    return bool(scope and scope.scope == REGIONAL and scope.visible_to(country))


def normalize_tab(tab: str | None) -> str:
    t = (tab or TAB_ALL).strip().lower()
    return t if t in VALID_TABS else TAB_ALL


def annotate_and_filter(cards, country: str | None, tab: str | None = None):
    """Set `.category` and `.near_you` on each card, then keep only those in `tab`.

    Works on the PulseCard pydantic schema objects the routes build. Always
    annotates (so the app can show/hide chips); filters only for a non-"all" tab.
    """
    tab = normalize_tab(tab)
    out = []
    for c in cards:
        c.category = category_for_source(c.source)
        c.near_you = is_near_you(c.source, country)
        if tab == TAB_ALL:
            keep = True
        elif tab == TAB_NEAR_YOU:
            keep = c.near_you
        elif tab == TAB_NEWSLETTERS:
            keep = c.category == CATEGORY_NEWSLETTER
        else:
            keep = c.category == CATEGORY_SOCIAL
        if keep:
            out.append(c)
    return out
