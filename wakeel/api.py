"""HTTP service: customers, bank staff, workers and the demo page.

    uvicorn wakeel.api:app --port 8000

Every endpoint knows who is calling (auth.py): a customer acts only on their
own cases and transactions; staff need the reviewer or supervisor role; the
reviewer recorded on a decision is the signed-in person, never a field.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from . import config, guards, metrics, otel
from . import llm as llms
from .auth import Auth, AuthError, Principal
from .cache import SemanticCache
from .graph import Agent, Deps
from .ledger import DaftarLedger, demo_ledger
from .rag import Index, embedder_from_env, load_corpus
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
DEMO_CUSTOMERS = {"sara": "Sara (charged twice at Jarir)", "omar": "Omar (charged twice at Extra, 4,599 SAR)"}
DEMO_STAFF = {"noura": ("Noura (reviewer)", ("reviewer",)), "fahad": ("Fahad (reviewer)", ("reviewer",)),
              "maha": ("Maha (supervisor)", ("reviewer", "supervisor"))}
CASES_PER_MINUTE = int(os.getenv("WAKEEL_CASES_PER_MINUTE", "10"))
RETENTION_DAYS = int(os.getenv("WAKEEL_RETENTION_DAYS", "90"))


def open_state(db_path: str | None = None):
    """Checkpoints and the store: Postgres when WAKEEL_DATABASE_URL is set
    (several replicas share it), SQLite otherwise (one process)."""
    url = os.getenv("WAKEEL_DATABASE_URL")
    if url and not db_path:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        size = int(os.getenv("WAKEEL_DB_POOL", "10"))
        pool = ConnectionPool(url, min_size=1, max_size=size, open=True,
                              kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row})
        saver = PostgresSaver(pool)
        saver.setup()
        plain = ConnectionPool(url, min_size=1, max_size=size, open=True, kwargs={"autocommit": True})
        return saver, Store.postgres(plain)
    path = db_path or os.getenv("WAKEEL_DB", str(ROOT / ".data" / "wakeel.db"))
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(path, check_same_thread=False)), Store.sqlite(path)


def build(db_path: str | None = None) -> tuple[Agent, dict]:
    config.check()

    def switched(provider: str, error: str) -> None:
        metrics.fallbacks.labels(provider).inc()
        info["fallbacks"].append({"provider": provider, "error": error[:120], "at": time.time()})
        del info["fallbacks"][:-20]

    info: dict = {"fallbacks": []}
    try:
        model = llms.from_env(on_switch=switched)
    except ValueError:
        model = None                                    # no keys: rules mode
    ledger = DaftarLedger(os.environ["DAFTAR_URL"], os.environ["DAFTAR_KEY"]) if os.getenv("DAFTAR_URL") else demo_ledger()
    index = Index(load_corpus(ROOT / "corpus"), embedder_from_env())
    checkpointer, store = open_state(db_path)
    tracer = otel.setup()

    def on_spans(spans, steps, case):
        metrics.observe(spans, steps)
        otel.export(tracer, spans, case)
        for s in spans:
            if s["node"] == "approval" and not s.get("duplicate"):
                metrics.decisions.labels(str(bool(s.get("approved"))).lower()).inc()
        if any(s["node"] in ("respond", "handoff") for s in spans) or case.get("status") in ("awaiting_approval", "needs_info"):
            metrics.cases.labels(case.get("status", "?"), case.get("intent", "?")).inc()

    corpus_version = hashlib.sha256("".join(c.text for c in index.chunks).encode()).hexdigest()[:12]
    guard = guards.guard_from_env()
    info.update({"env": "production" if config.production() else "demo", "models": model.name if model else "none (rules mode)",
                 "embeddings": index.embedder.name, "ledger": "daftar" if os.getenv("DAFTAR_URL") else "in-memory demo",
                 "chunks": len(index.chunks), "state": store.kind, "tracing": "otlp" if tracer else "off",
                 "cache": {"corpus_version": corpus_version}, "injection_screen": "patterns + prompt guard" if guard else "patterns",
                 "queue": os.getenv("WAKEEL_QUEUE") == "1", "retention_days": RETENTION_DAYS})
    deps = Deps(model, index, ledger, cache=SemanticCache(index.embedder, corpus_version), guard=guard,
                dual_approval_halalas=int(float(os.getenv("WAKEEL_DUAL_APPROVAL_SAR", "1000")) * 100))
    return Agent(deps, checkpointer=checkpointer, store=store, on_spans=on_spans), info


agent, info = build()
auth = Auth.from_env()
@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    start_background()
    yield
    stop_background()


app = FastAPI(title="Wakeel", version="2.0", description="A card-dispute agent for a fictional Saudi bank.", lifespan=lifespan)


@app.exception_handler(AuthError)
async def auth_error(_: Request, e: AuthError):
    return JSONResponse({"error": str(e)}, status_code=e.status, headers={"WWW-Authenticate": "Bearer"} if e.status == 401 else None)


@app.middleware("http")
async def headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = resp.headers.get("Cache-Control", "no-store")
    return resp


def _limit(who: Principal) -> None:
    """Cases per minute per customer, shared by every replica (in the store)."""
    ok, wait = agent.store.rate_hit(f"cases:{who.subject}", CASES_PER_MINUTE)
    if not ok:
        raise HTTPException(429, "too many cases; wait a minute", headers={"Retry-After": str(wait)})


def _own_case(case_id: str, who: Principal) -> dict:
    c = agent.get(case_id)
    owner = c.get("customer") or agent.store.case_owner(case_id)
    if owner is None or (who.kind == "customer" and owner != who.subject):
        raise HTTPException(404, "no such case")          # the same answer whether it exists or belongs to someone else
    return c if c.get("case_id") else {"case_id": case_id, "customer": owner, "status": "queued", "trace": []}


def _for_customer(c: dict) -> dict:
    """What a customer may see of their own case: no internal trace or tool calls."""
    keep = ("case_id", "status", "intent", "lang", "reply", "question", "proposal", "refund")
    return {k: c.get(k) for k in keep if k in c}


class NewCase(BaseModel):
    message: str = Field(min_length=3, max_length=2000)


class Decision(BaseModel):
    approved: bool
    note: str = Field(default="", max_length=300)


class Answer(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


# ---------------------------------------------------------------- operations

@app.get("/api/health")
def health():
    c = agent.deps.cache
    return {"ok": True, **info, "jobs": agent.store.job_counts(), **({"cache_hits": c.hits, "cache_misses": c.misses} if c else {})}


@app.get("/metrics")
def prometheus():
    metrics.jobs_state(agent.store.job_counts())
    return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------- customers

@app.post("/api/cases")
def new_case(body: NewCase, authorization: str | None = Header(default=None)):
    """With WAKEEL_QUEUE=1 the case is queued (202) and a worker runs it; the
    customer polls GET /api/cases/{id}. Otherwise it runs in the request."""
    who = auth.customer(authorization)
    _limit(who)
    if os.getenv("WAKEEL_QUEUE") == "1":
        case_id = f"case_{os.urandom(5).hex()}"
        agent.store.upsert_case({"case_id": case_id, "customer": who.subject, "status": "queued"})
        agent.store.enqueue(case_id, "start", {"customer": who.subject, "message": body.message})
        return JSONResponse({"case_id": case_id, "status": "queued"}, status_code=202)
    return _for_customer(agent.start(who.subject, body.message))


@app.post("/api/cases/stream")
def new_case_stream(body: NewCase, authorization: str | None = Header(default=None)):
    """Server-sent events: one 'step' event per graph node as it finishes,
    then the 'case' (the demo page shows the agent working)."""
    who = auth.customer(authorization)
    _limit(who)

    def events():
        for ev in agent.stream(who.subject, body.message):
            yield f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/me/cases/{case_id}")
def my_case(case_id: str, authorization: str | None = Header(default=None)):
    return _for_customer(_own_case(case_id, auth.customer(authorization)))


@app.post("/api/me/cases/{case_id}/reply")
def customer_reply(case_id: str, body: Answer, authorization: str | None = Header(default=None)):
    """The customer answers the agent's clarifying question."""
    _own_case(case_id, auth.customer(authorization))
    return _for_customer(agent.reply(case_id, body.message))


