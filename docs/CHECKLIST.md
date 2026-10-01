# Brief checklist

[← back to README](../README.md)

Every requirement in [the brief](ASSIGNMENT.md), and where it is met.

## Part 1: medallion pipeline

| Requirement | Status | Where |
|---|---|---|
| Bronze: raw ingestion, schema-on-read, no data loss | ✅ | verbatim jsonb payload; reconciliation asserted every run ([data pipeline](DATA_PIPELINE.md#bronze-raw-schema-on-read-no-data-loss)) |
| Bronze lineage: source file, `ingested_at`, row hash | ✅ | `bronze.tickets_raw`: `source_file`, `source_file_sha256`, `source_row_number`, `ingested_at`, `row_hash`, `run_id` |
| Silver: cleansed, deduplicated, typed, validated | ✅ | `silver.tickets` + quarantine + duplicates tables; quality gate |
| Silver: cleaning rules documented with reasons | ✅ | [CLEANING_RULES.md](CLEANING_RULES.md) |
| Gold: 2–3 business models, justified **in the README** | ✅ | 3 marts + star schema, justified in the [README](../README.md#layers-and-gold-models-why-these); full detail in [data pipeline](DATA_PIPELINE.md#gold-business-ready-models) |
| Clear layer separation, choice explained | ✅ | Postgres schemas `bronze` / `silver` / `gold` / `ref` / `ops` ([data pipeline](DATA_PIPELINE.md#storage-and-layer-separation)) |
| Idempotent and re-runnable | ✅ | content-addressed bronze, deterministic rebuilds; tested by running twice and comparing fingerprints |
| Clear logging per stage | ✅ | structured logs with trace/run/stage ids; `ops.stage_runs` metrics |
| Messiness handled without manual intervention | ✅ | all rules automatic; human review is only for *agent suggestions* about new values, never for running the pipeline |
| `data/raw_tickets.csv` not modified | ✅ | ingested as-is; sha256 `2bf36d21…` recorded in `ops.ingestion_manifest` |

## Part 2: agents (at least two of a–d)

| Option | Status | Where |
|---|---|---|
| (a) Schema inference & evolution | ⚪ agent not built; **drift detection (the bonus) done deterministically** in bronze | reasoning in [README](../README.md#agent-assessment-summary) |
| (b) Data quality: profile, NL rules, SQL, *why* it matters | ✅ | [AGENTS.md §2](AGENTS.md#2-data-quality-agent-option-b) |
| (c) Semantic classification: enrichment into silver columns, at scale | ✅ | [AGENTS.md §1](AGENTS.md#1-semantic-classification-agent-option-c-the-one-that-earns-its-keep) |
| (d) Gold design: marts + SQL, trust vs override | ✅ | [AGENTS.md §3](AGENTS.md#3-gold-layer-design-agent-option-d) |

## Evaluation criteria

| Area | Where to look |
|---|---|
| Medallion design | [DATA_PIPELINE.md](DATA_PIPELINE.md), [diagrams](diagrams/data-flow.svg) |
| Agent usefulness, honestly | [AGENTS.md](AGENTS.md): each agent has "did it save time?", including where it didn't |
| Prompt engineering | versioned prompts in `src/medallion/agents/prompts/`; guardrails in [AI_PLATFORM.md](AI_PLATFORM.md#guardrails); the v3 → v4 iteration in [EVALUATION.md](EVALUATION.md#prompt-iteration-v3--v4) |
| Cost and scale (10M rows) | [SCALING.md](SCALING.md); measured USD costs throughout [EVALUATION.md](EVALUATION.md) |
| Tradeoff awareness | [SCALING.md](SCALING.md#tradeoffs-and-what-i-would-do-differently); every A/B reports losses too |
| Code quality | 172 tests, lint-clean; [core vs optional](../README.md#core-vs-optional-to-keep-it-from-being-overkill) |

## Required in the README

| Requirement | Where |
|---|---|
| Architecture diagram | [README](../README.md) (SVG), plus 4 more in [ARCHITECTURE.md](ARCHITECTURE.md) |
| Agent assessment: what, sample input/output, honest take | [README summary](../README.md#agent-assessment-summary) + [AGENTS.md](AGENTS.md) |
| What changes at 100x | [README summary](../README.md#what-changes-at-100x-summary) + [SCALING.md](SCALING.md) |
| How to run: single command | `docker compose up --build` ([README](../README.md#run-it-one-command)) |

## Good to have

| Item | Status | Where |
|---|---|---|
| Data lineage end to end (who/what/when) | ✅ | [DATA_PIPELINE.md](DATA_PIPELINE.md#lineage-and-observability) |
| Metadata auto-tagging at landing | ✅ | `ops.column_catalog` (semantic type, sensitivity, PII hints) |
| Cross-source reconciliation | ✅ partial | bronze ↔ silver reconciliation every run; cross-file duplicate detection and schema drift across files (exercised with synthetic files) |
| Dimensional model recommendation | ✅ | conformed star schema in gold; the gold design agent proposes further marts |
| Agent evaluation harness | ✅ | accuracy, macro-F1, format compliance, latency, USD cost, promotion gate ([EVALUATION.md](EVALUATION.md)) |
| Human-in-the-loop approval gates | ✅ | every agent proposal; DQ checks and gold models are never auto-approved |
| Incremental + backfill strategy | ✅ designed, partly built | latest-wins re-delivery and content-addressed files are built; replay windows designed in [SCALING.md](SCALING.md) |
| Observability and SLA thinking | ✅ | stage metrics, null-rate drift, LLM failure rate and USD spend, alert thresholds ([OPERATIONS.md](OPERATIONS.md#observability)) |
