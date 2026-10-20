"""Orders the explore feed so no single source dominates it.

The feed used to be a plain random.shuffle, which let whichever connector had
the most cards (usually News, or Reddit) fill most of the screen. mix_feed
interleaves instead: cards are grouped by connector family (all news outlets
count as one "News" source, same grouping the card builder uses for its
per-source caps), each group is shuffled, and the groups are dealt out
round-robin, one card per source per round, with the order of sources
reshuffled every round.

Guarantees: nothing is dropped, nothing is duplicated, the input list is not
modified, and (whenever more than one source has cards) the same source never
appears twice in a row until the other sources have run out.
"""
from __future__ import annotations

import random
from typing import Sequence, TypeVar

from app.services.pulse_card_service import connector_family_for_source

T = TypeVar("T")


def _family(card) -> str:
    return connector_family_for_source(getattr(card, "source", None) or "")


def mix_feed(cards: Sequence[T], *, rng: random.Random | None = None) -> list[T]:
    rng = rng or random.Random()
    items = list(cards)
    if len(items) < 2:
        return items

    buckets: dict[str, list[T]] = {}
    for card in items:
        buckets.setdefault(_family(card), []).append(card)
    for bucket in buckets.values():
        rng.shuffle(bucket)

    out: list[T] = []
    last_family: str | None = None
    while any(buckets.values()):
        order = [f for f, b in buckets.items() if b]
        rng.shuffle(order)
        # Don't let a round start with the family that ended the previous
        # one, unless it is the only family left.
        if len(order) > 1 and order[0] == last_family:
            order.append(order.pop(0))
        for fam in order:
            out.append(buckets[fam].pop())
            last_family = fam
    return out
