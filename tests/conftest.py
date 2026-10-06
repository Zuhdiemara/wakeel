import json
from pathlib import Path

import pytest

from wakeel.graph import Agent, Deps
from wakeel.ledger import demo_ledger
from wakeel.llm import Reply, Scripted, ToolCall
from wakeel.rag import Index, load_corpus

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def index():
    return Index(load_corpus(ROOT / "corpus"))


def scripted(ops_plan, reply_text="", supervisor_intent="duplicate_charge", fake_citation=True):
    """A stand-in model that answers each node by recognising its system prompt.
    ops_plan: a list of tool-call lists, one per ReAct turn; then a summary."""
    turn = {"n": 0}

    def fn(msgs, tools, json_mode):
        system = msgs[0]["content"]
        if "triage" in system:
            return Reply(text=json.dumps({"intent": supervisor_intent}), tokens_in=50, tokens_out=8)
        if "grade search results" in system:
            n = msgs[1]["content"].count("\n\n[") + 1
            return Reply(text=json.dumps({"scores": [3] + [1] * (n - 1)}))
        if "ONLY the policy passages" in system:
            ids = [p["id"] for p in json.loads(msgs[1]["content"])["passages"]]
            cites = ["made_up#9"] + ids[:1] if fake_citation else ids[:1]
            return Reply(text=json.dumps({"answer": "Duplicate charges are refunded.", "citations": cites}))
        if "operations agent" in system:
            i = turn["n"]
            turn["n"] += 1
            if i < len(ops_plan):
                return Reply(tool_calls=[ToolCall(f"c{i}{j}", name, args) for j, (name, args) in enumerate(ops_plan[i])])
            return Reply(text="Investigation complete.")
        if "Write a short, warm reply" in system:
            return Reply(text=reply_text)
        raise AssertionError("unexpected prompt: " + system[:60])

    return Scripted(fn)


@pytest.fixture
def make_agent(index):
    def _make(llm=None):
        ledger = demo_ledger()
        return Agent(Deps(llm, index, ledger)), ledger
    return _make
