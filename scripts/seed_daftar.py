"""Creates a Daftar sandbox tenant with Wakeel's demo data and prints the
environment to point Wakeel at it.

    python scripts/seed_daftar.py http://localhost:8080
"""
import sys
import uuid

import httpx


def seed(base: str) -> dict:
    c = httpx.Client(base_url=base.rstrip("/"), timeout=60)
    key = c.post("/v1/tenants", json={"name": "Wakeel demo bank"}).raise_for_status().json()["api_key"]
    h = {"Authorization": f"Bearer {key}"}
    for path in ("/v1/customers/sara", "/v1/customers/omar", "/v1/merchants/jarir-bookstore", "/v1/merchants/starbucks-olaya", "/v1/merchants/extra-electronics"):
        c.post(path, headers=h).raise_for_status()

    def pay(customer, merchant, amount):
        pid = uuid.uuid4().hex[:10]
        for step, body in (("authorize", {"customer": customer, "merchant": merchant, "amount": amount, "ttl_ms": 600000}), ("capture", None)):
            r = c.post(f"/v1/payments/{pid}/{step}", headers={**h, "Idempotency-Key": f"seed-{pid}-{step}"}, json=body)
            r.raise_for_status()
        return pid

    for customer, amount in (("sara", 500000), ("omar", 1500000)):
        c.post("/v1/topups", headers={**h, "Idempotency-Key": f"seed-top-{customer}"}, json={"id": f"top-{customer}", "customer": customer, "amount": amount}).raise_for_status()
    ids = {"sara_original": pay("sara", "jarir-bookstore", 34900), "sara_duplicate": pay("sara", "jarir-bookstore", 34900),
           "sara_coffee": pay("sara", "starbucks-olaya", 1850), "omar_original": pay("omar", "extra-electronics", 459900),
           "omar_duplicate": pay("omar", "extra-electronics", 459900)}
    return {"DAFTAR_URL": base, "DAFTAR_KEY": key, **ids}


if __name__ == "__main__":
    for k, v in seed(sys.argv[1]).items():
        print(f"{k}={v}")
