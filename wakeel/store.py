"""Case index and audit trail, in SQLite (or anything with the same schema).

The audit trail is append-only and hash-chained, like a ledger: each entry's
hash covers the previous one, so editing or deleting a past approval breaks
every hash after it, and verify() finds where. Regulators ask "who approved
this refund, when, and on what basis"; this answers it.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone

GENESIS = "wakeel/audit/genesis"


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self.db, self.lock = conn, threading.Lock()
        with self.lock:
            self.db.executescript("""
            create table if not exists cases (
              case_id text primary key, customer text not null, status text not null, intent text,
              lang text, amount integer, transaction_id text, created text not null, updated text not null);
            create index if not exists cases_status on cases (status, updated);
            create table if not exists audit (
              seq integer primary key autoincrement, at text not null, case_id text not null,
              actor text not null, action text not null, detail text not null, prev text not null, hash text not null);
            create trigger if not exists audit_append_only_u before update on audit begin select raise(abort, 'audit is append-only'); end;
            create trigger if not exists audit_append_only_d before delete on audit begin select raise(abort, 'audit is append-only'); end;
            """)
            self.db.commit()

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def upsert_case(self, c: dict) -> None:
        p = c.get("proposal") or {}
        with self.lock:
            self.db.execute("""insert into cases (case_id, customer, status, intent, lang, amount, transaction_id, created, updated)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict (case_id) do update set status = excluded.status, intent = excluded.intent,
                  amount = excluded.amount, transaction_id = excluded.transaction_id, updated = excluded.updated""",
                (c["case_id"], c["customer"], c["status"], c.get("intent"), c.get("lang"), p.get("amount"), p.get("transaction_id"), self.now(), self.now()))
            self.db.commit()

    def cases(self, status: str | None = None, limit: int = 50) -> list[dict]:
        q = "select case_id, customer, status, intent, lang, amount, transaction_id, created, updated from cases"
        args: tuple = ()
        if status:
            q, args = q + " where status = ?", (status,)
        with self.lock:
            rows = self.db.execute(q + " order by updated desc limit ?", args + (limit,)).fetchall()
        keys = ["case_id", "customer", "status", "intent", "lang", "amount", "transaction_id", "created", "updated"]
        return [dict(zip(keys, r)) for r in rows]

    @staticmethod
    def _hash(prev: str, at: str, case_id: str, actor: str, action: str, detail: str) -> str:
        return hashlib.sha256("|".join((prev, at, case_id, actor, action, detail)).encode()).hexdigest()

    def audit(self, case_id: str, actor: str, action: str, detail: dict | None = None) -> None:
        d = json.dumps(detail or {}, sort_keys=True, ensure_ascii=False)
        with self.lock:
            row = self.db.execute("select hash from audit order by seq desc limit 1").fetchone()
            prev, at = (row[0] if row else GENESIS), self.now()
            self.db.execute("insert into audit (at, case_id, actor, action, detail, prev, hash) values (?, ?, ?, ?, ?, ?, ?)",
                            (at, case_id, actor, action, d, prev, self._hash(prev, at, case_id, actor, action, d)))
            self.db.commit()

    def trail(self, case_id: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute("select seq, at, actor, action, detail, hash from audit where case_id = ? order by seq", (case_id,)).fetchall()
        return [{"seq": s, "at": at, "actor": a, "action": ac, "detail": json.loads(d), "hash": h[:16]} for s, at, a, ac, d, h in rows]

    def verify(self) -> dict:
        """Recomputes the chain; returns the first broken entry, if any."""
        prev = GENESIS
        with self.lock:
            rows = self.db.execute("select seq, at, case_id, actor, action, detail, prev, hash from audit order by seq").fetchall()
        for seq, at, case_id, actor, action, detail, p, h in rows:
            if p != prev or h != self._hash(prev, at, case_id, actor, action, detail):
                return {"ok": False, "entries": len(rows), "broken_at": seq}
            prev = h
        return {"ok": True, "entries": len(rows)}
