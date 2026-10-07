"""LLM-as-judge for reply quality, with the configured models.

    GEMINI_API_KEY=... python -m evals.judge

For each case, a model grades the final reply against the facts the agent
had: grounded (no claim beyond the facts), language (the customer's), and
tone (clear, polite, no blame), each 1-5, with a reason. A judge is itself a
model: before trusting it, compare its grades with a person's on a sample.
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

from wakeel import llm as llms
from wakeel.graph import Agent, Deps
from wakeel.ledger import demo_ledger
from wakeel.rag import Index, embedder_from_env, load_corpus

HERE = Path(__file__).parent
RUBRIC = ("You grade a bank's reply to a customer. Score 1-5 for: grounded (every claim, amount and timeline is supported "
          "by the facts; 5 = nothing extra), language (the reply is in the customer's language), tone (clear, polite, no blame). "
          "Return JSON {\"grounded\": int, \"language\": int, \"tone\": int, \"reason\": str}.")


def main():
    import sys
    try:
        llms.from_env()
    except ValueError:
        sys.exit("No model keys found. Add GEMINI_API_KEY and/or GROQ_API_KEY to wakeel/.env (see .env.example).")
    model = llms.from_env()
    index = Index(load_corpus(HERE.parent / "corpus"), embedder_from_env())
    cases = [json.loads(l) for l in (HERE / "agent.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = []
    for c in cases:
        agent = Agent(Deps(model, index, demo_ledger()))
        s = agent.start("sara", c["msg"])
        if s["status"] == "awaiting_approval":
            s = agent.decide(s["case_id"], True, "judge")
        if not s.get("reply"):
            continue
        facts = {"lang": s["lang"], "policy": s.get("policy", {}).get("answer"), "proposal": s.get("proposal"), "refund": s.get("refund")}
        r = model.chat([{"role": "system", "content": RUBRIC},
                        {"role": "user", "content": json.dumps({"customer": s["text"], "facts": facts, "reply": s["reply"]}, ensure_ascii=False, default=str)}], json_mode=True)
        g = json.loads(r.text)
        rows.append({"msg": c["msg"], **{k: g.get(k) for k in ("grounded", "language", "tone", "reason")}})
    (HERE / "judge.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    for k in ("grounded", "language", "tone"):
        print(f"{k:9} mean {statistics.mean(r[k] for r in rows):.2f}  min {min(r[k] for r in rows)}")
    for r in rows:
        if min(r["grounded"], r["language"], r["tone"]) < 4:
            print("  review:", r["msg"], "→", r["reason"])


if __name__ == "__main__":
    main()
