"""The HTTP service: who may do what, demo sign-in, queue, retention."""
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("WAKEEL_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("WAKEEL_DEMO", "1")
    monkeypatch.setenv("WAKEEL_RETENTION", "0")
    from wakeel import api as mod
    from wakeel.auth import Auth
    mod.agent, mod.info = mod.build()
    mod.auth = Auth.from_env()
    mod.retention = mod.Retention(mod.agent, mod.RETENTION_DAYS)
    return mod


def token(c, kind, who):
    return {"Authorization": "Bearer " + c.post("/api/demo/token", json={"kind": kind, "subject": who}).json()["token"]}


def test_customers_and_staff_can_only_do_their_part(api):
    c = TestClient(api.app)
    sara, omar, noura, maha = token(c, "customer", "sara"), token(c, "customer", "omar"), token(c, "staff", "noura"), token(c, "staff", "maha")
    assert c.post("/api/cases", json={"message": "Jarir charged me twice"}).status_code == 401          # no sign-in
    r = c.post("/api/cases", json={"message": "I was charged twice at Jarir Bookstore"}, headers=sara).json()
    assert r["status"] == "awaiting_approval" and "trace" not in r                                 # customers see no internals
    cid = r["case_id"]
    assert c.get(f"/api/me/cases/{cid}", headers=omar).status_code == 404                           # someone else's case
    assert c.get("/api/cases", headers=sara).status_code == 401                                     # a customer token is not staff
    assert c.get("/api/audit/verify", headers=noura).status_code == 403                             # needs supervisor
    assert c.get("/api/me/transactions", headers=omar).json()["transactions"][0]["merchant"] == "Extra Electronics"
    done = c.post(f"/api/cases/{cid}/decision", json={"approved": True}, headers=noura).json()
    assert done["status"] == "refunded" and done["decision"]["reviewer"] == "noura"                  # the signed-in reviewer
    assert c.get("/api/audit/verify", headers=maha).json()["ok"]
    forged = {"Authorization": "Bearer " + token(c, "customer", "sara")["Authorization"][7:-2] + "xx"}
    assert c.get("/api/me/transactions", headers=forged).status_code == 401


def test_large_refunds_need_two_signed_in_reviewers(api):
    c = TestClient(api.app)
    omar, noura, fahad = token(c, "customer", "omar"), token(c, "staff", "noura"), token(c, "staff", "fahad")
    cid = c.post("/api/cases", json={"message": "Extra Electronics charged me twice"}, headers=omar).json()["case_id"]
    assert c.post(f"/api/cases/{cid}/decision", json={"approved": True}, headers=noura).json()["status"] == "awaiting_approval"
    assert c.post(f"/api/cases/{cid}/decision", json={"approved": True}, headers=noura).json()["status"] == "awaiting_approval"
    assert c.post(f"/api/cases/{cid}/decision", json={"approved": True}, headers=fahad).json()["status"] == "refunded"


def test_per_customer_rate_limit(api, monkeypatch):
    monkeypatch.setattr(api, "CASES_PER_MINUTE", 2)
    c = TestClient(api.app)
    sara = token(c, "customer", "sara")
    codes = [c.post("/api/cases", json={"message": "Why is there a foreign transaction fee?"}, headers=sara).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_queued_cases_run_on_a_worker(api, monkeypatch):
    monkeypatch.setenv("WAKEEL_QUEUE", "1")
    c = TestClient(api.app)
    sara = token(c, "customer", "sara")
    r = c.post("/api/cases", json={"message": "I was charged twice at Jarir Bookstore"}, headers=sara)
    assert r.status_code == 202 and r.json()["status"] == "queued"
    cid = r.json()["case_id"]
    assert c.get(f"/api/me/cases/{cid}", headers=sara).json()["status"] == "queued"
    assert api.Worker(api.agent, "test").once()
    assert c.get(f"/api/me/cases/{cid}", headers=sara).json()["status"] == "awaiting_approval"


def test_a_failing_job_is_retried_then_dead(api, monkeypatch):
    calls = []
    monkeypatch.setattr(api.agent, "start", lambda *a, **k: (calls.append(1), (_ for _ in ()).throw(RuntimeError("model down")))[1])
    jid = api.agent.store.enqueue("case_f", "start", {"customer": "sara", "message": "x"})
    w = api.Worker(api.agent, "test")
    for _ in range(3):
        with api.agent.store._tx() as cur:
            cur.execute("update jobs set run_at = 0 where id = ?", (jid,))   # skip the backoff wait
        assert w.once()
    assert api.agent.store.job(jid)["state"] == "dead" and len(calls) == 3
    assert api.agent.store.trail("case_f")[-1]["action"] == "case_failed"


def test_retention_and_erasure(api):
    c = TestClient(api.app)
    sara, maha = token(c, "customer", "sara"), token(c, "staff", "maha")
    cid = c.post("/api/cases", json={"message": "Why is there a foreign transaction fee?"}, headers=sara).json()["case_id"]
    later = datetime.now(timezone.utc) + timedelta(days=api.RETENTION_DAYS + 1)
    assert api.retention.run(later)["cases_deleted"] == 1
    assert c.get(f"/api/me/cases/{cid}", headers=sara).status_code == 404
    assert [e["action"] for e in api.agent.store.trail(cid)][-1] == "case_data_deleted"
    cid2 = c.post("/api/cases", json={"message": "Why is there a foreign transaction fee?"}, headers=sara).json()["case_id"]
    assert c.post("/api/admin/customers/sara/erase", headers=maha).json()["erased_cases"] == 1
    assert c.get(f"/api/me/cases/{cid2}", headers=sara).status_code == 404
    assert api.agent.store.verify()["ok"]                                  # the audit chain survives erasure


def test_production_refuses_demo_settings(monkeypatch):
    from wakeel import config
    monkeypatch.setenv("WAKEEL_ENV", "production")
    monkeypatch.setenv("WAKEEL_DEMO", "1")
    errs = config.problems()
    assert any("WAKEEL_DEMO" in e for e in errs) and any("WAKEEL_DATABASE_URL" in e for e in errs) and any("STAFF" in e for e in errs)
    with pytest.raises(SystemExit):
        config.check()
