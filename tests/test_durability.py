import sqlite3

from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver

from wakeel.graph import Agent, Deps
from wakeel.ledger import demo_ledger
from wakeel.store import Store


def test_a_case_awaiting_approval_survives_a_restart(index, tmp_path):
    db = str(tmp_path / "w.db")
    ledger = demo_ledger()

    def agent():
        return Agent(Deps(None, index, ledger), checkpointer=SqliteSaver(sqlite3.connect(db, check_same_thread=False)),
                     store=Store.sqlite(db))

    first = agent()
    c = first.start("sara", "I was charged twice at Jarir Bookstore")
    assert c["status"] == "awaiting_approval"
    del first                                     # the process restarts
    second = agent()
    assert [x["case_id"] for x in second.store.cases("awaiting_approval")] == [c["case_id"]]
    done = second.decide(c["case_id"], True, "noura")
    assert done["status"] == "refunded" and ledger.refund_calls == 1
    actions = [a["action"] for a in second.store.trail(c["case_id"])]
    assert actions == ["case_opened", "refund_proposed", "approved", "refund_executed"]
    assert second.store.verify() == {"ok": True, "entries": 4}


def test_the_audit_trail_is_append_only_and_tamper_evident(index, tmp_path):
    db = str(tmp_path / "a.db")
    s = Store.sqlite(db)
    for i in range(3):
        s.audit(f"case_{i}", "reviewer:x", "approved")
    raw = sqlite3.connect(db)
    try:
        raw.execute("update audit set actor = 'reviewer:y' where seq = 2")
        raise AssertionError("an update was allowed")
    except sqlite3.DatabaseError:
        pass
    # Even with the trigger dropped, an edit breaks the chain at that entry.
    raw.execute("drop trigger audit_append_only_u")
    raw.execute("update audit set actor = 'reviewer:y' where seq = 2")
    raw.commit()
    assert Store.sqlite(db).verify() == {"ok": False, "entries": 3, "broken_at": 2}


def test_streaming_queue_metrics_and_audit_over_http(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKEEL_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("WAKEEL_DEMO", "1")
    from wakeel import api
    from wakeel.auth import Auth
    api.agent, api.info = api.build()
    api.auth = Auth.from_env()
    c = TestClient(api.app)
    tok = lambda kind, who: {"Authorization": "Bearer " + c.post("/api/demo/token", json={"kind": kind, "subject": who}).json()["token"]}
    sara, noura, maha = tok("customer", "sara"), tok("staff", "noura"), tok("staff", "maha")
    with c.stream("POST", "/api/cases/stream", json={"message": "Jarir charged me twice"}, headers=sara) as r:
        body = "".join(r.iter_text())
    assert body.count("event: step") >= 4 and "event: case" in body
    queue = c.get("/api/cases", params={"status": "awaiting_approval"}, headers=noura).json()["cases"]
    assert len(queue) == 1
    cid = queue[0]["case_id"]
    c.post(f"/api/cases/{cid}/decision", json={"approved": True}, headers=noura)
    assert [a["action"] for a in c.get(f"/api/cases/{cid}/audit", headers=noura).json()["audit"]][-1] == "refund_executed"
    assert c.get("/api/audit/verify", headers=maha).json()["ok"]
    m = c.get("/metrics").text
    assert "wakeel_node_seconds_bucket" in m and 'wakeel_decisions_total{approved="true"}' in m and "wakeel_jobs" in m
