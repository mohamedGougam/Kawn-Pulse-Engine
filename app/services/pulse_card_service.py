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
    quote: str
    theme: str | None
    sentiment: str
    display_label: str


class PulseCardService:
    def __init__(self, ai: AIService) -> None:
        self.ai = ai

    def build_cards(self, topic: str, items: list[NormalizedItem], *, max_cards: int) -> list[CardDraft]:
        if not items:
            return []

        # Deterministic shuffle by topic for stable UI during dev.
        rnd = random.Random(abs(hash(topic)) % (2**32))
        pool = list(items)
        rnd.shuffle(pool)

        texts = [it.text or it.title or "" for it in pool]
        sentiments = self.ai.sentiment.analyze([trim_text(t, max_len=240) for t in texts[: max_cards * 2]])

        themes = self.ai.analyze_topic(topic, texts[:50]).themes or []

        drafts: list[CardDraft] = []
        for idx, it in enumerate(pool[: max_cards * 2]):
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

            s = sentiments[idx] if idx < len(sentiments) else None
            sentiment = (s.label if s else "neutral")

            theme = None
            if themes:
                theme = themes[idx % len(themes)]

            display_label = f"{it.source} reaction" if it.source.lower() != "news" else "News quote"

            drafts.append(
                CardDraft(
                    quote=quote,
                    theme=theme,
                    sentiment=sentiment,
                    display_label=display_label,
                )
            )

            if len(drafts) >= max_cards:
                break

        return drafts

