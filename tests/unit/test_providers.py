"""Adapters must translate each SDK's errors into our transient/permanent taxonomy, since that decides
retry vs. fallback. No network: SDK exceptions are constructed directly / clients are stubbed."""

from types import SimpleNamespace

import anthropic
import httpx2
import openai
import pytest
from google.genai import errors as genai_errors

from medallion.llm.providers.anthropic import AnthropicProvider
from medallion.llm.providers.gemini import GeminiProvider
from medallion.llm.providers.openai_compatible import OpenAICompatibleProvider
from medallion.llm.types import (
    InvalidOutputError,
    LLMRequest,
    ProviderPermanentError,
    ProviderRefusalError,
    ProviderTransientError,
)

REQ = LLMRequest(task="t", system="s", user="u", prompt_version="v1", json_schema={"type": "object"})
_request = httpx2.Request("POST", "https://example.test/v1")


def _status(cls, status, headers=None):
    return cls(f"status {status}", response=httpx2.Response(status, request=_request, headers=headers or {}),
               body=None)


@pytest.fixture
def oa():
    return OpenAICompatibleProvider("openai", "m", api_key="k", base_url=None, timeout_s=1)


@pytest.mark.parametrize(("status", "kind"), [(429, ProviderTransientError), (500, ProviderTransientError),
                                              (503, ProviderTransientError), (400, ProviderPermanentError),
                                              (401, ProviderPermanentError), (404, ProviderPermanentError)])
def test_openai_status_mapping(oa, status, kind):
    assert isinstance(oa._map_error(_status(openai.APIStatusError, status)), kind)


def test_openai_retry_after_and_timeouts(oa):
    err = oa._map_error(_status(openai.RateLimitError, 429, {"retry-after": "3"}))
    assert isinstance(err, ProviderTransientError) and err.retry_after == 3.0
    assert isinstance(oa._map_error(openai.APITimeoutError(request=_request)), ProviderTransientError)


def test_openai_request_shape_and_truncation(oa, monkeypatch):
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        msg = SimpleNamespace(content='{"a": 1}')
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                               usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3))

    monkeypatch.setattr(oa._client.chat.completions, "create", create)
    resp = oa.complete(REQ)
    assert (resp.text, resp.input_tokens, resp.output_tokens) == ('{"a": 1}', 7, 3)
    assert seen["response_format"]["json_schema"]["strict"] is True and "X-Trace-Id" in seen["extra_headers"]

    def truncated(**kwargs):
        msg = SimpleNamespace(content="{")
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="length")], usage=None)

    monkeypatch.setattr(oa._client.chat.completions, "create", truncated)
    with pytest.raises(InvalidOutputError):
        oa.complete(REQ)


@pytest.fixture
def an():
    return AnthropicProvider("claude-opus-5-5", api_key="k", timeout_s=1)


@pytest.mark.parametrize(("status", "kind"), [(429, ProviderTransientError), (529, ProviderTransientError),
                                              (500, ProviderTransientError), (400, ProviderPermanentError)])
def test_anthropic_status_mapping(an, status, kind):
    assert isinstance(an._map_error(_status(anthropic.APIStatusError, status)), kind)


def _anthropic_message(stop_reason, text='{"a": 1}'):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=11, output_tokens=4))


def test_anthropic_request_uses_effort_format_and_fallbacks(an, monkeypatch):
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return _anthropic_message("end_turn")

    monkeypatch.setattr(an._client.messages, "create", create)
    assert an.complete(REQ).text == '{"a": 1}'
    assert seen["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert "temperature" not in seen  # rejected by current Claude models
    assert seen["extra_body"] == {"fallbacks": "default"}


def test_anthropic_refusal_is_permanent(an, monkeypatch):
    monkeypatch.setattr(an._client.messages, "create", lambda **_: _anthropic_message("refusal"))
    with pytest.raises(ProviderRefusalError):
        an.complete(REQ)


@pytest.mark.parametrize(("code", "kind"), [(429, ProviderTransientError), (503, ProviderTransientError),
                                            (400, ProviderPermanentError), (403, ProviderPermanentError)])
def test_gemini_status_mapping(code, kind):
    g = GeminiProvider("gemini-x", api_key="k", timeout_s=1)
    assert isinstance(g._map_error(genai_errors.APIError(code, {"error": {"message": "x"}})), kind)


def test_hosted_provider_requires_a_model():
    with pytest.raises(ValueError, match="OPENAI_MODEL"):
        OpenAICompatibleProvider("openai", "", api_key="k", base_url=None, timeout_s=1)


def test_silent_server_side_prompt_truncation_is_detected(monkeypatch):
    local = OpenAICompatibleProvider("ollama", "m", api_key=None, base_url="http://x/v1", timeout_s=1)
    long_request = LLMRequest(task="t", system="x" * 20_000, user="y", prompt_version="v1")

    def truncated_context(**_):
        msg = SimpleNamespace(content="{}")
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                               usage=SimpleNamespace(prompt_tokens=2048, completion_tokens=2))

    monkeypatch.setattr(local._client.chat.completions, "create", truncated_context)
    with pytest.raises(InvalidOutputError, match="truncated the prompt"):
        local.complete(long_request)
