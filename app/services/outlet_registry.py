"""Which curated news outlets are shown to which viewers.

Deliberately separate from the URL-fetching config in app/config.py: this
only decides visibility, keyed by the outlet display name every card
already carries (PulseCard.source, e.g. "BBC", "France24"). It does not
touch how or whether an outlet's feed is fetched.

Every one of today's fixed outlets is listed as "global" here, which
reproduces exactly the current behaviour (shown to everyone) — adding this
registry changes nothing until an outlet is explicitly marked "regional".
"""
from __future__ import annotations

from dataclasses import dataclass, field

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


OUTLET_SCOPES: dict[str, OutletScope] = {
    "BBC": OutletScope(GLOBAL),
    "CNN": OutletScope(GLOBAL),
    "NYT": OutletScope(GLOBAL),
    "Al Jazeera": OutletScope(GLOBAL),
    "Al Arabiya": OutletScope(GLOBAL),
    "Euronews": OutletScope(GLOBAL),
    "Reuters": OutletScope(GLOBAL),
    # France24: shown only in France and other primarily French-speaking
    # markets it actually broadcasts/targets. Extend this set as needed —
    # it's the only thing that needs to change to widen/narrow reach.
    "France24": OutletScope(
        REGIONAL,
        frozenset({"FR", "BE", "CH", "MC", "LU", "CA", "SN", "CI", "MA", "DZ", "TN", "LB"}),
    ),
}


def is_outlet_visible(source_name: str, country: str | None) -> bool:
    """True for anything NOT in the registry (Reddit, Hacker News, the
    generic Google-News-templated "News" source, ...) — this only gates the
    curated fixed-outlet layer, not the whole aggregator."""
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
