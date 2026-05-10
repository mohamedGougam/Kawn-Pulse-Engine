from __future__ import annotations

import html as _html
import re
from hashlib import sha256


_WS_RE = re.compile(r"\s+")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"#\w+")
_MENTION_RE = re.compile(r"(?:^|\s)@\w+")
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F02F\U0001F100-\U0001F1FF]+",
    flags=re.UNICODE,
)
_BRACKET_NOTE_RE = re.compile(r"\[[^\]]{1,40}\]")  # like [music], [applause]
_MULTI_PUNCT_RE = re.compile(r"([!?.,])\1{2,}")
_NON_PRINTABLE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def strip_html(text: str) -> str:
    if not text:
        return ""
    no_tags = _HTML_TAG_RE.sub(" ", text)
    decoded = _html.unescape(no_tags)
    return decoded


def normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip())


def clean_text(text: str) -> str:
    """Aggressive cleanup for UI-quote-quality content."""
    if not text:
        return ""
    t = strip_html(text)
    t = _NON_PRINTABLE_RE.sub(" ", t)
    t = _URL_RE.sub(" ", t)
    t = _HASHTAG_RE.sub(" ", t)
    t = _MENTION_RE.sub(" ", t)
    t = _BRACKET_NOTE_RE.sub(" ", t)
    t = _EMOJI_RE.sub(" ", t)
    t = _MULTI_PUNCT_RE.sub(r"\1", t)
    t = normalize_ws(t)
    return t


def _ratio_alpha(text: str) -> float:
    if not text:
        return 0.0
    letters = sum(1 for c in text if c.isalpha())
    return letters / max(1, len(text))


def is_low_quality(text: str, *, min_len: int = 16, min_words: int = 3) -> bool:
    """Reject very short, hashtag-spam, link-heavy, or near-empty content."""
    t = normalize_ws(text)
    if len(t) < min_len:
        return True

    words = t.split()
    if len(words) < min_words:
        return True

    if _ratio_alpha(t) < 0.45:
        return True

    if len(set(t.lower())) < 6:
        return True

    return False


def is_spam_like(text: str, *, min_len: int = 12) -> bool:
    t = normalize_ws(text)
    if len(t) < min_len:
        return True
    if len(set(t.lower())) < 6:
        return True
    return False


def best_sentence(text: str, *, max_len: int = 240, min_len: int = 30) -> str:
    """Pick the most quote-worthy sentence from a longer paragraph."""
    t = normalize_ws(text)
    if not t:
        return ""

    parts = re.split(r"(?<=[.!?])\s+", t)
    candidates = [p.strip() for p in parts if p.strip()]
    good = [p for p in candidates if len(p) >= min_len and not is_low_quality(p, min_len=min_len, min_words=4)]

    if good:
        good.sort(key=lambda s: (abs(len(s) - 140), -len(s)))
        return trim_text(good[0], max_len=max_len)

    return trim_text(t, max_len=max_len)


def trim_text(text: str, *, max_len: int = 600) -> str:
    t = normalize_ws(text)
    if len(t) <= max_len:
        return t
    return t[: max_len - 1].rstrip() + "…"


def content_fingerprint(*parts: str) -> str:
    joined = "||".join(normalize_ws(p) for p in parts if p is not None)
    return sha256(joined.encode("utf-8")).hexdigest()
