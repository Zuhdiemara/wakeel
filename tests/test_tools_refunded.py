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
