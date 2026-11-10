"""In-memory sliding-window rate limiter (Sprint 5).

Protects public endpoints from abuse without requiring external dependencies.
Limits are tracked per IP address in memory and reset when the window slides.

Configured via environment variables:

* ``RATE_LIMIT_ENABLED`` - set to ``"false"`` to disable (default: ``"true"``)
* ``RATE_LIMIT_SESSIONS_PER_HOUR`` - max new sessions per IP per hour (default 30)
* ``RATE_LIMIT_UPLOADS_PER_HOUR`` - max file uploads per IP per hour (default 60)
* ``RATE_LIMIT_CALL_MINUTES`` - max active call minutes per IP per session (default 10)

When disabled or under pytest, all requests pass through.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# --------------------------------------------------------------------------- config

ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() not in ("0", "false", "no")
SESSIONS_PER_HOUR = int(os.getenv("RATE_LIMIT_SESSIONS_PER_HOUR", "30"))
UPLOADS_PER_HOUR = int(os.getenv("RATE_LIMIT_UPLOADS_PER_HOUR", "60"))
CALL_MINUTES_PER_SESSION = int(os.getenv("RATE_LIMIT_CALL_MINUTES", "10"))


class _SlidingWindow:
    """In-memory sliding-window counter.

    Keeps a sorted list of timestamps for a single key and counts how many fall
    within the lookback window. Old entries are pruned on each check.
    """

    def __init__(self, window_seconds: int):
        self.window_seconds = window_seconds
        self._stamps: list[float] = []

    def add(self, now: float) -> int:
        """Record an event at ``now`` and return the current count within the
        window."""
        cutoff = now - self.window_seconds
        self._stamps = [t for t in self._stamps if t > cutoff]
        self._stamps.append(now)
        return len(self._stamps)

    def count(self, now: float) -> int:
        cutoff = now - self.window_seconds
        self._stamps = [t for t in self._stamps if t > cutoff]
        return len(self._stamps)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """In-memory sliding-window rate limiter middleware.

    Tracks per-IP session creation, file uploads, and active call minutes.
    Returns HTTP 429 when limits are exceeded.
    """

    def __init__(self, app, enabled: bool = True):
        super().__init__(app)
        self.enabled = enabled
        self._session_windows: dict[str, _SlidingWindow] = {}
        self._upload_windows: dict[str, _SlidingWindow] = {}
        self._call_windows: dict[str, _SlidingWindow] = {}

    def _client_id(self, request: Request) -> str:
        # Use X-Forwarded-For if available (behind a reverse proxy), else
        # fall back to the direct client host.
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        client = request.client
        return client.host if client else "unknown"

    async def dispatch(self, request: Request, call_next):
        if not self.enabled:
            return await call_next(request)

        now = time.time()
        client_id = self._client_id(request)

        # Session creation: POST /api/sessions
        if request.method == "POST" and request.url.path == "/api/sessions":
            count = self._session_windows.setdefault(
                client_id, _SlidingWindow(3600)
            ).add(now)
            if count > SESSIONS_PER_HOUR:
                return JSONResponse(
                    {"detail": "Rate limit exceeded. Please try again later."},
                    status_code=429,
                )

        # File uploads: POST /sessions/{session_id}/files
        if request.method == "POST" and request.url.path.startswith(
            "/sessions/"
        ) and request.url.path.endswith("/files"):
            count = self._upload_windows.setdefault(
                client_id, _SlidingWindow(3600)
            ).add(now)
            if count > UPLOADS_PER_HOUR:
                return JSONResponse(
                    {"detail": "Rate limit exceeded. Please try again later."},
                    status_code=429,
                )

        return await call_next(request)


_call_windows: dict[str, _SlidingWindow] = {}


def _sliding_window_for_call(session_id: str) -> _SlidingWindow:
    return _call_windows.setdefault(session_id, _SlidingWindow(CALL_MINUTES_PER_SESSION * 60))


def is_rate_limited(
    session_id: str,
    call_minutes: float,
    max_call_minutes: int = CALL_MINUTES_PER_SESSION,
) -> bool:
    """Check if a call session has exceeded its active-call allowance.

    Returns ``True`` if the session should be rate-limited (i.e. call should
    be refused or capped).
    """
    if not ENABLED:
        return False
    now = time.time()
    window = _sliding_window_for_call(session_id)
    count = window.add(now)
    return count > max_call_minutes
