"""The single entry point agents use to talk to LLMs.

This is the LLM gateway. For each provider in the configured order (Chain of Responsibility):
  0. input guardrails: pre-flight (placeholders, context fit) and PII redaction for hosted providers;
  1. serve from cache if this exact prompt was already answered and validated by that model;
  2. call the (decorated) provider;
  3. validate the output against the Pydantic schema - on failure, one repair round-trip that shows
     the model its own output and the validation errors;
  4. cache only validated output, return.
Provider failures and unusable output fall through to the next provider. A spent budget stops the
chain entirely. If nothing works, NoProviderAvailableError tells the agent to use its deterministic
strategy instead - the pipeline itself never fails because an LLM did."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from medallion.llm.cache import LLMCache
from medallion.llm.decorators import LLMCallRecord, NullRecorder, UsageRecorder
from medallion.llm.guardrails import InputGuard, PromptRejected
from medallion.llm.structured import extract_json, transport_schema
from medallion.llm.types import (
    BudgetExceededError,
    InvalidOutputError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    NoProviderAvailableError,
    ProviderError,
)
from medallion.resilience.retry import TransientError

T = TypeVar("T", bound=BaseModel)
log = logging.getLogger(__name__)

_REPAIR_TEMPLATE = """{original}

---
Your previous reply could not be used:
<previous_reply>
{reply}
</previous_reply>
<errors>
{errors}
</errors>
Reply again with ONLY a JSON object that satisfies the schema. No prose, no code fences."""


@dataclass(frozen=True)
class StructuredResult(Generic[T]):
    value: T
    response: LLMResponse


class LLMRouter:
    def __init__(self, providers: list[LLMProvider], cache: LLMCache,
                 recorder: UsageRecorder | None = None, guard: InputGuard | None = None) -> None:
        self.providers = providers
        self._cache = cache
        self._recorder = recorder or NullRecorder()
        self._guard = guard or InputGuard()

    @property
    def enabled(self) -> bool:
        return bool(self.providers)

    def describe(self) -> str:
        return " -> ".join(p.id for p in self.providers) or "(none: deterministic mode)"

    def generate(self, request: LLMRequest, schema: type[T]) -> StructuredResult[T]:
        if request.json_schema is None:
            request = LLMRequest(**{**request.__dict__, "json_schema": transport_schema(schema),
                                    "schema_name": schema.__name__})
        failures: list[str] = []
        original = request
        for provider in self.providers:
            try:
                request, _ = self._guard.prepare(original, provider.name)
            except PromptRejected as exc:
                self._recorder.record(LLMCallRecord(original.task, provider.name, provider.model, "guard_rejected",
                                                    error=str(exc)[:500]))
                failures.append(f"{provider.id}: {exc}")
                continue
            key = request.fingerprint(provider.id)
            if (hit := self._cache.get(key)) is not None:
                try:
                    value = schema.model_validate(extract_json(hit.text))
                except (ValueError, ValidationError):
                    pass  # stale/incompatible cache entry: fall through to a live call
                else:
                    self._recorder.record(LLMCallRecord(request.task, provider.name, provider.model, "cache_hit"))
                    return StructuredResult(value, hit)
            try:
                value, resp = self._call_validated(provider, request, schema)
            except BudgetExceededError:
                raise
            except (ProviderError, TransientError) as exc:
                log.warning("llm.provider_failed", extra={"provider": provider.id, "task": request.task,
                                                          "error": str(exc)[:300]})
                failures.append(f"{provider.id}: {exc}")
                continue
            self._cache.put(key, request, resp)
            return StructuredResult(value, resp)
        raise NoProviderAvailableError("; ".join(failures) or "no LLM providers configured")

    def _call_validated(self, provider: LLMProvider, request: LLMRequest,
                        schema: type[T]) -> tuple[T, LLMResponse]:
        resp = provider.complete(request)
        try:
            return schema.model_validate(extract_json(resp.text)), resp
        except (ValueError, ValidationError) as first_error:
            self._recorder.record(LLMCallRecord(request.task, provider.name, provider.model, "invalid_output",
                                                error=str(first_error)[:500]))
            repair = LLMRequest(**{**request.__dict__, "user": _REPAIR_TEMPLATE.format(
                original=request.user, reply=resp.text[:4000], errors=str(first_error)[:2000])})
            resp = provider.complete(repair)
            try:
                return schema.model_validate(extract_json(resp.text)), resp
            except (ValueError, ValidationError) as second_error:
                self._recorder.record(LLMCallRecord(request.task, provider.name, provider.model,
                                                    "invalid_output", error=str(second_error)[:500]))
                raise InvalidOutputError(provider.name, f"schema validation failed twice: {second_error}") \
                    from second_error
