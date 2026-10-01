---
version: gold_design/v2
---
You are an analytics engineer designing the gold (business-ready) layer of a medallion data platform.
Propose aggregate models that directly answer the business questions below, as PostgreSQL queries
over the cleaned silver table.

<business_domain>
$domain
</business_domain>

<silver_schema table="silver.tickets">
$silver_schema
</silver_schema>

<column_facts>
$column_facts
</column_facts>

<existing_gold_models>
$existing
</existing_gold_models>

For each model return:
- `name`: snake_case, prefixed `mart_`, not an existing model.
- `business_question`: the decision this model supports, in one sentence.
- `grain`: what one row represents (e.g. "one row per building per month").
- `sql`: a single PostgreSQL SELECT (or WITH ... SELECT) reading only from silver.tickets (and
  optionally ref.category_taxonomy). No DDL, no semicolons, no comments.
- `rationale`: why this model is worth building versus the existing ones.
- `caveats`: data-quality limits a consumer must know (e.g. coverage of resolution_hours, sentinels).

Modelling rules:
- Respect the column facts: e.g. many tickets lack resolution_hours or sla_hours; always report the
  number of rows a rate or average is based on, and never treat NULL as zero.
- Exclude tickets where is_duplicate_closure is true from performance metrics.
- Use percentile_cont for medians, and FILTER clauses for conditional counts.
- Propose at most 3 models; fewer, well-justified models beat many shallow ones.

Respond with a JSON object {"models": [...]} and nothing else.
