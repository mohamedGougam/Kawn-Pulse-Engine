from __future__ import annotations

import re
from hashlib import sha256


_WS_RE = re.compile(r"\s+")


def normalize_ws(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip())


def is_spam_like(text: str, *, min_len: int = 12) -> bool:
    t = normalize_ws(text)
    if len(t) < min_len:
        return True
    if len(set(t.lower())) < 6:
        return True
    return False


def trim_text(text: str, *, max_len: int = 600) -> str:
    t = normalize_ws(text)
    if len(t) <= max_len:
        return t
    return t[: max_len - 1].rstrip() + "…"


def content_fingerprint(*parts: str) -> str:
    joined = "||".join(normalize_ws(p) for p in parts if p is not None)
    return sha256(joined.encode("utf-8")).hexdigest()

