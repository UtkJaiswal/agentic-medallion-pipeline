"""API idempotency, background worker semantics, outbox relay and agents - against real Postgres."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from medallion.agents.data_quality import DataQualityAgent
from medallion.agents.gold_design import GoldDesignAgent
from medallion.agents.profiler import profile_table
from medallion.agents.proposals import ProposalRepository
from medallion.api.app import create_app
from medallion.events.relay import OutboxRelay
from medallion.jobs.worker import Worker
from medallion.llm.cache import InMemoryLLMCache
from medallion.llm.router import LLMRouter
from medallion.llm.types import LLMProvider, LLMResponse
from medallion.pipeline.runner import create_run
from medallion.resilience.retry import TransientError

pytestmark = pytest.mark.integration


@pytest.fixture
def api(container):
    with TestClient(create_app(container)) as client:
        yield client


# ------------------------------------------------------------------------------------------ API
def test_idempotency_key_semantics(api):
    key = f"key-{uuid.uuid4()}"
    first = api.post("/v1/pipeline-runs", json={}, headers={"Idempotency-Key": key})
    assert first.status_code == 202 and first.headers["Location"].endswith(first.json()["run_id"])
    replay = api.post("/v1/pipeline-runs", json={}, headers={"Idempotency-Key": key})
    assert replay.status_code == 200 and replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json()["run_id"] == first.json()["run_id"]
    conflict = api.post("/v1/pipeline-runs", json={"as_of": "2025-01-01"}, headers={"Idempotency-Key": key})
    assert conflict.status_code == 422
    assert api.post("/v1/pipeline-runs", json={}).status_code == 422  # key is mandatory


def test_trace_id_is_propagated_and_echoed(api, container):
    trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    r = api.post("/v1/pipeline-runs", json={}, headers={"Idempotency-Key": f"k-{uuid.uuid4()}",
                                                         "traceparent": f"00-{trace}-00f067aa0ba902b7-01"})
    assert r.headers["X-Trace-Id"] == trace and r.json()["trace_id"] == trace


def test_path_traversal_is_rejected(api):
    r = api.post("/v1/pipeline-runs", json={"source_path": "../../etc/passwd"},
                 headers={"Idempotency-Key": f"k-{uuid.uuid4()}"})
    assert r.status_code == 422


def test_rate_limit_returns_429_with_retry_after(container):
    container.settings.api_rate_limit_rpm = 2
    with TestClient(create_app(container)) as client:
        codes = [client.post("/v1/pipeline-runs", json={}, headers={"Idempotency-Key": f"k-{uuid.uuid4()}"})
                 for _ in range(3)]
    assert [c.status_code for c in codes][-1] == 429 and "Retry-After" in codes[-1].headers


def test_api_key_is_enforced_when_configured(container):
    from pydantic import SecretStr
    container.settings.api_key = SecretStr("s3cret")
    with TestClient(create_app(container)) as client:
        assert client.get("/v1/pipeline-runs").status_code == 401
        assert client.get("/v1/pipeline-runs", headers={"X-API-Key": "s3cret"}).status_code == 200


# ------------------------------------------------------------------------------------------ worker
def test_worker_executes_queued_run(api, container):
    run_id = api.post("/v1/pipeline-runs", json={}, headers={"Idempotency-Key": f"k-{uuid.uuid4()}"}).json()["run_id"]
    worker = Worker(container, "w1")
    job = worker.claim()
    assert str(job["run_id"]) == run_id and worker.claim() is None  # SKIP LOCKED: nothing else to claim
    worker.process(job)
    view = api.get(f"/v1/pipeline-runs/{run_id}").json()
    assert view["status"].startswith("succeeded") and [s["stage"] for s in view["stages"]] == \
        ["bronze", "silver", "quality_gate", "gold"]


def test_worker_requeues_transient_failure_with_backoff(api, container, monkeypatch):
    run_id = api.post("/v1/pipeline-runs", json={}, headers={"Idempotency-Key": f"k-{uuid.uuid4()}"}).json()["run_id"]
    worker = Worker(container, "w1")

    def boom(_):
        raise TransientError("database blip")

    monkeypatch.setattr(container.runner, "execute", boom)
    worker.process(worker.claim())
    with container.db.transaction() as conn:
        row = conn.execute("SELECT status, attempts, not_before > now() AS delayed, error FROM ops.pipeline_runs "
                           "WHERE run_id = %s", (run_id,)).fetchone()
    assert row["status"] == "queued" and row["attempts"] == 1 and row["delayed"] and "blip" in row["error"]
    assert worker.claim() is None  # not claimable until its backoff has elapsed


def test_crashed_worker_lease_is_reclaimed(api, container):
    run_id = api.post("/v1/pipeline-runs", json={}, headers={"Idempotency-Key": f"k-{uuid.uuid4()}"}).json()["run_id"]
    Worker(container, "dead").claim()
    with container.db.transaction() as conn:
        conn.execute("UPDATE ops.pipeline_runs SET lease_expires_at = now() - interval '1 second' WHERE run_id = %s",
                     (run_id,))
    job = Worker(container, "alive").claim()
    assert str(job["run_id"]) == run_id and job["attempts"] == 2


# ------------------------------------------------------------------------------------------ relay
class FakeProducer:
    def __init__(self, fail_ids=()):
        self.sent, self.fail_ids = [], set(fail_ids)

    def send(self, topic, key, value, headers):
        self.sent.append((topic, key, json.loads(value), headers))

    def flush(self, timeout_s):
        return [h["event_id"] for *_, h in self.sent if h["event_id"] in self.fail_ids]


def test_outbox_relay_marks_only_acknowledged_events(container):
    with container.db.transaction() as conn:
        ids = [container.events.publish(conn, "test.event", "agg", {"i": i}) for i in range(3)]
    relay = OutboxRelay(container.db, FakeProducer(fail_ids=[ids[1]]), "topic")
    assert relay.relay_once() == 2
    with container.db.transaction() as conn:
        rows = conn.execute("SELECT event_id FROM ops.outbox WHERE published_at IS NULL")
        pending = [str(r["event_id"]) for r in rows]
    assert pending == [ids[1]]  # the failed one is retried on the next pass (at-least-once)


# ------------------------------------------------------------------------------------------ agents
class Canned(LLMProvider):
    name, model = "canned", "m"

    def __init__(self, payload):
        self.payload = payload

    def complete(self, request):
        return LLMResponse(json.dumps(self.payload), self.name, self.model)


def _profile(container):
    with container.db.transaction() as conn:
        header = conn.execute("SELECT header FROM ops.ingestion_manifest LIMIT 1").fetchone()["header"]
        return profile_table(conn, header)


def test_dq_agent_verifies_and_gates_proposals(container):
    container.runner.execute(create_run(container.db, trigger="test", params={}, trace_id="t" * 32))
    rules = {"rules": [
        {"check_id": "cost_negative", "column": "cost", "issue": "192 values are -1/-999", "rationale": "spend",
         "action": "set_null", "cleaning_logic": "null negatives",
         "cleaning_expression": "CASE WHEN v ~ '^-' THEN NULL ELSE v END", "severity": "critical",
         "violation_predicate": "cost_usd < 0", "threshold": 0.0},
        {"check_id": "evil_rule", "column": "cost", "issue": "x", "rationale": "x", "action": "flag_only",
         "cleaning_logic": "x", "cleaning_expression": "", "severity": "info",
         "violation_predicate": "true; DROP TABLE silver.tickets", "threshold": 0.0},
        {"check_id": "bad_column", "column": "cost", "issue": "x", "rationale": "x", "action": "flag_only",
         "cleaning_logic": "x", "cleaning_expression": "", "severity": "info",
         "violation_predicate": "no_such_column > 1", "threshold": 0.0}]}
    agent = DataQualityAgent(container.db, LLMRouter([Canned(rules)], InMemoryLLMCache()),
                             ProposalRepository(container.events))
    report = agent.run(_profile(container))
    assert report.proposed == 3 and report.rejected_by_guardrails == 2
    with container.db.transaction() as conn:
        rows = {r["subject"]: r for r in conn.execute("SELECT * FROM ops.agent_proposals WHERE agent='data_quality'")}
        assert rows["cost_negative"]["status"] == "proposed"
        assert rows["cost_negative"]["proposal"]["verification"]["predicate"]["violations"] == 0
        assert rows["evil_rule"]["status"] == "rejected" and rows["bad_column"]["status"] == "rejected"
        assert scalar_count(conn, "SELECT count(*) FROM silver.tickets") > 0  # the DROP never ran
        ProposalRepository(container.events).decide(conn, str(rows["cost_negative"]["proposal_id"]),
                                                    approve=True, reviewer="test")
        assert conn.execute("SELECT severity FROM ref.dq_checks WHERE check_id = 'cost_negative'"
                            ).fetchone()["severity"] == "critical"


def test_gold_design_agent_creates_sandbox_view_only_after_approval(container):
    container.runner.execute(create_run(container.db, trigger="test", params={}, trace_id="t" * 32))
    design = {"models": [{"name": "mart_cost_by_building", "business_question": "Where does spend go?",
                          "grain": "one row per building", "rationale": "r", "caveats": "c",
                          "sql": "SELECT building, sum(cost_usd) AS spend, count(cost_usd) AS n "
                                 "FROM silver.tickets GROUP BY building"}]}
    GoldDesignAgent(container.db, LLMRouter([Canned(design)], InMemoryLLMCache()),
                    ProposalRepository(container.events)).run("domain")
    with container.db.transaction() as conn:
        p = conn.execute("SELECT * FROM ops.agent_proposals WHERE agent = 'gold_design'").fetchone()
        assert p["status"] == "proposed" and p["proposal"]["verification"]["rows"] > 0
        assert conn.execute("SELECT to_regclass('gold_sandbox.mart_cost_by_building') AS r").fetchone()["r"] is None
        ProposalRepository(container.events).decide(conn, str(p["proposal_id"]), approve=True, reviewer="test")
        assert conn.execute("SELECT count(*) AS n FROM gold_sandbox.mart_cost_by_building").fetchone()["n"] > 0


def scalar_count(conn, sql):
    return conn.execute(sql).fetchone()["count"]


def test_cleaning_expressions_with_percent_signs_are_evaluated(container):
    from medallion.agents.data_quality import evaluate_cleaning_expression
    out = evaluate_cleaning_expression(container.db, "CASE WHEN v LIKE '%TBD%' THEN NULL ELSE v END", ["TBD", "12"])
    assert out["ok"] and out["examples"] == [["TBD", None], ["12", "12"]]
