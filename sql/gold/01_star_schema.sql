-- Conformed star schema at ticket grain. Natural keys (category, building, assignee, date) are used
-- deliberately: they are stable across full rebuilds, so downstream joins and bookmarks never break.
-- Parameters: current_setting('medallion.run_id'), current_setting('medallion.as_of')::date

DROP TABLE IF EXISTS gold.dim_date, gold.dim_category, gold.dim_building, gold.dim_assignee, gold.fct_tickets CASCADE;

CREATE TABLE gold.dim_category AS
SELECT category, description
FROM ref.category_taxonomy;

CREATE TABLE gold.dim_building AS
SELECT DISTINCT building
FROM silver.tickets
WHERE building IS NOT NULL;

CREATE TABLE gold.dim_assignee AS
SELECT DISTINCT assignee, assignee_type
FROM silver.tickets
WHERE assignee IS NOT NULL;

CREATE TABLE gold.dim_date AS
SELECT d::date                         AS date_key,
       extract(isoyear FROM d)::int    AS iso_year,
       extract(week FROM d)::int       AS iso_week,
       date_trunc('month', d)::date    AS month,
       extract(isodow FROM d)::int     AS iso_dow,
       extract(isodow FROM d) >= 6     AS is_weekend
FROM generate_series(
        (SELECT min(created_at)::date FROM silver.tickets),
        (SELECT greatest(max(created_at), max(resolved_at))::date FROM silver.tickets),
        interval '1 day') AS d;

CREATE TABLE gold.fct_tickets AS
SELECT t.ticket_id,
       t.created_at::date                                        AS created_date,
       t.resolved_at::date                                       AS resolved_date,
       t.category, t.building, t.assignee, t.assignee_type,
       t.priority, t.status, t.is_open,
       t.inferred_severity, t.is_safety_hazard, t.issue_type,
       t.resolution_hours, t.sla_hours, t.sla_met, t.cost_usd,
       t.resolution_outcome, t.is_duplicate_closure,
       -- explicit coverage flags so every metric can state what share of tickets it is based on
       (t.resolution_hours IS NOT NULL AND t.sla_hours IS NOT NULL AND NOT t.is_duplicate_closure) AS sla_measurable,
       CASE WHEN t.is_open AND t.created_at IS NOT NULL
            THEN round(extract(epoch FROM (current_setting('medallion.as_of')::date + 1 - t.created_at)) / 3600, 1)
       END                                                       AS open_age_hours,
       current_setting('medallion.run_id')::uuid                 AS _run_id,
       now()                                                     AS _built_at
FROM silver.tickets t;

ALTER TABLE gold.dim_category ADD PRIMARY KEY (category);
ALTER TABLE gold.dim_building ADD PRIMARY KEY (building);
ALTER TABLE gold.dim_assignee ADD PRIMARY KEY (assignee);
ALTER TABLE gold.dim_date     ADD PRIMARY KEY (date_key);
ALTER TABLE gold.fct_tickets  ADD PRIMARY KEY (ticket_id);
