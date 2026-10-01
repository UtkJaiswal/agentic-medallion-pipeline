"""Template method for concrete adapters: time the call, translate SDK exceptions into our error
taxonomy, and return a provider-neutral response. Subclasses only implement `_invoke` + `_map_error`."""

from __future__ import annotations

import time
from abc import abstractmethod

from medallion.llm.types import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderError,
    ProviderPermanentError,
    ProviderTransientError,
)

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


def parse_retry_after(value: str | None) -> float | None:
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None  # HTTP-date form: let our own backoff decide


def error_for_status(provider: str, status: int | None, message: str,
                     retry_after: float | None = None) -> ProviderError:
    if status is None or status in RETRYABLE_STATUS:
        return ProviderTransientError(provider, f"{status}: {message}", retry_after=retry_after)
    return ProviderPermanentError(provider, f"{status}: {message}")


class BaseProvider(LLMProvider):
    def __init__(self, name: str, model: str) -> None:
        if not model:
            raise ValueError(f"provider '{name}' needs a model; set {name.upper()}_MODEL in .env")
        self.name, self.model = name, model

    def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        try:
            text, in_tok, out_tok, *extra = self._invoke(request)
        except ProviderError:
            raise
        except Exception as exc:
            raise self._map_error(exc) from exc
        return LLMResponse(text=text, provider=self.name, model=self.model, input_tokens=in_tok,
                           output_tokens=out_tok, latency_ms=int((time.perf_counter() - started) * 1000),
                           meta=extra[0] if extra else {})

    @abstractmethod
    def _invoke(self, request: LLMRequest) -> tuple:
        """Return (text, input_tokens, output_tokens) or (text, input_tokens, output_tokens, meta), where meta
        may carry `billed_usd` (what the provider billed, in USD) and `served_model`
        (the model that actually answered)."""

    @abstractmethod
    def _map_error(self, exc: Exception) -> ProviderError: ...
