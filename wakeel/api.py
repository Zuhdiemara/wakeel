"""HTTP service: the customer, the reviewer and the demo page.

    uvicorn wakeel.api:app --port 8000
"""
from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import llm as llms
from .graph import Agent, Deps
from .ledger import DaftarLedger, demo_ledger
from .rag import Index, embedder_from_env, load_corpus

ROOT = Path(__file__).resolve().parent.parent
DEMO_CUSTOMERS = {"sara": "Sara (charged twice at Jarir)", "omar": "Omar (another customer)"}


def build() -> tuple[Agent, dict]:
    switches: list = []
    try:
        model = llms.from_env(on_switch=lambda p, e: switches.append({"provider": p, "error": e[:120], "at": time.time()}))
    except ValueError:
        model = None                                    # no keys: rules mode
    ledger = DaftarLedger(os.environ["DAFTAR_URL"], os.environ["DAFTAR_KEY"]) if os.getenv("DAFTAR_URL") else demo_ledger()
    index = Index(load_corpus(ROOT / "corpus"), embedder_from_env())
    info = {"models": model.name if model else "none (rules mode)", "embeddings": index.embedder.name,
            "ledger": "daftar" if os.getenv("DAFTAR_URL") else "in-memory demo", "chunks": len(index.chunks), "fallbacks": switches}
    return Agent(Deps(model, index, ledger)), info


agent, info = build()
app = FastAPI(title="Wakeel", version="1.0", description="A card-dispute agent for a fictional Saudi bank.")
_hits: dict[str, deque] = defaultdict(deque)


@app.middleware("http")
async def limits(request: Request, call_next):
    """10 cases a minute per address: each case costs model calls."""
    if request.method == "POST" and request.url.path == "/api/cases":
        ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[-1].strip()
        q, now = _hits[ip], time.monotonic()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= 10:
            return JSONResponse({"error": "too many cases; wait a minute"}, status_code=429, headers={"Retry-After": "60"})
        q.append(now)
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


class NewCase(BaseModel):
    customer: str
    message: str = Field(min_length=3, max_length=2000)


class Decision(BaseModel):
    approved: bool
    reviewer: str = Field(min_length=1, max_length=60)
    note: str = Field(default="", max_length=300)


@app.get("/api/health")
def health():
    return {"ok": True, **info}


@app.post("/api/cases")
def new_case(body: NewCase):
    if body.customer not in DEMO_CUSTOMERS:
        raise HTTPException(404, "unknown customer")
    return agent.start(body.customer, body.message)


@app.get("/api/cases/{case_id}")
def get_case(case_id: str):
    c = agent.get(case_id)
    if not c.get("case_id"):
        raise HTTPException(404, "no such case")
    return c


@app.post("/api/cases/{case_id}/decision")
def decide(case_id: str, body: Decision, x_reviewer_token: str | None = Header(default=None)):
    want = os.getenv("REVIEWER_TOKEN")
    if want and x_reviewer_token != want:
        raise HTTPException(403, "reviewer token required")
    if not agent.get(case_id).get("case_id"):
        raise HTTPException(404, "no such case")
    return agent.decide(case_id, body.approved, body.reviewer, body.note)


@app.get("/api/customers")
def customers():
    return DEMO_CUSTOMERS


@app.get("/api/customers/{customer}/transactions")
def transactions(customer: str):
    if customer not in DEMO_CUSTOMERS:
        raise HTTPException(404, "unknown customer")
    return {"transactions": [t.view() for t in agent.deps.ledger.transactions(customer)]}


@app.post("/api/reset")
def reset():
    """Demo only: fresh demo data (in-memory ledger)."""
    if os.getenv("DAFTAR_URL"):
        raise HTTPException(409, "not available with a real ledger")
    agent.deps.ledger = demo_ledger()
    return {"reset": True}


app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")


@app.get("/")
def index_page():
    return FileResponse(ROOT / "web" / "index.html")
