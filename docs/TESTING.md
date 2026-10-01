# Testing

[← back to README](../README.md)

**172 tests, all passing:** 151 unit tests that need no database (about 2 seconds), and 21 integration
tests against a throw-away Postgres database created per session, including random synthetic data scored
against ground truth.

| Area | What is covered |
|---|---|
| Cleaning rules | every date format, sentinels, enums, templates, entity extraction, notes parsing, person resolution (including refusing ambiguous initials) |
| Transform | reconciliation, quarantine, both dedup rules, sparse look-alikes *not* merged, swapped fields, label-vs-description precedence and conflicts, determinism under input reordering |
| Resilience | backoff/jitter bounds, `Retry-After`, no retry on permanent errors, polling timeout, token bucket and circuit breaker on fake clocks |
| LLM layer | fallback chain, repair round-trip, invalid-twice → next provider, cache stores only validated output, budget stops the chain, every SDK's error → transient/permanent mapping, Anthropic refusal, strict transport schema |
| Agents | SQL guard (injection, DDL, denied schemas), offline fallback, batching, strict-semantics/lenient-cosmetics validation, **prompt/eval leakage check**, every prompt renders |
| Integration | pipeline twice → identical silver fingerprint and 0 new bronze rows; lineage joins; schema drift; critical DQ check blocks gold and keeps the old gold; review gate with overrides and corrections; API idempotency (replay, 422 on a changed body, key required), trace propagation, path traversal, 429 + Retry-After, API key; worker claim/SKIP LOCKED, backoff re-queue, lease recovery; outbox relay at-least-once; DQ and gold agents with SQL guardrails |
| Guardrails | PII redaction (hosted only), context pre-flight falling through to the next provider, placeholder and empty-task rejection, injection flagging |
| Memory, judge, loop | similarity recall with leave-one-out, holdout never in memory, trust weighting, whole-decision human signal, bounded repair loop that survives a failing repair call |
| Jev | request shape (choices = taxonomy), OpenRouter decisions route (PII redacted, billed cost recorded), route selection, 529 retried with backoff then per-item fallback, 401 not retried |
| Ontology | related categories not flagged as conflicts, unrelated still flagged, unqualified-vendor flag |
| Metrics | per-category precision/recall/F1 and macro-F1, review-queue precision@k / recall@k, groundedness (including invented-number detection) |

## Random synthetic data

`medallion synth --rows N --seed S [--run]` generates messy tickets **with ground truth**: for every
row, what the pipeline *should* do (keep, quarantine or mark duplicate) and the true category,
timestamp and cost. With `--run` it ingests the file, runs the pipeline and scores the result.

The generator reproduces every kind of mess in the real file: 8 date formats including epoch,
placeholders, `$` and sentinel costs, SLA sentinels, junk rows, re-submitted and exact duplicates,
swapped fields, and spelling variants. It adds drift the real file never had: new category spellings
("Housekeeping Svcs", "HVAC/R"), new phrasings and new campuses (Bengaluru Tech Park, Pune Hinjewadi,
Hyderabad HITEC City, …) with Indian submitter names in all the usual variants (*Priya Sharma*,
*P. Sharma*, *priya sharma*).

Results (default settings):

| Run | Rows | Reconciled | Junk quarantined | Duplicates caught / **false merges** | `created_at` exact | `cost_usd` exact | Category (drift rows) | Time |
|---|---|---|---|---|---|---|---|---|
| seed 7, offline | 5,177 | ✅ | 25 / 25 | 138 of 152 / **0** | 100% | 100% | 99.92% (97.3%) | 4.5 s |
| seed 11, offline | 20,682 | ✅ | 100 / 100 | 535 of 582 / **0** | 100% | 100% | 99.97% (98.9%) | 8.8 s |
| seed 23, offline | 20,733 | ✅ | 100 / 100 | 576 of 633 / **0** | 100% | 100% | 99.94% (98.3%) | 12.8 s |
| seed 11, **live agent** (Jev Router + judge) | 20,682 | ✅ | 100 / 100 | as offline | 100% | 100% | **100% (100%)** for $0.056 | +70 s |
| seed 11, **live agent** (native Jev + Jev Router judge) | 20,682 | ✅ | 100 / 100 | 547 of 582 / **0** | 100% | 100% | **100% (100%)**: classification $0.006, judge $0.053 | +100 s |

**Duplicate detection is tuned for zero false merges.** Synthetic data showed that two genuinely
different but ordinary tickets ("elevator out of order again …", nothing else distinctive) can be
word-for-word identical. So a content match only merges when the content is too rare to coincide by
chance (see [CLEANING_RULES.md](CLEANING_RULES.md)); everything else is kept and flagged
`possible_duplicate`. The margin was chosen by sweeping it over the real file and all three synthetic
files:

| Margin (bits over log₂N²) | Real file: merged / 201 | Synthetic: false merges | Synthetic: duplicates caught |
|---|---|---|---|
| 0 | 200 | **1** | 1,281 / 1,367 |
| 1 | 199 | **1** | 1,270 / 1,367 |
| 2 | 198 | **1** | 1,261 / 1,367 |
| **3 (default)** | **198** | **0** | 1,249 / 1,367 |
| 5 | 194 | 0 | 1,214 / 1,367 |
| 7 | 185 | 0 | 1,183 / 1,367 |

(The default run above merges 195 on the real file alone; the sweep measured 198 with all files
loaded, because the threshold depends on N.) Synthetic recall is lower than the real file's because
templated synthetic text carries less information per field.

Offline, drift values that nothing recognises wait in the review queue, which is why categories are
just under 100%. With the agent on, they are classified, judged and auto-approved.

**What synthetic data found**, all invisible on the original file and all now fixed and tested:

| Finding | Why the original file hid it | Fix |
|---|---|---|
| Swapped category/description was missed when the sentence was new | Every swapped sentence in the real file was already in the label map | Detect a swap when the category field holds an unseen sentence and the description is a known label |
| With a judge configured, one failed judge batch made the policy fall back to self-confidence and auto-approve everything | The offline replay never ran the live path | **Fail safe**: no verdict → human review. Judge batches fail independently, missing items are retried in smaller batches, and the judge gets more output headroom |
| An approved DQ check required 4–5-digit ticket numbers; the quality gate blocked gold when IDs grew | Real IDs never exceeded 5 digits | Human correction through the review gate (`^TKT-[0-9]+$`). The gate itself worked exactly as designed: it kept the previous gold |
| `review correct` failed for seeded reference data with no proposal history | Corrections had only been made in a database with full history | Each proposal kind registers a loader for its current reference row |
| Field-count dedup could merge two genuinely different ordinary tickets (2 false merges in 20k) | The real file's duplicates all carried rich, rare content | Information-based merge rule with a measured safety margin (above): 0 false merges |
| My own generator first attached new labels to random categories | — | The pipeline had already flagged every one of those rows as a label/description conflict; the generator now keeps drift semantically consistent |

Two seeds run as integration tests on every test run (`tests/integration/test_synthetic.py`).

## Running

```bash
uv sync
uv run pytest -m "not integration"     # unit tests, no database (seconds)
docker compose up -d postgres          # then:
uv run pytest                          # everything; integration tests use a throw-away database
```
