"""Semantic Classification Agent (assignment option c).

Enriches two messy free-text columns into structured, queryable silver columns:
  * raw `category` labels (115 spellings)       -> canonical category + label kind (junk / generic / swapped)
  * `description` templates (~100 after masking) -> category, issue_type, severity, safety-hazard flag

Cost/scale design: the agent never sees rows. It classifies *distinct normalised values* that are
not yet in the approved reference maps, in batches, concurrently, behind a rate limiter, with every
answer cached. On 10k or 10M rows the LLM work is bounded by the number of new distinct values.

Two interchangeable strategies (Strategy pattern):
  * KeywordClassifier - deterministic, free, offline; the fallback and the evaluation baseline.
  * LLMClassifier     - uses the provider chain; falls back to keywords per item on any failure."""

from __future__ import annotations

import contextvars
import json
import logging
import re
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Annotated, Literal, Protocol

import psycopg
from pydantic import BaseModel, BeforeValidator, Field

from medallion.agents.judge import DEFAULT_WEIGHTS, HumanSignal, JudgeItem, LLMJudge, TrustWeights, trust_score
from medallion.agents.memory import AgentMemory
from medallion.agents.prompting import load_prompt
from medallion.agents.proposals import NewProposal, ProposalRepository, register_applier, register_loader
from medallion.db import Database
from medallion.llm.router import LLMRouter
from medallion.llm.types import BudgetExceededError, LLMRequest, NoProviderAvailableError
from medallion.pipeline.silver.parsers import NULL_TOKENS
from medallion.reference import Taxonomy

log = logging.getLogger(__name__)
Severity = Literal["low", "medium", "high", "critical"]


@dataclass(frozen=True)
class LabelDecision:
    label: str
    label_kind: str
    category: str
    confidence: float
    source: str  # rules | llm:<provider>/<model>


@dataclass(frozen=True)
class TemplateItem:
    template: str
    example: str


@dataclass(frozen=True)
class TemplateDecision:
    template: str
    category: str
    issue_type: str
    severity: str
    is_safety_hazard: bool
    confidence: float
    source: str


class Classifier(Protocol):
    def classify_labels(self, labels: Sequence[str]) -> list[LabelDecision]: ...
    def classify_templates(self, items: Sequence[TemplateItem]) -> list[TemplateDecision]: ...


# ===================================================================================== rules
_CRITICAL = ("critical", "sparking", "stuck between", "burst", "production", "overflowing", "water everywhere")
_HIGH = ("hazard", "fire", "smoke", "alarm", "extinguisher", "exit sign", "emergency", "can't get in",
         "multiple users", "won't lock", "leak", "offline", "tailgating", "not working", "out of order")
_LOW = ("paint", "stain", "furniture", "chairs", "request", "looks bad", "looks unprofessional", "need more")
_HAZARD = ("hazard", "sparking", "fire", "smoke", "extinguisher", "exit sign", "emergency lighting",
           "falling", "spill", "stuck between", "burst", "overflowing")


