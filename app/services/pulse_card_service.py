from __future__ import annotations

import random
from dataclasses import dataclass

from app.services.ai_service import AIService
from app.services.normalization_service import NormalizedItem
from app.utils.text_utils import (
    best_sentence,
    clean_text,
    is_low_quality,
    trim_text,
)


@dataclass
class CardDraft:
    source_index: int  # index back into items list, used by aggregation_service
    quote: str
    theme: str | None
    sentiment: str
    display_label: str


_NEWS_OUTLET_NAMES = {"news", "bbc", "cnn", "nyt", "al jazeera", "al arabiya", "euronews", "reuters"}


def connector_family_for_source(source: str) -> str:
    """Collapses a real outlet display name (BBC, CNN, Al Jazeera, ...) back
    to the shared "News" connector identity for grouping/capping purposes
    only (per-source item caps, round-robin card selection). Without this,
    splitting one connector's output across several display names would let
    it claim several caps' worth of items / round-robin slots instead of
    one, crowding out lower-volume connectors like Reddit or YouTube.
    display_label_for_source (below) still uses the real, un-collapsed name
    for what the user actually sees on each card."""
    return "News" if source.lower() in _NEWS_OUTLET_NAMES else source


def display_label_for_source(source: str) -> str:
    src_lower = source.lower()
    if src_lower in _NEWS_OUTLET_NAMES:
        return "News quote" if src_lower == "news" else f"{source} quote"
    elif src_lower == "youtube":
        return "YouTube clip"
    elif src_lower == "reddit":
        return "Reddit reaction"
    elif src_lower == "bluesky":
        return "Bluesky post"
    elif src_lower == "hackernews":
        return "HN discussion"
    else:
        return f"{source} reaction"


class PulseCardService:
    def __init__(self, ai: AIService) -> None:
        self.ai = ai

    def build_cards(self, topic: str, items: list[NormalizedItem], *, max_cards: int) -> list[CardDraft]:
        if not items:
            return []

        # Deterministic per-topic randomness so UI is stable across re-renders.
        rnd = random.Random(abs(hash(topic)) % (2**32))

        # Group items by connector family (real outlet names all collapse
        # back to "News" here — see connector_family_for_source), shuffled
        # within each group.
        by_source: dict[str, list[tuple[int, NormalizedItem]]] = {}
        for idx, it in enumerate(items):
            by_source.setdefault(connector_family_for_source(it.source), []).append((idx, it))
        for src in by_source:
            rnd.shuffle(by_source[src])

        # Round-robin pick across sources until max_cards reached.
        ordered_sources = sorted(by_source.keys(), key=lambda s: -len(by_source[s]))
        picked: list[tuple[int, NormalizedItem]] = []
        cursors = {s: 0 for s in ordered_sources}

        while len(picked) < max_cards * 2:  # over-pick; quality filter may drop some
            progressed = False
            for src in ordered_sources:
                if cursors[src] < len(by_source[src]):
                    picked.append(by_source[src][cursors[src]])
                    cursors[src] += 1
                    progressed = True
                    if len(picked) >= max_cards * 2:
                        break
            if not progressed:
                break

        if not picked:
            return []

        texts_for_ai = [(it.text or it.title or "") for _, it in picked]
        sentiments = self.ai.sentiment.analyze([trim_text(t, max_len=240) for t in texts_for_ai])

        themes = self.ai.extract_themes(topic) or []

        drafts: list[CardDraft] = []
        for i, (orig_idx, it) in enumerate(picked):
            raw = it.text or it.title or ""
            cleaned = clean_text(raw)
            quote = best_sentence(cleaned, max_len=220) if cleaned else ""

            if not quote or is_low_quality(quote, min_len=12, min_words=2):
                title_clean = clean_text(it.title or "")
                if title_clean and not is_low_quality(title_clean, min_len=12, min_words=2):
                    quote = title_clean
                else:
                    continue

            quote = trim_text(quote, max_len=220)

            s = sentiments[i] if i < len(sentiments) else None
            sentiment = (s.label if s else "neutral")

            theme = None
            if themes:
                theme = themes[i % len(themes)]

            display_label = display_label_for_source(it.source)

            drafts.append(
                CardDraft(
                    source_index=orig_idx,
                    quote=quote,
                    theme=theme,
                    sentiment=sentiment,
                    display_label=display_label,
                )
            )

            if len(drafts) >= max_cards:
                break

        return drafts
