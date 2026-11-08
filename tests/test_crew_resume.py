"""The CrewAI comparison survives a used-up daily quota: it keeps finished runs and continues next time."""
import importlib
import json
import sys
import types

import pytest


@pytest.fixture
def crew(monkeypatch, tmp_path):
    # CrewAI lives in its own environment; the resume logic does not need it.
    fake = types.ModuleType("crewai")
    fake.LLM = fake.Agent = fake.Crew = fake.Task = lambda *a, **k: None
    fake.Process = types.SimpleNamespace(sequential="sequential")
    tools = types.ModuleType("crewai.tools")
    tools.tool = lambda name: (lambda f: f)
    monkeypatch.setitem(sys.modules, "crewai", fake)
    monkeypatch.setitem(sys.modules, "crewai.tools", tools)
    monkeypatch.setenv("GROQ_API_KEY", "k")
    mod = importlib.import_module("crew.dispute_crew")
    monkeypatch.setattr(mod, "PROGRESS", tmp_path / "progress.json")
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    (tmp_path / "evals").mkdir()
    real = mod.Path(mod.__file__).resolve().parent.parent / "evals"
    for f in ("agent.jsonl", "injection.jsonl"):
        (tmp_path / "evals" / f).write_text((real / f).read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(mod, "Index", lambda *a, **k: None)
    monkeypatch.setattr(mod, "load_corpus", lambda *a, **k: [])
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return mod


def fake_runs(mod, monkeypatch, quota):
    """Answers correctly until `quota` calls are used, then every call is rate-limited."""
    refunds = {json.loads(l)["msg"]: json.loads(l)["refund"] for l in (mod.ROOT / "evals" / "agent.jsonl").read_text().splitlines() if l.strip()}
    calls = []

    def run_case(msg, index, llm):
        calls.append(msg)
        if len(calls) > quota:
            return {"proposal": None, "seconds": 0.1, "refunds_moved": 0, "error": "RateLimitError: 429"}
        return {"proposal": refunds.get(msg), "seconds": 1.0, "refunds_moved": 0, "error": ""}

    monkeypatch.setattr(mod, "run_case", run_case)
    return calls


def test_quota_pauses_then_the_next_run_finishes(crew, monkeypatch):
    calls = fake_runs(crew, monkeypatch, quota=10)
    with pytest.raises(SystemExit) as stop:
        crew.main()
    assert stop.value.code == crew.QUOTA_USED_UP
    assert len(json.loads(crew.PROGRESS.read_text())["results"]) == 10

    calls = fake_runs(crew, monkeypatch, quota=1000)      # a fresh quota the next day
    crew.main()
    assert len(calls) == 35 - 10                           # only the runs still missing
    out = json.loads((crew.ROOT / "evals" / "crew.json").read_text())
    assert out["decisions"] == "22/22" and out["valid"] and out["runs_with_errors"] == "0/35"
    assert not crew.PROGRESS.exists()                      # complete: the next run measures afresh


def test_progress_from_another_model_is_ignored(crew, monkeypatch):
    fake_runs(crew, monkeypatch, quota=5)
    with pytest.raises(SystemExit):
        crew.main()
    monkeypatch.setattr(crew, "MODEL", "openai/another-model")
    calls = fake_runs(crew, monkeypatch, quota=1000)
    crew.main()
    assert len(calls) == 35                                # nothing reused across models
