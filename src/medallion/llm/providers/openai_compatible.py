"""One adapter for every OpenAI-compatible endpoint: OpenAI itself, OpenRouter, Ollama and vLLM.
They differ only in base URL, auth and a couple of request knobs, so they share this class (DRY)
and are configured by the factory."""

from __future__ import annotations

import openai

from medallion.llm.providers.base import BaseProvider, error_for_status, parse_retry_after
from medallion.llm.types import InvalidOutputError, LLMRequest, ProviderError, ProviderTransientError
from medallion.observability.context import current_trace_id


def _check_not_truncated(provider: str, request: LLMRequest, prompt_tokens: int) -> None:
    """Self-hosted servers (Ollama's default context is 2k tokens) silently drop the *start* of a long
    prompt - usually the system instructions - and still answer. Detect it from the reported prompt
    size (~4 chars per token; 50% slack) rather than trusting an answer to a mangled prompt."""
    sent = (len(request.system) + len(request.user)) / 4
    if prompt_tokens and sent > 1000 and prompt_tokens < 0.5 * sent:
        raise InvalidOutputError(provider, f"server truncated the prompt ({prompt_tokens} tokens counted, ~{int(sent)} "
                                           f"sent); raise the model context window (e.g. OLLAMA_CONTEXT_LENGTH)")


class OpenAICompatibleProvider(BaseProvider):
    def __init__(self, name: str, model: str, *, api_key: str | None, base_url: str | None,
                 timeout_s: float, temperature: float | None = 0.0,
                 token_param: str = "max_tokens", extra_headers: dict[str, str] | None = None,
                 extra_body: dict | None = None, reasoning_headroom: bool = False) -> None:
        super().__init__(name, model)
        # max_retries=0: retries are owned by RetryingProvider so there is exactly one retry policy.
        self._client = openai.OpenAI(api_key=api_key or "not-needed", base_url=base_url,
                                     timeout=timeout_s, max_retries=0)
        self._temperature = temperature
        self._token_param = token_param
        self._extra_headers = extra_headers or {}
        self._extra_body = extra_body
        # Hosted reasoning models (and routers that may pick one) spend hidden reasoning tokens from the
        # same output budget; without headroom they stop at finish_reason=length mid-answer.
        self._headroom = reasoning_headroom

    def _invoke(self, request: LLMRequest) -> tuple[str, int, int]:
        kwargs: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": request.system},
                         {"role": "user", "content": request.user}],
            self._token_param: max(request.max_output_tokens * 4, 8000) if self._headroom
            else request.max_output_tokens,
            "extra_headers": {"X-Trace-Id": current_trace_id(), **self._extra_headers},
        }
        if self._extra_body:
            kwargs["extra_body"] = self._extra_body
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        if request.json_schema is not None:
            kwargs["response_format"] = {"type": "json_schema", "json_schema": {
                "name": request.schema_name, "schema": request.json_schema, "strict": True}}
        resp = self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise InvalidOutputError(self.name, "output truncated (finish_reason=length)")
        usage = resp.usage
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        _check_not_truncated(self.name, request, prompt_tokens)
        meta = {"served_model": getattr(resp, "model", None) or self.model}
        if (billed := getattr(usage, "cost", None)) is not None:  # OpenRouter reports the billed amount
            meta["billed_usd"] = float(billed)
        return choice.message.content or "", prompt_tokens, getattr(usage, "completion_tokens", 0) or 0, meta

    def _map_error(self, exc: Exception) -> ProviderError:
        if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
            return ProviderTransientError(self.name, f"{type(exc).__name__}: {exc}")
        if isinstance(exc, openai.APIStatusError):
            return error_for_status(self.name, exc.status_code, str(exc)[:300],
                                    parse_retry_after(exc.response.headers.get("retry-after")))
        return error_for_status(self.name, -1, f"{type(exc).__name__}: {exc}")
