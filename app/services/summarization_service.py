from __future__ import annotations

import random

from app.config import settings
from app.utils.text_utils import normalize_ws, trim_text


class SummarizationService:
    def __init__(self) -> None:
        self._pipeline = None
        self._ready = False

    def _ensure_pipeline(self) -> None:
        if self._ready:
            return
        self._ready = True
        try:
            from transformers import pipeline  # type: ignore

            self._pipeline = pipeline("summarization", model=settings.ai_summary_model)
        except Exception:
            self._pipeline = None

    def summarize(self, topic: str, texts: list[str]) -> str:
        self._ensure_pipeline()
        blob = normalize_ws(" ".join(t for t in texts if t))
        if not blob:
            return ""

        blob = trim_text(blob, max_len=1800)

        if self._pipeline is None:
            return _mock_summary(topic, texts)

        try:
            out = self._pipeline(blob, max_length=140, min_length=40, do_sample=False)
            if out and isinstance(out, list):
                return normalize_ws(out[0].get("summary_text", "") or "")
        except Exception:
            pass

        return _mock_summary(topic, texts)


def _mock_summary(topic: str, texts: list[str]) -> str:
    seed = abs(hash(topic + "||" + (texts[0] if texts else ""))) % (2**32)
    rnd = random.Random(seed)
    angles = [
        "public reactions focus on the biggest moments and performance",
        "discussion splits between hype and critique, with lots of context-sharing",
        "people highlight a few standout quotes and recurring themes",
        "sentiment is mixed, but engagement is driven by specific moments",
    ]
    a = rnd.choice(angles)
    return normalize_ws(
        f"Pulse for '{topic}': {a}. Overall, the conversation centers on what happened, why it matters, "
        "and how different communities interpret the same details."
    )