class KeywordClassifier:
    """Deterministic baseline. Conservative confidence so its guesses never pass auto-approval."""

    def __init__(self, taxonomy: Taxonomy) -> None:
        self.taxonomy = taxonomy
        # Prefix match on a word start ("plumb" -> "plumbing"); keywords of <= 3 chars must match a whole
        # word, otherwise "ac" would match "access".
        self._patterns = {cat: [re.compile(r"(?<![a-z])" + re.escape(k) + (r"(?![a-z])" if len(k) <= 3 else ""))
                                for k in kws] for cat, kws in taxonomy.keywords.items() if kws}

    def _score(self, text: str) -> tuple[str, float]:
        lowered = f" {text.lower()} "
        hits = {cat: sum(1 for p in pats if p.search(lowered)) for cat, pats in self._patterns.items()}
        best = max(hits.values(), default=0)
        if best == 0:
            return "unknown", 0.3
        winners = [c for c, h in hits.items() if h == best]
        return (winners[0], 0.6) if len(winners) == 1 else (sorted(winners)[0], 0.4)

    def _is_junk(self, text: str) -> bool:
        t = text.strip().lower()
        return t in NULL_TOKENS or t in {"test", "delete me"} or any(m in t for m in self.taxonomy.junk_markers)

    def classify_labels(self, labels: Sequence[str]) -> list[LabelDecision]:
        out = []
        for label in labels:
            if self._is_junk(label):
                out.append(LabelDecision(label, "junk", "unknown", 0.6, "rules"))
            elif label in self.taxonomy.generic_labels:
                out.append(LabelDecision(label, "generic_label", "unknown", 0.6, "rules"))
            else:
                kind = "description_text" if len(label.split()) >= 4 else "category_label"
                category, conf = self._score(label)
                out.append(LabelDecision(label, kind, category, conf, "rules"))
        return out

    def classify_templates(self, items: Sequence[TemplateItem]) -> list[TemplateDecision]:
        out = []
        for item in items:
            text = item.template.lower()
            if self._is_junk(text):
                out.append(TemplateDecision(item.template, "unknown", "unclassified", "low", False, 0.6, "rules"))
                continue
            category, conf = self._score(text)
            severity = ("critical" if any(k in text for k in _CRITICAL) else
                        "high" if any(k in text for k in _HIGH) else
                        "low" if any(k in text for k in _LOW) else "medium")
            out.append(TemplateDecision(item.template, category, "unclassified", severity,
                                        any(k in text for k in _HAZARD), conf, "rules"))
        return out


# ===================================================================================== LLM
def _slug(value: object) -> object:
    if not isinstance(value, str):
        return value
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.lower())).strip("_")[:60] or "unspecified"


def _clamp(value: object) -> object:
    return min(1.0, max(0.0, value)) if isinstance(value, int | float) else value


# Strict on semantics (category must be a taxonomy key), lenient on cosmetics: a model writing
# "door_won't_lock" or confidence 1.02 should not invalidate a whole batch of otherwise good answers.
Slug = Annotated[str, BeforeValidator(_slug)]
Confidence = Annotated[float, BeforeValidator(_clamp), Field(ge=0, le=1)]


def _schemas(categories: tuple[str, ...]) -> tuple[type[BaseModel], type[BaseModel]]:
    cat_type = Literal[categories]  # type: ignore[valid-type]

    class LabelResult(BaseModel):
        id: str
        label_kind: Literal["category_label", "generic_label", "description_text", "junk"]
        category: cat_type  # type: ignore[valid-type]
        confidence: Confidence

    class LabelBatch(BaseModel):
        results: list[LabelResult]

    class TemplateResult(BaseModel):
        id: str
        category: cat_type  # type: ignore[valid-type]
        issue_type: Slug
        severity: Severity
        is_safety_hazard: bool
        confidence: Confidence

    class TemplateBatch(BaseModel):
        results: list[TemplateResult]

    return LabelBatch, TemplateBatch


