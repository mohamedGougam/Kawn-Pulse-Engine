from __future__ import annotations

from app.services.normalization_service import NormalizedItem
from app.utils.text_utils import (
    best_sentence,
    clean_text,
    content_fingerprint,
    is_low_quality,
    trim_text,
)


class CleaningService:
    def clean(self, items: list[NormalizedItem]) -> list[NormalizedItem]:
        cleaned: list[NormalizedItem] = []
        seen: set[str] = set()

        for it in items:
            raw = it.text or it.title or ""
            text = clean_text(raw)

            if not text or is_low_quality(text):
                title_clean = clean_text(it.title or "")
                if title_clean and not is_low_quality(title_clean):
                    text = title_clean
                else:
                    continue

            text = best_sentence(text, max_len=240)
            if not text or is_low_quality(text):
                continue

            text = trim_text(text, max_len=240)

            fp = content_fingerprint(it.source, it.source_url, text)
            if fp in seen:
                continue
            seen.add(fp)

            cleaned.append(
                NormalizedItem(
                    source=it.source,
                    source_url=it.source_url,
                    topic=it.topic,
                    author=it.author,
                    text=text,
                    title=clean_text(it.title or "") or it.title,
                    published_at=it.published_at,
                    engagement_count=it.engagement_count,
                    language=it.language,
                    external_id=it.external_id,
                )
            )

        return cleaned
