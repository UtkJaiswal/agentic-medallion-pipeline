"""Google Gemini via the official `google-genai` SDK (JSON mode + JSON-schema-constrained output)."""

from __future__ import annotations

import httpx
from google import genai
from google.genai import errors, types

from medallion.llm.providers.base import BaseProvider, error_for_status
from medallion.llm.types import InvalidOutputError, LLMRequest, ProviderError, ProviderTransientError
from medallion.observability.context import current_trace_id


class GeminiProvider(BaseProvider):
    def __init__(self, model: str, *, api_key: str | None, timeout_s: float) -> None:
        super().__init__("gemini", model)
        self._client = genai.Client(api_key=api_key,
                                    http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))

    def _invoke(self, request: LLMRequest) -> tuple[str, int, int]:
        config = types.GenerateContentConfig(
            system_instruction=request.system,
            temperature=0.0,
            # thinking models count reasoning tokens against this limit: leave headroom
            max_output_tokens=max(request.max_output_tokens * 4, 8192),
            http_options=types.HttpOptions(headers={"X-Trace-Id": current_trace_id()}),
        )
        if request.json_schema is not None:
            config.response_mime_type = "application/json"
            config.response_json_schema = request.json_schema
        resp = self._client.models.generate_content(model=self.model, contents=request.user, config=config)
        candidate = (resp.candidates or [None])[0]
        finish = getattr(candidate, "finish_reason", None)
        if finish is not None and str(getattr(finish, "name", finish)) == "MAX_TOKENS":
            raise InvalidOutputError(self.name, "output truncated (finish_reason=MAX_TOKENS)")
        usage = resp.usage_metadata
        return (resp.text or "", getattr(usage, "prompt_token_count", 0) or 0,
                getattr(usage, "candidates_token_count", 0) or 0)

    def _map_error(self, exc: Exception) -> ProviderError:
        if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
            return ProviderTransientError(self.name, f"{type(exc).__name__}: {exc}")
        if isinstance(exc, errors.APIError):
            return error_for_status(self.name, exc.code, str(exc)[:300])
        return error_for_status(self.name, -1, f"{type(exc).__name__}: {exc}")
