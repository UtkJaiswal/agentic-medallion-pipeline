"""End-to-end against a real Postgres (throw-away database). Run `make up` first; skipped otherwise."""

import pytest

from medallion.observability.context import new_trace_id
from medallion.pipeline.runner import QualityGateBlocked, create_run

pytestmark = pytest.mark.integration
SILVER_FINGERPRINT = """SELECT md5(string_agg(t::text, ',' ORDER BY ticket_id))
                        FROM (SELECT ticket_id, category, priority, status, resolution_hours, cost_usd, sla_met,
                                     submitted_by, issue_type, dq_flags FROM silver.tickets) t"""


def run(container, **params):
    run_id = create_run(container.db, trigger="test", params=params, trace_id=new_trace_id())
    return container.runner.execute(run_id)


def scalar(container, sql, *args):
    with container.db.transaction() as conn:
        return next(iter(conn.execute(sql, args).fetchone().values()))


def test_full_pipeline_is_idempotent_and_reconciles(container):
    first = run(container)
    assert first["status"].startswith("succeeded")
    bronze = scalar(container, "SELECT count(*) FROM bronze.tickets_raw")
    silver = scalar(container, "SELECT count(*) FROM silver.tickets")
    quarantined = scalar(container, "SELECT count(*) FROM silver.tickets_quarantine")
    dups = scalar(container, "SELECT count(*) FROM silver.ticket_duplicates")
    assert bronze == 10280 and silver + quarantined + dups == bronze
    assert (quarantined, dups) == (30, 195)  # + 6 low-information pairs kept and flagged possible_duplicate
    snapshot = scalar(container, SILVER_FINGERPRINT)

    second = run(container)
    assert second["stages"]["bronze"]["rows_out"] == 0  # identical file: nothing re-landed
    assert scalar(container, "SELECT count(*) FROM bronze.tickets_raw") == bronze
    assert scalar(container, SILVER_FINGERPRINT) == snapshot
    assert scalar(container, "SELECT count(*) FROM gold.fct_tickets") == silver


def test_seeded_reference_data_resolves_categories(container):
    run(container)
    unresolved = scalar(container, "SELECT count(*) FROM silver.tickets WHERE category = 'unknown'")
    assert unresolved / scalar(container, "SELECT count(*) FROM silver.tickets") < 0.01


def test_lineage_and_events_are_recorded(container):
    result = run(container)
    with container.db.transaction() as conn:
        row = conn.execute("""SELECT t._run_id, t._bronze_id, b.source_file, b.row_hash = t._row_hash AS same_hash
                              FROM silver.tickets t JOIN bronze.tickets_raw b ON b.bronze_id = t._bronze_id
                              LIMIT 1""").fetchone()
        events = {r["event_type"] for r in conn.execute("SELECT event_type FROM ops.outbox WHERE aggregate_id = %s",
                                                        (result["run_id"],))}
        tags = {r["column_name"]: r["sensitivity"] for r in conn.execute("SELECT * FROM ops.column_catalog")}
    assert str(row["_run_id"]) == result["run_id"] and row["same_hash"] and row["source_file"] == "raw_tickets.csv"
    assert {"pipeline.run.started", "pipeline.stage.completed", "pipeline.run.completed"} <= events
    assert tags["submitted_by"] == "pii"


def test_critical_quality_check_blocks_gold_publication(container):
    run(container)
    gold_before = scalar(container, "SELECT max(_built_at) FROM gold.fct_tickets")
    with container.db.transaction() as conn:
        conn.execute("""INSERT INTO ref.dq_checks (check_id, description, rationale, severity, violation_predicate,
                                                   threshold, source, approved_by)
                        VALUES ('always_fails', 'x', 'test', 'critical', 'true', 0, 'test', 'test')""")
    with pytest.raises(QualityGateBlocked):
        run(container)
    assert scalar(container, "SELECT max(_built_at) FROM gold.fct_tickets") == gold_before  # old gold kept
    assert scalar(container, "SELECT status FROM ops.pipeline_runs ORDER BY created_at DESC LIMIT 1") == "failed"


def test_schema_drift_is_detected_without_breaking_ingestion(container, tmp_path):
    run(container)
    drifted = tmp_path / "drifted.csv"
    drifted.write_text("ticket_id,created_at,category,description,building,floor_area\n"
                       "TKT-90001,2025-06-30 10:00:00,Roof,Roof leaking into lobby,Tower A,120\n")
    result = run(container, source_path=str(drifted))
    with container.db.transaction() as conn:
        drift = conn.execute("SELECT schema_drift FROM ops.ingestion_manifest WHERE source_path = %s",
                             (str(drifted),)).fetchone()["schema_drift"]
        payload = conn.execute("SELECT payload FROM bronze.tickets_raw WHERE source_file = 'drifted.csv'"
                               ).fetchone()["payload"]
    assert "floor_area" in drift["added"] and "assigned_to" in drift["removed"]
    assert payload["floor_area"] == "120"  # schema-on-read: the new column is preserved, not dropped
    assert any("schema drift" in a for a in result["alerts"])


def test_new_values_go_through_the_review_gate(container, tmp_path):
    run(container)
    new = tmp_path / "new.csv"
    new.write_text("ticket_id,created_at,category,description,building\n"
                   "TKT-90002,2025-06-30 10:00:00,Roofing,Roof leaking into lobby after storm,Tower A\n")
    run(container, source_path=str(new))
    with container.db.transaction() as conn:
        t = conn.execute("SELECT category, category_source, dq_flags FROM silver.tickets WHERE ticket_id = 'TKT-90002'"
                         ).fetchone()
        pending = conn.execute("SELECT kind, status, review_reason FROM ops.agent_proposals WHERE subject = 'roofing'"
                               ).fetchone()
    # offline mode: the rule-based guess is never auto-approved, so the ticket waits for a human
    assert pending["status"] == "proposed" and "human review" in pending["review_reason"]
    assert t["category"] == "unknown" and t["category_source"] == "unresolved"

    from medallion.agents.proposals import ProposalRepository
    repo = ProposalRepository(container.events)
    with container.db.transaction() as conn:
        pid = conn.execute("SELECT proposal_id FROM ops.agent_proposals WHERE subject = 'roofing'").fetchone()
        decided = repo.decide(conn, str(pid["proposal_id"]), approve=True, reviewer="test",
                              overrides={"category": "general_maintenance", "label_kind": "category_label"})
    assert decided["proposal"]["human_overrides"]["category"] == "general_maintenance"
    run(container)
    assert scalar(container, "SELECT category FROM silver.tickets WHERE ticket_id = 'TKT-90002'") == \
        "general_maintenance"

    # a later human correction of an approved mapping is its own audited proposal
    with container.db.transaction() as conn:
        repo.correct(conn, "category_label", "roofing", {"category": "plumbing"}, reviewer="test")
        history = conn.execute("SELECT status, reviewed_by FROM ops.agent_proposals WHERE subject = 'roofing' "
                               "ORDER BY created_at").fetchall()
    assert [h["status"] for h in history] == ["approved", "approved"]
    run(container)
    assert scalar(container, "SELECT category FROM silver.tickets WHERE ticket_id = 'TKT-90002'") == "plumbing"
