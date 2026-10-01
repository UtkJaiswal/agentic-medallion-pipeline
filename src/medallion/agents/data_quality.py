"""Data Quality Agent (assignment option b).

Profiles bronze (deterministically, in SQL), asks the LLM to propose cleaning/validation rules with a
business rationale, then *verifies* every proposal before a human sees it:
  * the cleaning expression is executed on real sample values (before -> after shown to the reviewer);
  * the violation predicate is executed against silver (actual violation rate shown).
Proposals that fail verification are auto-rejected with the reason. Valid ones wait for approval;
approved predicates become ref.dq_checks, enforced by the quality gate on every run.
DQ checks are never auto-approved: they can block gold publication."""

from __future__ import annotations

import contextlib
import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

import psycopg
from pydantic import BaseModel, Field

from medallion.agents.loop import Verified, repair_loop
from medallion.agents.prompting import load_prompt
from medallion.agents.proposals import NewProposal, ProposalRepository, register_applier, register_loader
from medallion.agents.sql_guard import SQLEvidence, UnsafeSQLError, evaluate_predicate, static_check
from medallion.db import Database
from medallion.llm.router import LLMRouter
from medallion.llm.types import LLMRequest
from medallion.pipeline.silver.parsers import NULL_TOKENS

log = logging.getLogger(__name__)


class DQRule(BaseModel):
    check_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,60}$")
    column: str
    issue: str
    rationale: str
    action: Literal["quarantine_row", "set_null", "standardise", "deduplicate", "flag_only"]
    cleaning_logic: str
    cleaning_expression: str
    severity: Literal["info", "warning", "critical"]
    violation_predicate: str
    threshold: float = Field(ge=0, le=1)


class DQRuleSet(BaseModel):
    rules: list[DQRule] = Field(max_length=20)


@dataclass(frozen=True)
class DQRunReport:
    proposed: int
    rejected_by_guardrails: int
    strategy: str
    failed_first_pass: int = 0     # loop A/B: verification failures before any repair...
    repaired_by_loop: int = 0      # ...and how many the repair loop fixed
    repair_rounds_used: int = 0
    groundedness: float | None = None  # mean share of cited numbers found in the profile


def compact_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Trim the profile to what a reviewer (human or LLM) needs: bounded size regardless of data volume."""
    out: dict[str, Any] = {"table": profile["table"], "columns": {}}
    for col, p in profile["columns"].items():
        out["columns"][col] = {
            k: p.get(k) for k in ("total", "empty", "placeholder", "distinct_raw", "distinct_normalised",
                                  "whitespace_padded", "avg_length", "numeric_like", "currency_prefixed",
                                  "numeric", "day_month_evidence") if p.get(k) not in (None, 0, {})
        }
        out["columns"][col]["top_values"] = [[str(v["value"])[:60], v["n"]] for v in p["top_values"][:10]]
        out["columns"][col]["shapes"] = [[s["shape"][:40], s["n"], str(s["example"])[:40]] for s in p["shapes"][:8]]
    return out


_NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?%?")


def _profile_numbers(node: Any, out: set[float]) -> set[float]:
    if isinstance(node, bool):
        return out
    if isinstance(node, int | float | Decimal):
        out.add(round(float(node), 2))
    elif isinstance(node, str):
        for m in _NUMBER.findall(node):
            with contextlib.suppress(ValueError):
                out.add(round(float(m.rstrip("%").replace(",", "")), 2))
    elif isinstance(node, dict):
        for v in node.values():
            _profile_numbers(v, out)
    elif isinstance(node, list | tuple):
        for v in node:
            _profile_numbers(v, out)
    return out


def groundedness(text: str, compact: dict[str, Any], column: str | None = None) -> dict[str, Any]:
    """Share of the numbers a rule cites that are supported by the profile it was given: present in it,
    one sum/difference away from it, or a percentage of a column total. Catches invented statistics;
    small integers (<= 12, e.g. "8 formats") are treated as structural and ignored. A lower bound: deeper
    arithmetic is flagged for the reviewer rather than assumed."""
    # scope: the rule's own column (plus table-level facts) - a claim about `cost` must be backed by the
    # cost profile, not by any number that happens to appear somewhere else
    scope = compact["columns"].get(column) if column in compact["columns"] else compact["columns"]
    known = _profile_numbers([scope, compact["table"]], set())
    totals = [float(compact["table"]["total_rows"])]
    cited, ungrounded = 0, []
    for raw in _NUMBER.findall(text):
        value = float(raw.rstrip("%").replace(",", ""))
        if abs(value) <= 12 and not raw.endswith("%"):
            continue
        cited += 1
        ok = round(value, 2) in known
        if not ok and raw.endswith("%"):  # derived percentage: a count of this column over the row total
            places = len(raw.rstrip("%").partition(".")[2])  # compare at the precision the model cited
            ok = any(abs(100 * k / t - value) <= 0.5 * 10 ** -places + 1e-9
                     for t in totals for k in known if 0 < k <= t)
        if not ok:
            ungrounded.append(raw)
    return {"cited": cited, "grounded": cited - len(ungrounded),
            "score": round((cited - len(ungrounded)) / cited, 3) if cited else None, "ungrounded": ungrounded}


def evaluate_cleaning_expression(db: Database, expression: str, values: list[str]) -> dict[str, Any]:
    if not expression.strip():
        return {"ok": True, "skipped": "not a per-value rule"}
    try:
        static_check(expression, kind="predicate")
        with db.transaction() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '5s'")
            # the expression is spliced into a parameterised query: escape '%' (e.g. LIKE '%x%') for psycopg
            rows = conn.execute(f"SELECT v, ({expression.replace('%', '%%')})::text AS cleaned "
                                f"FROM unnest(%s::text[]) AS v", (values,)).fetchall()
    except (UnsafeSQLError, psycopg.Error) as exc:  # a failing expression is a verification failure
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}
    return {"ok": True, "examples": [[r["v"], r["cleaned"]] for r in rows]}


_REPAIR_PROMPT = """Repair round {round} of {max_rounds}. The following rules FAILED verification against the
real database; each comes with your latest version and its exact error. Return corrected versions with
the same check_id. Fix the SQL: predicates may use only the silver.tickets columns listed in the schema;
cleaning expressions may reference only the raw text value `v` (no column names at all). Omit any rule
that cannot be fixed.

