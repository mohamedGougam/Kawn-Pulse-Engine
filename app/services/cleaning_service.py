from __future__ import annotations

from app.services.normalization_service import NormalizedItem
from app.utils.text_utils import content_fingerprint, is_spam_like, trim_text


class CleaningService:
    def clean(self, items: list[NormalizedItem]) -> list[NormalizedItem]:
        cleaned: list[NormalizedItem] = []
        seen: set[str] = set()

        for it in items:
            text = it.text or it.title or ""
            text = trim_text(text, max_len=700)
            if not text or is_spam_like(text):
                continue

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
                    title=it.title,
                    published_at=it.published_at,
                    engagement_count=it.engagement_count,
                    language=it.language,
                    external_id=it.external_id,
                )
            )

        return cleaned

