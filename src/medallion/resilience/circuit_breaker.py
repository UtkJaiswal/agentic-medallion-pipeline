"""Circuit breaker: after N consecutive transient failures, stop calling a dependency for a cool-down
period and fail fast, so the fallback chain moves to the next provider immediately instead of
burning its whole retry budget on a provider that is down."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from enum import StrEnum
from typing import TypeVar

from medallion.resilience.retry import TransientError

T = TypeVar("T")


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(TransientError):
    pass


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 5, reset_timeout_s: float = 30.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout_s = reset_timeout_s
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state is CircuitState.OPEN and self._clock() - self._opened_at >= self.reset_timeout_s:
                self._state = CircuitState.HALF_OPEN
            return self._state

    def call(self, fn: Callable[[], T]) -> T:
        if self.state is CircuitState.OPEN:
            raise CircuitOpenError(f"circuit '{self.name}' is open")
        try:
            result = fn()
        except TransientError:
            self._record_failure()
            raise
        self._record_success()
        return result

    def _record_success(self) -> None:
        with self._lock:
            self._state, self._failures = CircuitState.CLOSED, 0

    def _record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state is CircuitState.HALF_OPEN or self._failures >= self.failure_threshold:
                self._state, self._opened_at = CircuitState.OPEN, self._clock()
