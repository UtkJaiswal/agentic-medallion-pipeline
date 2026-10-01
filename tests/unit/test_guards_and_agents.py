import csv

import pytest

from medallion.agents.classification import KeywordClassifier, LLMClassifier, TemplateItem
from medallion.agents.prompting import load_prompt
from medallion.agents.sql_guard import UnsafeSQLError, static_check
from medallion.llm.cache import InMemoryLLMCache
from medallion.llm.router import LLMRouter
from medallion.llm.types import LLMProvider, LLMResponse, ProviderTransientError
from medallion.observability.context import trace_id_from_headers
from medallion.pipeline.tagging import tag_column
from medallion.settings import PROJECT_ROOT


@pytest.mark.parametrize("sql", [
    "cost_usd < 0; DROP TABLE silver.tickets", "status = 'open' OR pg_sleep(10) IS NULL",
    "ticket_id IN (SELECT ticket_id FROM ops.pipeline_runs)", "exists (select 1 from bronze.tickets_raw)",
])
def test_sql_guard_rejects_unsafe_predicates(sql):
    with pytest.raises(UnsafeSQLError):
        static_check(sql, kind="predicate")


def test_sql_guard_accepts_reasonable_sql():
    static_check("resolved_at < created_at AND status <> 'drop; delete'", kind="predicate")  # literal is data
    static_check("WITH x AS (SELECT category FROM silver.tickets) SELECT category, count(*) FROM x GROUP BY 1",
                 kind="query")
    with pytest.raises(UnsafeSQLError):
        static_check("DELETE FROM silver.tickets", kind="query")


def test_prompts_are_versioned_and_render():
    params = {"classify_labels": ["taxonomy"], "classify_templates": ["taxonomy"],
              "dq_rules": ["profile", "silver_schema", "existing"],
              "gold_design": ["domain", "silver_schema", "column_facts", "existing"]}
    for name, keys in params.items():
        p = load_prompt(name)
        assert p.version.startswith(name)
        rendered = p.render(**{k: f"<<{k}>>" for k in keys})  # raises on any stray $placeholder
        assert all(f"<<{k}>>" in rendered for k in keys)


def test_eval_datasets_only_reference_taxonomy_categories(taxonomy):
    for name in ("category_labels.csv", "description_templates.csv", "holdout_descriptions.csv"):
        with (PROJECT_ROOT / "evals" / "datasets" / name).open() as fh:
            for row in csv.DictReader(fh):
                assert set(row["expected_category"].split("|")) <= set(taxonomy.names), row


def test_keyword_classifier_handles_junk_generic_and_short_keywords(taxonomy):
    kw = KeywordClassifier(taxonomy)
    by_label = {d.label: d for d in kw.classify_labels(["asdf", "misc", "access control", "a/c"])}
    assert by_label["asdf"].label_kind == "junk"
    assert by_label["misc"].label_kind == "generic_label"
    assert by_label["access control"].category == "security_access"  # "ac" must not match "access"
    assert by_label["a/c"].category == "hvac"
    assert all(d.confidence < 0.85 for d in by_label.values())  # rule guesses never auto-approve


class Down(LLMProvider):
    name, model = "down", "m"

    def complete(self, request):
        raise ProviderTransientError("down", "503")


class Echo(LLMProvider):
    """Answers every batch item with hvac/0.95."""
    name, model = "echo", "m"

    def complete(self, request):
        import json
        items = json.loads(request.user)
        return LLMResponse(json.dumps({"results": [
            {"id": i["id"], "category": "hvac", "issue_type": "x_y", "severity": "low", "is_safety_hazard": False,
             "confidence": 0.95} for i in items]}), "echo", "m")


def test_llm_classifier_falls_back_to_rules_when_llm_is_down(taxonomy):
    clf = LLMClassifier(LLMRouter([Down()], InMemoryLLMCache()), taxonomy, KeywordClassifier(taxonomy), batch_size=2)
    out = clf.classify_templates([TemplateItem("pipe burst", "pipe burst"), TemplateItem("wifi down", "wifi down"),
                                  TemplateItem("cold", "cold")])
    assert len(out) == 3 and all(d.source == "rules" for d in out)


def test_llm_classifier_batches_and_tags_source(taxonomy):
    clf = LLMClassifier(LLMRouter([Echo()], InMemoryLLMCache()), taxonomy, None, batch_size=2, concurrency=2)
    out = clf.classify_templates([TemplateItem(f"t{i}", f"e{i}") for i in range(5)])
    assert sorted(d.template for d in out) == [f"t{i}" for i in range(5)]
    assert {d.source for d in out} == {"llm:echo/m"}


def test_trace_id_from_headers():
    tp = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    assert trace_id_from_headers({"traceparent": tp}) == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert trace_id_from_headers({"X-Trace-Id": "abc-123-xyz"}) == "abc-123-xyz"
    assert len(trace_id_from_headers({"X-Trace-Id": "bad id; drop"})) == 32  # malformed => fresh id


