"""Backup and restore drill for production state (Postgres).

    python scripts/restore_drill.py prepare postgresql://.../wakeel          # make real cases
    pg_dump ... | pg_restore ... into a new database                          # the backup
    python scripts/restore_drill.py verify postgresql://.../wakeel_restored <case_id>

verify proves the restored copy works: the audit chain recomputes, the case
index is complete, and a case that was waiting for approval resumes from its
restored checkpoint and completes.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("GROQ_API_KEY", "")

from wakeel.graph import Agent, Deps  # noqa: E402
from wakeel.ledger import demo_ledger  # noqa: E402
from wakeel.rag import HashEmbedder, Index, load_corpus  # noqa: E402


def agent_on(url: str) -> Agent:
    os.environ["WAKEEL_DATABASE_URL"] = url
    from wakeel.api import open_state
    saver, store = open_state()
    return Agent(Deps(None, Index(load_corpus(Path(__file__).resolve().parent.parent / "corpus"), HashEmbedder()), demo_ledger()),
                 checkpointer=saver, store=store)


def prepare(url: str) -> None:
    a = agent_on(url)
    waiting = a.start("sara", "I was charged twice at Jarir Bookstore")
    done = a.start("omar", "Extra Electronics charged me twice")
    a.decide(done["case_id"], True, "noura")
    a.decide(done["case_id"], True, "fahad")
    a.start("sara", "Why is there a foreign transaction fee?")
    assert waiting["status"] == "awaiting_approval"
    print(json.dumps({"waiting": waiting["case_id"], "cases": len(a.store.cases(limit=1000)), "audit": a.store.verify()}))
    close(a)


def verify(url: str, waiting: str) -> None:
    a = agent_on(url)
    chain = a.store.verify()
    cases = a.store.cases(limit=1000)
    assert chain["ok"] and chain["entries"] > 0, chain
    assert len(cases) == 3, cases
    resumed = a.decide(waiting, True, "maha")
    assert resumed["status"] == "refunded", resumed["status"]
    print(json.dumps({"restored_cases": len(cases), "audit_chain": chain, "waiting_case_resumed": resumed["status"]}))
    close(a)


def close(a: Agent) -> None:
    a.store.pool.close()
    a.graph.checkpointer.conn.close()


if __name__ == "__main__":
    {"prepare": lambda: prepare(sys.argv[2]), "verify": lambda: verify(sys.argv[2], sys.argv[3])}[sys.argv[1]]()
