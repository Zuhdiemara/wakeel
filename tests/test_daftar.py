"""Against a running Daftar (set DAFTAR_TEST_URL), the agent finds the
duplicate, a human approves, and a retried refund is replayed, not paid twice."""
import os
import sys
from pathlib import Path

import pytest

from wakeel.graph import Agent, Deps
from wakeel.ledger import DaftarLedger

pytestmark = pytest.mark.skipif(not os.getenv("DAFTAR_TEST_URL"), reason="DAFTAR_TEST_URL not set")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def test_dispute_on_a_real_ledger(index):
    from seed_daftar import seed
    env = seed(os.environ["DAFTAR_TEST_URL"])
    ledger = DaftarLedger(env["DAFTAR_URL"], env["DAFTAR_KEY"])
    txns = ledger.transactions("sara")
    assert {t.merchant for t in txns} == {"jarir-bookstore", "starbucks-olaya"}

    agent = Agent(Deps(None, index, ledger))
    c = agent.start("sara", "I was charged twice at Jarir Bookstore")
    assert c["proposal"]["transaction_id"] == env["sara_duplicate"]
    assert agent.start("sara", "please refund it").get("proposal") is None
    done = agent.decide(c["case_id"], True, "reviewer")
    assert done["status"] == "refunded" and done["refund"]["ok"]

    # The same refund again (a retry after a timeout): Daftar replays it.
    again = ledger.refund("sara", env["sara_duplicate"], 34900, f"wakeel:{c['case_id']}:{env['sara_duplicate']}")
    assert again.ok and again.replayed
    dup = next(t for t in ledger.transactions("sara") if t.id == env["sara_duplicate"])
    assert dup.refunded == 34900                                   # paid once
    # Omar's payments are invisible from Sara's ledger view.
    assert ledger.refund("sara", env["omar_duplicate"], 459900, "x").error
