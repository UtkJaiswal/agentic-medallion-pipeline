"""Thread-safe token bucket. Used client-side to stay under provider RPM limits (so we spend our
retry budget on real failures, not self-inflicted 429s) and to throttle the public API."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class TokenBucket:
    def __init__(self, rate_per_s: float, capacity: float | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if rate_per_s <= 0:
            raise ValueError("rate_per_s must be positive")
        self.rate = rate_per_s
        self.capacity = capacity if capacity is not None else max(1.0, rate_per_s)
        self._tokens = self.capacity
        self._clock, self._sleep = clock, sleep
        self._updated = clock()
        self._lock = threading.Lock()

    @classmethod
    def per_minute(cls, rpm: int, **kw: object) -> TokenBucket:
        return cls(rpm / 60.0, capacity=max(1.0, rpm / 10), **kw)  # burst = 10% of a minute

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    def try_acquire(self, tokens: float = 1.0) -> float:
        """Take tokens if available. Returns 0.0 on success, else seconds until they would be."""
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return 0.0
            return (tokens - self._tokens) / self.rate

    def acquire(self, tokens: float = 1.0, timeout_s: float | None = None) -> bool:
        """Block until tokens are available (or timeout). Returns False on timeout."""
        waited = 0.0
        while (wait := self.try_acquire(tokens)) > 0:
            if timeout_s is not None and waited + wait > timeout_s:
                return False
            self._sleep(wait)
            waited += wait
        return True