@app.get("/api/me/transactions")
def my_transactions(authorization: str | None = Header(default=None)):
    who = auth.customer(authorization)
    return {"transactions": [t.view() for t in agent.deps.ledger.transactions(who.subject)]}


# ---------------------------------------------------------------- bank staff

@app.get("/api/cases")
def list_cases(status: str | None = None, authorization: str | None = Header(default=None)):
    """The reviewers' queue: GET /api/cases?status=awaiting_approval"""
    auth.staff(authorization, "reviewer", "supervisor")
    return {"cases": agent.store.cases(status)}


@app.get("/api/cases/{case_id}")
def get_case(case_id: str, authorization: str | None = Header(default=None)):
    return _own_case(case_id, auth.staff(authorization, "reviewer", "supervisor"))


@app.get("/api/cases/{case_id}/audit")
def case_audit(case_id: str, authorization: str | None = Header(default=None)):
    auth.staff(authorization, "reviewer", "supervisor")
    return {"audit": agent.store.trail(case_id)}


@app.post("/api/cases/{case_id}/decision")
def decide(case_id: str, body: Decision, authorization: str | None = Header(default=None)):
    """The reviewer is whoever signed in; two different reviewers for large refunds."""
    who = auth.staff(authorization, "reviewer", "supervisor")
    _own_case(case_id, who)
    return agent.decide(case_id, body.approved, who.subject, body.note)


@app.get("/api/customers/{customer}/transactions")
def transactions(customer: str, authorization: str | None = Header(default=None)):
    auth.staff(authorization, "reviewer", "supervisor")
    return {"transactions": [t.view() for t in agent.deps.ledger.transactions(customer)]}


@app.get("/api/audit/verify")
def verify_audit(authorization: str | None = Header(default=None)):
    auth.staff(authorization, "supervisor")
    return agent.store.verify()


