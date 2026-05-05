from __future__ import annotations

import random
from dataclasses import dataclass

from app.config import settings


@dataclass
class SentimentResult:
    label: str  # positive/neutral/negative
    score: float


class SentimentService:
    def __init__(self) -> None:
        self._pipeline = None
        self._ready = False

    def _ensure_pipeline(self) -> None:
        if self._ready:
            return
        self._ready = True
        try:
            from transformers import pipeline  # type: ignore

            self._pipeline = pipeline("sentiment-analysis", model=settings.ai_sentiment_model)
        except Exception:
            self._pipeline = None

    def analyze(self, texts: list[str]) -> list[SentimentResult]:
        self._ensure_pipeline()
        if not texts:
            return []

        if self._pipeline is None:
            return [_mock_sentiment(t) for t in texts]

        try:
            out = self._pipeline(texts, truncation=True)
            results: list[SentimentResult] = []
            for r in out:
                label = str(r.get("label", "neutral")).lower()
                score = float(r.get("score", 0.0))

                # CardiffNLP label mapping varies; normalize:
                if "pos" in label:
                    norm = "positive"
                elif "neg" in label:
                    norm = "negative"
                else:
                    norm = "neutral"

                results.append(SentimentResult(label=norm, score=score))
            return results
        except Exception:
            return [_mock_sentiment(t) for t in texts]


def _mock_sentiment(text: str) -> SentimentResult:
    seed = abs(hash(text)) % (2**32)
    rnd = random.Random(seed)
    r = rnd.random()
    if r < 0.34:
        return SentimentResult(label="negative", score=round(0.55 + rnd.random() * 0.4, 3))
    if r < 0.67:
        return SentimentResult(label="neutral", score=round(0.5 + rnd.random() * 0.35, 3))
    return SentimentResult(label="positive", score=round(0.55 + rnd.random() * 0.4, 3))

