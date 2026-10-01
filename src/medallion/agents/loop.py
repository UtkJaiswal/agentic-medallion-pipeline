"""Bounded propose -> verify -> repair loop ("loop engineering").

Agents whose output can be checked mechanically (SQL that must run, expressions that must parse) get
their verification errors fed back for a fixed number of repair rounds. The loop is bounded (cost and
latency are predictable), only failed items are re-sent (cheap), and the final verification result is
what the reviewer sees. Stats keep the first-pass result, so every run doubles as an A/B of the loop."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")
log = logging.getLogger(__name__)


@dataclass
class LoopStats:
    items: int = 0
    failed_first_pass: int = 0
    repaired: int = 0
    failed_final: int = 0
    rounds_used: int = 0
    history: list[dict] = field(default_factory=list)


@dataclass
class Verified(Generic[T]):
    item: T
    ok: bool
    error: str | None
    evidence: dict
    repair_round: int = 0


def repair_loop(items: list[T], verify: Callable[[T], Verified[T]],
                repair: Callable[[list[Verified[T]], int], list[T]], key: Callable[[T], str],
                max_rounds: int = 2) -> tuple[list[Verified[T]], LoopStats]:
    """`repair(failed, round_no)` receives each item's *latest* failed attempt and its error, so every
    round is a different request (identical requests would just be served from the LLM cache)."""
    results = {key(i): verify(i) for i in items}
    stats = LoopStats(items=len(items), failed_first_pass=sum(not r.ok for r in results.values()))
    for round_no in range(1, max_rounds + 1):
        failed = [r for r in results.values() if not r.ok]
        if not failed:
            break
        stats.rounds_used = round_no
        try:
            fixes = repair(failed, round_no)
        except Exception as exc:  # a failed repair call must never lose the first-pass results
            log.warning("loop.repair_failed", extra={"round": round_no, "error": str(exc)[:300]})
            break
        fixed_now = 0
        for fixed in fixes:
            k = key(fixed)
            if k in results and not results[k].ok:
                attempt = verify(fixed)
                attempt.repair_round = round_no
                if attempt.ok:
                    fixed_now += 1
                results[k] = attempt  # keep the latest attempt, failed or not, for the next round
        stats.history.append({"round": round_no, "sent": len(failed), "fixed": fixed_now})
    stats.failed_final = sum(not r.ok for r in results.values())
    stats.repaired = stats.failed_first_pass - stats.failed_final
    return list(results.values()), stats
