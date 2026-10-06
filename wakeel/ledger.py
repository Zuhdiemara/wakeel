"""The bank's ledger, as the agent sees it: a customer's card transactions,
and refunds that are idempotent.

Agents retry: a timeout after the refund call, a resumed graph, a reviewer
double-clicking "approve". Every refund therefore carries an idempotency key
derived from the case and the transaction, so a repeat returns the first
answer instead of paying twice. MemoryLedger implements that in-process;
DaftarLedger delegates it to Daftar, a double-entry ledger with exactly-once
requests (github.com/Zuhdiemara/daftar).
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx


@dataclass
class Txn:
    id: str
    merchant: str
    amount: int          # halalas: 34900 = 349.00 SAR
    at: str              # ISO 8601, UTC
    last4: str = "4821"
    currency: str = "SAR"
    refunded: int = 0

    def view(self) -> dict:
        d = asdict(self)
        d["amount_sar"], d["refunded_sar"] = self.amount / 100, self.refunded / 100
        return d


@dataclass
class RefundResult:
    ok: bool
    txn_id: str
    amount: int
    replayed: bool = False
    error: str = ""
    ref: str = ""


class Ledger(Protocol):
    def transactions(self, customer: str) -> list[Txn]: ...
    def refund(self, customer: str, txn_id: str, amount: int, key: str) -> RefundResult: ...


class MemoryLedger:
    def __init__(self):
        self.txns: dict[str, list[Txn]] = {}
        self.done: dict[str, RefundResult] = {}   # idempotency key -> first answer
        self.lock = threading.Lock()
        self.refund_calls = 0                     # how many times money actually moved

    def transactions(self, customer: str) -> list[Txn]:
        return [Txn(**asdict(t)) for t in self.txns.get(customer, [])]

    def refund(self, customer: str, txn_id: str, amount: int, key: str) -> RefundResult:
        with self.lock:
            if key in self.done:
                prev = self.done[key]
                return RefundResult(**{**asdict(prev), "replayed": True})
            t = next((x for x in self.txns.get(customer, []) if x.id == txn_id), None)
            if t is None:
                res = RefundResult(False, txn_id, amount, error="transaction not found for this customer")
            elif amount <= 0 or amount > t.amount - t.refunded:
                res = RefundResult(False, txn_id, amount, error="amount exceeds what is refundable")
            else:
                t.refunded += amount
                self.refund_calls += 1
                res = RefundResult(True, txn_id, amount, ref=f"RF-{len(self.done) + 1:05d}")
            self.done[key] = res
            return res


def demo_ledger(now: datetime | None = None) -> MemoryLedger:
    """Two demo customers. Sara was charged twice at Jarir; Omar's data must
    never be reachable from Sara's case."""
    now = now or datetime.now(timezone.utc)
    iso = lambda d: (now - d).replace(microsecond=0).isoformat()
    L = MemoryLedger()
    L.txns["sara"] = [
        Txn("tx_1001", "Jarir Bookstore", 34900, iso(timedelta(days=3, hours=2))),
        Txn("tx_1002", "Jarir Bookstore", 34900, iso(timedelta(days=3, hours=1, minutes=52))),
        Txn("tx_1003", "Starbucks Olaya", 1850, iso(timedelta(days=2))),
        Txn("tx_1004", "Noon.com", 120000, iso(timedelta(days=20))),
        Txn("tx_1005", "Amazon.com (USD)", 37200, iso(timedelta(days=9))),
        Txn("tx_1006", "Foreign transaction fee", 930, iso(timedelta(days=9))),
        Txn("tx_1009", "HungerStation", 8750, iso(timedelta(days=1, hours=5))),
        Txn("tx_1010", "HungerStation", 8750, iso(timedelta(days=1, hours=4, minutes=58))),
        Txn("tx_1007", "Nahdi Pharmacy", 6500, iso(timedelta(days=75))),
        Txn("tx_1008", "Nahdi Pharmacy", 6500, iso(timedelta(days=75, minutes=-3))),
    ]
    L.txns["omar"] = [
        Txn("tx_2001", "Extra Electronics", 459900, iso(timedelta(days=1)), last4="1177"),
        Txn("tx_2002", "Extra Electronics", 459900, iso(timedelta(days=1, minutes=-5)), last4="1177"),
    ]
    return L


class DaftarLedger:
    """Card payments in a Daftar tenant: each customer's wallet history
    (GET /v1/accounts/liabilities:wallet:<id>/lines) and refunds through
    POST /v1/payments/{id}/refunds with an Idempotency-Key."""

    def __init__(self, base: str, key: str):
        self.base, self.h = base.rstrip("/"), {"Authorization": f"Bearer {key}"}
        self.transfers: dict[str, dict] = {}   # a captured payment never changes, so its details are cached

    def _get(self, path: str) -> dict:
        for attempt in range(4):
            r = httpx.get(self.base + path, headers=self.h, timeout=30)
            if r.status_code != 429 or attempt == 3:
                break
            time.sleep(min(float(r.headers.get("Retry-After", "1")), 5))   # Daftar's per-key rate limit
        r.raise_for_status()
        return r.json()

    def _transfer(self, tid: str) -> dict:
        if tid not in self.transfers:
            self.transfers[tid] = self._get(f"/v1/transfers/{tid}")
        return self.transfers[tid]

    def transactions(self, customer: str) -> list[Txn]:
        lines = self._get(f"/v1/accounts/liabilities:wallet:{customer}/lines?limit=500").get("lines") or []
        out: dict[str, Txn] = {}
        for ln in lines:
            tid = ln["transfer"]
            if ln["kind"] != 1 or ln["side"] != "D" or not tid.endswith(":capture"):
                continue
            t = self._transfer(tid)
            pay = tid.split(":")[1]
            refunded = sum(x["amount"] for x in lines if x["kind"] == 1 and x["side"] == "C" and x["transfer"].startswith(f"pay:{pay}:refund:"))
            out[pay] = Txn(pay, (t.get("metadata") or {}).get("merchant", "unknown"), ln["amount"],
                           datetime.fromtimestamp(ln["at"] / 1000, timezone.utc).replace(microsecond=0).isoformat(), refunded=refunded)
        return sorted(out.values(), key=lambda x: x.at)

    def refund(self, customer: str, txn_id: str, amount: int, key: str) -> RefundResult:
        if not any(t.id == txn_id for t in self.transactions(customer)):
            return RefundResult(False, txn_id, amount, error="transaction not found for this customer")
        r = httpx.post(f"{self.base}/v1/payments/{txn_id}/refunds", timeout=30,
                       headers={**self.h, "Idempotency-Key": key}, json={"id": key[-24:], "amount": amount})
        body = r.json()
        if r.status_code in (200, 201) and body.get("ok"):
            return RefundResult(True, txn_id, amount, replayed=r.headers.get("Idempotent-Replayed") == "true", ref=key[-12:])
        err = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("error")
        return RefundResult(False, txn_id, amount, error=str(err or f"HTTP {r.status_code}"))
