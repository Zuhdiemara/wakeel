"""The agent's tools, bound to one case.

Security comes from structure, not from prompts:
  * the customer is fixed by the session; no tool takes a customer id, so a
    prompt injection cannot point the agent at someone else's account;
  * the agent can only *propose* a refund; money moves after a human
    approves (graph.py), with an idempotency key;
  * facts are checked in code: duplicates, the 60-day limit, the 5,000 SAR
    cap and the refundable amount. The model chooses; code verifies.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from .ledger import Ledger, Txn
from .rag import Index, rerank
from .text import normalise

# Arabic names customers use for the demo merchants.
_ALIASES = {"jarir": ["جرير"], "noon": ["نون"], "nahdi": ["نهدي"], "starbucks": ["ستاربكس"], "amazon": ["امازون"],
            "extra": ["اكسترا"], "hungerstation": ["هنقرستيشن", "هنقر"]}


def mentions(text: str, merchant: str) -> bool:
    """Whether the customer's text names this merchant (English or Arabic)."""
    t = normalise(text)
    for w in re.findall(r"[a-z]{4,}", merchant.lower()):
        if w in ("bookstore", "pharmacy", "olaya", "electronics"):
            continue
        if w in t or any(normalise(a) in t for a in _ALIASES.get(w, [])):
            return True
    return False

REPORT_DAYS = 60          # disputes §2
AUTO_CAP_HALALAS = 500000  # disputes §7: 5,000 SAR per case

SCHEMAS = [
    {"name": "search_policy", "description": "Search the bank's dispute, fee and privacy policies. Returns passages with section ids to cite.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "list_transactions", "description": "The customer's recent card transactions, newest first. Optionally filter by merchant name.",
     "parameters": {"type": "object", "properties": {"merchant": {"type": "string"}, "days": {"type": "integer", "description": "How far back, default 90"}}}},
    {"name": "find_duplicates", "description": "Groups of identical transactions (same merchant and amount within 24 hours). The first in each group is the original; the rest are duplicates.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "ask_customer", "description": "Ask the customer one short question when the request is ambiguous, for example which merchant when several duplicates qualify. Use at most once.",
     "parameters": {"type": "object", "properties": {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}}, "required": ["question"]}},
    {"name": "propose_refund", "description": "Propose a refund for a human to approve. Does not move money. Amount in SAR.",
     "parameters": {"type": "object", "properties": {
         "transaction_id": {"type": "string"}, "amount_sar": {"type": "number"},
         "reason": {"type": "string", "enum": ["duplicate_charge", "cancelled_still_charged", "fee_charged_by_mistake"]},
         "policy_section": {"type": "string", "description": "The section id that allows it, e.g. disputes#3"}},
         "required": ["transaction_id", "amount_sar", "reason", "policy_section"]}},
]


def _when(t: Txn) -> datetime:
    return datetime.fromisoformat(t.at)


def duplicate_groups(txns: list[Txn]) -> list[list[Txn]]:
    groups: list[list[Txn]] = []
    for t in sorted(txns, key=_when):
        for g in groups:
            if g[0].merchant == t.merchant and g[0].amount == t.amount and _when(t) - _when(g[0]) <= timedelta(hours=24):
                g.append(t)
                break
        else:
            groups.append([t])
    return [g for g in groups if len(g) > 1]


