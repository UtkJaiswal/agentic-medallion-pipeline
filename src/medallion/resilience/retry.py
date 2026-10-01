"""Retry with capped exponential backoff + jitter, and a polling helper built on the same policy.

Full jitter (delay ~ U(0, min(cap, base * 2^n))) is the AWS-recommended default: it spreads retries
from many clients so they don't re-synchronise into thundering herds after an outage."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeVar

T = TypeVar("T")
log = logging.getLogger(__name__)


class TransientError(Exception):
    """Failure worth retrying (timeouts, 429, 5xx). `retry_after` honours server back-pressure hints."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(Exception):
    """Failure that retrying cannot fix (bad request, auth, invalid output)."""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay_s: float = 0.5
    max_delay_s: float = 20.0
    jitter: Literal["full", "equal", "none"] = "full"

    def backoff(self, retry_number: int, rand: Callable[[], float] = random.random) -> float:
        """Delay before retry #`retry_number` (1-based)."""
        ceiling = min(self.max_delay_s, self.base_delay_s * (2 ** (retry_number - 1)))
        if self.jitter == "full":
            return ceiling * rand()
        if self.jitter == "equal":
            return ceiling / 2 + (ceiling / 2) * rand()
        return ceiling


def retry_call(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    retry_on: tuple[type[BaseException], ...] = (TransientError,),
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
    label: str = "call",
) -> T:
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            if attempt == policy.max_attempts:
                raise
            delay = policy.backoff(attempt, rand)
            hint = getattr(exc, "retry_after", None)
            if hint is not None:  # never retry sooner than the server asked; still respect our cap
                delay = min(max(delay, hint), policy.max_delay_s)
            log.warning("retry.scheduled", extra={"label": label, "attempt": attempt,
                                                  "delay_s": round(delay, 3), "error": str(exc)[:200]})
            sleep(delay)
    raise AssertionError("unreachable")


class PollTimeout(TimeoutError):
    pass


DEFAULT_POLL_POLICY = RetryPolicy(base_delay_s=0.5, max_delay_s=10.0, jitter="equal")


def poll_until(
    fetch: Callable[[], T],
    done: Callable[[T], bool],
    *,
    timeout_s: float,
    policy: RetryPolicy = DEFAULT_POLL_POLICY,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """Poll with growing, jittered intervals - cheap for the server, responsive early on."""
    deadline = clock() + timeout_s
    n = 0
    while True:
        value = fetch()
        if done(value):
            return value
        n += 1
        remaining = deadline - clock()
        if remaining <= 0:
            raise PollTimeout(f"condition not met within {timeout_s}s")
        sleep(min(policy.backoff(n), remaining))
