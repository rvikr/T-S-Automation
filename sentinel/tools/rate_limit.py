"""In-process, fixed-window rate limiting for the API.

Scope, stated plainly: this limiter lives in one process's memory. It stops a
single client from hammering a single worker — brute-forcing the admin token,
or burning the operator's OpenAI budget through /moderation/cases. It is not
DDoS protection and does not coordinate across workers; a multi-worker or
multi-node deployment still needs a gateway limiter in front (see SECURITY.md).

Client identity is ``request.client.host`` — the direct peer address. The
``X-Forwarded-For`` header is deliberately ignored: honouring it would let any
client mint fresh identities per request and bypass the limiter entirely. When
Sentinel runs behind a reverse proxy (where every peer is the proxy), do the
per-client limiting at the proxy, which actually knows the caller.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """Fixed-window counter per client key. A limit of 0 disables the limiter."""

    # Prune bookkeeping once the table exceeds this many distinct keys, so an
    # address-rotating client cannot grow memory without bound.
    _PRUNE_THRESHOLD = 10_000

    def __init__(self, limit_per_minute: int):
        self.limit = max(0, int(limit_per_minute))
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[int, int]] = {}  # key -> (window index, count)

    def check(self, key: str) -> tuple[bool, int]:
        """Record one request for ``key``; returns (allowed, retry_after_seconds)."""
        if self.limit <= 0:
            return True, 0
        now = time.time()
        window = int(now // 60)
        with self._lock:
            start, count = self._windows.get(key, (window, 0))
            if start != window:
                start, count = window, 0
            count += 1
            self._windows[key] = (start, count)
            if len(self._windows) > self._PRUNE_THRESHOLD:
                self._prune(window)
            if count > self.limit:
                return False, max(1, int((window + 1) * 60 - now))
        return True, 0

    def _prune(self, current_window: int) -> None:
        # Called with the lock held.
        stale = [key for key, (start, _) in self._windows.items() if start != current_window]
        for key in stale:
            del self._windows[key]