class CaseTools:
    def __init__(self, customer: str, ledger: Ledger, index: Index, rerank_llm=None, lang: str = "en", now: datetime | None = None,
                 message: str | None = None, clarified: bool = False):
        self.customer, self.ledger, self.index, self.rerank_llm, self.lang = customer, ledger, index, rerank_llm, lang
        self.message, self.clarified = message, clarified
        self.now = now or datetime.now(timezone.utc)
        self.proposal: dict | None = None
        self.question: dict | None = None
        self.cited: set[str] = set()

    def call(self, name: str, args: dict) -> str:
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return json.dumps({"error": f"unknown tool {name}"})
        try:
            return json.dumps(fn(**args), ensure_ascii=False)
        except TypeError as e:
            return json.dumps({"error": f"bad arguments: {e}"})

    def t_search_policy(self, query: str) -> dict:
        hits = rerank(self.rerank_llm, query, self.index.search(query, k=5, lang=self.lang), k=4)
        self.cited |= {h.chunk.id for h in hits}
        return {"passages": [{"id": h.chunk.id, "section": h.chunk.section, "text": h.chunk.text} for h in hits]}

    def t_list_transactions(self, merchant: str | None = None, days: int = 90) -> dict:
        since = self.now - timedelta(days=days)
        ts = [t for t in self.ledger.transactions(self.customer) if _when(t) >= since and (not merchant or merchant.lower() in t.merchant.lower())]
        return {"transactions": [t.view() for t in sorted(ts, key=_when, reverse=True)]}

    def t_find_duplicates(self) -> dict:
        out = []
        for g in duplicate_groups(self.ledger.transactions(self.customer)):
            age = (self.now - _when(g[0])).days
            out.append({"merchant": g[0].merchant, "amount_sar": g[0].amount / 100, "original": g[0].id,
                        "duplicates": [t.id for t in g[1:]], "days_ago": age, "within_report_window": age <= REPORT_DAYS})
        return {"groups": out}

    def t_ask_customer(self, question: str, options: list[str] | None = None) -> dict:
        self.question = {"question": question[:300], "options": [str(o)[:80] for o in (options or [])][:6]}
        return {"asked": True, "note": "Stop here; the customer's answer will come back as a new message."}

    def t_propose_refund(self, transaction_id: str, amount_sar: float, reason: str, policy_section: str) -> dict:
        txns = {t.id: t for t in self.ledger.transactions(self.customer)}
        t = txns.get(transaction_id)
        amount = round(float(amount_sar) * 100)
        if t is None:
            return {"error": "no such transaction on this customer's account"}
        if (self.now - _when(t)).days > REPORT_DAYS:
            return {"error": f"reported after {REPORT_DAYS} days: refer to a specialist (disputes#2)"}
        if amount <= 0 or amount > t.amount - t.refunded:
            return {"error": f"amount must be between 0 and {(t.amount - t.refunded) / 100:.2f} SAR"}
        if amount > AUTO_CAP_HALALAS:
            return {"error": "above the 5,000 SAR automated limit: refer to a specialist (disputes#7)"}
        if reason == "duplicate_charge":
            dup = next((g for g in duplicate_groups(list(txns.values())) if t in g), None)
            if dup is None:
                return {"error": "not a duplicate: no identical charge at this merchant within 24 hours (disputes#3)"}
            if dup[0].id == t.id:
                return {"error": f"{t.id} is the original purchase; refund the duplicate {dup[1].id} instead (disputes#3)"}
            # Never guess between merchants: if the customer named none and more
            # than one duplicate qualifies, the agent must ask first.
            if self.message is not None and not mentions(self.message, t.merchant):
                eligible = [g for g in duplicate_groups(list(txns.values())) if (self.now - _when(g[0])).days <= REPORT_DAYS]
                if len(eligible) > 1 and not self.clarified:
                    return {"error": "ambiguous: several duplicates qualify and the customer did not name the merchant; "
                                     "call ask_customer with the merchants as options",
                            "options": [g[0].merchant for g in eligible]}
        if not policy_section.split(".w")[0].replace(".ar", "").startswith(("disputes#", "fees#")):
            return {"error": "cite the policy section that allows this refund"}
        self.proposal = {"transaction_id": t.id, "merchant": t.merchant, "amount": amount, "reason": reason,
                         "policy_section": policy_section, "at": t.at}
        return {"proposed": True, "note": "A bank employee must approve before any money moves.", **self.proposal}
