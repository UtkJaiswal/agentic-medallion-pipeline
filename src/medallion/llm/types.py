"""Provider-neutral request/response types and the error taxonomy every adapter maps into."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from medallion.resilience.retry import PermanentError, TransientError


@dataclass(frozen=True)
class LLMRequest:
    task: str                      # logical task name, used for metering and caching
    system: str
    user: str
    prompt_version: str
    json_schema: dict[str, Any] | None = None
    schema_name: str = "output"
    max_output_tokens: int = 2048

    def fingerprint(self, provider_id: str) -> str:
        body = json.dumps({"p": provider_id, "v": self.prompt_version, "s": self.system, "u": self.user,
                           "j": self.json_schema}, sort_keys=True)
        return hashlib.sha256(body.encode()).hexdigest()


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cached: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


class LLMProvider(ABC):
    """A chat-completion backend. Concrete adapters and decorators share this interface (LSP)."""

    name: str
    model: str

    @property
    def id(self) -> str:
        return f"{self.name}/{self.model}"

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse: ...


# ---------------------------------------------------------------------------------------- errors
class ProviderError(Exception):
    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider


class ProviderTransientError(ProviderError, TransientError):
    def __init__(self, provider: str, message: str, *, retry_after: float | None = None) -> None:
        ProviderError.__init__(self, provider, message)
        self.retry_after = retry_after


class ProviderPermanentError(ProviderError, PermanentError):
    pass


class ProviderRefusalError(ProviderPermanentError):
    pass


class InvalidOutputError(ProviderPermanentError):
    pass


class BudgetExceededError(PermanentError):
    """The run's LLM token/cost budget is spent. Stops the whole chain, not just one provider."""


class NoProviderAvailableError(Exception):
    """No provider configured, or every provider in the chain failed."""