class LLMClassifier:
    def __init__(self, router: LLMRouter, taxonomy: Taxonomy, fallback: Classifier | None,
                 batch_size: int = 25, concurrency: int = 4, memory: AgentMemory | None = None,
                 leave_one_out: bool = False) -> None:
        """`fallback=None` drops items the LLM could not classify (used by the eval harness, where a
        failure must count as a failure rather than silently scoring the keyword fallback)."""
        self._router, self._taxonomy, self._fallback = router, taxonomy, fallback
        self._batch_size, self._concurrency = batch_size, concurrency
        self._label_schema, self._template_schema = _schemas(taxonomy.names)
        self._memory, self._leave_one_out = memory, leave_one_out

    def _with_memory(self, task: str, system: str, version: str, user: str, texts: list[str]
                     ) -> tuple[str, str, str]:
        if self._memory is None:
            return system, user, version
        sys_add, preamble = self._memory.render(task, texts, exclude=set(texts) if self._leave_one_out else None)
        return system + sys_add, preamble + user, f"{version}+mem:{self._memory.fingerprint}"

    def classify_labels(self, labels: Sequence[str]) -> list[LabelDecision]:
        prompt = load_prompt("classify_labels")
        system = prompt.render(taxonomy=self._taxonomy.prompt_block())

        def run(batch: list[str]) -> list[LabelDecision]:
            ids = {f"L{i}": label for i, label in enumerate(batch)}
            user = json.dumps([{"id": k, "label": v} for k, v in ids.items()], ensure_ascii=False)
            sys_, user, version = self._with_memory("classify_labels", system, prompt.version, user, list(batch))
            result = self._router.generate(LLMRequest("classify_labels", sys_, user, version,
                                                      max_output_tokens=80 * len(batch) + 200),
                                           self._label_schema)
            source = f"llm:{result.response.provider}/{result.response.model}"
            got = {r.id: LabelDecision(ids[r.id], r.label_kind, r.category, r.confidence, source)
                   for r in result.value.results if r.id in ids}
            missing = [label for key, label in ids.items() if key not in got]
            return list(got.values()) + (self._fallback.classify_labels(missing) if self._fallback else [])

        return self._fan_out(list(labels), run, self._fallback.classify_labels if self._fallback else None)

    def classify_templates(self, items: Sequence[TemplateItem]) -> list[TemplateDecision]:
        prompt = load_prompt("classify_templates")
        system = prompt.render(taxonomy=self._taxonomy.prompt_block())

        def run(batch: list[TemplateItem]) -> list[TemplateDecision]:
            ids = {f"T{i}": item for i, item in enumerate(batch)}
            user = json.dumps([{"id": k, "template": v.template, "example": v.example} for k, v in ids.items()],
                              ensure_ascii=False)
            sys_, user, version = self._with_memory("classify_templates", system, prompt.version, user,
                                                    [i.template for i in batch])
            result = self._router.generate(LLMRequest("classify_templates", sys_, user, version,
                                                      max_output_tokens=120 * len(batch) + 200),
                                           self._template_schema)
            source = f"llm:{result.response.provider}/{result.response.model}"
            got = {r.id: TemplateDecision(ids[r.id].template, r.category, r.issue_type, r.severity,
                                          r.is_safety_hazard, r.confidence, source)
                   for r in result.value.results if r.id in ids}
            missing = [item for key, item in ids.items() if key not in got]
            return list(got.values()) + (self._fallback.classify_templates(missing) if self._fallback else [])

        return self._fan_out(list(items), run, self._fallback.classify_templates if self._fallback else None)

    def _fan_out(self, items: list, run, fallback) -> list:  # type: ignore[no-untyped-def]
        batches = [items[i:i + self._batch_size] for i in range(0, len(items), self._batch_size)]

        def guarded(batch: list) -> list:
            try:
                return run(batch)
            except (NoProviderAvailableError, BudgetExceededError) as exc:
                log.warning("classification.batch_fallback", extra={"size": len(batch), "reason": str(exc)[:200]})
                return fallback(batch) if fallback else []

        # contextvars are not inherited by pool threads: copy them so trace/run ids follow each call
        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            futures = [pool.submit(contextvars.copy_context().run, guarded, b) for b in batches]
            return [d for f in futures for d in f.result()]


# ===================================================================================== agent
@dataclass(frozen=True)
class EnrichmentReport:
    new_labels: int
    new_templates: int
    auto_approved: int
    pending_review: int
    strategy: str


