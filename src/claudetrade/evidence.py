"""Paper-trading evidence gate for live authorisation (PRD §16 Phase 4, §18 Q7).

Operator decision (2026-10-22): **72 hours of paper trading** before live. Enforced as:

* at least ``min_paper_hours`` (72) between the paper deployment's first start and now, and
* at least ``min_clean_sessions`` (3) end-of-day reports from PAPER mode, where the most
  recent ones are all clean: verified flat, square-off met its deadline, zero reconciliation
  mismatches, no failed jobs.

The evidence is read from the paper deployment's own database (read-only), and its audit
chain must verify, so the record can't be hand-edited into compliance. Live authorisation
also requires a working alert channel, because the square-off failure procedure depends on
a human being reached.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .models import stable_hash
from .store import loads


@dataclass(frozen=True)
class LiveGate:
    min_paper_hours: float = 72.0
    min_clean_sessions: int = 3
    paper_evidence_db: str | None = None  # path to the PAPER deployment's database
    require_alert_channel: bool = True


@dataclass
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    paper_hours: float = 0.0
    clean_sessions: int = 0
    sessions: list[dict] = field(default_factory=list)


def _open_ro(path: str) -> sqlite3.Connection:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    # immutable=1: works on a read-only mount; the paper store checkpoints its WAL after each EOD report
    return sqlite3.connect(f"file:{p.resolve()}?mode=ro&immutable=1", uri=True)


def _verify_chain(conn: sqlite3.Connection) -> tuple[bool, list[dict]]:
    rows = conn.execute("SELECT seq, ts, actor, action, payload, prev_hash, hash FROM audit ORDER BY seq").fetchall()
    prev = "0" * 64
    events = []
    for seq, ts, actor, action, payload, prev_hash, h in rows:
        body = {"seq": seq, "ts": datetime.fromisoformat(ts), "actor": actor, "action": action,
                "payload": loads(payload), "prev": prev}
        if prev_hash != prev or stable_hash(body) != h:
            return False, []
        events.append(body)
        prev = h
    return True, events


def evaluate(gate: LiveGate, now: datetime, alert_channel_configured: bool) -> GateResult:
    res = GateResult(passed=False)
    if gate.require_alert_channel and not alert_channel_configured:
        res.reasons.append("no alert channel configured (set Telegram or webhook env vars; see RUNBOOK)")
    if not gate.paper_evidence_db:
        res.reasons.append("live_gate.paper_evidence_db is not set")
        return res
    try:
        conn = _open_ro(gate.paper_evidence_db)
    except (FileNotFoundError, sqlite3.Error) as e:
        res.reasons.append(f"cannot read paper evidence: {e}")
        return res
    try:
        ok, events = _verify_chain(conn)
        if not ok:
            res.reasons.append("paper audit chain does not verify; evidence rejected")
            return res
        starts = [e["ts"] for e in events if e["action"] == "STARTUP" and e["payload"].get("mode") == "PAPER"]
        if not starts:
            res.reasons.append("no PAPER-mode run found in the evidence database")
            return res
        res.paper_hours = (now - min(starts)).total_seconds() / 3600
        # Session results come only from the hash-chained audit log, never from mutable snapshots.
        reports = {e["payload"]["date"]: e["payload"] for e in events
                   if e["action"] == "EOD_REPORT" and "date" in e["payload"]}
        failed_jobs = {e["ts"].date().isoformat() for e in events if e["action"] == "JOB_FAILED"}
    finally:
        conn.close()

    for day in sorted(reports):
        r = reports[day]
        clean = (r.get("mode") == "PAPER" and r.get("verified_flat") is True and not r.get("square_off_failed")
                 and not r.get("mismatches")
                 and r.get("reconciliation_ok", True) and day not in failed_jobs)
        res.sessions.append({"date": day, "clean": clean, "orders": r.get("orders"), "realised_pnl": r.get("realised_pnl")})
    recent = res.sessions[-gate.min_clean_sessions:]
    res.clean_sessions = sum(1 for s in res.sessions if s["clean"])
    if res.paper_hours < gate.min_paper_hours:
        res.reasons.append(f"paper trading for {res.paper_hours:.1f} h; {gate.min_paper_hours:.0f} h required")
    if len(recent) < gate.min_clean_sessions or not all(s["clean"] for s in recent):
        res.reasons.append(f"need the last {gate.min_clean_sessions} paper sessions clean; "
                           f"have {[(s['date'], 'clean' if s['clean'] else 'NOT clean') for s in recent]}")
    res.passed = not res.reasons
    return res
