"""Native Jev (TypeSafe System-One model) classification strategy.

Jev does not generate text: given a `state` and typed questions it returns, per question, a choice
from the options *we* supply with a calibrated probability distribution. That fits this task exactly:
the answer cannot fall outside the taxonomy, and the confidence is a real probability rather than a
number the model wrote. One item per request. Two routes to the same model, same request shape:

- OpenRouter: POST https://openrouter.ai/api/alpha/decisions (`typesafe/jev-1.13`) with OPENROUTER_API_KEY;
- TypeSafe direct: POST https://api.typesafe.ai/v1/systemone with TYPESAFE_API_KEY (preferred when set).

Not to be confused with "Jev Router" (`openrouter:typesafe/jev-router`), a chat model that uses Jev to
*pick a model* for each request."""

from __future__ import annotations

import contextvars
import logging
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

from medallion.agents.classification import (
    Classifier,
    LabelDecision,
    TemplateDecision,
    TemplateItem,
)
from medallion.llm.decorators import LLMCallRecord, NullRecorder, UsageRecorder
from medallion.llm.guardrails import redact
from medallion.observability.context import current_trace_id
from medallion.reference import Taxonomy
from medallion.resilience.circuit_breaker import CircuitBreaker
from medallion.resilience.rate_limit import TokenBucket
from medallion.resilience.retry import PermanentError, RetryPolicy, TransientError, retry_call

log = logging.getLogger(__name__)

LABEL_KINDS = {
    "category_label": "A short label naming a kind of facilities work, an abbreviation or synonym of one.",
    "generic_label": "A label that names no specific kind of work.",
    "description_text": "A description of one specific incident (object, place or symptom), possibly cut off.",
    "junk": "A placeholder or test value.",
}
SEVERITIES = {
    "low": "Cosmetic issue or routine request.",
    "medium": "Degraded comfort or function for some people.",
    "high": "A safety hazard or a disruption to many people.",
    "critical": "Immediate risk to people or business-critical systems.",
}


# Same definition the LLM prompt gives through its severity examples (prompts/classify_templates.md)
HAZARD = {
    "true": "Could injure someone: e.g. a person trapped, sparking electrics, active flooding, faulty fire "
            "equipment, a slip/trip hazard.",
    "false": "Degraded comfort or function (temperature, a single broken fixture), cosmetic issues or routine "
             "requests.",
}


