import json

from wakeel.ledger import demo_ledger
from wakeel.tools import CaseTools


def tools(index, customer="sara"):
    return CaseTools(customer, demo_ledger(), index)


def call(t, name, **args):
    return json.loads(t.call(name, args))


def test_duplicates_found_in_code(index):
    groups = call(tools(index), "find_duplicates")["groups"]
    jarir = next(g for g in groups if g["merchant"] == "Jarir Bookstore")
    assert jarir == {**jarir, "original": "tx_1001", "duplicates": ["tx_1002"], "within_report_window": True}
    assert not next(g for g in groups if g["merchant"] == "Nahdi Pharmacy")["within_report_window"]


def test_refund_rules_are_enforced_by_code(index):
    t = tools(index)
    p = lambda tx, amt, reason="duplicate_charge": call(t, "propose_refund", transaction_id=tx, amount_sar=amt, reason=reason, policy_section="disputes#3")
    assert "original" in p("tx_1001", 349)["error"]               # never the original
    assert "no such transaction" in p("tx_2002", 4599)["error"]    # another customer's
    assert "60 days" in p("tx_1008", 65)["error"]                  # too late
    assert "between" in p("tx_1002", 500)["error"]                 # more than charged
    assert "not a duplicate" in p("tx_1003", 18.5)["error"]        # nothing identical
    assert t.proposal is None
    assert p("tx_1002", 349)["proposed"] and t.proposal["amount"] == 34900


def test_unknown_tool_and_bad_arguments_are_reported_to_the_model(index):
    t = tools(index)
    assert "unknown tool" in call(t, "transfer_money", to="x")["error"]
    assert "bad arguments" in call(t, "list_transactions", account="omar")["error"]
