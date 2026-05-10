from __future__ import annotations

import re
import unicodedata


_SCRIPT_RANGES = [
    ("ar", (0x0600, 0x06FF)),
    ("ar", (0x0750, 0x077F)),
    ("he", (0x0590, 0x05FF)),
    ("ru", (0x0400, 0x04FF)),
    ("hi", (0x0900, 0x097F)),
    ("el", (0x0370, 0x03FF)),
    ("ja", (0x3040, 0x309F)),
    ("ja", (0x30A0, 0x30FF)),
    ("ko", (0xAC00, 0xD7AF)),
    ("zh", (0x4E00, 0x9FFF)),
    ("th", (0x0E00, 0x0E7F)),
]


_STOPWORDS: dict[str, set[str]] = {
    "en": {
        "the", "and", "is", "of", "to", "in", "a", "for", "on", "with", "as",
        "this", "that", "are", "was", "but", "from", "by", "it", "be", "an",
        "have", "has", "you", "we", "they", "their", "his", "her", "not",
    },
    "fr": {
        "le", "la", "les", "un", "une", "des", "et", "à", "de", "du", "en",
        "que", "qui", "dans", "pour", "pas", "sur", "avec", "ce", "il", "elle",
        "nous", "vous", "ils", "elles", "est", "sont", "ne", "se",
    },
    "es": {
        "el", "la", "los", "las", "y", "de", "que", "un", "una", "en", "es",
        "por", "para", "con", "no", "se", "su", "lo", "más", "como", "pero",
        "este", "esta", "esto",
    },
    "de": {
        "der", "die", "das", "und", "ist", "ich", "nicht", "ein", "eine",
        "zu", "den", "mit", "sich", "auf", "für", "von", "im", "in", "am",
        "auch", "es", "wir", "sie", "war", "werden", "haben", "sind",
    },
    "pt": {
        "o", "a", "os", "as", "e", "de", "que", "do", "da", "para", "em",
        "com", "uma", "um", "por", "no", "na", "se", "não", "mais", "mas",
        "como", "está", "são",
    },
    "it": {
        "il", "la", "lo", "i", "gli", "le", "e", "di", "che", "in", "con",
        "per", "non", "una", "un", "ma", "come", "su", "del", "della", "delle",
        "sono", "è",
    },
}


_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ]+", re.UNICODE)


def _script_language(text: str) -> str | None:
    if not text:
        return None
    counts: dict[str, int] = {}
    total = 0
    for ch in text:
        cp = ord(ch)
        if cp < 0x0080:
            continue
        for lang, (lo, hi) in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                counts[lang] = counts.get(lang, 0) + 1
                total += 1
                break

    if total == 0:
        return None

    best_lang, best_count = max(counts.items(), key=lambda kv: kv[1])
    if best_count / total >= 0.4:
        return best_lang
    return None


def _latin_language(text: str) -> str | None:
    if not text:
        return None
    norm = unicodedata.normalize("NFC", text.lower())
    tokens = _TOKEN_RE.findall(norm)
    if not tokens:
        return None

    scores: dict[str, int] = {lang: 0 for lang in _STOPWORDS.keys()}
    for tok in tokens:
        for lang, words in _STOPWORDS.items():
            if tok in words:
                scores[lang] += 1

    best_lang, best_score = max(scores.items(), key=lambda kv: kv[1])
    if best_score == 0:
        return "en"
    return best_lang


def detect_language(text: str) -> str:
    """
    Returns ISO 639-1-like code (en, fr, ar, es, de, pt, it, ru, he, hi, ja, ko, zh, ...).
    Uses Unicode script first, then a small Latin stop-word heuristic.
    Returns "und" (undetermined) if it cannot decide; callers may default to "en".
    """
    if not text:
        return "und"

    script = _script_language(text)
    if script:
        return script

    latin = _latin_language(text)
    if latin:
        return latin

    return "und"


_LANG_LABELS: dict[str, str] = {
    "en": "English",
    "fr": "French",
    "ar": "Arabic",
    "es": "Spanish",
    "de": "German",
    "pt": "Portuguese",
    "it": "Italian",
    "ru": "Russian",
    "he": "Hebrew",
    "hi": "Hindi",
    "el": "Greek",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "th": "Thai",
    "und": "Unknown",
}


def language_label(code: str | None) -> str:
    if not code:
        return _LANG_LABELS["und"]
    return _LANG_LABELS.get(code.lower(), code.upper())
