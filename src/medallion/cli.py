"""Command-line entry point: `medallion <command>`. Thin adapter over the container - no logic here."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from medallion.container import Container
from medallion.observability.context import new_trace_id

Handler = Callable[[Container, argparse.Namespace], int]
_COMMANDS: dict[str, Handler] = {}


def command(name: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _COMMANDS[name] = fn
        return fn
    return deco


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ------------------------------------------------------------------------------------------ pipeline
@command("migrate")
def _migrate(c: Container, _: argparse.Namespace) -> int:
    from medallion.db import migrate
    applied = migrate(c.db)
    print(f"migrations applied: {applied or 'none (up to date)'}")
    return 0


@command("run")
def _run(c: Container, a: argparse.Namespace) -> int:
    from medallion.db import LockNotAcquired, migrate
    from medallion.pipeline.runner import create_run
    migrate(c.db)
    params = {k: v for k, v in {"source_path": a.source, "as_of": a.as_of}.items() if v}
    run_id = create_run(c.db, trigger="cli", params=params, trace_id=new_trace_id())
    try:
        summary = c.runner.execute(run_id)
    except LockNotAcquired:
        print("another pipeline run is in progress; try again shortly", file=sys.stderr)
        return 75
    except Exception as exc:
        print(f"pipeline failed: {exc}", file=sys.stderr)
        return 1
    _print(summary)
    return 0


@command("status")
def _status(c: Container, a: argparse.Namespace) -> int:
    with c.db.transaction() as conn:
        rows = conn.execute(
            """SELECT run_id, status, trigger, attempts, error, created_at, finished_at FROM ops.pipeline_runs
               WHERE %s::uuid IS NULL OR run_id = %s::uuid ORDER BY created_at DESC LIMIT 10""",
            (a.run_id, a.run_id)).fetchall()
    _print(rows)
    return 0


@command("report")
def _report(c: Container, _: argparse.Namespace) -> int:
    """A readable tour of every layer, for humans."""
    q = {
        "bronze": "SELECT count(*) AS rows, count(DISTINCT source_file_sha256) AS files, max(ingested_at) AS last "
                  "FROM bronze.tickets_raw",
        "silver": "SELECT (SELECT count(*) FROM silver.tickets) AS tickets, "
                  "(SELECT count(*) FROM silver.tickets_quarantine) AS quarantined, "
                  "(SELECT count(*) FROM silver.ticket_duplicates) AS duplicates",
        "quarantine reasons": "SELECT unnest(reasons) AS reason, count(*) FROM silver.tickets_quarantine GROUP BY 1",
        "duplicates by rule": "SELECT match_rule, count(*) FROM silver.ticket_duplicates GROUP BY 1",
        "category (source of truth)": "SELECT category, category_source, count(*) FROM silver.tickets "
                                      "GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 15",
        "top data-quality flags": "SELECT unnest(dq_flags) AS flag, count(*) FROM silver.tickets GROUP BY 1 "
                                  "ORDER BY 2 DESC LIMIT 12",
        "quality gate (latest run)": "SELECT check_id, severity, violations, violation_rate, threshold, passed "
                                     "FROM ops.dq_results WHERE run_id = (SELECT run_id FROM ops.dq_results "
                                     "ORDER BY checked_at DESC LIMIT 1) ORDER BY check_id",
        "gold: SLA compliance by category (all months)":
            "SELECT category, sum(tickets_created) AS created, sum(sla_measurable) AS measurable, "
            "round(100.0 * sum(sla_met) / nullif(sum(sla_measurable), 0), 1) AS sla_pct "
            "FROM gold.mart_sla_performance GROUP BY 1 ORDER BY 4",
        "gold: vendor scorecard": "SELECT assignee, assignee_type, tickets_assigned, median_resolution_hours AS "
                                  "median_h, sla_compliance_pct AS sla_pct, avg_cost_usd, temporary_fix_pct "
                                  "FROM gold.mart_vendor_scorecard ORDER BY tickets_assigned DESC",
        "gold: open backlog by building (top 8)": "SELECT building, sum(open_tickets) AS open, "
                                                  "sum(open_safety_hazards) AS hazards, sum(past_sla) AS past_sla, "
                                                  "max(oldest_open_days) AS oldest_days FROM gold.mart_open_backlog "
                                                  "GROUP BY 1 ORDER BY 2 DESC LIMIT 8",
        "gold: under-prioritised open hazards (enrichment)":
            "SELECT issue_type, priority, inferred_severity, count(*) FROM gold.v_underprioritised_hazards "
            "GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 8",
        "agents: proposals": "SELECT agent, status, count(*) FROM ops.agent_proposals GROUP BY 1, 2 ORDER BY 1, 2",
        "llm usage (all runs)": "SELECT provider, model, status, count(*) AS calls, sum(input_tokens) AS tok_in, "
                                "sum(output_tokens) AS tok_out, round(sum(cost_usd), 4) AS usd FROM ops.llm_calls "
                                "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3",
        "events (outbox)": "SELECT event_type, count(*), count(published_at) AS relayed FROM ops.outbox "
                           "GROUP BY 1 ORDER BY 2 DESC",
    }
    with c.db.transaction() as conn:
        for title, sql in q.items():
            try:
                rows = conn.execute(sql).fetchall()
            except Exception as exc:  # a layer that has not been built yet
                conn.rollback()
                print(f"\n== {title}: unavailable ({type(exc).__name__})")
                continue
            print(f"\n== {title}")
            if rows:
                cols = list(rows[0].keys())
                widths = [max(len(str(col)), *(len(str(r[col])) for r in rows)) for col in cols]
                print("  " + "  ".join(str(col).ljust(w) for col, w in zip(cols, widths, strict=True)))
                for r in rows:
                    print("  " + "  ".join(str(r[col]).ljust(w) for col, w in zip(cols, widths, strict=True)))
            else:
                print("  (none)")
    return 0


# ------------------------------------------------------------------------------------------ agents
@command("agent-dq")
def _agent_dq(c: Container, a: argparse.Namespace) -> int:
    from medallion.agents.data_quality import DataQualityAgent
    from medallion.agents.profiler import profile_table
    from medallion.agents.proposals import ProposalRepository
    with c.db.transaction() as conn:
        header = conn.execute("""SELECT header FROM ops.ingestion_manifest ORDER BY ingested_at DESC LIMIT 1"""
                              ).fetchone()
        if header is None:
            print("nothing ingested yet: run `medallion run` first", file=sys.stderr)
            return 1
        profile = profile_table(conn, header["header"])
    report = DataQualityAgent(c.db, c.router(), ProposalRepository(c.events)).run(profile)
    _print(report.__dict__)
    print("review with: medallion review list --agent data_quality")
    return 0


@command("agent-gold")
def _agent_gold(c: Container, a: argparse.Namespace) -> int:
    from medallion.agents.gold_design import DEFAULT_DOMAIN, GoldDesignAgent
    from medallion.agents.proposals import ProposalRepository
    domain = Path(a.domain_file).read_text() if a.domain_file else DEFAULT_DOMAIN
    report = GoldDesignAgent(c.db, c.router(), ProposalRepository(c.events)).run(domain)
    _print(report.__dict__)
    print("review with: medallion review list --agent gold_design")
    return 0


@command("review")
def _review(c: Container, a: argparse.Namespace) -> int:
    from medallion.agents.proposals import ProposalRepository
    repo = ProposalRepository(c.events)
    with c.db.transaction() as conn:
        if a.action == "list":
            rows = repo.list(conn, status=a.status, agent=a.agent)
            for r in rows:
                p = r["proposal"]
                summary = {k: p.get(k) for k in ("category", "label_kind", "issue_type", "severity", "issue",
                                                 "business_question", "violation_predicate", "threshold")
                           if p.get(k) is not None}
                if v := p.get("verification"):
                    summary["verification"] = v
                print(f"{r['proposal_id']}  [{r['agent']}/{r['kind']}] {r['subject'][:70]!r} "
                      f"conf={r['confidence']}  {r['review_reason'] or ''}")
                print("    " + json.dumps(summary, default=str)[:600])
            print(f"{len(rows)} proposal(s) with status={a.status}")
            return 0
        overrides = _parse_overrides(a.set)
        if a.action == "correct":
            kind, subject = a.ids[0], " ".join(a.ids[1:])
            pid = repo.correct(conn, kind, subject, overrides, a.reviewer)
            print(f"corrected {kind} {subject!r} -> {overrides} (proposal {pid})")
            return 0
        ids = a.ids
        if a.all_pending:
            ids = [str(r["proposal_id"]) for r in repo.list(conn, status="proposed", agent=a.agent)]
        for pid in ids:
            row = repo.decide(conn, pid, approve=a.action == "approve", reviewer=a.reviewer, overrides=overrides)
            print(f"{row['status']}: {pid} ({row['kind']} {row['subject'][:60]!r})"
                  + (f" with overrides {overrides}" if overrides and a.action == "approve" else ""))
    return 0


def _parse_overrides(pairs: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs or []:
        key, _, raw = pair.partition("=")
        try:
            out[key] = json.loads(raw)  # numbers, booleans
        except json.JSONDecodeError:
            out[key] = raw
    return out


@command("eval")
def _eval(c: Container, a: argparse.Namespace) -> int:
    from medallion.agents.evaluation import render, run_eval, save
    failed = False
    for strategy in a.strategies.split(","):
        report = run_eval(c.settings, c.taxonomy, strategy.strip())
        print(render(report))
        print(f"  saved: {save(report)}")
        if a.show_errors:
            for t in report.tasks:
                for e in t.errors:
                    print(f"    {t.task}: {e}")
        failed |= not report.passed(a.min_accuracy)
    if failed:
        print(f"FAIL: accuracy below promotion gate {a.min_accuracy:.0%}", file=sys.stderr)
    return 1 if failed else 0


@command("export-seeds")
def _export_seeds(c: Container, _: argparse.Namespace) -> int:
    """Write the approved reference maps back to config/seeds so they are versioned and code-reviewed."""
    import csv

    import yaml
    out = c.settings.seeds_dir
    out.mkdir(parents=True, exist_ok=True)
    with c.db.transaction() as conn:
        labels = conn.execute("SELECT label, label_kind, category, confidence, source FROM ref.category_label_map "
                              "ORDER BY label").fetchall()
        templates = conn.execute("SELECT template, category, issue_type, severity, is_safety_hazard, confidence, "
                                 "source FROM ref.description_template_map ORDER BY template").fetchall()
        checks = conn.execute("SELECT check_id, description, rationale, severity, violation_predicate, threshold, "
                              "source FROM ref.dq_checks WHERE enabled ORDER BY check_id").fetchall()
    for name, rows in (("category_label_map.csv", labels), ("description_template_map.csv", templates)):
        with (out / name).open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else [])
            writer.writeheader()
            for r in rows:
                writer.writerow({k: (str(v).lower() if isinstance(v, bool) else v) for k, v in r.items()})
    (out / "dq_checks.yaml").write_text(yaml.safe_dump([{**r, "threshold": float(r["threshold"])} for r in checks],
                                                       sort_keys=False, allow_unicode=True, width=110))
    from medallion.agents.memory import MEMORY_DIR, export_episodes
    with c.db.transaction() as conn:
        n_episodes = export_episodes(conn, MEMORY_DIR / "episodes.jsonl")
    print(f"exported {len(labels)} labels, {len(templates)} templates, {len(checks)} checks to {out}; "
          f"{n_episodes} episodes to {MEMORY_DIR}")
    return 0


@command("synth")
def _synth(c: Container, a: argparse.Namespace) -> int:
    """Generate messy synthetic tickets with ground truth; optionally run the pipeline on them and score it."""
    from medallion.db import migrate
    from medallion.pipeline.runner import create_run
    from medallion.synthetic import generate, score
    csv_path, truth_path = generate(a.rows, a.seed)
    print(f"generated {csv_path} (+ ground truth {truth_path.name})")
    if not a.run:
        return 0
    migrate(c.db)
    run_id = create_run(c.db, trigger="synthetic", params={"source_path": str(csv_path)}, trace_id=new_trace_id())
    c.runner.execute(run_id)
    _print(score(c.db, truth_path, csv_path.name))
    return 0


# ------------------------------------------------------------------------------------------ services
@command("api")
def _api(c: Container, a: argparse.Namespace) -> int:
    import uvicorn

    from medallion.api.app import create_app
    from medallion.db import migrate
    migrate(c.db)
    uvicorn.run(create_app(c), host=a.host, port=a.port, log_config=None)
    return 0


@command("worker")
def _worker(c: Container, _: argparse.Namespace) -> int:
    from medallion.jobs.worker import main as worker_main
    worker_main(c)
    return 0


@command("relay")
def _relay(c: Container, _: argparse.Namespace) -> int:
    import signal

    from medallion.events.relay import KafkaProducer, OutboxRelay
    if not c.settings.kafka_bootstrap_servers:
        print("KAFKA_BOOTSTRAP_SERVERS is not set: events stay in ops.outbox (that is fine)", file=sys.stderr)
        return 1
    relay = OutboxRelay(c.db, KafkaProducer(c.settings.kafka_bootstrap_servers), c.settings.kafka_topic)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: relay.stop.set())
    relay.run_forever(c.settings.outbox_poll_interval_s)
    return 0


@command("submit")
def _submit(c: Container, a: argparse.Namespace) -> int:
    """API client: idempotent submit with retries, then poll with backoff + jitter until the run finishes."""
    import uuid

    import httpx

    from medallion.resilience.retry import PollTimeout, RetryPolicy, TransientError, poll_until, retry_call
    headers = {"Idempotency-Key": a.idempotency_key or str(uuid.uuid4()), "X-Trace-Id": new_trace_id()}
    if c.settings.api_key:
        headers["X-API-Key"] = c.settings.api_key.get_secret_value()
    body = {k: v for k, v in {"source_path": a.source, "as_of": a.as_of}.items() if v}

    with httpx.Client(base_url=c.settings.api_url, timeout=10) as http:
        def post() -> httpx.Response:
            try:
                r = http.post("/v1/pipeline-runs", json=body, headers=headers)
            except httpx.TransportError as exc:
                raise TransientError(str(exc)) from exc
            if r.status_code == 429 or r.status_code >= 500:
                raise TransientError(f"HTTP {r.status_code}", retry_after=float(r.headers.get("Retry-After", 1)))
            r.raise_for_status()
            return r

        # Safe to retry blindly: the same Idempotency-Key can never create a second run.
        r = retry_call(post, RetryPolicy(max_attempts=5, base_delay_s=0.5, max_delay_s=8), label="submit")
        run = r.json()
        replay = " (idempotent replay)" if r.headers.get("Idempotent-Replayed") else ""
        print(f"run {run['run_id']} {run['status']}{replay}  key={headers['Idempotency-Key']}  "
              f"trace={r.headers.get('X-Trace-Id')}")
        if not a.wait:
            return 0
        try:
            final = poll_until(lambda: http.get(f"/v1/pipeline-runs/{run['run_id']}", headers=headers).json(),
                               lambda v: v["status"] not in ("queued", "running"), timeout_s=a.timeout)
        except PollTimeout:
            print(f"still running after {a.timeout}s; check `medallion status {run['run_id']}`", file=sys.stderr)
            return 2
    _print({k: final[k] for k in ("run_id", "status", "attempts", "error", "summary")})
    return 0 if final["status"].startswith("succeeded") else 1


@command("experiment")
def _experiment(c: Container, a: argparse.Namespace) -> int:
    """Offline A/B experiments on recorded decisions; results saved under evals/results/."""
    from datetime import UTC, datetime

    from medallion.agents.evaluation import EVAL_DIR
    from medallion.agents.experiments import (
        _recorded_decisions,
        export_recorded_decisions,
        judge_policy_ab,
        load_recorded_decisions,
    )
    if a.export:
        print(f"exported {export_recorded_decisions(c.db)} decisions")
        return 0
    from medallion.agents.judge import LLMJudge
    from medallion.llm.factory import build_router
    provider, _, model = a.judge.partition(":")
    router = build_router(c.settings, c.db, providers=[provider], models={provider: model} if model else None)
    decisions = _recorded_decisions(c.db) if a.from_db else load_recorded_decisions()
    judge = LLMJudge(router, c.taxonomy, c.settings.llm_batch_size, c.settings.llm_concurrency)
    result = judge_policy_ab(decisions, judge, threshold=a.threshold)
    result["judge"] = router.describe()
    with c.db.transaction() as conn:
        spent = conn.execute("SELECT sum(cost_usd) AS usd FROM ops.llm_calls WHERE task = 'judge' AND "
                             "created_at > now() - interval '1 hour'").fetchone()["usd"]
    result["judge_cost_usd_last_hour"] = float(spent) if spent is not None else None
    out = EVAL_DIR / "results" / f"{datetime.now(UTC):%Y-%m-%dT%H%M}_judge_policy_ab.json"
    out.write_text(json.dumps(result, indent=2, default=str))
    _print(result)
    print(f"saved: {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="medallion", description="AI-assisted medallion pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations")
    r = sub.add_parser("run", help="run bronze -> silver -> quality gate -> gold in-process")
    r.add_argument("--source", help="CSV to ingest (default: RAW_DATA_PATH)")
    r.add_argument("--as-of", help="snapshot date for backlog marts (default: latest ticket date)")
    s = sub.add_parser("status", help="show recent pipeline runs")
    s.add_argument("run_id", nargs="?")
    sub.add_parser("report", help="print a summary of every layer")
    sub.add_parser("export-seeds", help="write approved reference maps and DQ checks to config/seeds")

    sub.add_parser("agent-dq", help="data quality agent: profile bronze, propose verified rules for review")
    g = sub.add_parser("agent-gold", help="gold design agent: propose verified gold models for review")
    g.add_argument("--domain-file", help="plain-English business brief (default: built-in facilities brief)")

    rv = sub.add_parser("review", help="human-in-the-loop gate for agent proposals")
    rv.add_argument("action", choices=["list", "approve", "reject", "correct"])
    rv.add_argument("ids", nargs="*", help="proposal ids; for `correct`: KIND SUBJECT")
    rv.add_argument("--set", action="append", metavar="FIELD=VALUE",
                    help="human override applied on approve/correct, e.g. --set category=hvac --set threshold=0.4")
    rv.add_argument("--agent", choices=["classification", "data_quality", "gold_design"])
    rv.add_argument("--status", default="proposed", choices=["proposed", "approved", "auto_approved", "rejected"])
    rv.add_argument("--all-pending", action="store_true", help="act on every pending proposal (of --agent)")
    rv.add_argument("--reviewer", default="cli-user")

    e = sub.add_parser("eval", help="score classification strategies against the labelled eval set")
    e.add_argument("--strategies", default="rules", help="comma list: rules and/or provider names, e.g. rules,ollama")
    e.add_argument("--min-accuracy", type=float, default=0.9, help="promotion gate (exit 1 if any task is below)")
    e.add_argument("--show-errors", action="store_true")

    ex = sub.add_parser("experiment", help="offline A/B: auto-approval policies (agent vs judge vs human-weighted)")
    ex.add_argument("name", choices=["judge"])
    ex.add_argument("--judge", default="openrouter:typesafe/jev-router", help="provider[:model] used as judge")
    ex.add_argument("--threshold", type=float, default=0.85)
    ex.add_argument("--from-db", action="store_true", help="replay decisions from ops.agent_proposals instead of "
                                                           "evals/datasets/recorded_decisions.jsonl")
    ex.add_argument("--export", action="store_true", help="write the DB's recorded decisions to the dataset file")

    sy = sub.add_parser("synth", help="generate messy synthetic tickets with ground truth (and score a run)")
    sy.add_argument("--rows", type=int, default=5000)
    sy.add_argument("--seed", type=int, default=7)
    sy.add_argument("--run", action="store_true", help="ingest the file, run the pipeline and score the result")

    ap = sub.add_parser("api", help="serve the HTTP API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    sub.add_parser("worker", help="run the background worker (executes queued runs)")
    sub.add_parser("relay", help="relay outbox events to Kafka")
    sb = sub.add_parser("submit", help="submit a run through the API (idempotent, retried) and optionally wait")
    sb.add_argument("--source")
    sb.add_argument("--as-of")
    sb.add_argument("--idempotency-key", help="reuse to safely retry a submission (default: random)")
    sb.add_argument("--wait", action="store_true", help="poll until the run finishes")
    sb.add_argument("--timeout", type=float, default=900)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    container = Container()
    try:
        return _COMMANDS[args.cmd](container, args)
    finally:
        container.close()


if __name__ == "__main__":
    sys.exit(main())