@app.post("/api/admin/customers/{customer}/erase")
def erase_customer(customer: str, authorization: str | None = Header(default=None)):
    """A PDPL erasure request: removes the customer's cases (messages and
    agent state). The audit trail keeps the record of decisions, as financial
    records must be kept; its entries hold no message text."""
    who = auth.staff(authorization, "supervisor")
    erased = retention.erase(customer, by=who.subject)
    return {"erased_cases": erased}


@app.post("/api/admin/retention/run")
def run_retention(authorization: str | None = Header(default=None)):
    auth.staff(authorization, "supervisor")
    return retention.run()


# ---------------------------------------------------------------- demo only

class DemoToken(BaseModel):
    kind: str = Field(pattern="^(customer|staff)$")
    subject: str


@app.get("/api/demo/people")
def demo_people():
    if auth.demo is None:
        raise HTTPException(404, "not found")
    return {"customers": DEMO_CUSTOMERS, "staff": {k: v[0] for k, v in DEMO_STAFF.items()}}


@app.post("/api/demo/token")
def demo_token(body: DemoToken):
    """Demo sign-in (WAKEEL_DEMO=1 only; refused in production)."""
    if auth.demo is None:
        raise HTTPException(404, "not found")
    if body.kind == "customer" and body.subject in DEMO_CUSTOMERS:
        return {"token": auth.demo.issue("customer", body.subject, DEMO_CUSTOMERS[body.subject].split(" (")[0])}
    if body.kind == "staff" and body.subject in DEMO_STAFF:
        name, roles = DEMO_STAFF[body.subject]
        return {"token": auth.demo.issue("staff", body.subject, name, roles)}
    raise HTTPException(404, "unknown demo person")


@app.post("/api/demo/reset")
def reset():
    """Demo only: fresh demo data (in-memory ledger)."""
    if auth.demo is None or os.getenv("DAFTAR_URL"):
        raise HTTPException(404, "not found")
    agent.deps.ledger = demo_ledger()
    return {"reset": True}


app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")


@app.get("/")
def index_page():
    return FileResponse(ROOT / "web" / "index.html")


# ---------------------------------------------------------------- workers and retention

class Worker:
    """Runs queued cases. Several per process (WAKEEL_WORKERS) and any number
    of processes (python -m wakeel.worker): the store's queue hands each job
    to exactly one of them, and retries failures with backoff."""

    def __init__(self, agent: Agent, name: str | None = None):
        self.agent, self.name = agent, name or f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"

    def once(self) -> bool:
        job = self.agent.store.claim(self.name)
        if job is None:
            return False
        try:
            if job["kind"] == "start":
                self.agent.start(job["payload"]["customer"], job["payload"]["message"], case_id=job["case_id"])
            state = self.agent.store.finish(job["id"])
        except Exception as e:                         # the case is retried; after 3 attempts it is dead and alerted on
            state = self.agent.store.finish(job["id"], f"{type(e).__name__}: {e}")
            if state == "dead":
                self.agent.store.upsert_case({"case_id": job["case_id"], "customer": job["payload"]["customer"], "status": "failed"})
                self.agent.store.audit(job["case_id"], "system", "case_failed", {"error": type(e).__name__})
        metrics.jobs_done.labels(state).inc()
        return True

    def run(self, stop: threading.Event, idle: float = 0.5) -> None:
        while not stop.is_set():
            if not self.once():
                stop.wait(idle)


class Retention:
    """PDPL: case messages and agent state are kept for WAKEEL_RETENTION_DAYS
    after a case closes, then deleted; the audit trail (no message text) is
    kept 5 years, then purged."""

    def __init__(self, agent: Agent, days: int):
        self.agent, self.days = agent, days

    def _drop(self, case_id: str) -> None:
        try:
            self.agent.graph.checkpointer.delete_thread(case_id)
        except Exception:
            pass
        self.agent.store.forget_case(case_id)

    def run(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        closed = self.agent.store.closed_cases_before(now - timedelta(days=self.days))
        for cid in closed:
            self._drop(cid)
            self.agent.store.audit(cid, "system", "case_data_deleted", {"policy": f"{self.days} days after closing"})
        return {"cases_deleted": len(closed), "audit_entries_purged": self.agent.store.purge_audit(now)}

    def erase(self, customer: str, by: str) -> int:
        cases = self.agent.store.cases(customer=customer, limit=100000)
        for c in cases:
            self._drop(c["case_id"])
            self.agent.store.audit(c["case_id"], "supervisor:" + by, "erased_on_request", {"customer_request": True})
        return len(cases)


retention = Retention(agent, RETENTION_DAYS)
_stop = threading.Event()


def start_background() -> None:
    n = int(os.getenv("WAKEEL_WORKERS", "2" if os.getenv("WAKEEL_QUEUE") == "1" else "0"))
    for i in range(n):
        threading.Thread(target=Worker(agent).run, args=(_stop,), daemon=True, name=f"wakeel-worker-{i}").start()
    if os.getenv("WAKEEL_RETENTION", "1") == "1":
        def loop():
            while not _stop.wait(3600):
                try:
                    retention.run()
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True, name="wakeel-retention").start()


def stop_background() -> None:
    _stop.set()
