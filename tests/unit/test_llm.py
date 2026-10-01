from typing import Literal

import pytest
from pydantic import BaseModel

from medallion.llm.cache import InMemoryLLMCache
from medallion.llm.decorators import (
    Budget,
    CircuitBreakerProvider,
    MeteredProvider,
    RateLimitedProvider,
    RetryingProvider,
)
from medallion.llm.router import LLMRouter
from medallion.llm.structured import extract_json, transport_schema
from medallion.llm.types import (
    BudgetExceededError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    NoProviderAvailableError,
    ProviderPermanentError,
    ProviderTransientError,
)
from medallion.resilience.circuit_breaker import CircuitBreaker
from medallion.resilience.rate_limit import TokenBucket
from medallion.resilience.retry import RetryPolicy


class Out(BaseModel):
    category: Literal["hvac", "plumbing"]
    confidence: float


class Scripted(LLMProvider):
    """Replays a script of responses / exceptions."""

    def __init__(self, name, script):
        self.name, self.model, self.script, self.calls = name, "m", list(script), []

    def complete(self, request):
        self.calls.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResponse(text=item, provider=self.name, model=self.model, input_tokens=10, output_tokens=5)


class Recorder:
    def __init__(self):
        self.calls = []

    def record(self, call):
        self.calls.append(call)


REQ = LLMRequest(task="t", system="s", user="u", prompt_version="v1")
GOOD = '{"category": "hvac", "confidence": 0.9}'


def test_router_falls_back_to_next_provider():
    a = Scripted("a", [ProviderTransientError("a", "503")])
    b = Scripted("b", [GOOD])
    result = LLMRouter([a, b], InMemoryLLMCache()).generate(REQ, Out)
    assert result.value.category == "hvac" and result.response.provider == "b"


def test_router_repairs_invalid_output_once():
    a = Scripted("a", ['{"category": "elevator"}', GOOD])
    rec = Recorder()
    result = LLMRouter([a], InMemoryLLMCache(), rec).generate(REQ, Out)
    assert result.value.category == "hvac"
    assert "previous_reply" in a.calls[1].user
    assert [c.status for c in rec.calls] == ["invalid_output"]


def test_router_moves_on_after_two_invalid_outputs():
    a = Scripted("a", ["nonsense", "still nonsense"])
    b = Scripted("b", [GOOD])
    assert LLMRouter([a, b], InMemoryLLMCache()).generate(REQ, Out).response.provider == "b"


def test_router_caches_only_validated_output():
    cache = InMemoryLLMCache()
    a = Scripted("a", [GOOD])
    router = LLMRouter([a], cache)
    router.generate(REQ, Out)
    second = router.generate(REQ, Out)
    assert second.response.cached and len(a.calls) == 1


def test_router_raises_when_all_fail_or_none_configured():
    with pytest.raises(NoProviderAvailableError):
        LLMRouter([Scripted("a", [ProviderPermanentError("a", "401")])], InMemoryLLMCache()).generate(REQ, Out)
    with pytest.raises(NoProviderAvailableError):
        LLMRouter([], InMemoryLLMCache()).generate(REQ, Out)


def test_budget_exhaustion_stops_the_whole_chain():
    budget = Budget(max_tokens=10, max_cost_usd=100)
    a = MeteredProvider(Scripted("a", [GOOD]), Recorder(), budget)
    b = Scripted("b", [GOOD])
    router = LLMRouter([a], InMemoryLLMCache())
    router.generate(REQ, Out)  # spends 15 tokens
    with pytest.raises(BudgetExceededError):
        LLMRouter([a, b], InMemoryLLMCache()).generate(LLMRequest("t", "s", "other", "v1"), Out)
    assert b.calls == []


def test_decorator_stack_retries_transient_then_succeeds():
    inner = Scripted("a", [ProviderTransientError("a", "429", retry_after=0), GOOD])
    rec = Recorder()
    stack = CircuitBreakerProvider(
        RetryingProvider(RateLimitedProvider(MeteredProvider(inner, rec, Budget(1000, 1)), TokenBucket(100)),
                         RetryPolicy(max_attempts=3, base_delay_s=0, max_delay_s=0)),
        CircuitBreaker("a"))
    assert stack.complete(REQ).text == GOOD
    assert [c.status for c in rec.calls] == ["error", "ok"]  # every attempt is metered
    assert stack.id == "a/m"


def test_transport_schema_is_strict_and_inlines_refs():
    class Item(BaseModel):
        title: str            # a property literally called "title" must survive keyword stripping
        n: int = 3

    class Wrapper(BaseModel):
        items: list[Item]

    schema = transport_schema(Wrapper)
    item = schema["properties"]["items"]["items"]
    assert "$ref" not in str(schema) and item["additionalProperties"] is False
    assert item["required"] == ["title", "n"] and "title" in item["properties"]
    assert "default" not in item["properties"]["n"]


@pytest.mark.parametrize("text", [GOOD, f"```json\n{GOOD}\n```", f"Sure! Here you go: {GOOD} Hope that helps"])
def test_extract_json_tolerates_wrappers(text):
    assert extract_json(text) == {"category": "hvac", "confidence": 0.9}
