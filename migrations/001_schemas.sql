-- Layer separation is enforced with Postgres schemas:
--   bronze : raw, append-only, schema-on-read (jsonb payload) + lineage columns
--   silver : cleansed, typed, deduplicated tickets + quarantine + duplicate audit
--   gold   : business-ready star schema and marts (rebuilt atomically each run)
--   ref    : curated reference data (taxonomy, approved agent mappings, approved DQ checks)
--   ops    : pipeline metadata - runs/jobs, stage metrics, lineage, LLM usage & cache, outbox
CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;
CREATE SCHEMA IF NOT EXISTS ref;
CREATE SCHEMA IF NOT EXISTS ops;

-- ============================================================================ ops
-- A pipeline run doubles as a job-queue entry (queued -> running -> succeeded|failed).
CREATE TABLE ops.pipeline_runs (
    run_id            uuid PRIMARY KEY,
    trace_id          text        NOT NULL,
    status            text        NOT NULL CHECK (status IN
                        ('queued','running','succeeded','succeeded_with_warnings','failed')),
    trigger           text        NOT NULL,            -- cli | api
    params            jsonb       NOT NULL DEFAULT '{}',
    idempotency_key   text        UNIQUE,              -- API Idempotency-Key header
    request_hash      text,                            -- detects key reuse with a different body
    attempts          int         NOT NULL DEFAULT 0,
    max_attempts      int         NOT NULL DEFAULT 3,
    not_before        timestamptz NOT NULL DEFAULT now(), -- retry backoff for queued jobs
    lease_expires_at  timestamptz,                      -- crashed-worker recovery
    worker_id         text,
    error             text,
    summary           jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    started_at        timestamptz,
    finished_at       timestamptz
);
CREATE INDEX pipeline_runs_queue_idx ON ops.pipeline_runs (status, not_before);

CREATE TABLE ops.stage_runs (
    id             bigserial PRIMARY KEY,
    run_id         uuid NOT NULL REFERENCES ops.pipeline_runs (run_id),
    stage          text NOT NULL,
    status         text NOT NULL,
    rows_in        bigint,
    rows_out       bigint,
    rows_rejected  bigint,
    metrics        jsonb NOT NULL DEFAULT '{}',
    alerts         jsonb NOT NULL DEFAULT '[]',
    error          text,
    started_at     timestamptz NOT NULL,
    finished_at    timestamptz
);
CREATE INDEX stage_runs_run_idx ON ops.stage_runs (run_id);

-- One row per landed file (content-addressed): re-landing identical bytes is a no-op.
CREATE TABLE ops.ingestion_manifest (
    source_name         text NOT NULL,
    file_sha256         text NOT NULL,
    source_path         text NOT NULL,
    header              jsonb NOT NULL,
    header_fingerprint  text NOT NULL,
    row_count           int  NOT NULL,
    schema_drift        jsonb,
    run_id              uuid NOT NULL,
    ingested_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_name, file_sha256)
);

-- Metadata auto-tagging at landing (sensitivity / semantic type per column).
CREATE TABLE ops.column_catalog (
    source_name    text NOT NULL,
    column_name    text NOT NULL,
    semantic_type  text NOT NULL,
    tags           text[] NOT NULL DEFAULT '{}',
    sensitivity    text NOT NULL,      -- public | internal | pii
    evidence       jsonb NOT NULL DEFAULT '{}',
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_name, column_name)
);

-- Column profiles per run: the data-quality agent's input and the null-rate drift baseline.
CREATE TABLE ops.column_profiles (
    run_id       uuid NOT NULL,
    layer        text NOT NULL,
    table_name   text NOT NULL,
    column_name  text NOT NULL,
    profile      jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, layer, table_name, column_name)
);

