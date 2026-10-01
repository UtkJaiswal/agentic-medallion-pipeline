"""Token pricing in USD per 1M tokens, as providers publish it.
Only prices we are confident about are listed; anything else is reported as 'unpriced' (cost NULL)
rather than guessed. Override/extend via LLM_PRICING_JSON, e.g.
LLM_PRICING_JSON='{"gpt-4.1-mini": [0.4, 1.6]}'. The token budget guard works regardless."""

from __future__ import annotations

import json
import os

_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
_FREE_PROVIDERS = {"ollama", "vllm"}  # self-hosted: marginal token cost ~0 (hardware is the cost)


def _prices() -> dict[str, tuple[float, float]]:
    extra = os.getenv("LLM_PRICING_JSON")
    if not extra:
        return _PRICES
    return {**_PRICES, **{k: (float(v[0]), float(v[1])) for k, v in json.loads(extra).items()}}


def cost_usd(provider: str, model: str, input_tokens: int, output_tokens: int) -> float | None:
    if provider in _FREE_PROVIDERS:
        return 0.0
    price = _prices().get(model) or _prices().get(model.split("/")[-1])
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000
