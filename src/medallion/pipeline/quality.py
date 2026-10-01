"""Quality gate between silver and gold: runs every approved check in ref.dq_checks.

A critical check over its threshold blocks gold publication (the previous gold stays in place);
warnings are reported but do not block. Checks run read-only with a statement timeout, so even an
approved-but-bad predicate cannot modify data or hang the pipeline."""

from __future__ import annotations

import json
import logging

from medallion.pipeline.stage import RunContext, Stage, StageResult

log = logging.getLogger(__name__)


class QualityGateStage(Stage):
    name = "quality_gate"

    def execute(self, ctx: RunContext) -> StageResult:
        results, alerts, blocking = [], [], False
        with ctx.db.transaction() as conn:
            checks = conn.execute("SELECT * FROM ref.dq_checks WHERE enabled ORDER BY check_id").fetchall()
        for check in checks:
            with ctx.db.transaction() as conn:
                conn.execute("SET TRANSACTION READ ONLY")
                conn.execute("SET LOCAL statement_timeout = '30s'")
                pred = check["violation_predicate"]
                row = conn.execute(f"""SELECT count(*) AS total, count(*) FILTER (WHERE ({pred})) AS violations
                                       FROM silver.tickets""").fetchone()
                sample = [r["ticket_id"] for r in conn.execute(
                    f"SELECT ticket_id FROM silver.tickets WHERE ({pred}) ORDER BY ticket_id LIMIT 5")]
            rate = row["violations"] / max(1, row["total"])
            passed = rate <= float(check["threshold"])
            results.append((check, row, rate, passed, sample))
            if not passed:
                msg = (f"{check['severity']} check {check['check_id']} failed: {rate:.2%} violations "
                       f"> threshold {float(check['threshold']):.2%}")
                alerts.append(msg)
                blocking |= check["severity"] == "critical"

        with ctx.db.transaction() as conn:
            for check, row, rate, passed, sample in results:
                conn.execute(
                    """INSERT INTO ops.dq_results (run_id, check_id, severity, total_rows, violations, violation_rate,
                                                   threshold, passed, sample)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (ctx.run_id, check["check_id"], check["severity"], row["total"], row["violations"], round(rate, 6),
                     check["threshold"], passed, json.dumps(sample)))
                if not passed:
                    ctx.events.publish(conn, "dq.check.failed", ctx.run_id,
                                       {"check_id": check["check_id"], "severity": check["severity"],
                                        "violation_rate": rate})
        summary = {c["check_id"]: {"violations": r["violations"], "rate": round(rate, 4), "passed": p}
                   for c, r, rate, p, _ in results}
        return StageResult(rows_in=len(checks), rows_out=sum(1 for *_, p, _ in results if p),
                           rows_rejected=sum(1 for *_, p, _ in results if not p),
                           metrics={"checks": summary}, alerts=alerts, blocking=blocking)
