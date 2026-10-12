"""Durable, append-only persistence (§10, §12 Reliability/Auditability).

SQLite file with two tables:

* ``audit``   — the hash-chained audit log, one row per event;
* ``records`` — versioned snapshots of domain objects (orders, fills, ledger events,
  proposals, approvals, strategy versions, releases, controls, risk decisions, job runs).
  The current state of an object is its latest row; history is never rewritten.

Triggers make both tables append-only at the database level: any UPDATE or DELETE aborts.
Values are JSON with explicit tags for Decimal/datetime/date so nothing is pickled and the
file stays inspectable with any SQLite client.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS records (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS records_kind_key ON records(kind, key, seq);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS records_no_update BEFORE UPDATE ON records BEGIN SELECT RAISE(ABORT, 'records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS records_no_delete BEFORE DELETE ON records BEGIN SELECT RAISE(ABORT, 'records are append-only'); END;
"""


# ---- codec --------------------------------------------------------------------------------
def encode(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return {"$d": str(obj)}
    if isinstance(obj, datetime):
        return {"$t": obj.isoformat()}
    if isinstance(obj, date):
        return {"$date": obj.isoformat()}
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: encode(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, dict):
        return {str(k): encode(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [encode(v) for v in obj]
    return obj


def decode(obj: Any) -> Any:
    if isinstance(obj, dict):
        if len(obj) == 1:
            (k, v), = obj.items()
            if k == "$d":
                return Decimal(v)
            if k == "$t":
                return datetime.fromisoformat(v)
            if k == "$date":
                return date.fromisoformat(v)
        return {k: decode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [decode(v) for v in obj]
    return obj


def dumps(obj: Any) -> str:
    return json.dumps(encode(obj), sort_keys=True, separators=(",", ":"))


def loads(s: str) -> Any:
    return decode(json.loads(s))


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL" if self.path != ":memory:" else "PRAGMA journal_mode=MEMORY")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._last: dict[tuple[str, str], str] = {}

    # ---- audit ----------------------------------------------------------------------------
    def append_audit(self, ev) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit(seq, ts, actor, action, payload, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                (ev.seq, ev.ts.isoformat(), ev.actor, ev.action, dumps(ev.payload), ev.prev_hash, ev.hash),
            )

    def audit_rows(self) -> list[dict[str, Any]]:
        cur = self._conn.execute("SELECT seq, ts, actor, action, payload, prev_hash, hash FROM audit ORDER BY seq")
        return [
            {"seq": r[0], "ts": datetime.fromisoformat(r[1]), "actor": r[2], "action": r[3],
             "payload": loads(r[4]), "prev_hash": r[5], "hash": r[6]}
            for r in cur.fetchall()
        ]

    # ---- records --------------------------------------------------------------------------
    def put(self, kind: str, key: str, obj: Any, *, dedupe: bool = True) -> bool:
        """Append a new version of (kind, key). Skips identical consecutive snapshots."""
        body = dumps(obj)
        with self._lock:
            if dedupe and self._last.get((kind, key)) == body:
                return False
            self._conn.execute("INSERT INTO records(kind, key, body) VALUES (?,?,?)", (kind, key, body))
            self._last[(kind, key)] = body
            return True

    def latest(self, kind: str) -> dict[str, Any]:
        cur = self._conn.execute(
            "SELECT key, body FROM records WHERE seq IN (SELECT MAX(seq) FROM records WHERE kind=? GROUP BY key) ORDER BY seq",
            (kind,),
        )
        out = {}
        for key, body in cur.fetchall():
            out[key] = loads(body)
            self._last[(kind, key)] = body
        return out

    def all(self, kind: str) -> list[tuple[str, Any]]:
        cur = self._conn.execute("SELECT key, body FROM records WHERE kind=? ORDER BY seq", (kind,))
        return [(k, loads(b)) for k, b in cur.fetchall()]

    def history(self, kind: str, key: str) -> list[Any]:
        cur = self._conn.execute("SELECT body FROM records WHERE kind=? AND key=? ORDER BY seq", (kind, key))
        return [loads(b) for (b,) in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
