from wakeel.cache import SemanticCache
from wakeel.graph import Agent, Deps
from wakeel.ledger import demo_ledger
from wakeel.llm import Reply, Scripted


def test_repeated_policy_questions_skip_the_model(index):
    calls = []

    def fn(msgs, tools, json_mode):
        calls.append(msgs[0]["content"][:30])
        system = msgs[0]["content"]
        if "triage" in system:
            return Reply(text='{"intent": "fee_question"}')
        if "grade search results" in system:
            return Reply(text='{"scores": [3, 1, 1, 1, 1, 1]}')
        if "ONLY the policy passages" in system:
            import json
            ids = [p["id"] for p in json.loads(msgs[1]["content"])["passages"]]
            return Reply(text=json.dumps({"answer": "A 2.5% foreign transaction fee applies.", "citations": ids[:1]}))
        return Reply(text="A 2.5% foreign transaction fee applies [fees#2].")

    cache = SemanticCache(index.embedder, "v1", threshold=0.8)
    agent = Agent(Deps(Scripted(fn), index, demo_ledger(), cache=cache))
    agent.start("sara", "Why is there a foreign transaction fee on my statement?")
    first = len(calls)
    c = agent.start("sara", "why is there a foreign transaction fee on my card statement")
    policy = next(t for t in c["trace"] if t["node"] == "policy_agent")
    assert policy.get("cache_hit") and cache.hits == 1
    assert len(calls) - first < first                          # fewer model calls the second time
    # Ledger cases are never served from the cache.
    agent2 = Agent(Deps(None, index, demo_ledger(), cache=cache))
    d = agent2.start("sara", "I was charged twice at Jarir")
    assert not next(t for t in d["trace"] if t["node"] == "policy_agent").get("cache_hit")
