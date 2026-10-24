"""Recognising "the database is unavailable" as opposed to "the code is broken".

Neon's free tier refuses connections when the project's quota is used up
(asyncpg.exceptions.InsufficientResourcesError: "Your account or project has
exceeded the quota"), suspends compute, or is briefly unreachable. Those
should surface as a clean 503 the client can retry, not as a crash at startup
or an opaque 500 that includes internal details.
"""
from __future__ import annotations

import re
import socket

import asyncpg
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import exc as sa_exc


# Connection errors can echo the DSN; never let credentials reach logs or
# the /health/db response.
_SECRETS = [
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1***:***@"),
    (re.compile(r"(?i)\b(password|passwd|pwd|token|secret)=[^&\s'\"]+"), r"\1=***"),
]


def redact(text: object) -> str:
    out = str(text)
    for pattern, repl in _SECRETS:
        out = pattern.sub(repl, out)
    return out

_ASYNCPG_NAMES = (
    "InsufficientResourcesError",   # Neon: project/account quota exceeded
    "CannotConnectNowError",        # server starting up / shutting down
    "AdminShutdownError",           # "terminating connection due to administrator command" (e.g. compute suspended)
    "CrashShutdownError",
    "TooManyConnectionsError",
    "PostgresConnectionError",
    "ConnectionDoesNotExistError",
    "ConnectionFailureError",
    "InterfaceError",
)
_ASYNCPG_ERRORS = tuple(
    getattr(asyncpg.exceptions, n) for n in _ASYNCPG_NAMES if hasattr(asyncpg.exceptions, n)
)
_UNAVAILABLE = _ASYNCPG_ERRORS + (
    sa_exc.OperationalError,
    sa_exc.InterfaceError,
    sa_exc.TimeoutError,      # SQLAlchemy pool exhausted
    ConnectionError,          # refused / reset / aborted
    socket.gaierror,          # DNS failure
)


def is_db_unavailable(exc: BaseException | None) -> bool:
    """True if `exc`, or anything it wraps/was raised from, means the
    database could not be reached or refused service."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, _UNAVAILABLE):
            return True
        exc = getattr(exc, "orig", None) or exc.__cause__ or exc.__context__
    return False


def describe(exc: BaseException) -> str:
    """One safe line for logs/diagnostics (no credentials)."""
    return redact(f"{type(exc).__name__}: {exc}")[:300]


def unavailable_response() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": "The database is temporarily unavailable. Please retry in a little while."},
        headers={"Retry-After": "30"},
    )


async def db_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if is_db_unavailable(exc):
        return unavailable_response()
    # Any other database error stays an ordinary server error (and is logged
    # by the framework); only the response body is kept free of internals.
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
