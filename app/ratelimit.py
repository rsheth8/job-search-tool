"""A tiny thread-safe token-bucket rate limiter.

Caps how often we hit the paid LLM API. When the bucket is empty we don't
block or error — the caller falls back to the offline heuristic router, so the
user still gets an answer (the system's "never block the user" rule).
"""
from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, rate_per_minute: int, burst: int | None = None) -> None:
        self.capacity = float(burst if burst is not None else rate_per_minute)
        self.tokens = self.capacity
        self.refill_per_sec = rate_per_minute / 60.0
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        """Consume one token if available; return whether the call may proceed."""
        with self._lock:
            now = time.monotonic()
            self.tokens = min(
                self.capacity, self.tokens + (now - self._last) * self.refill_per_sec
            )
            self._last = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False
