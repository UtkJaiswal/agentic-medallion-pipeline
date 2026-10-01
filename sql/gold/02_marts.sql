-- Business marts. Duplicate closures ("Duplicate of ticket #N") are excluded from performance metrics
-- because they represent no work; they are still counted as created volume.

DROP TABLE IF EXISTS gold.mart_sla_performance, gold.mart_vendor_scorecard, gold.mart_open_backlog CASCADE;

-- 1) SLA performance: are we meeting SLAs, by month / category / priority?
CREATE TABLE gold.mart_sla_performance AS
SELECT date_trunc('month', created_date)::date                                     AS month,
       category,
       coalesce(priority, 'unspecified')                                           AS priority,
       count(*)                                                                    AS tickets_created,
       count(*) FILTER (WHERE resolved_date IS NOT NULL AND NOT is_duplicate_closure) AS tickets_resolved,
       count(*) FILTER (WHERE sla_measurable)                                      AS sla_measurable,
       count(*) FILTER (WHERE sla_measurable AND sla_met)                          AS sla_met,
       round(100.0 * count(*) FILTER (WHERE sla_measurable AND sla_met)
             / nullif(count(*) FILTER (WHERE sla_measurable), 0), 1)               AS sla_compliance_pct,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY resolution_hours)
              FILTER (WHERE NOT is_duplicate_closure))::numeric, 1)                AS median_resolution_hours,
       round((percentile_cont(0.9) WITHIN GROUP (ORDER BY resolution_hours)
              FILTER (WHERE NOT is_duplicate_closure))::numeric, 1)                AS p90_resolution_hours,
       round(100.0 * count(*) FILTER (WHERE sla_measurable) / count(*), 1)         AS metric_coverage_pct,
       current_setting('medallion.run_id')::uuid                                   AS _run_id
FROM gold.fct_tickets
WHERE created_date IS NOT NULL
GROUP BY 1, 2, 3;

-- 2) Vendor / team scorecard: who is fast, who is expensive, who does rework?
CREATE TABLE gold.mart_vendor_scorecard AS
SELECT assignee,
       assignee_type,
       count(*)                                                                    AS tickets_assigned,
       count(*) FILTER (WHERE is_open)                                             AS open_now,
       count(*) FILTER (WHERE resolved_date IS NOT NULL AND NOT is_duplicate_closure) AS resolved,
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY resolution_hours)
              FILTER (WHERE NOT is_duplicate_closure))::numeric, 1)                AS median_resolution_hours,
       round((percentile_cont(0.9) WITHIN GROUP (ORDER BY resolution_hours)
              FILTER (WHERE NOT is_duplicate_closure))::numeric, 1)                AS p90_resolution_hours,
       round(100.0 * count(*) FILTER (WHERE sla_measurable AND sla_met)
             / nullif(count(*) FILTER (WHERE sla_measurable), 0), 1)               AS sla_compliance_pct,
       count(*) FILTER (WHERE sla_measurable)                                      AS sla_measurable,
       sum(cost_usd)                                                               AS total_cost_usd,
       round(avg(cost_usd), 2)                                                     AS avg_cost_usd,
       count(cost_usd)                                                             AS tickets_with_cost,
       -- rework signals: work that did not actually fix the problem
       round(100.0 * count(*) FILTER (WHERE resolution_outcome = 'temporary_fix') / count(*), 1)  AS temporary_fix_pct,
       round(100.0 * count(*) FILTER (WHERE resolution_outcome = 'not_reproduced') / count(*), 1) AS not_reproduced_pct,
       count(*) FILTER (WHERE is_safety_hazard)                                    AS safety_tickets,
       current_setting('medallion.run_id')::uuid                                   AS _run_id
FROM gold.fct_tickets
WHERE assignee IS NOT NULL
GROUP BY 1, 2;

-- 3) Open backlog snapshot (as of the latest ticket in the data, so rebuilds are deterministic).
CREATE TABLE gold.mart_open_backlog AS
SELECT current_setting('medallion.as_of')::date                                    AS as_of_date,
       coalesce(building, 'unknown')                                               AS building,
       category,
       coalesce(priority, 'unspecified')                                           AS priority,
       count(*)                                                                    AS open_tickets,
       count(*) FILTER (WHERE is_safety_hazard)                                    AS open_safety_hazards,
       count(*) FILTER (WHERE inferred_severity = 'critical')                      AS open_inferred_critical,
       count(*) FILTER (WHERE open_age_hours <= 24 * 7)                            AS age_0_7d,
       count(*) FILTER (WHERE open_age_hours > 24 * 7 AND open_age_hours <= 24 * 30)  AS age_8_30d,
       count(*) FILTER (WHERE open_age_hours > 24 * 30 AND open_age_hours <= 24 * 90) AS age_31_90d,
       count(*) FILTER (WHERE open_age_hours > 24 * 90)                            AS age_over_90d,
       count(*) FILTER (WHERE sla_hours IS NOT NULL AND open_age_hours > sla_hours) AS past_sla,
       round(max(open_age_hours) / 24, 1)                                          AS oldest_open_days,
       current_setting('medallion.run_id')::uuid                                   AS _run_id
FROM gold.fct_tickets
WHERE is_open AND NOT is_duplicate_closure
GROUP BY 1, 2, 3, 4;

-- Enrichment-powered view: open safety hazards that a human filed as low/medium/no priority.
-- Only possible because the classification agent derives severity + hazard from free text.
CREATE OR REPLACE VIEW gold.v_underprioritised_hazards AS
SELECT f.ticket_id, f.building, f.category, f.issue_type, f.priority, f.inferred_severity,
       round(f.open_age_hours / 24, 1) AS open_days, s.description
FROM gold.fct_tickets f
JOIN silver.tickets s USING (ticket_id)
WHERE f.is_open AND f.is_safety_hazard AND NOT f.is_duplicate_closure
  AND f.inferred_severity IN ('critical', 'high')
  AND coalesce(f.priority, 'low') IN ('low', 'medium')
ORDER BY f.inferred_severity, f.open_age_hours DESC;
