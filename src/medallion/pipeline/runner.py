"""Pipeline orchestration: run lifecycle, stage sequencing, locking and the run summary.

Used by both entry points - the CLI runs a pipeline in-process, the background worker runs queued
runs claimed from ops.pipeline_runs - so behaviour is identical regardless of how a run starts."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from medallion.db import Database, advisory_lock
from medallion.events.outbox import EventPublisher
from medallion.llm.router import LLMRouter
from medallion.observability import context
from medallion.pipeline.bronze import BronzeIngestStage
from medallion.pipeline.gold import GoldStage
from medallion.pipeline.quality import QualityGateStage
from medallion.pipeline.silver.stage import SilverStage
from medallion.pipeline.stage import RunContext, Stage
from medallion.reference import Taxonomy, load_reference_data
from medallion.resilience.retry import PermanentError
from medallion.settings import Settings

log = logging.getLogger(__name__)
PIPELINE_LOCK = "medallion.pipeline"


class QualityGateBlocked(PermanentError):
    """A critical data-quality check failed; gold was not published. Retrying cannot help."""


def create_run(db: Database, *, trigger: str, params: dict[str, Any], trace_id: str, status: str = "queued",
               max_attempts: int = 3) -> str:
    run_id = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute("""INSERT INTO ops.pipeline_runs (run_id, trace_id, status, trigger, params, max_attempts)
                        VALUES (%s, %s, %s, %s, %s, %s)""",
                     (run_id, trace_id, status, trigger, json.dumps(params), max_attempts))
    return run_id


class PipelineRunner:
    def __init__(self, settings: Settings, db: Database, events: EventPublisher,
                 router_factory: Callable[[], LLMRouter]) -> None:
        self.settings, self.db, self.events = settings, db, events
        self.router_factory = router_factory
        self.taxonomy = Taxonomy.load(settings.taxonomy_path)

    def stages(self, params: dict[str, Any]) -> list[Stage]:
        path = Path(params["source_path"]) if params.get("source_path") else None
        return [BronzeIngestStage(path), SilverStage(), QualityGateStage(), GoldStage()]

    def execute(self, run_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            run = conn.execute("SELECT * FROM ops.pipeline_runs WHERE run_id = %s", (run_id,)).fetchone()
        params = run["params"] or {}
        ctx = RunContext(run_id=run_id, trace_id=run["trace_id"], settings=self.settings, db=self.db,
                         events=self.events, taxonomy=self.taxonomy, router_factory=self.router_factory,
                         as_of=date.fromisoformat(params["as_of"]) if params.get("as_of") else None, params=params)

        with context.bind(trace_id=run["trace_id"], run_id=run_id), self.db.connection() as lock_conn, \
                advisory_lock(lock_conn, PIPELINE_LOCK):
            self._mark(run_id, "running", event="pipeline.run.started")
            log.info("pipeline.started", extra={"params": params})
            stage_results: dict[str, Any] = {}
            try:
                with self.db.transaction() as conn:
                    load_reference_data(conn, self.taxonomy, self.settings.seeds_dir)
                for stage in self.stages(params):
                    result = stage.run(ctx)
                    stage_results[stage.name] = {"rows_in": result.rows_in, "rows_out": result.rows_out,
                                                 "rows_rejected": result.rows_rejected, "alerts": result.alerts}
                    if result.blocking:
                        raise QualityGateBlocked(f"stage '{stage.name}' blocked downstream publication: "
                                                 f"{'; '.join(result.alerts)}")
            except Exception as exc:
                summary = self._summary(run_id, stage_results)
                self._mark(run_id, "failed", error=f"{type(exc).__name__}: {exc}", summary=summary,
                           event="pipeline.run.failed")
                log.error("pipeline.failed", extra={"error": str(exc)[:500]})
                raise
            summary = self._summary(run_id, stage_results)
            status = "succeeded_with_warnings" if summary["alerts"] else "succeeded"
            self._mark(run_id, status, summary=summary, event="pipeline.run.completed")
            log.info("pipeline.finished", extra={"status": status, "alerts": len(summary["alerts"])})
            return {"run_id": run_id, "status": status, **summary}

    def _summary(self, run_id: str, stages: dict[str, Any]) -> dict[str, Any]:
        with self.db.transaction() as conn:
            llm = conn.execute(
                """SELECT count(*) FILTER (WHERE status <> 'cache_hit') AS calls,
                          count(*) FILTER (WHERE status = 'cache_hit') AS cache_hits,
                          count(*) FILTER (WHERE status IN ('error','invalid_output','refused')) AS failures,
                          coalesce(sum(input_tokens + output_tokens), 0) AS tokens,
                          sum(cost_usd) AS cost_usd
                   FROM ops.llm_calls WHERE run_id = %s""", (run_id,)).fetchone()
        alerts = [a for s in stages.values() for a in s["alerts"]]
        calls = llm["calls"] or 0
        limit = self.settings.alert_llm_failure_rate
        if calls and llm["failures"] / calls > limit:
            alerts.append(f"LLM failure rate {llm['failures']}/{calls} exceeds {limit:.0%}")
        return {"stages": stages, "llm": {k: (float(v) if v is not None and k == "cost_usd" else v)
                                         for k, v in llm.items()}, "alerts": alerts}

    def _mark(self, run_id: str, status: str, *, event: str, error: str | None = None,
              summary: dict[str, Any] | None = None) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE ops.pipeline_runs SET status = %s, error = %s,
                          summary = coalesce(%s, summary),
                          started_at = CASE WHEN %s = 'running' THEN coalesce(started_at, now()) ELSE started_at END,
                          finished_at = CASE WHEN %s IN ('running') THEN NULL ELSE now() END
                   WHERE run_id = %s""",
                (status, error, json.dumps(summary, default=str) if summary else None, status, status, run_id))
            self.events.publish(conn, event, run_id, {"status": status, "error": error})
