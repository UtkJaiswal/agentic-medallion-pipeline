import json

import httpx
import pytest

from medallion.agents.classification import KeywordClassifier, TemplateItem
from medallion.agents.jev import JevClassifier


def _answer(q, value, confidence=0.93):
    if q == "is_safety_hazard":
        return {"type": "noul", "noul": value, "confidence": confidence}
    return {"type": "choice", "choice": value, "probabilities": {value: confidence}, "confidence": confidence}


def make(handler, **kw):
    return JevClassifier(kw.pop("taxonomy"), api_key="k", transport=httpx.MockTransport(handler), concurrency=2, **kw)


def test_jev_request_shape_and_template_parsing(taxonomy):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.url.path, request.headers["authorization"], body))
        return httpx.Response(200, json={"model": "jev-1.13.0", "usage": {"input_tokens": 50, "output_tokens": 3},
                                         "answers": {"category": _answer("category", "electrical"),
                                                     "severity": _answer("severity", "critical", 0.8),
                                                     "is_safety_hazard": _answer("is_safety_hazard", 0.91)}})

    out = make(handler, taxonomy=taxonomy).classify_templates([TemplateItem("sparks from socket", "Sparks!")])
    path, auth, body = seen[0]
    assert path.endswith("/systemone") and auth == "Bearer k" and body["model"] == "jev-latest"
    assert set(body["questions"]["category"]["criteria"]) == set(taxonomy.names)  # answers can't leave taxonomy
    d = out[0]
    assert (d.category, d.severity, d.is_safety_hazard, d.confidence) == ("electrical", "critical", True, 0.8)


def test_jev_retries_overload_then_falls_back_per_item(taxonomy, monkeypatch):
    monkeypatch.setattr("medallion.resilience.retry.time.sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(529, json={"error": "overloaded"})

    clf = make(handler, taxonomy=taxonomy, fallback=KeywordClassifier(taxonomy))
    out = clf.classify_labels(["plumbing"])
    assert calls["n"] == 4  # retried with backoff (RetryPolicy.max_attempts)
    assert out[0].source == "rules" and out[0].category == "plumbing"


def test_jev_permanent_error_is_not_retried(taxonomy):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, json={"error": "bad key"})

    assert make(handler, taxonomy=taxonomy).classify_labels(["x"]) == [] and calls["n"] == 1


def test_jev_via_openrouter_decisions(taxonomy):
    """OpenRouter's response omits `confidence` on some answers: the chosen option's probability is used."""
    from medallion.agents.evaluation import MemoryRecorder
    seen, recorder = [], MemoryRecorder()

    def handler(request):
        body = json.loads(request.content)
        seen.append((request.url, body))
        return httpx.Response(200, json={
            "model": "typesafe/jev-1.13-20260917", "provider": "TypeSafe",
            "usage": {"input_tokens": 395, "output_tokens": 63, "cost": 1.659e-05},
            "answers": {"category": {"type": "choice", "choice": "plumbing", "probabilities": {"plumbing": 0.9}},
                        "label_kind": {"type": "choice", "choice": "category_label", "confidence": 0.97}}})

    clf = JevClassifier(taxonomy, api_key="k", model="typesafe/jev-1.13", base_url="https://openrouter.ai/api/alpha",
                        path="/decisions", provider="openrouter", transport=httpx.MockTransport(handler),
                        recorder=recorder)
    d = clf.classify_labels(["plumbng - call jo@example.com"])[0]
    url, body = seen[0]
    assert str(url) == "https://openrouter.ai/api/alpha/decisions" and body["model"] == "typesafe/jev-1.13"
    assert "jo@example.com" not in json.dumps(body)  # PII redacted before leaving the process
    assert (d.category, d.confidence) == ("plumbing", 0.9)
    call = recorder.calls[0]
    assert (call.provider, call.cost_usd, call.served_model) == ("openrouter", 1.659e-05, "typesafe/jev-1.13-20260917")


def test_jev_route_selection(taxonomy, monkeypatch, tmp_path):
    from medallion.agents.jev import build_jev
    from medallion.settings import Settings
    monkeypatch.chdir(tmp_path)  # no .env here: only the variables set below are seen
    for var in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        build_jev(Settings(), taxonomy, None, None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    assert build_jev(Settings(), taxonomy, None, None)[1] == "openrouter-decisions:typesafe/jev-1.13"
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key")  # a direct key wins
    assert build_jev(Settings(), taxonomy, None, None)[1] == "typesafe/jev-latest"