def test_column_tagging_marks_pii_and_free_text():
    people = {"top_values": [{"value": "John Smith", "n": 50}, {"value": "J. Doe", "n": 40}], "shapes": [],
              "distinct_raw": 30, "avg_length": 9, "placeholder": 0}
    assert tag_column("submitted_by", people).sensitivity == "pii"
    text = {"top_values": [{"value": "Pipe burst in kitchen", "n": 2}], "shapes": [], "distinct_raw": 4000,
            "avg_length": 60}
    t = tag_column("description", text)
    assert t.semantic_type == "free_text" and "may_contain_pii" in t.tags
    assert tag_column("ticket_id", {"top_values": [], "shapes": []}).semantic_type == "identifier"
    dates = {"top_values": [{"value": "2024-03-15 10:30:00", "n": 9}, {"value": "03/15/2024", "n": 5}], "shapes": []}
    assert tag_column("created_at", dates).sensitivity == "internal"  # dates are not phone numbers
    phones = {"top_values": [{"value": "+91 98765 43210", "n": 9}], "shapes": [], "avg_length": 15}
    assert "pii:contact" in tag_column("contact", phones).tags


def test_llm_output_is_strict_on_semantics_lenient_on_cosmetics(taxonomy):
    from pydantic import ValidationError

    from medallion.agents.classification import _schemas
    _, batch = _schemas(taxonomy.names)
    ok = batch.model_validate({"results": [{"id": "T0", "category": "security_access", "issue_type": "Door won't lock",
                                            "severity": "high", "is_safety_hazard": False, "confidence": 1.04}]})
    assert ok.results[0].issue_type == "door_won_t_lock" and ok.results[0].confidence == 1.0
    with pytest.raises(ValidationError):  # an invented category is still rejected
        batch.model_validate({"results": [{"id": "T0", "category": "roofing", "issue_type": "x", "severity": "low",
                                           "is_safety_hazard": False, "confidence": 0.9}]})


def test_prompts_do_not_leak_eval_items(taxonomy):
    """Prompt examples must not quote eval items, or the eval would measure memorisation."""
    import re
    text = " ".join(load_prompt(n).template.template.lower() for n in ("classify_labels", "classify_templates"))
    items = []
    for name, col in (("category_labels.csv", "label"), ("description_templates.csv", "template"),
                      ("holdout_descriptions.csv", "template")):
        with (PROJECT_ROOT / "evals" / "datasets" / name).open() as fh:
            items += [r[col] for r in csv.DictReader(fh)]
    leaks = {i for i in items if len(i) > 2 and i not in taxonomy.names and re.search(f'"{re.escape(i)}"', text)}
    assert not leaks


def test_holdout_never_leaks_into_agent_memory():
    """The memory A/B is scored on the holdout set, so no holdout item may be in episodes or procedures."""
    memory_dir = PROJECT_ROOT / "config" / "memory"
    text = (memory_dir / "episodes.jsonl").read_text().lower() + " ".join(
        p.read_text().lower() for p in (memory_dir / "procedures").glob("*.md"))
    with (PROJECT_ROOT / "evals" / "datasets" / "holdout_descriptions.csv").open() as fh:
        holdout = [r["template"] for r in csv.DictReader(fh)]
    assert not [h for h in holdout if h in text]


def test_memory_recall_is_similarity_based_and_supports_leave_one_out():
    from medallion.agents.memory import AgentMemory, Episode
    eps = [Episode("classify_templates", "drain clogged in cleaner cupboard", {"category": "janitorial"},
                   {"category": "plumbing"}),
           Episode("classify_templates", "paint peeling in lobby", {"category": "x"}, {"category": "y"})]
    mem = AgentMemory({"classify_templates": "rule"}, eps)
    assert [e.text for e in mem.recall("classify_templates", ["kitchen drain clogged again"])] == \
        ["drain clogged in cleaner cupboard"]
    assert mem.recall("classify_templates", ["drain clogged in cleaner cupboard"],
                      exclude={"drain clogged in cleaner cupboard"}) == []
    system, user = mem.render("classify_templates", ["kitchen drain clogged again"])
    assert "<procedures" in system and "reviewer_decided" in user


def test_every_proposal_kind_has_an_applier_even_in_a_fresh_process():
    import subprocess
    import sys
    code = ("from medallion.agents.proposals import _applier\n"
            "for k in ('category_label', 'description_template', 'dq_check', 'gold_model'): _applier(k)")
    assert subprocess.run([sys.executable, "-c", code], capture_output=True).returncode == 0


def test_category_report_precision_recall_f1():
    from medallion.agents.evaluation import category_report
    pairs = [("hvac", "hvac"), ("hvac", "plumbing"), ("plumbing", "plumbing"), ("plumbing", "missing")]
    macro, per = category_report(pairs)
    assert per["hvac"] == {"precision": 1.0, "recall": 0.5, "f1": 0.667, "support": 2}
    assert per["plumbing"] == {"precision": 0.5, "recall": 0.5, "f1": 0.5, "support": 2}
    assert macro == round((0.667 + 0.5) / 2, 4)


def test_groundedness_flags_invented_numbers():
    from medallion.agents.data_quality import groundedness
    compact = {"table": {"total_rows": 10280},
               "columns": {"cost": {"total": 10280, "empty": 2637, "top_values": [["N/A", 494], ["-999", 3]]}}}
    g = groundedness("Cost has 2,637 empty (25.65%), 494 N/A, -999 sentinels and 8 formats", compact, "cost")
    assert g["score"] == 1.0 and g["cited"] == 4  # 8 is structural; 25.7% = 2637/10280
    bad = groundedness("Cost has 3,100 empty values and 494 N/A", compact, "cost")
    assert bad["ungrounded"] == ["3,100"] and bad["score"] == 0.5
