"""Input guardrails, applied by the LLM gateway (LLMRouter) before any provider call.

1. Pre-flight - is this prompt fit to send to *this* provider?
   * no unrendered template placeholders (a bug, never worth paying for);
   * a non-empty task;
   * estimated prompt + output budget fits the provider's context window. Self-hosted servers silently
     truncate (measured: Ollama's default 2k context cut a 12k-token prompt to 2,051 tokens), so a
     prompt that doesn't fit is routed to the next provider instead of producing a confident answer
     to half a prompt.
2. PII redaction - free text bound for a *hosted* provider has e-mail addresses, phone numbers and
   card-like numbers masked. Self-hosted models (ollama, vllm) keep data in-house and see raw text.
3. Prompt-injection screening - ticket text containing instruction-like phrases is flagged and
   logged. It is still sent (prompts already frame inputs strictly as data), so a malicious ticket
   cannot silently steer an agent and cannot block the pipeline either.

Output-side guardrails live elsewhere: schema validation + repair (router), enum-constrained answers,
truncation detection (adapters), SQL guard (agents/sql_guard.py), budgets (decorators), human review."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace

from medallion.llm.types import LLMRequest, ProviderPermanentError

log = logging.getLogger(__name__)

SELF_HOSTED = frozenset({"ollama", "vllm"})
_PLACEHOLDER = re.compile(r"(?<!\$)\$\{?[a-z_]{3,}\}?")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{8,}\d)(?!\w)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_INJECTION = re.compile(r"ignore (?:all |the )?(?:previous|above|prior) (?:instructions|rules)|system prompt|"
                        r"you are now|disregard (?:the|your) (?:instructions|rules)|act as (?:an?|the) ",
                        re.IGNORECASE)


class PromptRejected(ProviderPermanentError):
    """The request is not fit for this provider; the gateway moves on to the next one."""


@dataclass(frozen=True)
class GuardReport:
    redactions: int = 0
    injection_suspected: bool = False


def estimate_tokens(text: str) -> int:
    return len(text) // 3 + 1  # deliberately pessimistic (JSON-heavy prompts run ~3 chars/token)


def _luhn_ok(digits: str) -> bool:
    nums = [int(d) for d in digits][::-1]
    total = sum(n if i % 2 == 0 else (n * 2 - 9 if n * 2 > 9 else n * 2) for i, n in enumerate(nums))
    return total % 10 == 0


def redact(text: str) -> tuple[str, int]:
    count = 0

    def sub(pattern: re.Pattern[str], label: str, s: str, check=lambda m: True) -> str:  # type: ignore[no-untyped-def]
        nonlocal count

        def repl(m: re.Match[str]) -> str:
            nonlocal count
            if not check(m):
                return m.group(0)
            count += 1
            return label
        return pattern.sub(repl, s)

    text = sub(_EMAIL, "[EMAIL]", text)
    text = sub(_CARD, "[CARD]", text, lambda m: _luhn_ok(re.sub(r"\D", "", m.group(0))))
    text = sub(_PHONE, "[PHONE]", text, lambda m: sum(c.isdigit() for c in m.group(0)) >= 10)
    return text, count


class InputGuard:
    def __init__(self, context_tokens: dict[str, int | None] | None = None) -> None:
        self._context = context_tokens or {}

    def prepare(self, request: LLMRequest, provider_name: str) -> tuple[LLMRequest, GuardReport]:
        if _PLACEHOLDER.search(request.system):
            raise PromptRejected(provider_name, f"unrendered placeholder in system prompt: "
                                                f"{_PLACEHOLDER.search(request.system).group(0)}")  # type: ignore[union-attr]
        if not request.user.strip():
            raise PromptRejected(provider_name, "empty user content")
        limit = self._context.get(provider_name)
        needed = estimate_tokens(request.system + request.user) + request.max_output_tokens
        if limit is not None and needed > limit:
            raise PromptRejected(provider_name, f"needs ~{needed} tokens but the configured context is {limit}; "
                                                f"raise {provider_name.upper()}_CONTEXT_TOKENS (and the server's)")
        injection = bool(_INJECTION.search(request.user))
        if injection:
            log.warning("guard.injection_suspected", extra={"provider": provider_name, "task": request.task})
        if provider_name in SELF_HOSTED:
            return request, GuardReport(0, injection)
        user, n = redact(request.user)
        if n:
            log.info("guard.pii_redacted", extra={"provider": provider_name, "redactions": n})
        return (replace(request, user=user) if n else request), GuardReport(n, injection)
