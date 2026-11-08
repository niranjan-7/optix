"""A small thread-safe token bucket, shared by the webhook and the Telegram alerts."""
from __future__ import annotations

import threading
import time
from typing import Callable


class TokenBucket:
    """Allows `burst` requests at once, refilling at `per_minute`."""

    def __init__(self, per_minute: float, burst: int, clock: Callable[[], float] = time.monotonic):
        self._rate, self._burst, self._clock = per_minute / 60.0, float(burst), clock
        self._tokens, self._last = float(burst), clock()
        self._lock = threading.Lock()

    def take(self) -> float:
        """0.0 if allowed (a token was used); otherwise seconds until one is available."""
        with self._lock:
            now = self._clock()
            self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            return (1.0 - self._tokens) / self._rate
