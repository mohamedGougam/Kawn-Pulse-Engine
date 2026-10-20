from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.utils.lang_utils import detect_language


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
            import torch  # noqa
            from transformers import pipeline  # type: ignore

            self._pipeline = pipeline("sentiment-analysis", model=settings.ai_sentiment_model)
        except Exception:
            self._pipeline = None

    def analyze(self, texts: list[str]) -> list[SentimentResult]:
        self._ensure_pipeline()
        if not texts:
            return []

        if self._pipeline is None:
            return [lexicon_sentiment(t) for t in texts]

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
            return [lexicon_sentiment(t) for t in texts]


_vader = None


def _get_vader():
    global _vader
    if _vader is None:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

        _vader = SentimentIntensityAnalyzer()
    return _vader


def lexicon_sentiment(text: str) -> SentimentResult:
    """Real (if simple) sentiment for when the transformer model isn't installed.

    The production build installs requirements.txt only - no torch/transformers
    (they don't fit a 512 MB instance) - so the pipeline above is normally
    None. This used to fall back to a *random* label seeded from hash(text),
    which is why an obviously happy sentence could come out "Negative". VADER is
    a small pure-Python lexicon scorer: no model download, a few MB of RAM.

    It only understands English, so other languages are reported "neutral"
    rather than guessed at. Anything whose compound score sits inside
    +/- settings.sentiment_neutral_band is "neutral".
    """
    text = (text or "").strip()
    if not text or detect_language(text) not in ("en", "und"):
        return SentimentResult(label="neutral", score=0.5)
    try:
        compound = float(_get_vader().polarity_scores(text)["compound"])
    except Exception:
        return SentimentResult(label="neutral", score=0.5)

    band = settings.sentiment_neutral_band
    if compound >= band:
        return SentimentResult(label="positive", score=round(abs(compound), 3))
    if compound <= -band:
        return SentimentResult(label="negative", score=round(abs(compound), 3))
    return SentimentResult(label="neutral", score=round(1.0 - abs(compound), 3))
