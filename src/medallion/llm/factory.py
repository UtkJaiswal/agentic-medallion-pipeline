"""Builds the configured provider chain (Factory). The only module that knows concrete adapter classes."""

from __future__ import annotations

from medallion.db import Database
from medallion.llm.cache import LLMCache, PostgresLLMCache
from medallion.llm.decorators import (
    Budget,
    CircuitBreakerProvider,
    LLMCallRecord,
    MeteredProvider,
    RateLimitedProvider,
    RetryingProvider,
    UsageRecorder,
)
from medallion.llm.guardrails import InputGuard
from medallion.llm.providers.anthropic import AnthropicProvider
from medallion.llm.providers.gemini import GeminiProvider
from medallion.llm.providers.openai_compatible import OpenAICompatibleProvider
from medallion.llm.router import LLMRouter
from medallion.llm.types import LLMProvider
from medallion.observability.context import current_run_id, current_trace_id
from medallion.resilience.circuit_breaker import CircuitBreaker
from medallion.resilience.rate_limit import TokenBucket
from medallion.resilience.retry import RetryPolicy
from medallion.settings import AnthropicSettings, ProviderName, Settings


class PostgresUsageRecorder:
    """Writes each LLM attempt to ops.llm_calls in its own transaction, so failed calls are recorded
    even when the surrounding pipeline transaction rolls back."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def record(self, call: LLMCallRecord) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """INSERT INTO ops.llm_calls (run_id, trace_id, task, provider, model, served_model, status,
                                              input_tokens, output_tokens, cost_usd, latency_ms, error)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (current_run_id(), current_trace_id(), call.task, call.provider, call.model, call.served_model,
                 call.status, call.input_tokens, call.output_tokens, call.cost_usd, call.latency_ms, call.error))


def build_adapter(name: ProviderName, settings: Settings, model: str | None = None) -> LLMProvider:
    cfg = settings.provider(name)
    if model:
        cfg = cfg.model_copy(update={"model": model})
    key = cfg.api_key.get_secret_value() if cfg.api_key else None
    timeout = settings.llm_timeout_s
    if name in ("openai", "openrouter", "gemini", "anthropic") and not key:
        raise ValueError(f"LLM provider '{name}' is in LLM_PROVIDERS but {name.upper()}_API_KEY is not set")
    match name:
        case "anthropic":
            assert isinstance(cfg, AnthropicSettings)
            return AnthropicProvider(cfg.model or "", api_key=key, timeout_s=timeout, effort=cfg.effort)
        case "gemini":
            return GeminiProvider(cfg.model or "", api_key=key, timeout_s=timeout)
        case "openai":
            # current OpenAI reasoning models reject custom temperature and `max_tokens`
            return OpenAICompatibleProvider("openai", cfg.model or "", api_key=key, base_url=cfg.base_url,
                                            timeout_s=timeout, temperature=None,
                                            token_param="max_completion_tokens", reasoning_headroom=True)
        case "openrouter":
            return OpenAICompatibleProvider("openrouter", cfg.model or "", api_key=key, base_url=cfg.base_url,
                                            timeout_s=timeout, temperature=0.0,
                                            extra_headers={"X-Title": "medallion-pipeline"},
                                            extra_body={"usage": {"include": True}}, reasoning_headroom=True)
        case "ollama" | "vllm":
            return OpenAICompatibleProvider(name, cfg.model or "", api_key=key, base_url=cfg.base_url,
                                            timeout_s=timeout, temperature=0.0)
    raise ValueError(f"unknown provider {name}")


def decorate(adapter: LLMProvider, settings: Settings, recorder: UsageRecorder, budget: Budget,
             rpm: int) -> LLMProvider:
    provider: LLMProvider = MeteredProvider(adapter, recorder, budget)
    provider = RateLimitedProvider(provider, TokenBucket.per_minute(rpm))
    provider = RetryingProvider(provider, RetryPolicy(max_attempts=settings.llm_max_attempts,
                                                      base_delay_s=settings.llm_backoff_base_s,
                                                      max_delay_s=settings.llm_backoff_max_s))
    breaker = CircuitBreaker(adapter.id, settings.circuit_failure_threshold, settings.circuit_reset_s)
    return CircuitBreakerProvider(provider, breaker)


def build_router(settings: Settings, db: Database | None, *, providers: list[ProviderName] | None = None,
                 cache: LLMCache | None = None, recorder: UsageRecorder | None = None,
                 models: dict[str, str] | None = None) -> LLMRouter:
    if db is None and (cache is None or recorder is None):
        raise ValueError("without a database, pass both cache and recorder")
    recorder = recorder or PostgresUsageRecorder(db)  # type: ignore[arg-type]
    budget = Budget(settings.llm_max_tokens_per_run, settings.llm_max_cost_usd_per_run)
    names = settings.llm_providers if providers is None else providers
    chain = [decorate(build_adapter(n, settings, (models or {}).get(n)), settings, recorder, budget,
                      settings.provider(n).rpm)
             for n in names]
    guard = InputGuard({n: settings.provider(n).context_tokens for n in names})
    return LLMRouter(chain, cache or PostgresLLMCache(db), recorder, guard)  # type: ignore[arg-type]
