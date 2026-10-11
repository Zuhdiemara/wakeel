"""Exports reviewed cases as training data for a smaller model (for example
an Arabic model hosted in the Kingdom), in the chat format most fine-tuning
tools accept.

    python -m wakeel.dataset out.jsonl          # reads WAKEEL_DB or WAKEEL_DATABASE_URL

Only what people confirmed becomes a label: the triage of cases that were
resolved, and each refund proposal with the reviewers' decision. Only the
masked text is exported, never the raw message, and the customer id is left
out. Nothing here trains anything: it builds the dataset, so the decision to
fine-tune can be made on real, reviewed examples.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

from . import schemas
from .graph import Agent, Deps
from .ledger import MemoryLedger
from .store import Store

TRIAGE_PROMPT = f"Classify the bank customer's card-dispute request. Return JSON {{\"intent\": one of {schemas.INTENTS}}}."
REVIEW_PROMPT = "Decide whether the evidence supports this proposed refund. Return JSON {\"verdict\": \"supports\" | \"doubt\"}."
DONE = ("refunded", "rejected", "answered", "handed_off")


def examples(agent: Agent, store: Store, limit: int = 10000):
    for row in store.cases(limit=limit):
        if row["status"] not in DONE:
            continue
        c = agent.get(row["case_id"])
        text = c.get("text")
        if not text or c.get("injection"):
            continue                                  # attacks are not examples of good triage
        yield {"task": "triage", "case": row["case_id"], "messages": [
            {"role": "system", "content": TRIAGE_PROMPT}, {"role": "user", "content": text},
            {"role": "assistant", "content": json.dumps({"intent": c["intent"]})}]}
        d, p = c.get("decision"), c.get("proposal")
        if d and p:                                   # people decided: the label is theirs, not the model's
            evidence = {"proposal": {k: p.get(k) for k in ("merchant", "amount", "reason", "policy_section")},
                        "facts": (c.get("verification") or {}).get("facts"), "message": text}
            yield {"task": "review", "case": row["case_id"], "messages": [
                {"role": "system", "content": REVIEW_PROMPT}, {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)},
                {"role": "assistant", "content": json.dumps({"verdict": "supports" if d.get("approved") else "doubt"})}]}


def _open():
    url = os.getenv("WAKEEL_DATABASE_URL")
    if url:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        saver = PostgresSaver(ConnectionPool(url, kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row}))
        return saver, Store.postgres(ConnectionPool(url, kwargs={"autocommit": True}))
    from langgraph.checkpoint.sqlite import SqliteSaver
    path = os.getenv("WAKEEL_DB", ".data/wakeel.db")
    return SqliteSaver(sqlite3.connect(path, check_same_thread=False)), Store.sqlite(path)


def main(argv: list[str]) -> None:
    out = argv[1] if len(argv) > 1 else "dataset.jsonl"
    saver, store = _open()
    agent = Agent(Deps(None, None, MemoryLedger()), checkpointer=saver)   # reads saved cases only
    counts: dict[str, int] = {}
    with open(out, "w", encoding="utf-8") as f:
        for ex in examples(agent, store):
            counts[ex["task"]] = counts.get(ex["task"], 0) + 1
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")
    print(f"wrote {out}: {counts or 'no reviewed cases yet'}")


if __name__ == "__main__":
    main(sys.argv)
