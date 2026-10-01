"""Agent evaluation harness: a fixed, hand-labelled test set and a scoring rubric, used to compare
strategies (rules vs. each LLM provider/model) and to gate prompt/model changes before promotion.

Rubric per task:
  * accuracy        - category in the acceptable set (some items are genuinely ambiguous and list
                      several acceptable answers; a model is not penalised for either)
  * kind / hazard   - label_kind accuracy (labels) and is_safety_hazard accuracy where labelled
  * format          - share of LLM calls whose first response passed schema validation
  * latency, tokens, cost - from the same metering used in production

Every run is a live measurement: a fresh in-memory cache, so nothing is served from earlier runs."""

from __future__ import annotations

import csv
import json
import statistics
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from medallion.agents.classification import Classifier, KeywordClassifier, LLMClassifier, TemplateItem
from medallion.agents.jev import build_jev
from medallion.agents.memory import AgentMemory
from medallion.agents.prompting import load_prompt
from medallion.llm.cache import InMemoryLLMCache
from medallion.llm.decorators import LLMCallRecord
from medallion.llm.factory import build_router
from medallion.reference import Taxonomy
from medallion.settings import ProviderName, Settings

EVAL_DIR = Path(__file__).resolve().parents[3] / "evals"


class MemoryRecorder:
    def __init__(self) -> None:
        self.calls: list[LLMCallRecord] = []
        self._lock = threading.Lock()

    def record(self, call: LLMCallRecord) -> None:
        with self._lock:
            self.calls.append(call)


@dataclass
class TaskScore:
    task: str
    items: int
    accuracy: float
    secondary_name: str
    secondary_accuracy: float | None
    wall_time_s: float
    errors: list[dict[str, Any]] = field(default_factory=list)
    macro_f1: float | None = None
    per_category: dict[str, dict[str, float]] = field(default_factory=dict)  # precision / recall / f1 / support


def category_report(pairs: list[tuple[str, str]]) -> tuple[float | None, dict[str, dict[str, float]]]:
    """Per-category precision/recall/F1 and macro-F1 over the categories present in the truth.
    For ambiguous items (several acceptable answers) the truth is the prediction if it was acceptable,
    otherwise the first acceptable answer; a missing prediction counts as a miss."""
    labels = sorted({t for t, _ in pairs})
    out: dict[str, dict[str, float]] = {}
    for c in labels:
        tp = sum(1 for t, p in pairs if t == c and p == c)
        fp = sum(1 for t, p in pairs if t != c and p == c)
        fn = sum(1 for t, p in pairs if t == c and p != c)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        out[c] = {"precision": round(prec, 3), "recall": round(rec, 3), "f1": round(f1, 3), "support": tp + fn}
    macro = round(sum(v["f1"] for v in out.values()) / len(out), 4) if out else None
    return macro, out


def _truth(expected: str, predicted: str | None) -> str:
    options = expected.split("|")
    return predicted if predicted in options else options[0]


@dataclass
class EvalReport:
    strategy: str
    started_at: str
    tasks: list[TaskScore]
    llm_calls: int
    format_compliance: float | None
    latency_ms_p50: float | None
    latency_ms_p95: float | None
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    prompt_versions: dict[str, str] | None = None
    served_models: dict[str, int] | None = None  # which models actually answered (routers pick per call)

    def passed(self, min_accuracy: float) -> bool:
        return all(t.accuracy >= min_accuracy for t in self.tasks)


def _accepts(expected: str, actual: str) -> bool:
    return actual in expected.split("|")


def score_labels(classifier: Classifier, rows: list[dict[str, str]]) -> TaskScore:
    started = time.perf_counter()
    decisions = {d.label: d for d in classifier.classify_labels([r["label"] for r in rows])}
    errors, cat_ok, kind_ok, pairs = [], 0, 0, []
    for r in rows:
        d = decisions.get(r["label"])
        pairs.append((_truth(r["expected_category"], d.category if d else None), d.category if d else "missing"))
        c = bool(d and _accepts(r["expected_category"], d.category))
        k = bool(d and _accepts(r["expected_kind"], d.label_kind))
        cat_ok += c
        kind_ok += k
        if not (c and k):
            errors.append({"label": r["label"], "expected": f"{r['expected_kind']}/{r['expected_category']}",
                           "got": f"{d.label_kind}/{d.category}" if d else "missing"})
    n = len(rows)
    macro, per = category_report(pairs)
    return TaskScore("category_labels", n, round(cat_ok / n, 4), "label_kind", round(kind_ok / n, 4),
                     round(time.perf_counter() - started, 2), errors, macro, per)


