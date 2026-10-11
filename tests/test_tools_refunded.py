from datetime import datetime, timezone

from wakeel.ledger import demo_ledger
from wakeel.tools import CaseTools


def test_refunded_duplicate_is_not_offered_again():
    now = datetime.now(timezone.utc)
    ledger = demo_ledger(now)
    ledger.refund("sara", "tx_1002", 34900, "k1")              # Jarir's duplicate already refunded
    tools = CaseTools("sara", ledger, index=None, now=now, message="I was charged twice last week")
    groups = {g["merchant"]: g for g in tools.t_find_duplicates()["groups"]}
    assert groups["Jarir Bookstore"]["already_refunded"] is True
    assert groups["HungerStation"]["already_refunded"] is False
    # Only HungerStation still qualifies, so naming no merchant is no longer ambiguous.
    r = tools.t_propose_refund("tx_1010", 87.5, "duplicate_charge", "disputes#3")
    assert r.get("proposed") is True


def test_rules_mode_does_not_ask_about_a_refunded_merchant(index):
    from wakeel.graph import Agent, Deps
    ledger = demo_ledger()
    agent = Agent(Deps(None, index, ledger))
    c = agent.start("sara", "I was charged twice at Jarir Bookstore")
    agent.decide(c["case_id"], True, "noura")
    c = agent.start("sara", "I was charged twice last week")
    # Only HungerStation is left, so there is nothing to ask: it is proposed directly.
    assert c["status"] == "awaiting_approval" and c["proposal"]["transaction_id"] == "tx_1010"


# Each of these came from a live-model evaluation run (12 October 2026).

def _tools(msg="x"):
    now = datetime.now(timezone.utc)
    return CaseTools("sara", demo_ledger(now), index=None, now=now, message=msg)


def test_a_cancelled_order_needs_the_confirmation_a_person_checks():
    r = _tools().t_propose_refund("tx_1005", 372, "cancelled_still_charged", "disputes#8")
    assert "cancellation confirmation" in r["error"]


def test_a_purchase_is_not_a_fee_and_a_correct_fee_is_not_a_mistake():
    assert "not a bank fee" in _tools().t_propose_refund("tx_1003", 18.5, "fee_charged_by_mistake", "fees#6")["error"]
    assert "correct 2.5%" in _tools().t_propose_refund("tx_1006", 9.3, "fee_charged_by_mistake", "fees#6")["error"]


def test_no_question_when_the_customer_named_the_merchant():
    r = _tools("خصم مكرر في بطاقتي من جرير").t_ask_customer("Which merchant?", ["Jarir Bookstore", "HungerStation"])
    assert "already named Jarir Bookstore" in r["error"]