class ClassificationAgent:
    """Keeps the reference maps complete for whatever values bronze currently contains."""

    def __init__(self, classifier: Classifier, proposals: ProposalRepository, auto_approve_confidence: float,
                 strategy_name: str, judge: LLMJudge | None = None, weights: TrustWeights = DEFAULT_WEIGHTS) -> None:
        self._classifier, self._proposals = classifier, proposals
        self._threshold, self._strategy = auto_approve_confidence, strategy_name
        self._judge, self._weights = judge, weights

    def ensure_mapped(self, db: Database, labels: Iterable[str], templates: dict[str, str]) -> EnrichmentReport:
        # 1) short read transaction; 2) LLM work with no connection held; 3) short write transaction.
        with db.transaction() as conn:
            known_labels = {r["label"] for r in conn.execute("SELECT label FROM ref.category_label_map")}
            known_templates = {r["template"] for r in
                               conn.execute("SELECT template FROM ref.description_template_map")}
            waiting_l = self._proposals.pending_subjects(conn, "category_label")
            waiting_t = self._proposals.pending_subjects(conn, "description_template")
            human = _human_decisions(conn) if self._judge else None

        new_labels = sorted(set(labels) - known_labels - waiting_l)
        new_templates = sorted(set(templates) - known_templates - waiting_t)
        if not new_labels and not new_templates:
            return EnrichmentReport(0, 0, 0, 0, self._strategy)

        log.info("classification.started", extra={"new_labels": len(new_labels),
                                                  "new_templates": len(new_templates), "strategy": self._strategy})
        label_decisions = self._classifier.classify_labels(new_labels) if new_labels else []
        template_decisions = self._classifier.classify_templates(
            [TemplateItem(t, templates[t]) for t in new_templates]) if new_templates else []

        scores = self._trust(label_decisions, template_decisions, human) if self._judge else {}
        auto = pending = 0
        with db.transaction() as conn:
            for kind, decisions in (("category_label", label_decisions),
                                    ("description_template", template_decisions)):
                version = load_prompt("classify_labels" if kind == "category_label" else "classify_templates").version
                for d in decisions:
                    subject = d.label if isinstance(d, LabelDecision) else d.template
                    score = scores.get((kind, subject))
                    ok, reason = self._policy(d, score)
                    provider, _, model = d.source.removeprefix("llm:").partition("/")
                    self._proposals.create(conn, NewProposal(
                        agent="classification", kind=kind, subject=subject,
                        proposal={**asdict(d), **(score or {})}, confidence=d.confidence,
                        provider=None if d.source == "rules" else provider, model=model or None,
                        prompt_version=version), auto_approve=ok, reason=reason)
                    auto, pending = (auto + 1, pending) if ok else (auto, pending + 1)
        report = EnrichmentReport(len(new_labels), len(new_templates), auto, pending, self._strategy)
        log.info("classification.finished", extra=asdict(report))
        return report

    def _policy(self, d: LabelDecision | TemplateDecision, score: dict | None = None) -> tuple[bool, str]:
        """Auto-approve only well-supported LLM answers; everything else goes to a human.
        With a judge: human-weighted trust (see agents/judge.py). Without: the agent's own confidence."""
        if d.source == "rules":
            return False, "rule-based guess: needs human review"
        if self._judge is not None and (score is None or score.get("judge") is None):
            return False, "judge configured but gave no verdict: needs human review (fail safe)"
        value, name = (score["trust"], "trust") if score else (d.confidence, "confidence")
        if value < self._threshold:
            return False, f"{name} {value:.2f} < auto-approve threshold {self._threshold}"
        return True, f"{name} {value:.2f} >= {self._threshold}"

    def _trust(self, labels: list[LabelDecision], templates: list[TemplateDecision],
               human: HumanSignal | None) -> dict[tuple[str, str], dict]:
        items: list[tuple[str, str, dict, float]] = []
        items += [("category_label", d.label, {"category": d.category, "label_kind": d.label_kind}, d.confidence)
                  for d in labels if d.source != "rules"]
        items += [("description_template", d.template, {"category": d.category,
                                                        "is_safety_hazard": d.is_safety_hazard}, d.confidence)
                  for d in templates if d.source != "rules"]
        if not items or self._judge is None:
            return {}
        try:
            verdicts = self._judge.judge([JudgeItem(f"J{i}", k, s, a) for i, (k, s, a, _) in enumerate(items)])
        except (NoProviderAvailableError, BudgetExceededError) as exc:
            log.warning("classification.judge_unavailable", extra={"reason": str(exc)[:200]})
            return {}  # falls back to the agent-confidence policy
        out = {}
        for i, (kind, subject, answer, conf) in enumerate(items):
            v = verdicts.get(f"J{i}")
            judge_p = None if v is None else (v["p_correct"] if v["agrees"] else min(v["p_correct"], 0.5))
            h = human(kind, subject, answer) if human else None
            out[(kind, subject)] = {"judge": v, "human_signal": h,
                                    "trust": round(trust_score(conf, judge_p, h, self._weights), 4)}
        return out


