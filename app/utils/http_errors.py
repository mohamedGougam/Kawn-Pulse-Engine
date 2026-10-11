"""Turn an exception raised by a connector into something safe to log and
to show on a diagnostics endpoint.

Why this exists: httpx's HTTPStatusError message contains the full request
URL. Several connectors put secrets in the query string (YouTube's
`?key=...`), so a plain `logger.warning("... failed: %s", e)` writes the API
key into the logs on every failure. This module never includes a query
string, and additionally scrubs anything that looks like a credential, so
the same helper is safe to use for every connector.

It also classifies the failure, because "403" on its own means very
different things per source (bad credentials, exhausted quota, blocked IP).
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

import httpx

# kinds
AUTH_REJECTED = "auth_rejected"
QUOTA_EXCEEDED = "quota_exceeded"
RATE_LIMITED = "rate_limited"
NOT_FOUND = "not_found"
SERVER_ERROR = "server_error"
HTTP_ERROR = "http_error"
TIMEOUT = "timeout"
NETWORK = "network"
BAD_RESPONSE = "bad_response"
UNAVAILABLE = "unavailable"
ERROR = "error"

_QUERY_RE = re.compile(r"\?[^\s'\"<>)]*")
_SECRET_PAIR_RE = re.compile(
    r"(?i)\b(key|api_key|apikey|token|access_token|client_secret|secret|password|authorization)\b\s*[=:]\s*[^\s&'\",;]+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")

_QUOTA_REASONS = {"quotaexceeded", "dailylimitexceeded", "userratelimitexceeded", "ratelimitexceeded"}


class ConnectorUnavailable(RuntimeError):
    """Raised by a connector that already knows a call would fail (e.g. a
    daily quota that is known to be exhausted), so it can say so without
    making the request. Treated as its own kind so it shows up clearly in
    health/diagnostics instead of looking like a network error."""


@dataclass
class ErrorInfo:
    kind: str
    message: str
    status: int | None = None

    def as_dict(self) -> dict:
        d = {"kind": self.kind, "message": self.message}
        if self.status is not None:
            d["status"] = self.status
        return d


def scrub(text: str, *, max_len: int = 200) -> str:
    """Remove query strings and credential-looking pairs from free text."""
    t = _QUERY_RE.sub("", text or "")
    t = _BEARER_RE.sub("bearer [redacted]", t)
    t = _SECRET_PAIR_RE.sub(lambda m: f"{m.group(1)}=[redacted]", t)
    t = " ".join(t.split())
    return t if len(t) <= max_len else t[: max_len - 1] + "…"


def _body_reason(response: httpx.Response) -> str:
    """Best-effort short reason from an error body. Different APIs put it in
    different places; every branch is optional."""
    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""

    err = data.get("error")
    # Google style: {"error": {"errors": [{"reason": "quotaExceeded"}], "message": "..."}}
    if isinstance(err, dict):
        for e in err.get("errors") or []:
            if isinstance(e, dict) and e.get("reason"):
                return str(e["reason"])
        for key in ("status", "message"):
            if err.get(key):
                return str(err[key])
    # AT Protocol / Reddit style: {"error": "AuthMissing", "message": "..."}
    if isinstance(err, str) and err:
        return err
    for key in ("reason", "message", "detail"):
        if data.get(key):
            return str(data[key])
    return ""


def describe_error(exc: BaseException) -> ErrorInfo:
    if isinstance(exc, ConnectorUnavailable):
        return ErrorInfo(UNAVAILABLE, scrub(str(exc)))

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        reason = scrub(_body_reason(exc.response), max_len=80)
        target = f"{exc.request.url.host}{exc.request.url.path}"  # never the query string
        suffix = f" ({reason})" if reason else ""
        message = f"HTTP {status} from {target}{suffix}"

        if reason.lower().replace("_", "") in _QUOTA_REASONS and status in (403, 429):
            return ErrorInfo(QUOTA_EXCEEDED, message, status)
        if status in (401, 403):
            return ErrorInfo(AUTH_REJECTED, message, status)
        if status == 429:
            return ErrorInfo(RATE_LIMITED, message, status)
        if status == 404:
            return ErrorInfo(NOT_FOUND, message, status)
        if status >= 500:
            return ErrorInfo(SERVER_ERROR, message, status)
        return ErrorInfo(HTTP_ERROR, message, status)

    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError)):
        return ErrorInfo(TIMEOUT, "timed out")

    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError)):
        return ErrorInfo(NETWORK, f"{type(exc).__name__}: {scrub(str(exc))}")

    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return ErrorInfo(BAD_RESPONSE, f"{type(exc).__name__}: {scrub(str(exc))}")

    return ErrorInfo(ERROR, f"{type(exc).__name__}: {scrub(str(exc))}")
