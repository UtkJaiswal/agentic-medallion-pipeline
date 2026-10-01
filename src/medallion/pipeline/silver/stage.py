"""Silver stage: enrich reference maps (classification agent), transform, and atomically replace the
silver tables. Full deterministic rebuild from bronze inside one transaction: readers never see a
half-built layer and re-running produces identical output (idempotency by construction)."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import asdict, fields

from psycopg.types.json import Jsonb

from medallion.agents.classification import ClassificationAgent, KeywordClassifier, LLMClassifier
from medallion.agents.judge import LLMJudge, TrustWeights
from medallion.agents.memory import AgentMemory
from medallion.agents.proposals import ProposalRepository
from medallion.llm.factory import build_router
from medallion.pipeline.silver.parsers import description_template, normalize_label
from medallion.pipeline.silver.transform import (
    BronzeRecord,
    LabelInfo,
    ReferenceMaps,
    SilverTicket,
    SilverTransformer,
    TemplateInfo,
    effective_label_and_description,
    known_buildings,
    quarantine_reasons,
    swap_candidate_labels,
)
from medallion.pipeline.stage import RunContext, Stage, StageFailed, StageResult

log = logging.getLogger(__name__)

_TICKET_COLUMNS = [f.name for f in fields(SilverTicket) if f.name not in
                   ("bronze_id", "row_hash", "source_file", "content_fingerprint")]
_NULL_RATE_COLUMNS = ("created_at", "resolved_at", "priority", "status", "building", "submitted_by",
                      "assignee", "cost_usd", "sla_hours", "resolution_hours")


def build_classification_agent(ctx: RunContext) -> ClassificationAgent:
    keyword = KeywordClassifier(ctx.taxonomy)
    router = ctx.router
    if ctx.settings.classifier_backend == "jev":
        from medallion.agents.jev import build_jev
        from medallion.llm.factory import PostgresUsageRecorder
        classifier, name = build_jev(ctx.settings, ctx.taxonomy, None, keyword, PostgresUsageRecorder(ctx.db))
        strategy = f"jev[{name}]"
    elif router.enabled:
        with ctx.db.transaction() as conn:
            memory = AgentMemory.load(conn)  # episodic memory = this deployment's own review history
        classifier = LLMClassifier(router, ctx.taxonomy, keyword, ctx.settings.llm_batch_size,
                                   ctx.settings.llm_concurrency, memory=memory)
        strategy = f"llm[{router.describe()}]"
    else:
        classifier, strategy = keyword, "rules (no LLM_PROVIDERS configured)"
    s, judge = ctx.settings, None
    if router.enabled and s.judge:
        provider, _, model = s.judge.partition(":")
        judge = LLMJudge(build_router(s, ctx.db, providers=[provider], models={provider: model} if model else None),
                         ctx.taxonomy, s.llm_batch_size, s.llm_concurrency)
        strategy += f" + judge[{s.judge}]"
    return ClassificationAgent(classifier, ProposalRepository(ctx.events), s.llm_auto_approve_confidence, strategy,
                               judge=judge, weights=TrustWeights(s.trust_weight_human, s.trust_weight_judge,
                                                                 s.trust_weight_agent))


def load_maps(ctx: RunContext) -> ReferenceMaps:
    with ctx.db.transaction() as conn:
        labels = {r["label"]: LabelInfo(r["label_kind"], r["category"], float(r["confidence"]))
                  for r in conn.execute("SELECT * FROM ref.category_label_map")}
        templates = {r["template"]: TemplateInfo(r["category"], r["issue_type"], r["severity"], r["is_safety_hazard"],
                                                 float(r["confidence"]))
                     for r in conn.execute("SELECT * FROM ref.description_template_map")}
    return ReferenceMaps(labels, templates)


class SilverStage(Stage):
    name = "silver"

    def execute(self, ctx: RunContext) -> StageResult:
        with ctx.db.transaction() as conn:
            records = [BronzeRecord(r["bronze_id"], r["payload"], r["row_hash"], r["source_file"]) for r in
                       conn.execute("SELECT bronze_id, payload, row_hash, source_file FROM bronze.tickets_raw "
                                    "ORDER BY bronze_id")]
        valid = [r for r in records if not quarantine_reasons(r.payload, ctx.taxonomy)]

        # 1) Enrichment: make sure every distinct label / description template has an approved mapping.
        #    Two passes because swap detection needs label kinds before templates can be computed.
        agent = build_classification_agent(ctx)
        labels = {normalize_label(r.payload.get("category")) for r in valid} | swap_candidate_labels(valid)
        report_l = agent.ensure_mapped(ctx.db, sorted(labels - {""}), {})
        maps = load_maps(ctx)
        buildings = known_buildings(records, ctx.taxonomy.junk_markers)
        templates: dict[str, str] = {}
        for r in valid:
            _, desc, _ = effective_label_and_description(r.payload, maps)
            if desc and (t := description_template(desc, buildings)) and maps.template(t) is None:
                templates.setdefault(t, desc)  # truncated variants of approved templates need no agent
        report_t = agent.ensure_mapped(ctx.db, [], templates)
        maps = load_maps(ctx)

        # 2) Transform (pure) and 3) atomically replace silver.
        build = SilverTransformer(ctx.taxonomy, maps, ctx.settings.dedup_safety_bits).build(records)
        accounted = len(build.tickets) + len(build.quarantined) + len(build.duplicates)
        if accounted != len(records):
            raise StageFailed(f"reconciliation failed: bronze={len(records)} != silver+quarantine+dup={accounted}")

        with ctx.db.transaction() as conn:
            conn.execute("TRUNCATE silver.tickets, silver.tickets_quarantine, silver.ticket_duplicates")
            cols = ", ".join([*_TICKET_COLUMNS, "_bronze_id", "_row_hash", "_source_file", "_run_id"])
            with conn.cursor().copy(f"COPY silver.tickets ({cols}) FROM STDIN") as copy:
                for t in build.tickets:
                    row = [getattr(t, c) for c in _TICKET_COLUMNS]
                    row[_TICKET_COLUMNS.index("raw")] = Jsonb(t.raw)
                    copy.write_row([*row, t.bronze_id, t.row_hash, t.source_file, ctx.run_id])
            with conn.cursor().copy("COPY silver.tickets_quarantine (_bronze_id, ticket_id_raw, reasons, raw, _run_id) "
                                    "FROM STDIN") as copy:
                for q in build.quarantined:
                    copy.write_row([q.bronze_id, q.ticket_id_raw, q.reasons, Jsonb(q.raw), ctx.run_id])
            with conn.cursor().copy("COPY silver.ticket_duplicates (_bronze_id, duplicate_ticket_id, "
                                    "survivor_ticket_id, match_rule, _run_id) FROM STDIN") as copy:
                for d in build.duplicates:
                    copy.write_row([d.bronze_id, d.duplicate_ticket_id, d.survivor_ticket_id, d.match_rule, ctx.run_id])
            null_rates = self._null_rates(conn)
            pending_review = conn.execute("""SELECT count(*) AS n FROM ops.agent_proposals
                                             WHERE agent = 'classification' AND status = 'proposed'""").fetchone()["n"]
            previous = conn.execute(
                """SELECT s.metrics->'null_rates' AS nr FROM ops.stage_runs s JOIN ops.pipeline_runs p USING (run_id)
                   WHERE s.stage = 'silver' AND s.status IN ('succeeded','warning') AND s.run_id <> %s
                   ORDER BY s.finished_at DESC LIMIT 1""", (ctx.run_id,)).fetchone()

        flags = Counter(f for t in build.tickets for f in t.dq_flags)
        metrics = {
            "bronze_rows": len(records), "silver_rows": len(build.tickets), "quarantined": len(build.quarantined),
            "duplicates": len(build.duplicates),
            "duplicates_by_rule": dict(Counter(d.match_rule for d in build.duplicates)),
            "quarantine_reasons": dict(Counter(r for q in build.quarantined for r in q.reasons)),
            "category_source": dict(Counter(t.category_source for t in build.tickets)),
            "dq_flags": dict(flags.most_common()),
            "null_rates": null_rates,
            "enrichment": {"labels": asdict(report_l), "templates": asdict(report_t)},
            "proposals_pending_review": pending_review,
        }
        alerts = self._alerts(ctx, metrics, previous["nr"] if previous else None)
        log.info("silver.summary", extra={k: metrics[k] for k in ("silver_rows", "quarantined", "duplicates")})
        return StageResult(rows_in=len(records), rows_out=len(build.tickets),
                           rows_rejected=len(build.quarantined) + len(build.duplicates), metrics=metrics, alerts=alerts)

    @staticmethod
    def _null_rates(conn) -> dict[str, float]:  # type: ignore[no-untyped-def]
        exprs = ", ".join(f"round(avg(({c} IS NULL)::int)::numeric, 4) AS {c}" for c in _NULL_RATE_COLUMNS)
        row = conn.execute(f"SELECT {exprs} FROM silver.tickets").fetchone()
        return {k: float(v or 0) for k, v in row.items()}

    @staticmethod
    def _alerts(ctx: RunContext, m: dict, previous_null_rates: dict | None) -> list[str]:
        s, alerts = ctx.settings, []
        rate = m["quarantined"] / max(1, m["bronze_rows"])
        if rate > s.alert_quarantine_rate:
            alerts.append(f"quarantine rate {rate:.2%} exceeds {s.alert_quarantine_rate:.2%}")
        if pending := m["proposals_pending_review"]:
            unresolved = m["category_source"].get("unresolved", 0)
            alerts.append(f"{pending} classification proposals await human review (medallion review list); "
                          f"{unresolved} tickets have no category until then")
        for col, now in (m["null_rates"] or {}).items():
            before = (previous_null_rates or {}).get(col)
            if before is not None and abs(now - before) > s.alert_null_rate_delta:
                alerts.append(f"null rate of {col} moved {before:.2%} -> {now:.2%}")
        return alerts

