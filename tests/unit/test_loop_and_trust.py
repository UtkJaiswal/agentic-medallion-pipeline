from medallion.agents.judge import HumanSignal, TrustWeights, trust_score
from medallion.agents.loop import Verified, repair_loop


def _verify(x: str) -> Verified[str]:
    ok = "bad" not in x
    return Verified(x, ok, None if ok else f"{x} is invalid", {})


def test_repair_loop_fixes_failures_and_records_first_pass():
    fixes = {"bad-1": "good-1"}

    def repair(failed, round_no):
        return [fixes.get(f.item, f.item) for f in failed]

    results, stats = repair_loop(["ok-0", "bad-1", "bad-2"], _verify, repair, key=lambda s: s.split("-")[1],
                                 max_rounds=2)
    by_key = {r.item.split("-")[1]: r for r in results}
    assert (stats.failed_first_pass, stats.repaired, stats.failed_final) == (2, 1, 1)
    assert by_key["1"].ok and by_key["1"].repair_round == 1 and not by_key["2"].ok
    assert stats.rounds_used == 2  # bad-2 was retried once more, then the loop stopped (bounded)


def test_repair_loop_survives_a_failing_repair_call():
    def boom(failed, round_no):
        raise RuntimeError("provider down")

    results, stats = repair_loop(["bad-1"], _verify, boom, key=lambda s: s, max_rounds=3)
    assert stats.failed_final == 1 and len(results) == 1


def test_trust_weights_humans_over_judge_over_agent():
    # agent is confident, judge disagrees, humans agreed on similar items -> humans dominate
    assert trust_score(agent_conf=0.95, judge_p=0.1, human_signal=1.0) > 0.6
    # agent confident but judge and humans disagree -> low trust
    assert trust_score(agent_conf=0.99, judge_p=0.1, human_signal=0.0) < 0.15
    # missing signals are renormalised away
    assert trust_score(agent_conf=None, judge_p=0.8, human_signal=None) == 0.8
    w = TrustWeights()
    assert w.human > w.judge > w.agent


def test_human_signal_uses_similar_decisions_and_leave_one_out():
    sig = HumanSignal([("description_template", "drain clogged in kitchen", {"category": "plumbing"}),
                       ("description_template", "drain clogged in kitchen sink", {"category": "plumbing"}),
                       ("description_template", "paint peeling in lobby", {"category": "general_maintenance"})])
    assert sig("description_template", "drain clogged in kitchen", {"category": "plumbing"}) == 1.0  # self excluded
    assert sig("description_template", "drain clogged in kitchen", {"category": "janitorial"}) == 0.0
    assert sig("description_template", "wasps in canteen", {"category": "pest_control"}) is None


def test_human_signal_compares_the_whole_decision():
    sig = HumanSignal([("category_label", "trash overflowing kitchen west", {"category": "janitorial",
                                                                            "label_kind": "description_text"})])
    right_category_wrong_kind = {"category": "janitorial", "label_kind": "category_label"}
    assert sig("category_label", "trash overflowing kitchen east", right_category_wrong_kind) == 0.0


def test_classification_agent_policy_uses_trust_when_a_judge_is_configured():
    from medallion.agents.classification import ClassificationAgent, LabelDecision

    agent = ClassificationAgent(classifier=None, proposals=None, auto_approve_confidence=0.85,  # type: ignore[arg-type]
                                strategy_name="t", judge=object())  # type: ignore[arg-type]
    confident_but_disputed = LabelDecision("x", "category_label", "hvac", 0.99, "llm:p/m")
    ok, reason = agent._policy(confident_but_disputed, {"trust": 0.2, "judge": {"agrees": False}})
    assert not ok and "trust" in reason
    ok, reason = agent._policy(confident_but_disputed, None)  # judge configured but no verdict -> fail safe
    assert not ok and "fail safe" in reason
    no_judge = ClassificationAgent(classifier=None, proposals=None, auto_approve_confidence=0.85,  # type: ignore[arg-type]
                                   strategy_name="t")
    assert no_judge._policy(confident_but_disputed, None)[0]  # no judge configured -> agent confidence policy
    ok, reason = agent._policy(LabelDecision("x", "category_label", "hvac", 0.99, "rules"), {"trust": 1.0, "judge": {}})
    assert not ok and "rule-based" in reason


def test_each_repair_round_sees_the_latest_attempt():
    seen = []

    def repair(failed, round_no):
        seen.append((round_no, [f.item for f in failed]))
        return [f.item + "-retry" for f in failed]

    repair_loop(["bad"], _verify, repair, key=lambda s: s.split("-")[0], max_rounds=2)
    assert seen == [(1, ["bad"]), (2, ["bad-retry"])]  # round 2 differs from round 1 (no cache replay)


def test_review_queue_precision_and_recall_at_k():
    from medallion.agents.experiments import review_queue_ranking
    scored = [{"correct": c, "agent_conf": a, "judge_p": a, "trust": a}
              for c, a in [(False, 0.1), (False, 0.2), (True, 0.3), (True, 0.9), (True, 0.95)]]
    r = review_queue_ranking(scored, ks=(2, 3))
    assert r["trust (human-weighted)"]["@2"] == {"precision": 1.0, "recall": 1.0}
    assert r["trust (human-weighted)"]["@3"]["precision"] == round(2 / 3, 3)
    assert r["random_baseline_precision"] == 0.4