def _human_decisions(conn: psycopg.Connection) -> HumanSignal:
    """Human-approved mappings (reviews and reviewed seeds; not policy auto-approvals) as precedents."""
    decisions = [("category_label", r["label"], {"category": r["category"], "label_kind": r["label_kind"]})
                 for r in conn.execute("SELECT * FROM ref.category_label_map WHERE approved_by <> 'policy:auto'")]
    decisions += [("description_template", r["template"],
                   {"category": r["category"], "is_safety_hazard": r["is_safety_hazard"]})
                  for r in conn.execute("SELECT * FROM ref.description_template_map "
                                        "WHERE approved_by <> 'policy:auto'")]
    return HumanSignal(decisions)


# ===================================================================================== appliers
@register_loader("category_label")
def _load_label(conn: psycopg.Connection, subject: str) -> dict | None:
    row = conn.execute("SELECT * FROM ref.category_label_map WHERE label = %s", (subject,)).fetchone()
    return None if row is None else {"label": row["label"], "label_kind": row["label_kind"],
                                     "category": row["category"], "confidence": float(row["confidence"]),
                                     "source": row["source"]}


@register_loader("description_template")
def _load_template(conn: psycopg.Connection, subject: str) -> dict | None:
    row = conn.execute("SELECT * FROM ref.description_template_map WHERE template = %s", (subject,)).fetchone()
    return None if row is None else {k: row[k] for k in ("template", "category", "issue_type", "severity",
                                                         "is_safety_hazard", "source")} | {
        "confidence": float(row["confidence"])}


@register_applier("category_label")
def _apply_label(conn: psycopg.Connection, p: dict, reviewer: str) -> None:
    conn.execute(
        """INSERT INTO ref.category_label_map (label, label_kind, category, confidence, source, proposal_id,
                                               approved_by, approved_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s, now())
           ON CONFLICT (label) DO UPDATE SET label_kind = EXCLUDED.label_kind, category = EXCLUDED.category,
               confidence = EXCLUDED.confidence, source = EXCLUDED.source, proposal_id = EXCLUDED.proposal_id,
               approved_by = EXCLUDED.approved_by, approved_at = now()""",
        (p["label"], p["label_kind"], p["category"], p["confidence"], p["source"], p["proposal_id"], reviewer))


@register_applier("description_template")
def _apply_template(conn: psycopg.Connection, p: dict, reviewer: str) -> None:
    conn.execute(
        """INSERT INTO ref.description_template_map (template, category, issue_type, severity, is_safety_hazard,
                                                     confidence, source, proposal_id, approved_by, approved_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
           ON CONFLICT (template) DO UPDATE SET category = EXCLUDED.category, issue_type = EXCLUDED.issue_type,
               severity = EXCLUDED.severity, is_safety_hazard = EXCLUDED.is_safety_hazard,
               confidence = EXCLUDED.confidence, source = EXCLUDED.source, proposal_id = EXCLUDED.proposal_id,
               approved_by = EXCLUDED.approved_by, approved_at = now()""",
        (p["template"], p["category"], p["issue_type"], p["severity"], p["is_safety_hazard"], p["confidence"],
         p["source"], p["proposal_id"], reviewer))
