# Silver cleaning rules

[← back to README](../README.md) · [Data pipeline](DATA_PIPELINE.md)

Every rule below is implemented in `src/medallion/pipeline/silver/` and unit-tested in
`tests/unit/test_parsers.py` and `tests/unit/test_transform.py`. Counts refer to the provided
`data/raw_tickets.csv` (10,280 rows).

Two principles drive all of them:

1. **Never invent data.** When a value is unusable it becomes `NULL` and the row gets a flag in
   `silver.tickets.dq_flags`. Nothing is imputed: a guessed value looks just as real as a true one
   in a dashboard.
2. **Never lose data silently.** Every bronze row ends up in exactly one of `silver.tickets`,
   `silver.tickets_quarantine` (with reasons) or `silver.ticket_duplicates` (with the surviving ticket).
   The silver stage checks this on every run and fails if the three don't add up to the bronze count.
   The original values stay in `silver.tickets.raw` for audit.

## Row-level rules

| ID | Rule | Rows | Why |
|---|---|---|---|
| Q1 | **Quarantine** rows whose `ticket_id` doesn't match `TKT-<n>` (`N/A`, `NULL`, empty, `15`). | 26 | Without a business key a row can't be deduplicated, joined or updated later. |
| Q2 | **Quarantine** rows carrying explicit test or junk markers (`DELETE ME`, `IGNORE THIS ROW`, `asdf…`, `duplicate entry please delete`, `test building`). | 19 (4 with valid IDs) | They are self-declared non-tickets. Keeping them would inflate volume. Markers live in `config/taxonomy.yaml`. |
| D1 | **Same `ticket_id` landed twice** (for example a corrected re-delivery): the latest bronze row wins. | 0 in this file | Standard upsert semantics for incremental loads. |
| D2 | **Re-submitted tickets**: rows identical on every field except `ticket_id`, `created_at` and `submitted_by` are merged into the lowest ticket number, **but only if the shared content is too rare to coincide by chance.** Each ticket's information is estimated as Σ −log₂(share of tickets with that value) over non-empty fields; a merge needs ≥ log₂(N²) + 3 bits (with N tickets there are ~N²/2 pairs). Below that, both tickets are kept and flagged `possible_duplicate`. | 195 merged, 6 pairs flagged | The file hides 200 re-submissions (TKT-11000+) plus 1 exact copy. A field-count rule caught all of them here, but random synthetic data showed it can merge two genuinely different, ordinary tickets. A false merge loses a ticket; a missed duplicate over-counts one and stays reviewable. The margin (`DEDUP_SAFETY_BITS=3`) is the smallest that gave zero false merges on 45k synthetic tickets ([TESTING.md](TESTING.md#random-synthetic-data)). |

## Value-level rules

| Column | Rule | Why |
|---|---|---|
| all text | Trim and collapse whitespace. Placeholder tokens (`N/A`, `NULL`, `???`, `unknown`, `TBD`, `error`, `-`, `.`, `asap`, `never`, `pending`, `not a date`, `00/00/0000`) become `NULL`. | Placeholders look like values to BI tools and silently distort counts. |
| `created_at`, `resolved_at` | Parse 8 formats: ISO with `T` or a space, ISO date, `MM/DD/YYYY hh:mm AM`, `MM/DD/YYYY`, `MM-DD-YYYY HH:MM:SS`, `DD-Mon-YYYY HH:MM`, and 10-digit epoch seconds (UTC). Years outside 2000–2100 are rejected. | **MM/DD rather than DD/MM is decided by evidence, not assumption.** Across about 2,800 slash or dash dates, the first component is never above 12 and the second often is (the profiler records this as `day_month_evidence`). The source has no timezone; wall-clock time is kept as-is. |
| `resolved_at` | If it is earlier than `created_at`: keep it, set `resolution_hours = NULL`, flag `resolved_before_created`. | 1,835 rows (35% of resolved tickets). A negative duration would corrupt every average; dropping the row would hide a source-system defect that someone needs to fix. |
| `status` | Map 6 canonical values to snake_case; derive `is_open`. Flag `resolved_at_on_open_ticket` (2,084) and `closed_without_resolved_at` (98). | The flags show that status and timestamps disagree in about 20% of tickets. Neither is "corrected", because we can't know which is right. |
| `priority` | `hi`→high, `crit`→critical, `med`/`Normal`→medium, `lo`→low (22 spellings → 4 levels). Missing stays `NULL`. | Priority is **not** inferred. Profiling showed priority is statistically independent of `sla_hours`, so no signal exists to infer it from. The LLM-derived `inferred_severity` sits in its own column and never overwrites the human value. |
| `cost` | Strip `$` and `,`. Negative values (`-1`, `-999`) are sentinels → `NULL` (190). `0` is kept (288; in-house work costs nothing extra). Amounts are in US dollars, as in the source; the original value stays in `raw`. | Sentinels would push vendor spend negative. |
| `sla_hours` | `999` is a sentinel → `NULL` (546). `0` or `-1` are out of range → `NULL` (474). | An SLA of 0 hours makes every ticket a breach. |
| `submitted_by` | Resolve 37 spellings to 16 people. The key is (first initial after nickname expansion, surname), e.g. `J. Smith` = `john smith` = `John Smith`, `Bob Martinez` = `Robert Martinez`. The display name is the most common full spelling. System accounts (`test`, `admin`, `system`) → `NULL`. If two first names share an initial and surname, initials are **not** guessed (flag `submitter_ambiguous`). | Per-person reporting needs a single identity. The resolver refuses to guess rather than merging two people. |
| `assigned_to` | Map to master data in `config/taxonomy.yaml`: canonical name plus `assignee_type` (`in_house` or `vendor`). Unknown values are kept and flagged. A specialist vendor working outside its qualification (per the ontology) is flagged `assignee_not_qualified_for_category` (2,230 rows). | Lets the vendor scorecard compare in-house teams with vendors, and exposes dispatch problems. |
| `category` | Normalise, then look up in `ref.category_label_map`: 92 label spellings → 10 categories, maintained by the classification agent behind a human-review gate. If the category and description fields were swapped, swap them back (flag `category_description_swapped`). Generic labels (`Other`, `Misc`, `General`) defer to the description. If the label and description disagree, the human label wins; the row is flagged `category_conflicts_with_description` only when the two categories are *unrelated* in the [ontology](AI_PLATFORM.md#ontology-and-knowledge-graph). | Before the ontology, 450 rows were flagged and every one was a legitimate overlap (emergency lighting: fire safety and electrical). See [AGENTS.md](AGENTS.md). |
| `description` | Mask buildings, asset IDs and numbers into a template (4,616 distinct → 92 templates), then look up enrichment: `issue_type`, `inferred_severity`, `is_safety_hazard`. A template truncated by the source's 60-character limit inherits the mapping of the unique approved template it is a prefix of. Extract `floor`, `room` and `asset_id` with regex. | Enrichment is per template, so its cost doesn't grow with row count. |
| `resolution_notes` | Regex-parsed into `resolution_outcome`, `root_cause`, `part_replaced`, `eta_business_days`, `onsite_hours` and `duplicate_of_ticket_id`. | The notes are machine-templated (42 templates). Regex is exact and free, so **no LLM is used here, on purpose**. |
| duplicate closures | Notes like `Duplicate of ticket #N` set `is_duplicate_closure`. 808 tickets; every referenced ticket exists. | They represent no work. Gold keeps them in volume counts but excludes them from performance metrics. |