-- Every LLM attempt (including failures and cache hits) for cost / latency / failure-rate tracking.
CREATE TABLE ops.llm_calls (
    id             bigserial PRIMARY KEY,
    run_id         uuid,
    trace_id       text,
    task           text NOT NULL,
    provider       text NOT NULL,
    model          text NOT NULL,
    status         text NOT NULL,      -- ok | cache_hit | error | invalid_output | refused
    input_tokens   int  NOT NULL DEFAULT 0,
    output_tokens  int  NOT NULL DEFAULT 0,
    cost_usd       numeric(12,6),
    latency_ms     int,
    error          text,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX llm_calls_run_idx ON ops.llm_calls (run_id);

-- Response cache keyed by (model, prompt version, exact prompt): reruns never pay twice.
-- Only schema-validated responses are cached.
CREATE TABLE ops.llm_cache (
    cache_key       text PRIMARY KEY,
    provider        text NOT NULL,
    model           text NOT NULL,
    task            text NOT NULL,
    prompt_version  text NOT NULL,
    response_text   text NOT NULL,
    input_tokens    int  NOT NULL,
    output_tokens   int  NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Agent suggestions awaiting (or past) human review: the human-in-the-loop gate.
CREATE TABLE ops.agent_proposals (
    proposal_id     uuid PRIMARY KEY,
    agent           text NOT NULL,     -- classification | data_quality | gold_design
    kind            text NOT NULL,     -- category_label | description_template | dq_check | gold_model
    subject         text NOT NULL,     -- what the proposal is about (label, template, check id, model name)
    proposal        jsonb NOT NULL,
    confidence      numeric(4,3),
    status          text NOT NULL CHECK (status IN ('proposed','approved','auto_approved','rejected')),
    review_reason   text,
    provider        text,
    model           text,
    prompt_version  text,
    run_id          uuid,
    trace_id        text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    reviewed_by     text,
    reviewed_at     timestamptz
);
CREATE INDEX agent_proposals_open_idx ON ops.agent_proposals (agent, status);

CREATE TABLE ops.dq_results (
    run_id          uuid NOT NULL,
    check_id        text NOT NULL,
    severity        text NOT NULL,
    total_rows      bigint NOT NULL,
    violations      bigint NOT NULL,
    violation_rate  numeric(8,6) NOT NULL,
    threshold       numeric(8,6) NOT NULL,
    passed          boolean NOT NULL,
    sample          jsonb NOT NULL DEFAULT '[]',
    checked_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, check_id)
);

-- Transactional outbox: events are written in the same transaction as the state change they
-- describe, then relayed to Kafka (if enabled) by a separate process. No dual-write problem.
CREATE TABLE ops.outbox (
    event_id      uuid PRIMARY KEY,
    event_type    text NOT NULL,
    aggregate_id  text NOT NULL,
    payload       jsonb NOT NULL,
    trace_id      text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    published_at  timestamptz
);
CREATE INDEX outbox_unpublished_idx ON ops.outbox (created_at) WHERE published_at IS NULL;

-- ============================================================================ ref
CREATE TABLE ref.category_taxonomy (
    category     text PRIMARY KEY,
    description  text NOT NULL
);

-- Raw category label (normalised) -> canonical category.
CREATE TABLE ref.category_label_map (
    label         text PRIMARY KEY,
    label_kind    text NOT NULL CHECK (label_kind IN ('category_label','generic_label','description_text','junk')),
    category      text NOT NULL REFERENCES ref.category_taxonomy (category),
    confidence    numeric(4,3) NOT NULL,
    source        text NOT NULL,       -- seed | llm:<provider>/<model> | rules | human
    proposal_id   uuid,
    approved_by   text NOT NULL,
    approved_at   timestamptz NOT NULL DEFAULT now()
);

-- Description template (numbers / building names / asset ids masked) -> semantic enrichment.
CREATE TABLE ref.description_template_map (
    template           text PRIMARY KEY,
    category           text NOT NULL REFERENCES ref.category_taxonomy (category),
    issue_type         text NOT NULL,
    severity           text NOT NULL CHECK (severity IN ('low','medium','high','critical')),
    is_safety_hazard   boolean NOT NULL,
    confidence         numeric(4,3) NOT NULL,
    source             text NOT NULL,
    proposal_id        uuid,
    approved_by        text NOT NULL,
    approved_at        timestamptz NOT NULL DEFAULT now()
);

-- Approved data-quality checks, executed against silver on every run.
CREATE TABLE ref.dq_checks (
    check_id     text PRIMARY KEY,
    description  text NOT NULL,
    rationale    text NOT NULL,
    severity     text NOT NULL CHECK (severity IN ('info','warning','critical')),
    violation_predicate text NOT NULL,  -- boolean SQL over silver.tickets, TRUE = violating row
    threshold    numeric(8,6) NOT NULL, -- max tolerated violation rate
    source       text NOT NULL,
    enabled      boolean NOT NULL DEFAULT true,
    approved_by  text NOT NULL,
    approved_at  timestamptz NOT NULL DEFAULT now()
);

-- ============================================================================ bronze
CREATE TABLE bronze.tickets_raw (
    bronze_id           bigserial PRIMARY KEY,
    source_name         text   NOT NULL,
    source_file         text   NOT NULL,
    source_file_sha256  text   NOT NULL,
    source_row_number   int    NOT NULL,   -- 1-based data row (header excluded)
    payload             jsonb  NOT NULL,   -- every field exactly as received, as text
    row_hash            text   NOT NULL,   -- sha256 of the canonical payload: content dedup tracking
    ingested_at         timestamptz NOT NULL DEFAULT now(),
    run_id              uuid   NOT NULL,
    UNIQUE (source_file_sha256, source_row_number)
);
CREATE INDEX tickets_raw_row_hash_idx ON bronze.tickets_raw (row_hash);

-- Convenience projection for humans and SQL; bronze itself stays schema-on-read.
CREATE VIEW bronze.v_tickets_raw AS
SELECT bronze_id, source_file, source_row_number, row_hash, ingested_at, run_id,
       payload->>'ticket_id' AS ticket_id, payload->>'created_at' AS created_at,
       payload->>'resolved_at' AS resolved_at, payload->>'category' AS category,
       payload->>'priority' AS priority, payload->>'status' AS status,
       payload->>'building' AS building, payload->>'description' AS description,
       payload->>'submitted_by' AS submitted_by, payload->>'assigned_to' AS assigned_to,
       payload->>'resolution_notes' AS resolution_notes, payload->>'cost' AS cost,
       payload->>'sla_hours' AS sla_hours
FROM bronze.tickets_raw;

-- ============================================================================ silver
CREATE TABLE silver.tickets (
    ticket_id                 text PRIMARY KEY,
    ticket_number             int  NOT NULL,
    created_at                timestamp,      -- source has no timezone; stored as site-local wall time
    resolved_at               timestamp,
    resolution_hours          numeric(10,2),  -- NULL when not resolved or resolved_at < created_at
    status                    text,
    is_open                   boolean,
    priority                  text CHECK (priority IN ('low','medium','high','critical')),
    category                  text NOT NULL REFERENCES ref.category_taxonomy (category),
    category_source           text NOT NULL,  -- label_map | description_template | unresolved
    category_confidence       numeric(4,3),
    category_from_description text,
    issue_type                text,
    inferred_severity         text,
    is_safety_hazard          boolean,
    building                  text,
    floor                     int,
    room                      text,
    asset_id                  text,
    description               text,
    description_template      text,
    submitted_by              text,
    assignee                  text,
    assignee_type             text CHECK (assignee_type IN ('in_house','vendor')),
    resolution_notes          text,
    resolution_outcome        text,
    root_cause                text,
    part_replaced             text,
    eta_business_days         int,
    onsite_hours              numeric(6,2),
    is_duplicate_closure      boolean NOT NULL DEFAULT false,
    duplicate_of_ticket_id    text,
    cost_usd                  numeric(12,2),
    sla_hours                 int,
    sla_met                   boolean,
    dq_flags                  text[] NOT NULL DEFAULT '{}',
    raw                       jsonb NOT NULL,  -- original values, for audit
    _bronze_id                bigint NOT NULL,
    _row_hash                 text   NOT NULL,
    _source_file              text   NOT NULL,
    _run_id                   uuid   NOT NULL,
    _processed_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX silver_tickets_created_idx ON silver.tickets (created_at);
CREATE INDEX silver_tickets_category_idx ON silver.tickets (category);

CREATE TABLE silver.tickets_quarantine (
    _bronze_id    bigint PRIMARY KEY,
    ticket_id_raw text,
    reasons       text[] NOT NULL,
    raw           jsonb NOT NULL,
    _run_id       uuid NOT NULL,
    _processed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE silver.ticket_duplicates (
    _bronze_id           bigint PRIMARY KEY,
    duplicate_ticket_id  text NOT NULL,
    survivor_ticket_id   text NOT NULL,
    match_rule           text NOT NULL,
    _run_id              uuid NOT NULL
);
