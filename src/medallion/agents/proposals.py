"""Human-in-the-loop gate for agent output.

Every agent suggestion is stored as a proposal. Low-risk, high-confidence suggestions may be
auto-approved by policy; everything else waits for `medallion review approve|reject`. Approving a
proposal runs the applier registered for its kind (Strategy registry), so adding a new agent never
requires editing this module."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg

from medallion.events.outbox import EventPublisher
from medallion.observability.context import current_run_id, current_trace_id

Applier = Callable[[psycopg.Connection, dict[str, Any], str], None]
_APPLIERS: dict[str, Applier] = {}
_AGENT_KINDS = {"classification": {"category_label", "description_template"}, "data_quality": {"dq_check"},
                "gold_design": {"gold_model"}}


def _applier(kind: str) -> Applier:
    """Appliers register themselves when their agent module is imported; make sure all are loaded."""
    if kind not in _APPLIERS:
        import importlib
        for module in ("classification", "data_quality", "gold_design"):
            importlib.import_module(f"medallion.agents.{module}")
    return _APPLIERS[kind]


Loader = Callable[[psycopg.Connection, str], dict[str, Any] | None]
_LOADERS: dict[str, Loader] = {}


def register_loader(kind: str) -> Callable[[Loader], Loader]:
    """How to read the *current* reference row for a subject (needed to correct seeded data)."""
    def deco(fn: Loader) -> Loader:
        _LOADERS[kind] = fn
        return fn
    return deco


def register_applier(kind: str) -> Callable[[Applier], Applier]:
    def deco(fn: Applier) -> Applier:
        _APPLIERS[kind] = fn
        return fn
    return deco


@dataclass(frozen=True)
class NewProposal:
    agent: str
    kind: str
    subject: str
    proposal: dict[str, Any]
    confidence: float | None
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None


class ProposalRepository:
    def __init__(self, events: EventPublisher) -> None:
        self._events = events

    def create(self, conn: psycopg.Connection, p: NewProposal, *, auto_approve: bool, reason: str) -> str:
        proposal_id = str(uuid.uuid4())
        status = "auto_approved" if auto_approve else "proposed"
        conn.execute(
            """INSERT INTO ops.agent_proposals (proposal_id, agent, kind, subject, proposal, confidence, status,
                   review_reason, provider, model, prompt_version, run_id, trace_id, reviewed_by, reviewed_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s THEN now() END)""",
            (proposal_id, p.agent, p.kind, p.subject, json.dumps(p.proposal, default=str), p.confidence, status, reason,
             p.provider, p.model, p.prompt_version, current_run_id(), current_trace_id(),
             "policy:auto" if auto_approve else None, auto_approve))
        if auto_approve:
            _applier(p.kind)(conn, {**p.proposal, "proposal_id": proposal_id}, "policy:auto")
        self._events.publish(conn, f"agent.proposal.{status}", proposal_id,
                             {"agent": p.agent, "kind": p.kind, "subject": p.subject, "confidence": p.confidence})
        return proposal_id

    def pending_subjects(self, conn: psycopg.Connection, kind: str) -> set[str]:
        rows = conn.execute("SELECT subject FROM ops.agent_proposals WHERE kind = %s AND status = 'proposed'",
                            (kind,))
        return {r["subject"] for r in rows}

    def list(self, conn: psycopg.Connection, status: str = "proposed", agent: str | None = None) -> list[dict]:
        return conn.execute(
            """SELECT proposal_id, agent, kind, subject, proposal, confidence, status, review_reason,
                      provider, model, created_at
               FROM ops.agent_proposals
               WHERE status = %s AND (%s::text IS NULL OR agent = %s)
               ORDER BY (proposal->>'trust')::numeric NULLS LAST, created_at""",  # least trusted first
            (status, agent, agent)).fetchall()

    def decide(self, conn: psycopg.Connection, proposal_id: str, *, approve: bool, reviewer: str,
               overrides: dict[str, Any] | None = None) -> dict:
        """Approve (optionally with human corrections) or reject a pending proposal."""
        row = conn.execute("SELECT * FROM ops.agent_proposals WHERE proposal_id = %s FOR UPDATE",
                           (proposal_id,)).fetchone()
        if row is None:
            raise LookupError(f"proposal {proposal_id} not found")
        if row["status"] != "proposed":
            raise ValueError(f"proposal {proposal_id} is already {row['status']}")
        status = "approved" if approve else "rejected"
        proposal = dict(row["proposal"])
        if approve and overrides:
            original = {k: proposal.get(k) for k in overrides}  # kept: episodic memory learns from the diff
            proposal = {**proposal, **overrides, "human_overrides": overrides, "agent_original": original,
                        "source": f"{proposal.get('source', 'agent')}+human"}
        conn.execute("""UPDATE ops.agent_proposals SET status = %s, reviewed_by = %s, reviewed_at = now(), proposal = %s
                        WHERE proposal_id = %s""", (status, reviewer, json.dumps(proposal, default=str), proposal_id))
        if approve:
            _applier(row["kind"])(conn, {**proposal, "proposal_id": str(proposal_id)}, reviewer)
        self._events.publish(conn, f"agent.proposal.{status}", str(proposal_id),
                             {"kind": row["kind"], "subject": row["subject"], "reviewer": reviewer,
                              "overrides": overrides or {}})
        return {**row, "proposal": proposal, "status": status}

    def correct(self, conn: psycopg.Connection, kind: str, subject: str, values: dict[str, Any],
                reviewer: str) -> str:
        """Human correction of an existing (e.g. auto-approved) mapping: recorded as its own approved
        proposal so the audit trail shows what changed, who changed it and why."""
        latest = conn.execute("""SELECT proposal FROM ops.agent_proposals WHERE kind = %s AND subject = %s
                                 AND status IN ('approved', 'auto_approved') ORDER BY created_at DESC LIMIT 1""",
                              (kind, subject)).fetchone()
        if latest:
            base = dict(latest["proposal"])
        else:  # e.g. seeded reference data that never went through a proposal
            _applier(kind)
            base = (_LOADERS[kind](conn, subject) if kind in _LOADERS else None) or {}
        if not base:
            raise LookupError(f"no current {kind} named {subject!r} to correct")
        proposal = {**base, **values, "human_overrides": values, "source": "human",
                    "agent_original": {k: base.get(k) for k in values}}
        agent = next((a for a, kinds in _AGENT_KINDS.items() if kind in kinds), "human")
        pid = self.create(conn, NewProposal(agent=agent, kind=kind, subject=subject, proposal=proposal,
                                            confidence=1.0), auto_approve=False, reason=f"correction by {reviewer}")
        self.decide(conn, pid, approve=True, reviewer=reviewer)
        return pid
