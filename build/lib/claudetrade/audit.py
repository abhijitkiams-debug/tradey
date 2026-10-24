"""Append-only, hash-chained audit log (§G, §10, §12 Auditability).

Every event carries the hash of its predecessor, so a silent edit or deletion anywhere in
the chain is detectable with ``verify()``. Corrections are new events, never rewrites.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import canonical_json, stable_hash

GENESIS = "0" * 64


@dataclass(frozen=True)
class AuditEvent:
    seq: int
    ts: datetime
    actor: str
    action: str
    payload: dict[str, Any]
    prev_hash: str
    hash: str


class AuditLog:
    def __init__(self, clock: Callable[[], datetime], path: Path | None = None):
        self._clock = clock
        self._events: list[AuditEvent] = []
        self._lock = threading.Lock()
        self._path = path
        self.sink: Callable[[AuditEvent], None] | None = None  # durable store; must not fail silently
        self.after_record: Callable[[AuditEvent], None] | None = None  # e.g. state checkpoint

    def record(self, actor: str, action: str, **payload: Any) -> AuditEvent:
        ev = self._record(actor, action, payload)
        if self.after_record is not None:
            self.after_record(ev)
        return ev

    def _record(self, actor: str, action: str, payload: dict[str, Any]) -> AuditEvent:
        with self._lock:
            prev = self._events[-1].hash if self._events else GENESIS
            seq = len(self._events)
            ts = self._clock()
            body = {"seq": seq, "ts": ts, "actor": actor, "action": action, "payload": payload, "prev": prev}
            ev = AuditEvent(seq, ts, actor, action, payload, prev, stable_hash(body))
            if self.sink is not None:
                self.sink(ev)  # persist first: an event that can't be stored is an error, not a gap
            self._events.append(ev)
            if self._path is not None:
                with self._path.open("a") as fh:
                    fh.write(canonical_json({**body, "hash": ev.hash}) + "\n")
            return ev

    def load(self, rows: list[dict[str, Any]]) -> None:
        """Restore from durable storage; refuses a chain that doesn't verify."""
        with self._lock:
            self._events = [AuditEvent(r["seq"], r["ts"], r["actor"], r["action"], r["payload"], r["prev_hash"], r["hash"])
                            for r in rows]
            if not self.verify():
                self._events = []
                raise RuntimeError("audit chain failed verification on restore; refusing to start")

    def events(self, action: str | None = None) -> list[AuditEvent]:
        return [e for e in self._events if action is None or e.action == action]

    def verify(self) -> bool:
        prev = GENESIS
        for e in self._events:
            body = {"seq": e.seq, "ts": e.ts, "actor": e.actor, "action": e.action, "payload": e.payload, "prev": prev}
            if e.prev_hash != prev or stable_hash(body) != e.hash:
                return False
            prev = e.hash
        return True

    def dump(self) -> str:
        return "\n".join(json.dumps({"seq": e.seq, "ts": str(e.ts), "actor": e.actor, "action": e.action}) for e in self._events)
