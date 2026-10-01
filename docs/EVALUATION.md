# Evaluation and A/B results

[← back to README](../README.md)

Every number on this page comes from a real run. The raw results are in
[`evals/results/reference/`](../evals/results/reference/), and each section ends with the command
that reproduces it. Total spend on hosted models for everything on this page: **$1.99** (OpenRouter's billed usage).

- [The harness](#the-harness)
- [Classification strategies](#classification-strategies) (rules vs local vs frontier vs Jev Router vs native Jev)
- [Prompt iteration v3 → v4](#prompt-iteration-v3--v4)
- [Memory](#memory)
- [LLM-as-judge + human-weighted trust](#llm-as-judge--human-weighted-trust)
- [Loop engineering](#loop-engineering)
- [Ontology](#ontology)
- [Groundedness of agent claims](#groundedness-of-agent-claims)
- [Metrics deliberately not computed](#metrics-deliberately-not-computed)
- [Human review of agent output](#human-review-of-agent-output)
- [Operational lessons the harness caught](#operational-lessons-the-harness-caught)

## The harness

`medallion eval --strategies <s1>,<s2>,...` scores classification strategies against hand-labelled
sets, and acts as a promotion gate (`--min-accuracy`; exit code 1 below it).

| Set | Items | Purpose |
|---|---|---|
| `category_labels.csv` | 98 | all 92 distinct category spellings + 6 junk values |
| `description_templates.csv` | 92 | all description templates, with safety-hazard labels |
| `holdout_descriptions.csv` | 60 | **tickets in phrasing that never appears in the source**: measures generalisation, not memorisation |

Rules of the harness:
- Genuinely ambiguous items list every acceptable answer.
- Every run is live: a fresh cache, and **no fallback**, so an LLM failure counts as a failure.
- Unit tests fail if any eval item appears in a prompt, or any holdout item appears in agent memory.
- Strategies: `rules`, `<provider>[:<model>]`, `+mem` suffix for memory, `jev[:<model>]` for native Jev.
- **Statistics:** with n = 60, one holdout item is 1.7 points. Differences of one or two items are
  noise, and I say so where it matters.

## Classification strategies

Accuracy is category / secondary (label kind for labels, safety hazard for descriptions). **Macro-F1**
is the unweighted mean of per-category F1, so one weak category can't hide behind a strong average.
Prompts v4, batch size 10.

| Strategy | Labels (98) acc / F1 | Templates (92) acc / F1 | **Holdout (60)** acc / F1 | p50 per batch | Cost, full eval | Answered by |
|---|---|---|---|---|---|---|
| Keyword rules (offline) | 99.0 / 0.991 | 95.7 / 0.957 | **31.7 / 0.411** | <1 ms | $0 | — |
| gemma3:4b (local, Ollama) | 94.9 / 0.935 | 91.3 / 0.882 | **90.0 / 0.924** | 17.5 s | $0 | gemma3:4b |
| Claude Sonnet 5.5 (OpenRouter) | 100 / 1.000 | 100 / 1.000 | **98.3 / 0.985** | 4.1 s | $0.238 | sonnet-5.5 ×26 |
| **Jev Router** (OpenRouter) | 100 / 1.000 | 100 / 1.000 | **100 / 1.000** | 7.2 s | **$0.058–0.081** | DeepSeek v4.1 Flash, Gemini 3.8 Flash |
| **Native Jev** 1.13 (OpenRouter decisions API) | 98.0 / 0.970 | 98.9 / 0.962 | **98.3 / 0.988** | 0.54 s per item | **$0.008** | jev-1.13 ×250 |

What this shows:
1. **The keyword rules are overfit.** They score 96–99% on the data they were written against, and
   31.7% on new phrasing. That gap is the strongest argument for an LLM in this pipeline: next
   month's tickets won't use this month's words.
2. **Jev Router ≈ a fixed frontier model on quality, at 3–4× lower cost.** It answered by routing to
   cheaper reasoning models. Its 100% vs Sonnet's 98.3% on the holdout is *one item*, which is not a
   meaningful difference; the cost gap is the finding. Its cost varied between runs ($0.058 and $0.081)
   because the router's choices vary; Sonnet's was stable at $0.238. The trade-off: 3–4× more
   (hidden) output tokens and higher latency.
3. **A 4B local model is a credible free fallback** (90% on the holdout), but it's weak on the
   subtle `label_kind` distinction (73.5%), and slow on a laptop. Its per-category scores show
   *where* it fails, which accuracy hides: it over-uses "unknown" for bare category words
   (precision 0.14 for that class), and on new phrasing its weakest category is hvac (recall 0.60).
4. **Native Jev is the cheapest hosted option by far, and close on categories.** It answers typed
   questions directly (no text generation, no JSON to validate: format compliance is 100% by
   construction), at **$0.008 for the full eval: 7–10× cheaper than Jev Router, 28× cheaper than
   Sonnet**. It missed 3 of 250 categories ("bugs in office" → unknown, "general" → general_maintenance).
   Its weakness is the safety-hazard flag, which it **over-flags**: 91% vs 96–100% for the
   generative models. All of its hazard errors are false alarms; it missed no real hazard, which is
   the safe direction for a flag that feeds the "under-prioritised hazards" view.

**Native Jev, hazard definition A/B.** The generative prompt defines a hazard through its severity
examples. The first Jev version asked the bare question ("could this plausibly injure someone?")
without them; the second passes those same examples as the yes/no criteria (no new wording, nothing
tuned to individual items):

| Native Jev | Templates hazard | Holdout hazard | Category accuracy |
|---|---|---|---|
| Bare question | 80.0% | 79.1% | unchanged |
| + criteria from the prompt's severity examples | **91.1%** | **90.7%** | unchanged |

Both runs: [`native-jev-1.13__no-hazard-criteria.json`](../evals/results/reference/native-jev-1.13__no-hazard-criteria.json),
[`native-jev-1.13__holdout60.json`](../evals/results/reference/native-jev-1.13__holdout60.json).
Needs only `OPENROUTER_API_KEY`: `medallion eval --strategies jev`.

> **Why Jev Router is the recommended default:** on this closed-taxonomy task it gave frontier-level
> accuracy and macro-F1, including on unseen phrasing, at a third to a quarter of the cost. Cost matters most at scale,
> because classification work grows with every new value. Native Jev is cheaper still and the better
> fit at very high volume, but its hazard flag is the least accurate of the hosted options, so it is an
> opt-in (`CLASSIFIER_BACKEND=jev`), not the default. See [Jev by TypeSafe](AI_PLATFORM.md#jev-by-typesafe).

Reproduce: `medallion eval --strategies rules,ollama,openrouter:anthropic/claude-sonnet-5.5,openrouter:typesafe/jev-router`

## Prompt iteration v3 → v4

Error analysis on gemma3 (labels and templates sets only; the holdout was never used for tuning, and
these runs used the original 30-item holdout):

| Prompt | Labels category / kind | Templates category / hazard | Holdout (30) |
|---|---|---|---|
| v3 | 89.8 / 81.6 | 88.0 / 100 | 86.7 |
| v4 | **94.9** / 73.5 ↓ | **91.3** / 93.3 ↓ | **90.0** |

The v3 prompt told the model to answer "unknown" for a bare category word in the description field,
and the model obeyed. Fixing that raised category accuracy everywhere. `label_kind` and hazard
accuracy went down, so the result is mixed. v4 shipped because category is what lands in silver.
I stopped iterating rather than tune a 4B model against a 98-item set.

## Memory

Episodic and procedural memory on vs off, leave-one-out (an item never sees its own correction):

| Strategy | Labels kind | Templates category / hazard | **Holdout** category |
|---|---|---|---|
| gemma3:4b | 73.5 | 91.3 / 93.3 | 90.0 |
| gemma3:4b + memory | 76.5 | **94.6 / 100** | 86.7 |
| Jev Router | 99.0 | 100 / 97.8 | 100 |
| Jev Router + memory | 100 | 100 / 100 | 100 |

The logical reading:
- **Memory helps on the distribution it was learned from.** The procedures were consolidated from
  review of these templates, so the template gain is partly in-sample; but that *is* the production
  case, because the same kinds of tickets recur every day.
- **It does not generalise to novel phrasing.** The holdout moved by −2 of 60 items, which is noise.
- **For a strong model there is nothing to gain:** it is at the ceiling either way.

So memory is worth keeping for a small or cheap model on recurring data. It is not a general
accuracy lever, and I don't present it as one.

Reproduce: `medallion eval --strategies ollama,ollama+mem`

## LLM-as-judge + human-weighted trust

Offline replay of **187 real proposals** (gemma3:4b's first run) against the final human-reviewed
answers. A proposal counts as correct only if category *and* label kind (or hazard) match. Judge:
Jev Router. Threshold 0.85.

| Auto-approval policy | Coverage (work automated) | Precision | Wrong answers auto-approved |
|---|---|---|---|
| A: agent self-confidence (original) | 54.5% | 79.4% | 21 |
| B: judge only | 83.4% | 92.3% | 12 |
| C′: judge + agent (no human signal) | 80.2% | 93.3% | 10 |
| **C: human 0.6 / judge 0.3 / agent 0.1** | **78.1%** | **96.6%** | **5** |

- The independent judge alone already beats self-confidence on both axes.
- Adding the human signal (C′ → C) **halves the errors (10 → 5)**, for 2 points of coverage. Human
  feedback is weighted above the judge because it measurably earns it.
- Against the original policy: **+23.6 points of work automated and 76% fewer errors**. Judge cost:
  $0.085 for 187 items.
- A first version that compared humans on *category only* was worse than judge-only: most errors were
  in `label_kind`, which a category-only signal vouches for. Comparing the whole decision fixed it.
- Run-to-run variation: an earlier run of the same experiment (smaller judge output budget, some
  items unjudged) gave 94.7% / 8 errors for policy C. The ranking of the policies was the same.

**Review-queue ranking (precision@k / recall@k).** Reviewers work the queue least-trusted first, so
what matters is how many of the 41 actual mistakes they meet early. precision@k is the share of the
first k items that are wrong; recall@k is the share of all mistakes found within the first k.

| Queue ordered by | precision@10 | precision@25 | recall@25 | recall@50 |
|---|---|---|---|---|
| random (expected) | 0.22 | 0.22 | 0.13 | 0.27 |
| agent self-confidence | 0.70 | 0.36 | 0.22 | 0.37 |
| judge | **1.00** | **1.00** | **0.61** | 0.76 |
| human-weighted trust | **1.00** | 0.92 | 0.56 | **0.88** |

Reviewing just the first 50 trust-ranked items catches 88% of all mistakes, versus 37% when the agent's
own confidence orders the queue.

Reproduce: `medallion experiment judge --judge openrouter:typesafe/jev-router`

## Loop engineering

Propose → verify (execute the SQL) → send failures back with the exact error, for up to 2 rounds.
First-pass and final results come from the same run, so each run is a paired A/B.

DQ rules + gold marts per model (in a throw-away database copy):

| Model | Items | Failed first pass | Repaired by the loop | Failed after loop |
|---|---|---|---|---|
| Claude Sonnet 5.5 | 15 | 0 | — | 0 |
| Claude Haiku 4.5 | 15 | 1 | **1** (round 1) | **0** |
| Gemini 3.1 Flash Lite | 8 | 1 | **1** (round 1) | **0** |
| gemma3:4b (local) | 15 | 13 | 0 | 13 |
| **Jev Router** (the recommended setup) | 14 | 2 | **1** (round 1) | 1, rejected by the guardrail |

The logical reading: **the loop pays off for mid-tier models.** They make occasional, fixable
mistakes, and the exact database error is enough for them to fix it. A frontier model doesn't need
it. A 4B model can't use it: it renamed IDs in its repairs (so fixes couldn't be matched), "repaired"
items that had already passed, and repeated the same invalid expression. Its failures are still
caught and rejected by the guard, so nothing bad reaches review. The loop just doesn't rescue them.

**Jev Router on the SQL agents** (the configuration the README recommends; $0.11 for both agents,
answered by DeepSeek v4.1 Flash, with the repair rounds routed to Gemini 3.8 Flash). All 3 gold marts
passed first time, with sensible caveats (e.g. it flagged that `resolution_hours` exists for only 34%
of tickets). Of 11 DQ rules, one failed verification and was fixed in repair round 1 (a consistency check on
`resolution_hours`); one put a
window function inside `FILTER`, kept doing so through both repair rounds, and was **rejected by the
guardrail rather than shown to a reviewer**. That puts it between Haiku and Sonnet on first-pass SQL
validity. A reviewer should also check one rule whose text ("infer the category from the description")
says more than its SQL does (it only nulls placeholders); the review gate is where that gets caught.
Reproduce: `LLM_PROVIDERS=openrouter OPENROUTER_MODEL=typesafe/jev-router medallion agent-dq` (and `agent-gold`).

Two bugs the experiment found in my own code, both fixed and tested:
- **Round 2 was an exact replay of round 1.** An unchanged failing item produced a byte-identical
  prompt, so the LLM cache served the same bad answer. Now each round sends the latest attempt plus
  the round number.
- **A `%` in a model's `LIKE '%…%'` expression broke evaluation.** Psycopg read it as a parameter
  placeholder, which no amount of model repair could fix. It is now escaped.

Raw: `evals/results/reference/loop_ab.json`.

## Ontology

| | Without ontology | With ontology |
|---|---|---|
| "label conflicts with description" | 450 tickets | **0** (all 450 were declared overlaps; unrelated pairs still flagged, unit-tested) |
| vendor outside its qualification | not detectable | 2,230 / 2,970 specialist tickets flagged |

## Groundedness of agent claims

The Data Quality Agent writes claims like "cost has 2,637 empty, 494 N/A". **Groundedness** = the
share of numbers it cites that are actually in the profile *for the rule's column* (or a percentage
of the row total at the precision cited). Ungrounded numbers are listed next to the rule for the
reviewer. The metric was validated before use: on random numbers it wrongly accepts 0–3% (an
unscoped first version accepted up to 100% of random small numbers, so it was discarded).

| Model | Rules | Groundedness | What the flags were |
|---|---|---|---|
| Claude Haiku 4.5 | 13 | **0.99** | one rounding slip ("25.6%" for 25.65%) |
| Claude Sonnet 5.5 | 12 | **0.84** | all checked by hand and **correct**: multi-step arithmetic (10,254 = 10,280 − 26) or facts from another column (4,939 empty `resolved_at` cited in a `status` rule) |
| **Jev Router** | 11 | **0.82** | all checked by hand against bronze and **correct**: status counts cited in rules about other columns (Open 1,714 … Escalated 1,716, summed to 6,958 in an `assigned_to` rule) |

No model invented a statistic. The metric is a strict lower bound whose value is telling the
reviewer *which* numbers to double-check.

## Metrics deliberately not computed

| Metric | Why not here |
|---|---|
| Tonality / style | Nothing generated here is user-facing prose. Outputs are categories, SQL and short rationales for engineers, so a tone score would be a vanity metric. |
| BLEU / ROUGE / semantic similarity | No free-text generation with reference answers; the outputs are typed and checked exactly. |
| precision@k for retrieval | Episodic memory recall is top-4 by token similarity over 41 precedents; its value is measured end to end (the memory A/B) rather than as a retrieval score. |

## Human review of agent output

| Agent | Proposals | Approved as-is | Corrected by a human | Rejected |
|---|---|---|---|---|
| Classification (gemma3:4b) | 187 | 146 | 41 (mostly `label_kind`) | 0 |
| Data quality (Sonnet 5.5) | 12 | 8 | **4 wrong-but-valid SQL** (case mismatch ×2, a critical check that would block every run, an impossible predicate) | 0 |
| Gold design (Sonnet 5.5) | 3 | 3 | 0 | 0 |

The DQ row is why verification evidence (actual violation rates) is shown next to every rule: all 4
problems were obvious from the evidence in seconds, and invisible from the SQL alone.

## Operational lessons the harness caught

All of these were found by running the evals, not in production:

| Symptom | Cause | Fix |
|---|---|---|
| Jev Router scored 71% / 11% / 33% | Router picked reasoning models whose hidden tokens exhausted a ~1.4k output budget (`finish_reason=length`); every answer it *did* return was correct | Output headroom for hosted reasoning models → 100% |
| gemma3 batches silently missing | Ollama's default 2k context truncated the prompt **without error** (12,105-token prompt counted as 2,051) | Adapter detects truncation; gateway pre-flight checks context fit; docs require `OLLAMA_CONTEXT_LENGTH` |
| One bad field lost a batch of 10 | `issue_type: "door_won't_lock"` failed a regex, then the repair failed too | Strict on semantics (taxonomy enum), lenient on cosmetics (normalise) |
| Judge skipped 18 of 187 items | Model omitted items from batched answers | Explicit IDs + one retry of missing items in smaller batches |
| 25-item batches timed out locally | ~3k output tokens per call on a laptop | Batch 10 for small local models |
