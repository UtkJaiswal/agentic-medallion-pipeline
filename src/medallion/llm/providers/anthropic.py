"""Anthropic Messages API via the official SDK.

Notes for current Claude models: sampling params (temperature) are not accepted, depth is controlled
with `output_config.effort`, JSON is enforced with `output_config.format`, and a policy decline comes
back as `stop_reason == "refusal"` (we treat it as permanent so the chain moves on). For models that
support it we opt into server-side refusal fallbacks (`fallbacks: "default"`)."""

from __future__ import annotations

import anthropic

from medallion.llm.providers.base import BaseProvider, error_for_status, parse_retry_after
from medallion.llm.types import (
    InvalidOutputError,
    LLMRequest,
    ProviderError,
    ProviderRefusalError,
    ProviderTransientError,
)
from medallion.observability.context import current_trace_id

_SERVER_FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1")


class AnthropicProvider(BaseProvider):
    def __init__(self, model: str, *, api_key: str | None, timeout_s: float, effort: str = "low") -> None:
        super().__init__("anthropic", model)
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=0)
        self._effort = effort

    def _invoke(self, request: LLMRequest) -> tuple[str, int, int]:
        output_config: dict = {}
        if "haiku" not in self.model:  # effort is rejected on Haiku 4.5
            output_config["effort"] = self._effort
        if request.json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": request.json_schema}
        headers = {"X-Trace-Id": current_trace_id()}
        extra_body = None
        if self.model in _SERVER_FALLBACK_MODELS:
            headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
            extra_body = {"fallbacks": "default"}
        resp = self._client.messages.create(
            model=self.model,
            # thinking is always on for current models: leave headroom beyond the answer itself
            max_tokens=max(request.max_output_tokens * 2, 8000),
            system=request.system,
            messages=[{"role": "user", "content": request.user}],
            output_config=output_config or anthropic.NOT_GIVEN,
            extra_headers=headers,
            extra_body=extra_body,
        )
        if resp.stop_reason == "refusal":
            raise ProviderRefusalError(self.name, "request declined (stop_reason=refusal)")
        if resp.stop_reason == "max_tokens":
            raise InvalidOutputError(self.name, "output truncated (stop_reason=max_tokens)")
        text = "".join(b.text for b in resp.content if b.type == "text")
        return text, resp.usage.input_tokens, resp.usage.output_tokens

    def _map_error(self, exc: Exception) -> ProviderError:
        if isinstance(exc, anthropic.APITimeoutError | anthropic.APIConnectionError):
            return ProviderTransientError(self.name, f"{type(exc).__name__}: {exc}")
        if isinstance(exc, anthropic.APIStatusError):
            return error_for_status(self.name, exc.status_code, str(exc)[:300],
                                    parse_retry_after(exc.response.headers.get("retry-after")))
        return error_for_status(self.name, -1, f"{type(exc).__name__}: {exc}")