def score_templates(classifier: Classifier, rows: list[dict[str, str]], task: str = "description_templates"
                    ) -> TaskScore:
    started = time.perf_counter()
    decisions = {d.template: d for d in classifier.classify_templates(
        [TemplateItem(r["template"], r["example"]) for r in rows])}
    errors, cat_ok, haz_ok, haz_n = [], 0, 0, 0
    pairs: list[tuple[str, str]] = []
    for r in rows:
        d = decisions.get(r["template"])
        pairs.append((_truth(r["expected_category"], d.category if d else None), d.category if d else "missing"))
        c = bool(d and _accepts(r["expected_category"], d.category))
        cat_ok += c
        h = True
        if r["expected_is_safety_hazard"]:
            haz_n += 1
            h = bool(d and str(d.is_safety_hazard).lower() == r["expected_is_safety_hazard"])
            haz_ok += h
        if not (c and h):
            errors.append({"template": r["template"][:70], "expected": r["expected_category"],
                           "got": d.category if d else "missing",
                           "hazard": f"{r['expected_is_safety_hazard'] or '-'} vs {d.is_safety_hazard if d else '?'}"})
    n = len(rows)
    macro, per = category_report(pairs)
    return TaskScore(task, n, round(cat_ok / n, 4), "is_safety_hazard",
                     round(haz_ok / haz_n, 4) if haz_n else None, round(time.perf_counter() - started, 2), errors,
                     macro, per)


def _read(name: str) -> list[dict[str, str]]:
    with (EVAL_DIR / "datasets" / name).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def run_eval(settings: Settings, taxonomy: Taxonomy, strategy: str) -> EvalReport:
    """`strategy` is "rules" or "<provider>[:<model>]", e.g. "ollama", "openrouter:typesafe/jev-router"."""
    recorder = MemoryRecorder()
    keyword = KeywordClassifier(taxonomy)
    if strategy == "rules":
        classifier: Classifier = keyword
        label = "rules"
    elif strategy.startswith("jev"):
        classifier, label = build_jev(settings, taxonomy, strategy.partition(":")[2] or None, None, recorder)
    else:
        base, use_memory = strategy.removesuffix("+mem"), strategy.endswith("+mem")
        provider, _, model = base.partition(":")
        name: ProviderName = provider  # type: ignore[assignment]
        router = build_router(settings, None, providers=[name], cache=InMemoryLLMCache(), recorder=recorder,
                              models={name: model} if model else None)
        memory = AgentMemory.load() if use_memory else None  # file-based: config/memory/
        classifier = LLMClassifier(router, taxonomy, None, settings.llm_batch_size, settings.llm_concurrency,
                                   memory=memory, leave_one_out=True)
        label = router.describe() + (" +memory" if use_memory else "")

    started = datetime.now(UTC).isoformat(timespec="seconds")
    tasks = [score_labels(classifier, _read("category_labels.csv")),
             score_templates(classifier, _read("description_templates.csv")),
             # novel phrasing never seen in the source data: measures generalisation, not memorisation
             score_templates(classifier, _read("holdout_descriptions.csv"), task="holdout_descriptions")]
    calls = [c for c in recorder.calls if c.status != "cache_hit"]
    ok = [c for c in calls if c.status == "ok"]
    invalid = sum(1 for c in calls if c.status == "invalid_output")
    latencies = sorted(c.latency_ms for c in ok if c.latency_ms is not None)
    costs = [c.cost_usd for c in ok]
    return EvalReport(
        strategy=label, started_at=started, tasks=tasks, llm_calls=len(ok),
        format_compliance=round(1 - invalid / len(ok), 4) if ok else None,
        latency_ms_p50=statistics.median(latencies) if latencies else None,
        latency_ms_p95=latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))] if latencies else None,
        input_tokens=sum(c.input_tokens for c in ok), output_tokens=sum(c.output_tokens for c in ok),
        cost_usd=round(sum(costs), 4) if ok and all(c is not None for c in costs) else None,
        served_models=dict(Counter(c.served_model or "?" for c in ok)) if ok else None,
        prompt_versions=None if strategy == "rules" or strategy.startswith("jev") else
        {n: load_prompt(n).version for n in ("classify_labels", "classify_templates")})


def save(report: EvalReport) -> Path:
    out = EVAL_DIR / "results"
    out.mkdir(exist_ok=True)
    slug = report.strategy.replace("/", "_").replace(" ", "").replace(":", "-")[:60]
    versions = "_".join(v.split("/")[-1] for v in (report.prompt_versions or {}).values())
    path = out / f"{report.started_at[:16].replace(':', '')}_{slug}{'_' + versions if versions else ''}.json"
    path.write_text(json.dumps(asdict(report), indent=2, default=str))
    return path


def render(report: EvalReport) -> str:
    lines = [f"strategy: {report.strategy}"]
    for t in report.tasks:
        sec = f"{t.secondary_accuracy:.1%}" if t.secondary_accuracy is not None else "n/a"
        f1 = f"{t.macro_f1:.3f}" if t.macro_f1 is not None else "n/a"
        lines.append(f"  {t.task:<22} n={t.items:<3} accuracy={t.accuracy:.1%}  macro_f1={f1}  "
                     f"{t.secondary_name}={sec}  time={t.wall_time_s}s  errors={len(t.errors)}")
    if report.llm_calls:
        cost = f"${report.cost_usd:.4f}" if report.cost_usd is not None else "unpriced"
        lines.append(f"  llm: calls={report.llm_calls} format_compliance={report.format_compliance:.1%} "
                     f"latency p50={report.latency_ms_p50}ms p95={report.latency_ms_p95}ms "
                     f"tokens={report.input_tokens}+{report.output_tokens} cost={cost}")
        lines.append(f"  served by: {report.served_models}")
    return "\n".join(lines)