class JevClassifier:
    def __init__(self, taxonomy: Taxonomy, *, api_key: str, model: str = "jev-latest",
                 base_url: str = "https://api.typesafe.ai/v1", timeout_s: float = 30, rpm: int = 300,
                 concurrency: int = 8, fallback: Classifier | None = None, recorder: UsageRecorder | None = None,
                 transport: httpx.BaseTransport | None = None, path: str = "/systemone",
                 provider: str = "typesafe") -> None:
        self._taxonomy, self._model, self._fallback = taxonomy, model, fallback
        self._path, self._provider = path, provider
        self._client = httpx.Client(base_url=base_url, timeout=timeout_s, transport=transport,
                                    headers={"Authorization": f"Bearer {api_key}"})
        self._bucket, self._breaker = TokenBucket.per_minute(rpm), CircuitBreaker("jev")
        self._retry = RetryPolicy(max_attempts=4, base_delay_s=0.5, max_delay_s=20)
        self._concurrency, self._recorder = concurrency, recorder or NullRecorder()
        self._categories = dict(taxonomy.categories)

    # ------------------------------------------------------------------ transport
    def _ask(self, task: str, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
        # hosted model: same PII redaction the LLM gateway applies before any hosted provider
        state = {k: redact(v)[0] if isinstance(v, str) else v for k, v in state.items()}

        def call() -> dict[str, Any]:
            self._bucket.acquire()
            try:
                r = self._client.post(self._path, json={"state": state, "model": self._model,
                                                        "questions": questions},
                                      headers={"X-Trace-Id": current_trace_id()})
            except httpx.TransportError as exc:
                raise TransientError(f"jev transport: {exc}") from exc
            if r.status_code in (429, 500, 502, 503, 504, 529):
                raise TransientError(f"jev {r.status_code}", retry_after=_retry_after(r))
            if r.status_code >= 400:
                raise PermanentError(f"jev {r.status_code}: {r.text[:200]}")
            return r.json()

        started = time.perf_counter()
        body = self._breaker.call(lambda: retry_call(call, self._retry, label="jev"))
        usage = body.get("usage") or {}
        self._recorder.record(LLMCallRecord(task, self._provider, self._model, "ok", usage.get("input_tokens", 0),
                                            usage.get("output_tokens", 0), usage.get("cost"),
                                            int((time.perf_counter() - started) * 1000),
                                            served_model=body.get("model")))
        return body["answers"]

    def _fan_out(self, items: list, one, fallback) -> list:  # type: ignore[no-untyped-def]
        def guarded(item):  # type: ignore[no-untyped-def]
            try:
                return [one(item)]
            except (TransientError, PermanentError, KeyError) as exc:
                log.warning("jev.item_failed", extra={"error": str(exc)[:200]})
                return fallback([item]) if fallback else []

        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            futures = [pool.submit(contextvars.copy_context().run, guarded, i) for i in items]
            return [d for f in futures for d in f.result()]

    # ------------------------------------------------------------------ Classifier
    def classify_labels(self, labels: Sequence[str]) -> list[LabelDecision]:
        def one(label: str) -> LabelDecision:
            a = self._ask("classify_labels", {"category_field_value": label}, {
                "category": {"type": "choice", "criteria": self._categories, "instructions":
                             "Which facilities category does this hand-typed category value refer to? "
                             "Use 'unknown' for placeholders and values naming no kind of work."},
                "label_kind": {"type": "choice", "criteria": LABEL_KINDS,
                               "instructions": "What kind of value is this?"}})
            conf = min(_confidence(a["category"]), _confidence(a["label_kind"]))
            return LabelDecision(label, a["label_kind"]["choice"], a["category"]["choice"], round(conf, 3),
                                 f"jev:{self._model}")

        return self._fan_out(list(labels), one, self._fallback.classify_labels if self._fallback else None)

    def classify_templates(self, items: Sequence[TemplateItem]) -> list[TemplateDecision]:
        def one(item: TemplateItem) -> TemplateDecision:
            a = self._ask("classify_templates", {"ticket_description": item.example, "template": item.template}, {
                "category": {"type": "choice", "criteria": self._categories,
                             "instructions": "Which facilities category does this ticket belong to?"},
                "severity": {"type": "choice", "criteria": SEVERITIES,
                             "instructions": "How severe is the problem described, judged from the text alone?"},
                "is_safety_hazard": {"type": "noul", "criteria": HAZARD, "instructions":
                                     "Could the problem as described plausibly injure someone?"}})
            conf = min(_confidence(a["category"]), _confidence(a["severity"]))
            return TemplateDecision(item.template, a["category"]["choice"], "unspecified", a["severity"]["choice"],
                                    a["is_safety_hazard"]["noul"] >= 0.5, round(conf, 3), f"jev:{self._model}")

        return self._fan_out(list(items), one, self._fallback.classify_templates if self._fallback else None)


def _confidence(answer: dict[str, Any]) -> float:
    """`confidence` is optional in the decisions API; the chosen option's probability is the same signal."""
    if answer.get("confidence") is not None:
        return float(answer["confidence"])
    return float((answer.get("probabilities") or {}).get(answer["choice"], 0.0))


def _retry_after(r: httpx.Response) -> float | None:
    try:
        return float(r.headers["retry-after"])
    except (KeyError, ValueError):
        return None


def build_jev(settings: Any, taxonomy: Taxonomy, model: str | None, fallback: Classifier | None,
              recorder: UsageRecorder | None = None) -> tuple[JevClassifier, str]:
    """TypeSafe direct when TYPESAFE_API_KEY is set, otherwise OpenRouter's decisions endpoint."""
    from medallion.settings import OpenRouterSettings, TypeSafeSettings
    direct, openrouter = TypeSafeSettings(), OpenRouterSettings()
    common = {"timeout_s": settings.llm_timeout_s, "rpm": direct.rpm, "concurrency": settings.llm_concurrency,
              "fallback": fallback, "recorder": recorder}
    if direct.api_key is not None:
        model = model or direct.model or "jev-latest"
        clf = JevClassifier(taxonomy, api_key=direct.api_key.get_secret_value(), model=model,
                            base_url=direct.base_url or "https://api.typesafe.ai/v1", **common)
        return clf, f"typesafe/{model}"
    if openrouter.api_key is not None:
        model = model or "typesafe/jev-1.13"
        clf = JevClassifier(taxonomy, api_key=openrouter.api_key.get_secret_value(), model=model,
                            base_url="https://openrouter.ai/api/alpha", path="/decisions", provider="openrouter",
                            **common)
        return clf, f"openrouter-decisions:{model}"
    raise ValueError("Jev needs OPENROUTER_API_KEY (or TYPESAFE_API_KEY) in .env")
