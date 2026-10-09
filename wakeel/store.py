"""Durable state beside the graph's checkpoints: the case index, the audit
trail, shared rate limits, the job queue and retention. One implementation,
two backends: SQLite for a single process (the demo), Postgres for several
replicas (production), chosen by WAKEEL_DATABASE_URL.

The audit trail is append-only and hash-chained, like a ledger: each entry's
hash covers the previous one, so editing or deleting a past approval breaks
every hash after it, and verify() finds where. Database triggers refuse
updates and deletes; on Postgres, writers take an advisory lock so two
replicas can never fork the chain.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone

GENESIS = "wakeel/audit/genesis"

SCHEMA = """
create table if not exists cases (
  case_id text primary key, customer text not null, status text not null, intent text,
  lang text, amount integer, transaction_id text, created text not null, updated text not null);
create index if not exists cases_status on cases (status, updated);
create index if not exists cases_customer on cases (customer);
create table if not exists audit (
  seq {serial}, at text not null, case_id text not null,
  actor text not null, action text not null, detail text not null, prev text not null, hash text not null);
create table if not exists rate (k text primary key, win bigint not null, n integer not null);
create table if not exists jobs (
  id {serial}, case_id text not null, kind text not null, payload text not null,
  state text not null default 'queued', attempts integer not null default 0, run_at double precision not null,
  locked_by text, error text, created double precision not null);
create index if not exists jobs_due on jobs (state, run_at);
"""

SQLITE_TRIGGERS = """
create trigger if not exists audit_append_only_u before update on audit begin select raise(abort, 'audit is append-only'); end;
create trigger if not exists audit_append_only_d before delete on audit
  when old.at > strftime('%Y-%m-%dT%H:%M:%S', 'now', '-5 years') begin select raise(abort, 'audit is append-only'); end;
"""

PG_TRIGGERS = """
create or replace function wakeel_audit_append_only() returns trigger language plpgsql as $$
begin
  if tg_op = 'DELETE' and old.at < to_char(now() at time zone 'utc' - interval '5 years', 'YYYY-MM-DD"T"HH24:MI:SS') then
    return old;   -- past the 5-year retention: may be purged
  end if;
  raise exception 'audit is append-only';
