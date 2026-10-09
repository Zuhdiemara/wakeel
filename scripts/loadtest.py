"""Load test against a running Wakeel: concurrent signed-in customers open
cases and reviewers approve them. Reports throughput, latency percentiles and
errors, and checks that no refund is paid twice.

    python scripts/loadtest.py http://localhost:8000 --customers 20 --seconds 30

Rules mode (no model keys) measures the service itself: graph, retrieval,
database, locks. With models, latency is dominated by the provider.
"""
import argparse
import json
import statistics
import threading
import time

import httpx

MESSAGES = ["I was charged twice at Jarir Bookstore", "Why is there a foreign transaction fee on my statement?",
            "تم خصم المبلغ مرتين من مكتبة جرير", "There's a charge I don't recognise, my card was stolen"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--customers", type=int, default=20)
    ap.add_argument("--seconds", type=int, default=30)
    a = ap.parse_args()
    c = httpx.Client(base_url=a.base, timeout=60)
    tok = lambda kind, who: {"Authorization": "Bearer " + c.post("/api/demo/token", json={"kind": kind, "subject": who}).json()["token"]}
    sara, noura = tok("customer", "sara"), tok("staff", "noura")
    lat, errors, codes, cases = [], [], {}, []
    lock = threading.Lock()
    stop = time.monotonic() + a.seconds

    def customer(i):
        client = httpx.Client(base_url=a.base, timeout=60)
        n = 0
        while time.monotonic() < stop:
            t0 = time.monotonic()
            try:
                r = client.post("/api/cases", json={"message": MESSAGES[(i + n) % len(MESSAGES)]}, headers=sara)
                with lock:
                    codes[r.status_code] = codes.get(r.status_code, 0) + 1
                    if r.status_code in (200, 202):
                        lat.append(time.monotonic() - t0)
                        cases.append(r.json())
                    elif r.status_code != 429:
                        errors.append(r.status_code)
            except httpx.HTTPError as e:
                with lock:
                    errors.append(type(e).__name__)
            n += 1

    threads = [threading.Thread(target=customer, args=(i,)) for i in range(a.customers)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    took = time.monotonic() - t0
    pending = [x["case_id"] for x in cases if x.get("status") == "awaiting_approval"]
    approved = 0
    for cid in pending:                                    # every reviewer approval at once would race; one each is the realistic load
        if c.post(f"/api/cases/{cid}/decision", json={"approved": True}, headers=noura).json().get("status") == "refunded":
            approved += 1
    lat.sort()
    out = {"customers": a.customers, "seconds": round(took, 1), "cases": len(cases), "cases_per_second": round(len(cases) / took, 1),
           "p50_ms": round(statistics.median(lat) * 1000) if lat else None, "p95_ms": round(lat[int(0.95 * (len(lat) - 1))] * 1000) if lat else None,
           "p99_ms": round(lat[int(0.99 * (len(lat) - 1))] * 1000) if lat else None, "status_codes": codes, "errors": len(errors),
           "refunds_paid": approved, "proposals_for_the_same_duplicate": len(pending)}
    print(json.dumps(out))
    # Many cases propose the same duplicate; the ledger must pay it once.
    assert approved <= 1, "a duplicate was refunded more than once"
    assert not errors, f"errors: {errors[:5]}"


if __name__ == "__main__":
    main()
