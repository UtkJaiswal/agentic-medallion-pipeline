import pytest

from medallion.resilience.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState
from medallion.resilience.rate_limit import TokenBucket
from medallion.resilience.retry import (
    PermanentError,
    PollTimeout,
    RetryPolicy,
    TransientError,
    poll_until,
    retry_call,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def test_backoff_is_exponential_capped_and_jittered():
    p = RetryPolicy(base_delay_s=1, max_delay_s=10, jitter="none")
    assert [p.backoff(n) for n in range(1, 7)] == [1, 2, 4, 8, 10, 10]
    full = RetryPolicy(base_delay_s=1, max_delay_s=10, jitter="full")
    assert full.backoff(3, rand=lambda: 0.0) == 0 and full.backoff(3, rand=lambda: 0.999) < 4
    equal = RetryPolicy(base_delay_s=1, max_delay_s=10, jitter="equal")
    assert equal.backoff(3, rand=lambda: 0.0) >= 2 and equal.backoff(3, rand=lambda: 1.0) == 4


def test_retry_succeeds_after_transient_failures():
    calls, sleeps = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TransientError("boom")
        return "ok"

    assert retry_call(flaky, RetryPolicy(max_attempts=4), sleep=sleeps.append, rand=lambda: 0.5) == "ok"
    assert len(calls) == 3 and len(sleeps) == 2


def test_retry_honours_retry_after_and_gives_up():
    sleeps = []

    def always():
        raise TransientError("429", retry_after=7)

    with pytest.raises(TransientError):
        retry_call(always, RetryPolicy(max_attempts=3, base_delay_s=0.1, max_delay_s=30), sleep=sleeps.append)
    assert sleeps == [7, 7]


def test_retry_does_not_retry_permanent_errors():
    calls = []

    def bad():
        calls.append(1)
        raise PermanentError("400")

    with pytest.raises(PermanentError):
        retry_call(bad, RetryPolicy(max_attempts=5), sleep=lambda s: None)
    assert len(calls) == 1


def test_poll_until_returns_and_times_out():
    clock, values = FakeClock(), iter(["queued", "running", "succeeded"])
    assert poll_until(lambda: next(values), lambda v: v == "succeeded", timeout_s=60,
                      sleep=clock.sleep, clock=clock) == "succeeded"
    with pytest.raises(PollTimeout):
        poll_until(lambda: "running", lambda v: False, timeout_s=5, sleep=clock.sleep, clock=clock)


def test_token_bucket_limits_rate():
    clock = FakeClock()
    bucket = TokenBucket(rate_per_s=2, capacity=2, clock=clock, sleep=clock.sleep)
    assert bucket.try_acquire() == 0 and bucket.try_acquire() == 0
    assert bucket.try_acquire() == pytest.approx(0.5)
    assert bucket.acquire() and clock.t == pytest.approx(0.5)
    assert bucket.acquire(timeout_s=0.1) is False


def test_circuit_breaker_opens_then_half_opens_then_closes():
    clock = FakeClock()
    cb = CircuitBreaker("p", failure_threshold=2, reset_timeout_s=30, clock=clock)

    def fail():
        raise TransientError("down")

    for _ in range(2):
        with pytest.raises(TransientError):
            cb.call(fail)
    assert cb.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        cb.call(lambda: "never called")
    clock.t += 31
    assert cb.state is CircuitState.HALF_OPEN
    assert cb.call(lambda: "ok") == "ok" and cb.state is CircuitState.CLOSED


def test_circuit_breaker_ignores_permanent_errors():
    cb = CircuitBreaker("p", failure_threshold=1)

    def bad_request():
        raise PermanentError("400")

    with pytest.raises(PermanentError):
        cb.call(bad_request)
    assert cb.state is CircuitState.CLOSED
