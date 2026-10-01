"""Stage abstraction (Template Method). `run()` owns the bookkeeping every stage needs - timing,
ops.stage_runs row, lineage event, structured logs, alert collection - and concrete stages only
implement `execute()`."""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from functools import cached_property
from typing import Any

from medallion.db import Database
from medallion.events.outbox import EventPublisher
from medallion.llm.router import LLMRouter
from medallion.observability import context
from medallion.reference import Taxonomy
from medallion.settings import Settings

log = logging.getLogger(__name__)


@dataclass
class RunContext:
    run_id: str
    trace_id: str
    settings: Settings
    db: Database
    events: EventPublisher
    taxonomy: Taxonomy
    router_factory: Any  # () -> LLMRouter; lazy so offline runs never touch provider config
    as_of: date | None = None
    params: dict[str, Any] = field(default_factory=dict)

    @cached_property
    def router(self) -> LLMRouter:
        return self.router_factory()


@dataclass
class StageResult:
    rows_in: int | None = None
    rows_out: int | None = None
    rows_rejected: int | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    alerts: list[str] = field(default_factory=list)
    blocking: bool = False  # True => downstream stages must not run (e.g. critical DQ failure)


class StageFailed(RuntimeError):
    pass


class Stage(ABC):
    name: str

    def run(self, ctx: RunContext) -> StageResult:
        started = time.perf_counter()
        with context.bind(stage=self.name), ctx.db.transaction() as conn:
            stage_id = conn.execute(
                """INSERT INTO ops.stage_runs (run_id, stage, status, started_at)
                   VALUES (%s, %s, 'running', now()) RETURNING id""", (ctx.run_id, self.name)).fetchone()["id"]
        with context.bind(stage=self.name):
            log.info("stage.started")
            try:
                result = self.execute(ctx)
            except Exception as exc:
                with ctx.db.transaction() as conn:
                    conn.execute("""UPDATE ops.stage_runs SET status = 'failed', error = %s, finished_at = now()
                                    WHERE id = %s""", (f"{type(exc).__name__}: {exc}"[:2000], stage_id))
                    ctx.events.publish(conn, "pipeline.stage.failed", ctx.run_id,
                                       {"stage": self.name, "error": str(exc)[:500]})
                log.exception("stage.failed")
                raise
            elapsed = round(time.perf_counter() - started, 3)
            result.metrics.setdefault("duration_s", elapsed)
            status = "blocked" if result.blocking else ("warning" if result.alerts else "succeeded")
            with ctx.db.transaction() as conn:
                conn.execute(
                    """UPDATE ops.stage_runs SET status = %s, rows_in = %s, rows_out = %s, rows_rejected = %s,
                              metrics = %s, alerts = %s, finished_at = now() WHERE id = %s""",
                    (status, result.rows_in, result.rows_out, result.rows_rejected,
                     json.dumps(result.metrics, default=str), json.dumps(result.alerts), stage_id))
                ctx.events.publish(conn, "pipeline.stage.completed", ctx.run_id,
                                   {"stage": self.name, "status": status, "rows_in": result.rows_in,
                                    "rows_out": result.rows_out, "rows_rejected": result.rows_rejected})
            for alert in result.alerts:
                log.warning("stage.alert", extra={"alert": alert})
            log.info("stage.finished", extra={"status": status, "rows_in": result.rows_in,
                                              "rows_out": result.rows_out, "rows_rejected": result.rows_rejected,
                                              "duration_s": elapsed})
            return result

    @abstractmethod
    def execute(self, ctx: RunContext) -> StageResult: ...
