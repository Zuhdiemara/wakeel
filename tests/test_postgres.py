"""Production state on Postgres, with two "replicas" sharing one database.
Run with WAKEEL_TEST_PG=postgresql://localhost/wakeel_test (CI does)."""
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from wakeel.graph import Agent, Deps
from wakeel.ledger import demo_ledger

URL = os.getenv("WAKEEL_TEST_PG")
pytestmark = pytest.mark.skipif(not URL, reason="WAKEEL_TEST_PG not set")


@pytest.fixture
def pg(monkeypatch):
    import psycopg
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("drop table if exists cases, audit, rate, jobs, checkpoints, checkpoint_blobs, checkpoint_writes, checkpoint_migrations cascade")
    monkeypatch.setenv("WAKEEL_DATABASE_URL", URL)
    from wakeel.api import open_state
    return open_state


def replica(index, ledger, open_state):
    saver, store = open_state()
    return Agent(Deps(None, index, ledger), checkpointer=saver, store=store)


def test_two_replicas_share_cases_and_pay_once(index, pg):
    ledger = demo_ledger()
    a, b = replica(index, ledger, pg), replica(index, ledger, pg)
    c = a.start("sara", "I was charged twice at Jarir Bookstore")
    assert c["status"] == "awaiting_approval"
    assert [x["case_id"] for x in b.store.cases("awaiting_approval")] == [c["case_id"]]   # B sees A's case
    with ThreadPoolExecutor(10) as pool:                       # 10 approvals, split across the replicas
        results = list(pool.map(lambda i: (a if i % 2 else b).decide(c["case_id"], True, f"r{i}"), range(10)))
    assert ledger.refund_calls == 1 and {r["status"] for r in results} == {"refunded"}
    assert [e["action"] for e in b.store.trail(c["case_id"])] == ["case_opened", "refund_proposed", "approved", "refund_executed"]
    assert a.store.verify()["ok"]


def test_audit_is_append_only_on_postgres(pg):
    import psycopg
    _, store = pg()
    store.audit("case_x", "reviewer:a", "approved")
    with psycopg.connect(URL, autocommit=True) as c, pytest.raises(psycopg.errors.RaiseException):
        c.execute("update audit set actor = 'reviewer:b'")


def test_rate_limits_are_shared_between_replicas(pg):
    _, s1 = pg()
    _, s2 = pg()
    hits = [s1.rate_hit("ip:1.2.3.4", 3)[0], s2.rate_hit("ip:1.2.3.4", 3)[0], s1.rate_hit("ip:1.2.3.4", 3)[0], s2.rate_hit("ip:1.2.3.4", 3)[0]]
    assert hits == [True, True, True, False]


def test_queue_workers_never_take_the_same_job(pg):
    _, s1 = pg()
    _, s2 = pg()
    ids = {s1.enqueue(f"case_{i}", "start", {"i": i}) for i in range(20)}
    taken = []
    with ThreadPoolExecutor(8) as pool:
        def work(store):
            got = []
            while (j := store.claim("w")) is not None:
                got.append(j["id"])
                store.finish(j["id"])
            return got
        for got in pool.map(work, [s1, s2] * 4):
            taken += got
    assert sorted(taken) == sorted(ids) and len(taken) == len(set(taken))
