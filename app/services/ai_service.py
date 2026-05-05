from __future__ import annotations

import json
import random
from dataclasses import dataclass

from app.services.sentiment_service import SentimentService
from app.services.summarization_service import SummarizationService
from app.utils.text_utils import normalize_ws


@dataclass
class TopicAIResult:
    summary_text: str
    sentiment_label: str
    sentiment_score: float
    breakdown: dict[str, float]
    themes: list[str]


class AIService:
    def __init__(self) -> None:
        self.sentiment = SentimentService()
        self.summarizer = SummarizationService()

    def analyze_topic(self, topic: str, texts: list[str]) -> TopicAIResult:
        texts = [normalize_ws(t) for t in texts if t]
        if not texts:
            return TopicAIResult(
                summary_text="",
                sentiment_label="neutral",
                sentiment_score=0.0,
                breakdown={"positive": 0.0, "neutral": 1.0, "negative": 0.0},
                themes=[],
            )

        sentiments = self.sentiment.analyze(texts[:60])
        counts = {"positive": 0, "neutral": 0, "negative": 0}
        score_acc = {"positive": 0.0, "neutral": 0.0, "negative": 0.0}
        for s in sentiments:
            lbl = s.label if s.label in counts else "neutral"
            counts[lbl] += 1
            score_acc[lbl] += float(s.score or 0.0)

        total = max(1, sum(counts.values()))
        breakdown = {k: round(v / total, 4) for k, v in counts.items()}

        # Pick dominant sentiment
        sentiment_label = max(counts.items(), key=lambda kv: kv[1])[0]
        denom = max(1, counts[sentiment_label])
        sentiment_score = round(score_acc[sentiment_label] / denom, 4)

        summary = self.summarizer.summarize(topic, texts[:40])
        themes = _extract_themes_mock(topic, texts)

        return TopicAIResult(
            summary_text=summary,
            sentiment_label=sentiment_label,
            sentiment_score=sentiment_score,
            breakdown=breakdown,
            themes=themes,
        )

    def dumps_themes(self, themes: list[str]) -> str:
        return json.dumps(themes, ensure_ascii=False)

    def loads_themes(self, themes_json: str) -> list[str]:
        try:
            data = json.loads(themes_json or "[]")
            if isinstance(data, list):
                return [str(x) for x in data][:12]
        except Exception:
            pass
        return []


def _extract_themes_mock(topic: str, texts: list[str]) -> list[str]:
    seed = abs(hash(topic)) % (2**32)
    rnd = random.Random(seed)
    candidates = [
        "Key moments",
        "Performance / quality",
        "Context and backstory",
        "Hot takes & debate",
        "Community reaction",
        "Unexpected twists",
        "Stats & comparisons",
        "Future implications",
        "Coaching / strategy",
        "Media coverage",
    ]
    rnd.shuffle(candidates)
    n = 3 + rnd.randint(0, 3)
    return candidates[:n]