{failures}"""


class DataQualityAgent:
    def __init__(self, db: Database, router: LLMRouter, proposals: ProposalRepository,
                 max_repair_rounds: int = 2) -> None:
        self._db, self._router, self._proposals = db, router, proposals
        self._max_rounds = max_repair_rounds

    def _verify(self, rule: DQRule, compact: dict[str, Any]) -> Verified[DQRule]:
        sample_values = [v for v, _ in compact["columns"].get(rule.column, {}).get("top_values", [])]
        cleaning = evaluate_cleaning_expression(self._db, rule.cleaning_expression, sample_values)
        check = (evaluate_predicate(self._db, rule.violation_predicate) if rule.violation_predicate.strip()
                 else SQLEvidence(True, metrics={"skipped": "no runtime check proposed"}))
        evidence = {"cleaning": cleaning,
                    "predicate": {"ok": check.ok, "error": check.error, **check.metrics, "sample": check.sample},
                    "groundedness": groundedness(rule.issue, compact, rule.column)}
        error = None if cleaning["ok"] and check.ok else (cleaning.get("error") or check.error)
        return Verified(rule, error is None, error, evidence)

    def run(self, profile: dict[str, Any]) -> DQRunReport:
        compact = compact_profile(profile)
        with self._db.transaction() as conn:
            schema = conn.execute("""SELECT column_name, data_type FROM information_schema.columns
                                     WHERE table_schema = 'silver' AND table_name = 'tickets'
                                       AND left(column_name, 1) <> '_' AND column_name <> 'raw'
                                     ORDER BY ordinal_position""").fetchall()
            existing = [r["check_id"] for r in conn.execute("SELECT check_id FROM ref.dq_checks")]
        provider = model = version = None
        if self._router.enabled:
            prompt = load_prompt("dq_rules")
            system = prompt.render(profile=json.dumps(compact, default=str, separators=(",", ":")),
                                   silver_schema="\n".join(f"{r['column_name']} {r['data_type']}" for r in schema),
                                   existing=", ".join(existing) or "(none)")
            result = self._router.generate(
                LLMRequest("dq_rules", system, "Propose the rules now.", prompt.version, max_output_tokens=6000),
                DQRuleSet)
            rules, strategy = result.value.rules, f"llm:{result.response.provider}/{result.response.model}"
            provider, model, version = result.response.provider, result.response.model, prompt.version

            def repair(failed: list[Verified[DQRule]], round_no: int) -> list[DQRule]:
                failures = "\n".join(json.dumps({"rule": f.item.model_dump(), "error": f.error}) for f in failed)
                fixed = self._router.generate(LLMRequest("dq_rules_repair", system, _REPAIR_PROMPT.format(
                    round=round_no, max_rounds=self._max_rounds, failures=failures), prompt.version,
                    max_output_tokens=4000), DQRuleSet)
                return fixed.value.rules
            max_rounds = self._max_rounds
        else:
            rules, strategy = heuristic_rules(compact), "rules"

            def repair(failed: list[Verified[DQRule]], round_no: int) -> list[DQRule]:
                return []
            max_rounds = 0

        verified, stats = repair_loop(rules, lambda r: self._verify(r, compact), repair,
                                      key=lambda r: r.check_id, max_rounds=max_rounds)
        with self._db.transaction() as conn:
            for v in verified:
                payload = {**v.item.model_dump(), "source": strategy, "repair_round": v.repair_round,
                           "verification": v.evidence}
                pid = self._proposals.create(conn, NewProposal(
                    agent="data_quality", kind="dq_check", subject=v.item.check_id, proposal=payload,
                    confidence=None, provider=provider, model=model, prompt_version=version),
                    auto_approve=False,
                    reason="verified; awaiting human review" if v.ok else f"failed verification: {v.error}")
                if not v.ok:
                    conn.execute("""UPDATE ops.agent_proposals SET status = 'rejected', reviewed_by = 'guardrail',
                                    reviewed_at = now() WHERE proposal_id = %s""", (pid,))
        scores = [v.evidence["groundedness"]["score"] for v in verified
                  if v.evidence.get("groundedness", {}).get("score") is not None]
        report = DQRunReport(len(rules), stats.failed_final, strategy, stats.failed_first_pass, stats.repaired,
                             stats.rounds_used, round(sum(scores) / len(scores), 3) if scores else None)
        log.info("dq_agent.finished", extra=report.__dict__)
        return report


def heuristic_rules(compact: dict[str, Any]) -> list[DQRule]:
    """Offline fallback: deterministic rules derived from the profile, with templated rationale."""
    rules: list[DQRule] = []
    total = compact["table"]["total_rows"] or 1
    for col, p in compact["columns"].items():
        if p.get("placeholder"):
            share = p["placeholder"] / total
            rules.append(DQRule(
                check_id=f"{col}_placeholders", column=col,
                issue=f"{p['placeholder']} values ({share:.1%}) are placeholder tokens such as "
                      f"{[v for v, _ in p['top_values'] if v.strip().lower() in NULL_TOKENS][:3]}",
                rationale="Placeholders look like real values to downstream tools and silently skew counts.",
                action="set_null", cleaning_logic="Map placeholder tokens to NULL.",
                cleaning_expression="CASE WHEN lower(btrim(v)) IN ('n/a','null','tbd','???','unknown','error') "
                                    "THEN NULL ELSE v END",
                severity="warning", violation_predicate="", threshold=0.0))
        if (p.get("distinct_raw") or 0) > (p.get("distinct_normalised") or 0) > 0:
            rules.append(DQRule(
                check_id=f"{col}_case_variants", column=col,
                issue=f"{p['distinct_raw']} raw spellings collapse to {p['distinct_normalised']} after case folding",
                rationale="Case variants split one category into several groups in every report.",
                action="standardise", cleaning_logic="Trim and normalise case before mapping to canonical values.",
                cleaning_expression="lower(btrim(v))", severity="info", violation_predicate="", threshold=0.0))
    return rules


@register_loader("dq_check")
def _load_check(conn: Any, subject: str) -> dict | None:
    row = conn.execute("SELECT * FROM ref.dq_checks WHERE check_id = %s", (subject,)).fetchone()
    if row is None:
        return None
    return {"check_id": row["check_id"], "issue": row["description"], "cleaning_logic": "(unchanged)",
            "rationale": row["rationale"], "severity": row["severity"],
            "violation_predicate": row["violation_predicate"], "threshold": float(row["threshold"]),
            "source": row["source"]}


@register_applier("dq_check")
def _apply_check(conn: Any, p: dict, reviewer: str) -> None:
    if not p["violation_predicate"].strip():
        return  # an approved cleaning-only rule: implemented in the silver transform, nothing to monitor
    description = p["issue"] if p.get("cleaning_logic") == "(unchanged)" else f"{p['issue']} -> {p['cleaning_logic']}"
    conn.execute(
        """INSERT INTO ref.dq_checks (check_id, description, rationale, severity, violation_predicate, threshold,
                                      source, approved_by, approved_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
           ON CONFLICT (check_id) DO UPDATE SET description = EXCLUDED.description, rationale = EXCLUDED.rationale,
               severity = EXCLUDED.severity, violation_predicate = EXCLUDED.violation_predicate,
               threshold = EXCLUDED.threshold, source = EXCLUDED.source, approved_by = EXCLUDED.approved_by,
               approved_at = now(), enabled = true""",
        (p["check_id"], description[:1000], p["rationale"], p["severity"], p["violation_predicate"], p["threshold"],
         p.get("source", "agent"), reviewer))
