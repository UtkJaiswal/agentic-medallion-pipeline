# Scaling and tradeoffs

[← back to README](../README.md)

## What changes at 100x

Scenario: 1M+ rows, daily incremental loads, late-arriving updates.

**Ingestion and storage**
- Land files in object storage and keep bronze as Parquet/Iceberg (or Delta), partitioned by
  `ingest_date`, with the same lineage columns. Postgres stays for `ops` and serving gold.
- The content-addressed manifest already makes daily files idempotent. If the source becomes a
  CDC stream, Kafka moves to the ingestion side and bronze becomes an append of change events.

**Silver becomes incremental (the biggest change)**
- Process only bronze rows from new manifests, then `MERGE` into silver on `ticket_id`. `row_hash`
  skips unchanged rows, and the latest event per ticket wins (already the D1 rule).
- Handle late-arriving and corrected records with a **replay window**: re-derive only the
  `created_at` month partitions touched by recent landings (for example the last 7 days), never the
  whole table. Silver stays a deterministic function of bronze, so a backfill is "replay these
  partitions", with no double counting.
- Duplicate detection moves from in-memory to a persisted fingerprint table (`fingerprint → survivor`)
  checked by index lookup.
- Move the transform from Python rows to set-based SQL (Postgres or DuckDB) or Spark. The rules are
  already isolated as pure functions, which makes the port mechanical and keeps the unit tests as
  the spec.

**Gold**
- Rebuild per month partition, or use incremental materialized views. Ship the backlog snapshot daily
  as an append-only history table (`as_of_date` is already a column), which gives trend lines for free.

**LLM cost and scale**

The classification design already scales with **distinct values, not rows**:

| | 10k rows (this data) | 10M rows (estimate) |
|---|---|---|
| distinct descriptions | 4,616 | ~millions |
| distinct *templates* (what the LLM sees) | **92** | low thousands (new templates grow slowly) |
| distinct category labels | 92 | hundreds |
| LLM calls per daily run after backfill | ~0 (all cached or approved) | only new templates: tens per day |

- **Batch**: 25 items per call by default (10 for small local models), bounded concurrency,
  token-bucket rate limiting.
- **Cache**: by (model, prompt version, prompt). **Skip**: anything already in the approved maps, and
  anything with a pending proposal.
- **Budget guard**: tokens and USD per run. When it's exhausted the agent degrades to rules, and the
  run doesn't fail.
- For a one-off backfill of tens of thousands of templates, use provider **batch APIs** (about 50%
  cheaper, latency irrelevant) or a self-hosted **vLLM** endpoint. That's the point where a GPU beats
  per-token pricing.
- **Routing for cost:** measured on this task, Jev Router matched a fixed frontier model at 3–4×
  lower cost ([evaluation](EVALUATION.md#classification-strategies)). At 10M rows that ratio matters
  more than the last point of accuracy. Native Jev is another 7–10× cheaper again for categories
  (one typed call per value, no output tokens billed); at that volume, a split worth testing is native
  Jev for category plus a generative model only for the free-text `issue_type`.
- **Auto-approval at scale:** human-weighted trust automated 78% of review decisions at 96.6% precision
  ([evaluation](EVALUATION.md#llm-as-judge--human-weighted-trust)). Review effort grows with *new*
  values, not rows.
- The residual long tail (descriptions that don't template well) is where a cheap embedding
  nearest-neighbour step against already-labelled templates would remove most remaining LLM calls.
  I'd add it only when measurement shows the tail is big.
- **DQ and gold-design agents** don't run per load. DQ runs on schema drift or profile drift (null-rate
  alerts already exist). Gold design runs on demand. Their cost is negligible at any scale.

**Operations**
- Replace the home-grown worker with Dagster or Airflow (asset-based scheduling, backfills, SLAs).
  The stage boundaries map 1:1 to assets.
- The API rate limiter is in-process (fine for one replica). With several replicas, move it to Redis.
- Ship `ops.stage_runs` and `ops.llm_calls` metrics to Prometheus/Grafana and page on the existing
  thresholds.

## Tradeoffs and what I would do differently

- **Python row transform instead of set-based SQL in silver.** At 10k rows it's sub-second, and it
  makes every rule a pure, unit-tested function, which is worth more here than raw speed. It is the
  first thing I'd port at 100x (see above); the tests become the port's spec.
- **Full deterministic rebuild instead of incremental MERGE.** It's the simplest correct form of
  idempotency, and it fits one source file. Incremental with a replay window is the 100x design.
- **Seeds are checked into git.** That's deliberate: approved reference data is configuration, and a
  mapping change should show up in code review. The cost is a two-step workflow (approve in the DB,
  then `medallion export-seeds` + PR). A team would put a small review UI on the existing
  `/v1/proposals` API.
- **Auto-approval policy is coarse** (one confidence threshold, LLM answers only, never for DQ checks
  or gold models). Model confidence is poorly calibrated, especially in small local models. In
  production I'd calibrate the threshold per model against the eval set, and sample auto-approved
  items for spot checks.
- **The keyword baseline is overfit to this file.** I wrote its keywords after profiling this exact
  data, which is why it scores ~96–99% here and ~27% on the holdout set. I kept it because that
  contrast is the most honest way to show what the LLM is actually for.
- **Memory is not a general accuracy lever.** Measured: it helps a small model on recurring data and
  does nothing on novel phrasing or for a strong model. I kept it because recurring data is the
  production case.
- **Vendor qualifications are assumed from names.** The 75% "outside qualification" finding needs
  the facilities team to confirm `ontology.yaml` before it drives alerts.
- **I didn't use an agent framework** (LangGraph, CrewAI). Each agent is a single structured call plus
  deterministic verification. No step needs model-driven tool selection, so a framework would add
  abstraction without capability. That would change if an agent had to explore, e.g. iteratively
  query the data to test its own hypotheses.
- **One model for everything in the chain.** Classification could run on a cheaper model than DQ or
  gold design. The factory can already build a router for any provider subset; I'd split them once
  the eval shows the cheaper model holds accuracy.
- **Rate limiting and circuit breakers are per process.** Fine for one worker and one API replica;
  shared state (Redis) is needed beyond that.
