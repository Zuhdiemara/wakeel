"""Ten reviewers press "approve" on the same case at the same moment, and
fifty customers open cases at once: money moves exactly once."""
from concurrent.futures import ThreadPoolExecutor

from wakeel.graph import Agent, Deps
from wakeel.ledger import demo_ledger


def test_simultaneous_approvals_pay_once(index):
    ledger = demo_ledger()
    agent = Agent(Deps(None, index, ledger))
    c = agent.start("sara", "I was charged twice at Jarir Bookstore")
    with ThreadPoolExecutor(10) as pool:
        results = list(pool.map(lambda i: agent.decide(c["case_id"], True, f"reviewer{i}"), range(10)))
    assert ledger.refund_calls == 1
    assert {r["status"] for r in results} == {"refunded"}          # every reviewer sees the final state
    assert agent.get(c["case_id"])["status"] == "refunded"


def test_many_customers_at_once(index):
    ledger = demo_ledger()
    agent = Agent(Deps(None, index, ledger))
    with ThreadPoolExecutor(16) as pool:
        cases = list(pool.map(lambda i: agent.start("sara", "Jarir charged me twice"), range(50)))
    assert all(c["proposal"]["transaction_id"] == "tx_1002" for c in cases)
    with ThreadPoolExecutor(16) as pool:
        list(pool.map(lambda c: agent.decide(c["case_id"], True, "r"), cases))
    # Fifty cases about the same duplicate: one refund is paid; the other
    # forty-nine find nothing left to refund (the ledger enforces it).
    assert ledger.refund_calls == 1
    tx = next(t for t in ledger.transactions("sara") if t.id == "tx_1002")
    assert tx.refunded == tx.amount
