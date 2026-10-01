"""Gold Layer Design Agent (assignment option d).

Given the silver schema, column facts (null rates, value sets) and a plain-English business brief,
the agent proposes gold models with SQL. Every query is EXPLAINed and executed read-only before review
(row count, columns, sample shown). Proposals are never auto-approved; approval materialises a VIEW in
`gold_sandbox` for analysts to try. Promotion to `gold` is a code change (a new sql/gold/*.sql file),
reviewed like any other code."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg import sql
from pydantic import BaseModel, Field

from medallion.agents.loop import Verified, repair_loop
from medallion.agents.prompting import load_prompt
from medallion.agents.proposals import NewProposal, ProposalRepository, register_applier
from medallion.agents.sql_guard import evaluate_query
from medallion.db import Database
from medallion.llm.router import LLMRouter
from medallion.llm.types import LLMRequest

log = logging.getLogger(__name__)

DEFAULT_DOMAIN = """Facilities management for a multi-building corporate campus. Facilities leadership wants to
know (1) whether service levels are being met and where they are slipping, (2) which vendors and in-house
teams deliver value for money, (3) which buildings and asset types generate recurring problems, and
(4) where open safety risks are sitting unattended. Reports are reviewed weekly and monthly."""


class GoldModelProposal(BaseModel):
    name: str = Field(pattern=r"^mart_[a-z0-9_]{3,60}$")
    business_question: str
    grain: str
    sql: str
    rationale: str
    caveats: str


class GoldDesign(BaseModel):
    models: list[GoldModelProposal] = Field(max_length=5)


@dataclass(frozen=True)
class GoldDesignReport:
    proposed: int
    rejected_by_guardrails: int
    strategy: str
    failed_first_pass: int = 0
    repaired_by_loop: int = 0
    repair_rounds_used: int = 0


def column_facts(conn: psycopg.Connection) -> dict[str, Any]:
    cols = [r["column_name"] for r in conn.execute(
        """SELECT column_name FROM information_schema.columns WHERE table_schema = 'silver'
           AND table_name = 'tickets' AND left(column_name, 1) <> '_' AND column_name NOT IN ('raw', 'dq_flags')
           ORDER BY ordinal_position""")]
    total = conn.execute("SELECT count(*) AS n FROM silver.tickets").fetchone()["n"] or 1
    facts: dict[str, Any] = {"row_count": total}
    for c in cols:
        row = conn.execute(f"SELECT count(*) FILTER (WHERE {c} IS NULL) AS nulls, count(DISTINCT {c}) AS d "
                           f"FROM silver.tickets").fetchone()
        fact: dict[str, Any] = {"null_pct": round(100 * row["nulls"] / total, 1), "distinct": row["d"]}
        if 0 < row["d"] <= 12:
            fact["values"] = [r["v"] for r in conn.execute(
                f"SELECT DISTINCT {c}::text AS v FROM silver.tickets WHERE {c} IS NOT NULL ORDER BY 1")]
        facts[c] = fact
    return facts


class GoldDesignAgent:
    def __init__(self, db: Database, router: LLMRouter, proposals: ProposalRepository,
                 max_repair_rounds: int = 2) -> None:
        self._db, self._router, self._proposals = db, router, proposals
        self._max_rounds = max_repair_rounds

    def _verify(self, model: GoldModelProposal) -> Verified[GoldModelProposal]:
        evidence = evaluate_query(self._db, model.sql)
        return Verified(model, evidence.ok, evidence.error,
                        {"ok": evidence.ok, "error": evidence.error, **evidence.metrics, "sample": evidence.sample})

    def run(self, domain: str = DEFAULT_DOMAIN) -> GoldDesignReport:
        if not self._router.enabled:
            log.warning("gold_design.skipped", extra={"reason": "requires an LLM provider (LLM_PROVIDERS)"})
            return GoldDesignReport(0, 0, "skipped: no LLM configured")
        with self._db.transaction() as conn:
            schema = conn.execute("""SELECT column_name, data_type FROM information_schema.columns
                                     WHERE table_schema = 'silver' AND table_name = 'tickets'
                                       AND left(column_name, 1) <> '_' AND column_name <> 'raw'
                                     ORDER BY ordinal_position""").fetchall()
            facts = column_facts(conn)
            existing = [r["table_name"] for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'gold' ORDER BY 1")]
        prompt = load_prompt("gold_design")
        system = prompt.render(domain=domain.strip(),
                               silver_schema="\n".join(f"{r['column_name']} {r['data_type']}" for r in schema),
                               column_facts=json.dumps(facts, default=str, separators=(",", ":")),
                               existing=", ".join(existing))
        result = self._router.generate(LLMRequest("gold_design", system, "Propose the gold models now.",
                                                  prompt.version, max_output_tokens=6000), GoldDesign)
        strategy = f"llm:{result.response.provider}/{result.response.model}"

        def repair(failed: list[Verified[GoldModelProposal]], round_no: int) -> list[GoldModelProposal]:
            failures = "\n".join(json.dumps({"model": f.item.model_dump(), "error": f.error}) for f in failed)
            fixed = self._router.generate(LLMRequest(
                "gold_design_repair", system,
                f"Repair round {round_no} of {self._max_rounds}. "
                "These models FAILED when executed against the database (exact errors included). Return "
                "corrected versions with the same name, or omit any that cannot be fixed.\n\n" + failures,
                prompt.version, max_output_tokens=4000), GoldDesign)
            return fixed.value.models

        verified, stats = repair_loop(result.value.models, self._verify, repair, key=lambda m: m.name,
                                      max_rounds=self._max_rounds)
        with self._db.transaction() as conn:
            for v in verified:
                model, evidence = v.item, v
                payload = {**model.model_dump(), "source": strategy, "repair_round": v.repair_round,
                           "verification": v.evidence}
                pid = self._proposals.create(conn, NewProposal(
                    agent="gold_design", kind="gold_model", subject=model.name, proposal=payload, confidence=None,
                    provider=result.response.provider, model=result.response.model, prompt_version=prompt.version),
                    auto_approve=False,
                    reason="verified (EXPLAIN + read-only execution); awaiting human review" if evidence.ok
                           else f"failed verification: {evidence.error}")
                if not evidence.ok:
                    conn.execute("""UPDATE ops.agent_proposals SET status = 'rejected', reviewed_by = 'guardrail',
                                    reviewed_at = now() WHERE proposal_id = %s""", (pid,))
        report = GoldDesignReport(len(result.value.models), stats.failed_final, strategy, stats.failed_first_pass,
                                  stats.repaired, stats.rounds_used)
        log.info("gold_design.finished", extra=report.__dict__)
        return report


@register_applier("gold_model")
def _apply_gold_model(conn: psycopg.Connection, p: dict, reviewer: str) -> None:
    # Name is pattern-validated (^mart_[a-z0-9_]+$) and the SQL passed the guard before review.
    view = sql.Identifier("gold_sandbox", p["name"])
    conn.execute(sql.SQL("CREATE OR REPLACE VIEW {} AS ").format(view) + sql.SQL(p["sql"].strip().rstrip(";")))
    note = f"{p['business_question']} | grain: {p['grain']} | approved by {reviewer} | proposal {p['proposal_id']}"
    conn.execute(sql.SQL("COMMENT ON VIEW {} IS {}").format(view, sql.Literal(note)))