end $$;
drop trigger if exists audit_append_only on audit;
create trigger audit_append_only before update or delete on audit for each row execute function wakeel_audit_append_only();
"""

AUDIT_RETENTION = timedelta(days=5 * 365)


class Store:
    """Use Store.sqlite(path) or Store.postgres(pool)."""

    def __init__(self, kind: str, conn=None, pool=None):
        self.kind, self.conn, self.pool = kind, conn, pool
        self._lock = threading.RLock()                 # SQLite: one writer at a time in this process
        self._case_locks: dict[str, threading.Lock] = {}
        with self._tx() as cur:
            serial = "integer primary key autoincrement" if kind == "sqlite" else "bigserial primary key"
            for stmt in SCHEMA.format(serial=serial).split(";"):
                if stmt.strip():
                    cur.execute(stmt)
            if kind == "postgres":
                cur.execute("select pg_advisory_xact_lock(7700)")     # replicas starting together
                cur.execute(PG_TRIGGERS)
        if kind == "sqlite":
            self.conn.executescript(SQLITE_TRIGGERS)

    @classmethod
    def sqlite(cls, path_or_conn) -> "Store":
        conn = path_or_conn if isinstance(path_or_conn, sqlite3.Connection) else sqlite3.connect(path_or_conn, check_same_thread=False)
        return cls("sqlite", conn=conn)

    @classmethod
    def postgres(cls, pool) -> "Store":
        return cls("postgres", pool=pool)

    # ---------------------------------------------------------------- plumbing

    @contextlib.contextmanager
    def _tx(self):
        """One transaction; yields a cursor whose execute() accepts '?' placeholders."""
        if self.kind == "sqlite":
            with self._lock:
                cur = self.conn.cursor()
                try:
                    yield _Cursor(cur, "?")
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise
        else:
            with self.pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        yield _Cursor(cur, "%s")

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    @contextlib.contextmanager
    def case_lock(self, case_id: str):
        """One decision at a time per case, across every replica (Postgres
        advisory lock) or this process (SQLite)."""
        if self.kind == "sqlite":
            with self._lock:
                lock = self._case_locks.setdefault(case_id, threading.Lock())
            with lock:
                yield
            return
        key = zlib.crc32(case_id.encode())
        with self.pool.connection() as conn:
            conn.execute("select pg_advisory_lock(%s)", (key,))
            try:
                yield
            finally:
                conn.execute("select pg_advisory_unlock(%s)", (key,))

    # ---------------------------------------------------------------- cases

    def upsert_case(self, c: dict) -> None:
        p = c.get("proposal") or {}
        now = self.now()
        with self._tx() as cur:
            cur.execute("""insert into cases (case_id, customer, status, intent, lang, amount, transaction_id, created, updated)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict (case_id) do update set status = excluded.status, intent = excluded.intent,
                  amount = excluded.amount, transaction_id = excluded.transaction_id, updated = excluded.updated""",
                        (c["case_id"], c["customer"], c["status"], c.get("intent"), c.get("lang"), p.get("amount"), p.get("transaction_id"), now, now))

    def cases(self, status: str | None = None, limit: int = 50, customer: str | None = None) -> list[dict]:
        q, args = "select case_id, customer, status, intent, lang, amount, transaction_id, created, updated from cases where 1 = 1", []
        if status:
            q, args = q + " and status = ?", args + [status]
        if customer:
            q, args = q + " and customer = ?", args + [customer]
        with self._tx() as cur:
            rows = cur.execute(q + " order by updated desc limit ?", tuple(args + [limit])).fetchall()
        keys = ["case_id", "customer", "status", "intent", "lang", "amount", "transaction_id", "created", "updated"]
        return [dict(zip(keys, r)) for r in rows]

    def case_owner(self, case_id: str) -> str | None:
        with self._tx() as cur:
            r = cur.execute("select customer from cases where case_id = ?", (case_id,)).fetchone()
        return r[0] if r else None

    # ---------------------------------------------------------------- audit

    @staticmethod
    def _hash(prev: str, at: str, case_id: str, actor: str, action: str, detail: str) -> str:
        return hashlib.sha256("|".join((prev, at, case_id, actor, action, detail)).encode()).hexdigest()

    def audit(self, case_id: str, actor: str, action: str, detail: dict | None = None, at: str | None = None) -> None:
        d = json.dumps(detail or {}, sort_keys=True, ensure_ascii=False)
        with self._tx() as cur:
            if self.kind == "postgres":
                cur.execute("select pg_advisory_xact_lock(7701)")   # one chain writer at a time, across replicas
            row = cur.execute("select hash from audit order by seq desc limit 1").fetchone()
            prev, at = (row[0] if row else GENESIS), at or self.now()
            cur.execute("insert into audit (at, case_id, actor, action, detail, prev, hash) values (?, ?, ?, ?, ?, ?, ?)",
                        (at, case_id, actor, action, d, prev, self._hash(prev, at, case_id, actor, action, d)))

    def trail(self, case_id: str) -> list[dict]:
        with self._tx() as cur:
            rows = cur.execute("select seq, at, actor, action, detail, hash from audit where case_id = ? order by seq", (case_id,)).fetchall()
        return [{"seq": s, "at": at, "actor": a, "action": ac, "detail": json.loads(d), "hash": h[:16]} for s, at, a, ac, d, h in rows]

    def verify(self) -> dict:
        """Recomputes the chain; returns the first broken entry, if any. After
        retention purges the oldest entries, the chain starts at the first
        surviving entry's recorded predecessor."""
        with self._tx() as cur:
            rows = cur.execute("select seq, at, case_id, actor, action, detail, prev, hash from audit order by seq").fetchall()
        prev = rows[0][6] if rows else GENESIS
        for seq, at, case_id, actor, action, detail, p, h in rows:
            if p != prev or h != self._hash(prev, at, case_id, actor, action, detail):
                return {"ok": False, "entries": len(rows), "broken_at": seq}
            prev = h
        return {"ok": True, "entries": len(rows)}

    # ---------------------------------------------------------------- shared rate limits

    def rate_hit(self, key: str, limit: int, window: int = 60) -> tuple[bool, int]:
        """A fixed-window counter shared by every replica. Returns (allowed,
        seconds until the window resets)."""
        now = int(time.time())
        win = now - now % window
        with self._tx() as cur:
            cur.execute("""insert into rate (k, win, n) values (?, ?, 1)
                on conflict (k) do update set n = case when rate.win = excluded.win then rate.n + 1 else 1 end, win = excluded.win""",
                        (key, win))
            n = cur.execute("select n from rate where k = ?", (key,)).fetchone()[0]
        return n <= limit, win + window - now

    # ---------------------------------------------------------------- job queue

    def enqueue(self, case_id: str, kind: str, payload: dict) -> int:
        now = time.time()
        with self._tx() as cur:
            cur.execute("insert into jobs (case_id, kind, payload, run_at, created) values (?, ?, ?, ?, ?)",
                        (case_id, kind, json.dumps(payload, ensure_ascii=False), now, now))
            if self.kind == "sqlite":
                return cur.lastrowid()
            return cur.execute("select lastval()").fetchone()[0]

    def claim(self, worker: str, lease: float = 120) -> dict | None:
        """Takes the oldest due job. Postgres: FOR UPDATE SKIP LOCKED, so many
        workers never take the same job. A job whose worker died is retaken
        after its lease runs out."""
        now = time.time()
        with self._tx() as cur:
            lock = " for update skip locked" if self.kind == "postgres" else ""
            r = cur.execute("""select id, case_id, kind, payload, attempts from jobs
                where state in ('queued', 'running') and run_at <= ?
                order by run_at limit 1""" + lock, (now,)).fetchone()
            if not r:
                return None
            cur.execute("update jobs set state = 'running', locked_by = ?, attempts = attempts + 1, run_at = ? where id = ?",
                        (worker, now + lease, r[0]))
        return {"id": r[0], "case_id": r[1], "kind": r[2], "payload": json.loads(r[3]), "attempts": r[4] + 1}

    def finish(self, job_id: int, error: str | None = None, max_attempts: int = 3) -> str:
        """Marks a job done, or schedules a retry with backoff, or dead after
        max_attempts. Returns the new state."""
        with self._tx() as cur:
            if error is None:
                cur.execute("update jobs set state = 'done', error = null where id = ?", (job_id,))
                return "done"
            attempts = cur.execute("select attempts from jobs where id = ?", (job_id,)).fetchone()[0]
            if attempts >= max_attempts:
                cur.execute("update jobs set state = 'dead', error = ? where id = ?", (error[:500], job_id))
                return "dead"
            cur.execute("update jobs set state = 'queued', error = ?, run_at = ? where id = ?",
                        (error[:500], time.time() + 5 * 4 ** (attempts - 1), job_id))   # 5 s, 20 s, ...
            return "queued"

    def job(self, job_id: int) -> dict | None:
        with self._tx() as cur:
            r = cur.execute("select id, case_id, state, attempts, error from jobs where id = ?", (job_id,)).fetchone()
        return dict(zip(["id", "case_id", "state", "attempts", "error"], r)) if r else None

    def job_counts(self) -> dict:
        with self._tx() as cur:
            rows = cur.execute("select state, count(*) from jobs group by state").fetchall()
        return {s: n for s, n in rows}

    # ---------------------------------------------------------------- retention (PDPL)

    def closed_cases_before(self, cutoff: datetime) -> list[str]:
        with self._tx() as cur:
            rows = cur.execute("""select case_id from cases where status in ('refunded', 'rejected', 'answered', 'handed_off', 'refund_failed')
                and updated < ?""", (cutoff.replace(microsecond=0).isoformat(),)).fetchall()
        return [r[0] for r in rows]

    def forget_case(self, case_id: str) -> None:
        """Removes the case from the index and the queue (the caller removes its
        checkpoints). The audit trail keeps the record of decisions."""
        with self._tx() as cur:
            cur.execute("delete from cases where case_id = ?", (case_id,))
            cur.execute("delete from jobs where case_id = ?", (case_id,))

    def purge_audit(self, now: datetime | None = None) -> int:
        """Deletes audit entries past the 5-year retention period."""
        cutoff = ((now or datetime.now(timezone.utc)) - AUDIT_RETENTION).replace(microsecond=0).isoformat()
        with self._tx() as cur:
            n = cur.execute("select count(*) from audit where at < ?", (cutoff,)).fetchone()[0]
            cur.execute("delete from audit where at < ?", (cutoff,))
        return n


class _Cursor:
    """Lets one SQL text with '?' placeholders run on SQLite and psycopg."""

    def __init__(self, cur, style: str):
        self.cur, self.style = cur, style

    def execute(self, sql: str, args: tuple = ()):
        if self.style == "%s":
            sql = sql.replace("?", "%s")
        self.cur.execute(sql, args)
        return self

    def fetchone(self):
        return self.cur.fetchone()

    def fetchall(self):
        return self.cur.fetchall()

    def lastrowid(self):
        return self.cur.lastrowid
