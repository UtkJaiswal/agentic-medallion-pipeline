"""LLM-as-a-judge and human-weighted trust.

A proposal's trust combines three signals, weighted by how much each deserves to be believed:

  human  (0.6)  agreement of past human decisions on similar items with this proposal
  judge  (0.3)  an independent model's probability that the proposal is correct
  agent  (0.1)  the generating model's own confidence

Rationale for the order: humans own the business semantics, so their decisions are ground truth; a
judge is a second opinion from a *different* model (errors less correlated with the generator's); a
model's self-reported confidence is the least calibrated signal (gemma3 reported 0.95 on answers a
reviewer later corrected). Missing signals are dropped and the remaining weights renormalised. A human
decision on the item itself is never overridden - trust only gates *automatic* approval and orders the
review queue (lowest trust first)."""

from __future__ import annotations

import contextvars
import json
import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field

from medallion.agents.memory import _tokens
from medallion.agents.prompting import load_prompt
from medallion.llm.router import LLMRouter
from medallion.llm.types import LLMRequest, NoProviderAvailableError
from medallion.reference import Taxonomy

log = logging.getLogger(__name__)


def _clamp(value: object) -> object:
    return min(1.0, max(0.0, value)) if isinstance(value, int | float) else value


@dataclass(frozen=True)
class TrustWeights:
    human: float = 0.6
    judge: float = 0.3
    agent: float = 0.1


DEFAULT_WEIGHTS = TrustWeights()


def trust_score(agent_conf: float | None, judge_p: float | None, human_signal: float | None,
                w: TrustWeights = DEFAULT_WEIGHTS) -> float:
    parts = [(w.human, human_signal), (w.judge, judge_p), (w.agent, agent_conf)]
    present = [(wt, s) for wt, s in parts if s is not None]
    return sum(wt * s for wt, s in present) / sum(wt for wt, _ in present) if present else 0.0


def _norm(answer: dict) -> dict[str, str]:
    return {k: str(v).lower() for k, v in answer.items()}


@dataclass(frozen=True)
class JudgeItem:
    id: str
    kind: str          # category_label | description_template
    input: str
    proposal: dict     # the fields being judged (category, label_kind, severity, ...)


class HumanSignal:
    """Agreement of human-approved decisions on *similar* inputs with a proposed answer. Compares the
    whole decision (e.g. category AND label_kind), not just the category: most real agent errors were
    in the secondary field, and a category-only signal would vouch for them."""

    def __init__(self, decisions: list[tuple[str, str, dict]], min_similarity: float = 0.5) -> None:
        # (kind, input, human-approved answer fields)
        self._index = [(kind, text, _tokens(text), _norm(ans)) for kind, text, ans in decisions]
        self._min = min_similarity

    def __call__(self, kind: str, text: str, answer: dict, exclude_self: bool = True) -> float | None:
        q, proposed = _tokens(text), _norm(answer)
        votes = [all(ans.get(f) == v for f, v in proposed.items()) for k, t, toks, ans in self._index
                 if k == kind and not (exclude_self and t == text) and q and toks
                 and len(q & toks) / len(q | toks) >= self._min]
        return sum(votes) / len(votes) if votes else None


class LLMJudge:
    def __init__(self, router: LLMRouter, taxonomy: Taxonomy, batch_size: int = 10, concurrency: int = 4) -> None:
        self._router, self._taxonomy = router, taxonomy
        self._batch, self._concurrency = batch_size, concurrency
        cat = Literal[taxonomy.names]  # type: ignore[valid-type]

        class Verdict(BaseModel):
            id: str
            agrees: bool
            p_correct: Annotated[float, BeforeValidator(_clamp), Field(ge=0, le=1)]
            better_category: cat  # type: ignore[valid-type]
            reason: str

        class Verdicts(BaseModel):
            verdicts: list[Verdict]

        self._schema = Verdicts

    def judge(self, items: Sequence[JudgeItem]) -> dict[str, dict]:
        prompt = load_prompt("judge")
        related = "\n".join(f"- {' / '.join(sorted(p))}" for p in self._taxonomy.related) or "(none)"
        system = prompt.render(taxonomy=self._taxonomy.prompt_block(), related=related)

        def run(batch: list[JudgeItem]) -> dict[str, dict]:
            user = json.dumps([{"id": i.id, "kind": i.kind, "input": i.input, "proposed": i.proposal} for i in batch],
                              ensure_ascii=False)
            try:
                result = self._router.generate(LLMRequest("judge", system, user, prompt.version,
                                                          max_output_tokens=160 * len(batch) + 400), self._schema)
            except NoProviderAvailableError as exc:  # one bad batch must not void the others
                log.warning("judge.batch_failed", extra={"size": len(batch), "error": str(exc)[:200]})
                return {}
            ids = {i.id for i in batch}
            return {v.id: v.model_dump() for v in result.value.verdicts if v.id in ids}

        def fan_out(todo: list[JudgeItem], size: int) -> dict[str, dict]:
            batches = [todo[i:i + size] for i in range(0, len(todo), size)]
            with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
                futures = [pool.submit(contextvars.copy_context().run, run, b) for b in batches]
                result: dict[str, dict] = {}
                for f in futures:
                    result |= f.result()
            return result

        out = fan_out(list(items), self._batch)
        missing = [i for i in items if i.id not in out]
        if missing:  # models occasionally skip items in a batch: retry those once, in small batches
            out |= fan_out(missing, max(1, self._batch // 3))
        return out
