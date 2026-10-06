from fastapi.testclient import TestClient

from wakeel import api


def test_http_flow():
    c = TestClient(api.app)
    assert c.get("/api/health").json()["ok"]
    assert c.post("/api/reset").json()["reset"]
    r = c.post("/api/cases", json={"customer": "sara", "message": "I was charged twice at Jarir Bookstore"}).json()
    assert r["status"] == "awaiting_approval"
    done = c.post(f"/api/cases/{r['case_id']}/decision", json={"approved": True, "reviewer": "t"}).json()
    assert done["status"] == "refunded"
    txns = c.get("/api/customers/sara/transactions").json()["transactions"]
    assert next(t for t in txns if t["id"] == "tx_1002")["refunded"] == 34900
    assert c.post("/api/cases", json={"customer": "mallory", "message": "hello there"}).status_code == 404
    assert c.get("/api/cases/nope").status_code == 404
    assert c.get("/").status_code == 200
