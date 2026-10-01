"""Offline A/B experiments on recorded agent decisions (no pipeline side effects).

judge_policy_ab: replays the classification agent's original proposals against the final
human-reviewed answers. The decisions come from evals/datasets/recorded_decisions.jsonl (exported
from ops.agent_proposals + ref.* after the first review session) so the experiment is reproducible.
It compares auto-approval policies at the same threshold:
  A  agent self-confidence only           (the policy the pipeline started with)
  B  independent judge only
  C  human-weighted trust: human 0.6 / judge 0.3 / agent 0.1  (human signal = agreement of human
     decisions on *similar* items, leave-one-out so an item never sees its own review)
Metrics: coverage (share auto-approved = review work saved) and precision (share of auto-approved
items that the human review agreed with = errors that would have slipped through)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from medallion.agents.judge import HumanSignal, JudgeItem, LLMJudge, TrustWeights, trust_score
from medallion.db import Database


@dataclass(frozen=True)
class PolicyResult:
    policy: str
    auto_approved: int
    coverage: float
    precision: float | None
    wrong_auto_approved: int
    sent_to_review: int


def _recorded_decisions(db: Database) -> list[dict[str, Any]]:
    """Original agent answer + confidence + final human-reviewed answer per subject."""
    with db.transaction() as conn:
        rows = conn.execute("""
            SELECT DISTINCT ON (p.kind, p.subject) p.kind, p.subject, p.proposal, p.confidence
            FROM ops.agent_proposals p
            WHERE p.agent = 'classification' AND p.provider IS NOT NULL
              AND coalesce(p.proposal->>'source', '') <> 'human'
            ORDER BY p.kind, p.subject, p.created_at""").fetchall()
        labels = {r["label"]: r for r in conn.execute("SELECT * FROM ref.category_label_map")}
        templates = {r["template"]: r for r in conn.execute("SELECT * FROM ref.description_template_map")}
    out = []
    for r in rows:
        original = {**r["proposal"], **(r["proposal"].get("agent_original") or {})}
        final = (labels if r["kind"] == "category_label" else templates).get(r["subject"])
        if final is None:
            continue
        fields = ("category", "label_kind") if r["kind"] == "category_label" else ("category", "is_safety_hazard")
        correct = all(str(original.get(f)).lower() == str(final[f]).lower() for f in fields)
        out.append({"kind": r["kind"], "input": r["subject"], "proposal": {f: original.get(f) for f in fields},
                    "agent_conf": float(r["confidence"]), "final": {f: final[f] for f in fields}, "correct": correct})
    return out


DECISIONS_FILE = Path(__file__).resolve().parents[3] / "evals" / "datasets" / "recorded_decisions.jsonl"


def export_recorded_decisions(db: Database, path: Path = DECISIONS_FILE) -> int:
    decisions = _recorded_decisions(db)
    path.write_text("".join(json.dumps(d, ensure_ascii=False, default=str) + "\n" for d in decisions))
    return len(decisions)


def load_recorded_decisions(path: Path = DECISIONS_FILE) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def judge_policy_ab(decisions: list[dict[str, Any]], judge: LLMJudge, threshold: float = 0.85,
                    weights: TrustWeights = TrustWeights()) -> dict[str, Any]:  # noqa: B008
    items = [JudgeItem(f"J{i}", d["kind"], d["input"], d["proposal"]) for i, d in enumerate(decisions)]
    verdicts = judge.judge(items)
    human = HumanSignal([(d["kind"], d["input"], d["final"]) for d in decisions])

    scored = []
    for i, d in enumerate(decisions):
        v = verdicts.get(f"J{i}")
        judge_p = None if v is None else (v["p_correct"] if v["agrees"] else min(v["p_correct"], 0.5))
        h = human(d["kind"], d["input"], d["proposal"])
        scored.append({**d, "judge_p": judge_p, "human_signal": h,
                       "trust": trust_score(d["agent_conf"], judge_p, h, weights),
                       "trust_no_human": trust_score(d["agent_conf"], judge_p, None, weights)})

    def evaluate(name: str, score_key: str) -> PolicyResult:
        approved = [s for s in scored if (s[score_key] or 0) >= threshold]
        wrong = sum(1 for s in approved if not s["correct"])
        n = len(scored)
        return PolicyResult(name, len(approved), round(len(approved) / n, 4),
                            round(1 - wrong / len(approved), 4) if approved else None, wrong, n - len(approved))

    ranking = review_queue_ranking(scored)
    results = [evaluate("A: agent confidence", "agent_conf"), evaluate("B: judge only", "judge_p"),
               evaluate("C: trust (human 0.6 / judge 0.3 / agent 0.1)", "trust"),
               evaluate("C': trust without human signal", "trust_no_human")]
    return {"items": len(scored), "actually_correct": sum(s["correct"] for s in scored), "threshold": threshold,
            "judge_verdicts": len(verdicts), "with_human_signal": sum(s["human_signal"] is not None for s in scored),
            "policies": [asdict(r) for r in results], "review_queue_ranking": ranking}


def review_queue_ranking(scored: list[dict[str, Any]], ks: tuple[int, ...] = (10, 25, 50)) -> dict[str, Any]:
    """The review queue is worked least-trusted first. precision@k = share of the first k items that are
    actually wrong; recall@k = share of all wrong items found in the first k. Random order's expected
    precision@k is the base error rate."""
    wrong_total = sum(not s["correct"] for s in scored)
    out: dict[str, Any] = {"items": len(scored), "wrong": wrong_total,
                           "random_baseline_precision": round(wrong_total / len(scored), 3)}
    for name, key in (("agent confidence", "agent_conf"), ("judge", "judge_p"), ("trust (human-weighted)", "trust")):
        order = sorted(scored, key=lambda s: (s[key] if s[key] is not None else 0.0))
        out[name] = {f"@{k}": {"precision": round(sum(not s["correct"] for s in order[:k]) / k, 3),
                               "recall": round(sum(not s["correct"] for s in order[:k]) / wrong_total, 3)}
                     for k in ks}
    return out
