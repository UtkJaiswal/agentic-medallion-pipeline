"""Cross-cutting behaviour layered onto any provider (Decorator pattern). Each concern is one small
class, so adapters stay focused on protocol translation (SRP) and new concerns are added without
editing them (OCP). The factory composes, outermost first:

    CircuitBreaker -> Retry(backoff + jitter) -> RateLimit -> Metered(budget + usage log) -> adapter
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Protocol

from medallion.llm.pricing import cost_usd
from medallion.llm.types import (
    BudgetExceededError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderError,
    ProviderRefusalError,
    ProviderTransientError,
)
from medallion.resilience.circuit_breaker import CircuitBreaker
from medallion.resilience.rate_limit import TokenBucket
from medallion.resilience.retry import RetryPolicy, retry_call

log = logging.getLogger(__name__)


class ProviderDecorator(LLMProvider):
    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner
        self.name, self.model = inner.name, inner.model


class RateLimitedProvider(ProviderDecorator):
    def __init__(self, inner: LLMProvider, bucket: TokenBucket, max_wait_s: float = 120.0) -> None:
        super().__init__(inner)
        self._bucket, self._max_wait = bucket, max_wait_s

    def complete(self, request: LLMRequest) -> LLMResponse:
        if not self._bucket.acquire(timeout_s=self._max_wait):
            raise ProviderTransientError(self.name, "client-side rate limit wait exceeded")
        return self.inner.complete(request)


class RetryingProvider(ProviderDecorator):
    def __init__(self, inner: LLMProvider, policy: RetryPolicy) -> None:
        super().__init__(inner)
        self._policy = policy

    def complete(self, request: LLMRequest) -> LLMResponse:
        return retry_call(lambda: self.inner.complete(request), self._policy,
                          retry_on=(ProviderTransientError,), label=f"llm.{self.id}")


class CircuitBreakerProvider(ProviderDecorator):
    def __init__(self, inner: LLMProvider, breaker: CircuitBreaker) -> None:
        super().__init__(inner)
        self.breaker = breaker

    def complete(self, request: LLMRequest) -> LLMResponse:
        return self.breaker.call(lambda: self.inner.complete(request))


# ------------------------------------------------------------------------------- metering & budget
@dataclass(frozen=True)
class LLMCallRecord:
    task: str
    provider: str
    model: str
    status: str  # ok | cache_hit | error | invalid_output | refused
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: int | None = None
    error: str | None = None
    served_model: str | None = None


class UsageRecorder(Protocol):
    def record(self, call: LLMCallRecord) -> None: ...


class NullRecorder:
    def record(self, call: LLMCallRecord) -> None:
        pass


class Budget:
    """Per-run spend guard (tokens and USD) shared by all providers. Thread-safe."""

    def __init__(self, max_tokens: int, max_cost_usd: float) -> None:
        self.max_tokens, self.max_cost_usd = max_tokens, max_cost_usd
        self.tokens, self.cost = 0, 0.0
        self._lock = threading.Lock()

    def check(self) -> None:
        with self._lock:
            if self.tokens >= self.max_tokens or self.cost >= self.max_cost_usd:
                raise BudgetExceededError(
                    f"LLM budget exhausted: {self.tokens}/{self.max_tokens} tokens, "
                    f"${self.cost:.4f}/${self.max_cost_usd:.2f}")

    def add(self, tokens: int, cost: float | None) -> None:
        with self._lock:
            self.tokens += tokens
            self.cost += cost or 0.0


class MeteredProvider(ProviderDecorator):
    def __init__(self, inner: LLMProvider, recorder: UsageRecorder, budget: Budget) -> None:
        super().__init__(inner)
        self._recorder, self._budget = recorder, budget

    def complete(self, request: LLMRequest) -> LLMResponse:
        self._budget.check()
        try:
            resp = self.inner.complete(request)
        except ProviderError as exc:
            status = "refused" if isinstance(exc, ProviderRefusalError) else "error"
            self._recorder.record(LLMCallRecord(request.task, self.name, self.model, status, error=str(exc)[:500]))
            raise
        billed = resp.meta.get("billed_usd")
        cost = billed if billed is not None else cost_usd(self.name, self.model, resp.input_tokens,
                                                          resp.output_tokens)
        self._budget.add(resp.input_tokens + resp.output_tokens, cost)
        self._recorder.record(LLMCallRecord(request.task, self.name, self.model, "ok", resp.input_tokens,
                                            resp.output_tokens, cost, resp.latency_ms,
                                            served_model=resp.meta.get("served_model")))
        return resp
