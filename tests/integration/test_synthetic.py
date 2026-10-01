"""Random synthetic data with ground truth: the pipeline must do exactly what the truth says."""

import pytest

from medallion.observability.context import new_trace_id
from medallion.pipeline.runner import create_run
from medallion.synthetic import generate, score

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("seed", [3, 101])
def test_pipeline_matches_ground_truth_on_random_messy_data(container, tmp_path, seed):
    csv_path, truth_path = generate(1500, seed, out_dir=tmp_path)
    run_id = create_run(container.db, trigger="test", params={"source_path": str(csv_path)}, trace_id=new_trace_id())
    container.runner.execute(run_id)
    s = score(container.db, truth_path, csv_path.name)
    assert s["reconciled"]
    assert s["quarantine"]["actual"] == s["quarantine"]["expected"]
    assert s["duplicates"]["recall_pct"] == 100.0 and s["duplicates"]["false_merges"] == 0
    assert s["created_at_exact_pct"] == 100.0 and s["cost_usd_exact_pct"] == 100.0
    assert s["category_pct"] >= 99.0  # offline: unseen drift values wait for review, the rest is exact


def test_generator_is_deterministic(tmp_path):
    a, _ = generate(200, 5, out_dir=tmp_path / "a")
    b, _ = generate(200, 5, out_dir=tmp_path / "b")
    assert a.read_bytes() == b.read_bytes()
