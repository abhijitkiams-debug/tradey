"""Operator API and dashboard (§4 permissions, §FR-11 dashboard).

Every route except ``/healthz`` and the static dashboard shell requires the operator token
(``Authorization: Bearer ...``). If ``CLAUDETRADE_OPERATOR_TOKEN`` is unset the API fails
closed. The token is compared in constant time; only its SHA-256 is held in memory.

The API serves a ``Desk`` (paper book + optional live book). Book-scoped routes take
``?book=paper|live`` (default: paper, or the only book). Operator-only actions live here,
never in Claude's tool set: strategy switches, the live master switch, swing approvals,
releases, limit approval, kill switch, resume.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .desk import LIVE_STRATEGY_CONFIRMATION, Desk, StrategyMode
from .models import OperatingMode, TradeMode
from .runtime import Runtime
from .tools import to_jsonable

LIVE_CONFIRMATION = LIVE_STRATEGY_CONFIRMATION
STATIC = Path(__file__).parent / "static"


class Reason(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class Approve(BaseModel):
    version: int
    hash: str = Field(min_length=64, max_length=64)


class Release(BaseModel):
    session_date: date


class LiveAuth(BaseModel):
    confirm: str


class AgentTask(BaseModel):
    task: str = Field(min_length=5, max_length=4000)


class ModeReason(Reason):
    mode: TradeMode | None = None


class SetMode(BaseModel):
    mode: StrategyMode
    confirm: str | None = None
    reason: str = Field(default="", max_length=500)


def create_app(target: Desk | Runtime, token: str | None = None) -> FastAPI:
    desk = target if isinstance(target, Desk) else Desk.single(target)
    token = token if token is not None else os.environ.get("CLAUDETRADE_OPERATOR_TOKEN")
    token_hash = hashlib.sha256(token.encode()).digest() if token else None
    api = FastAPI(title="ClaudeTrade", docs_url=None, redoc_url=None)

    def operator(authorization: str = Header(default=""), x_operator: str = Header(default="owner")) -> str:
        if token_hash is None:
            raise HTTPException(503, "operator token not configured; API disabled")
        supplied = authorization.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(), token_hash):
            raise HTTPException(401, "invalid operator token")
        return f"operator:{x_operator[:40]}"

    def pick(book: str | None) -> Runtime:
        if book is None:
            return desk.books.get("paper") or next(iter(desk.books.values()))
        if book not in desk.books:
            raise HTTPException(404, f"no '{book}' book (have: {', '.join(desk.books)})")
        return desk.books[book]

    def locked(rt: Runtime, fn, *a: Any, **kw: Any) -> Any:
        with rt.app.lock:
            try:
                return to_jsonable(fn(*a, **kw))
            except (PermissionError, KeyError, ValueError) as e:
                raise HTTPException(409, str(e)) from None

    def guarded(fn, *a: Any, **kw: Any) -> Any:
        try:
            return to_jsonable(fn(*a, **kw))
        except (PermissionError, KeyError, ValueError) as e:
            raise HTTPException(409, str(e)) from None

    # ---- desk-wide ------------------------------------------------------------------------
    @api.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "books": {b: {"mode": rt.app.mode.value,
                                          "last_tick": rt.scheduler.last_tick.isoformat() if rt.scheduler.last_tick else None}
                                      for b, rt in desk.books.items()}}

    @api.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text()

    @api.get("/api/desk")
    def desk_overview(op: str = Depends(operator)) -> dict:
        return guarded(desk.overview)

    @api.post("/api/strategies/{key}/mode")
    def set_mode(key: str, body: SetMode, op: str = Depends(operator)) -> dict:
        t = guarded(desk.set_mode, key, body.mode, op, body.confirm, body.reason)
        return {"ok": True, "toggle": t, "message": f"{t['label']} → {t['mode']}"}

    @api.get("/api/strategies/{key}/gate")
    def strategy_gate(key: str, op: str = Depends(operator)) -> dict:
        return guarded(lambda: {**to_jsonable(desk.gate(key, fresh=True)), "live_blockers": desk.live_blockers(key)})

    @api.post("/api/live/authorise")
    def authorise_live(body: LiveAuth, op: str = Depends(operator)) -> dict:
        if body.confirm != LIVE_CONFIRMATION:
            raise HTTPException(400, f"type exactly: {LIVE_CONFIRMATION}")
        if desk.live is None:
            raise HTTPException(409, "process was not started with a LIVE book")
        guarded(desk.authorise_live, op)
        return {"ok": True, "live_authorised": True, "message": "Live master switch ON"}

    @api.post("/api/live/revoke")
    def revoke_live(op: str = Depends(operator)) -> dict:
        guarded(desk.revoke_live, op)
        return {"ok": True, "live_authorised": False, "message": "Live master switch OFF; LIVE strategies moved to PAPER"}

    @api.get("/api/live/gate")
    def live_gate(op: str = Depends(operator)) -> dict:
        from .evidence import evaluate

        live = desk.live
        if live is None or live.app.live_gate is None:
            return {"passed": live is not None, "reasons": ["no gate configured" if live else "no live book"]}
        return to_jsonable(evaluate(live.app.live_gate, live.app.clock(), live.app.alert_channel_configured))

    @api.post("/api/controls/kill-all")
    def kill_all(body: Reason, op: str = Depends(operator)) -> dict:
        for rt in desk.books.values():
            locked(rt, rt.app.monitor.activate_kill_switch, op, body.reason)
        return {"ok": True, "message": "Kill switch ON in every book"}

    # ---- book-scoped reads ------------------------------------------------------------------
    @api.get("/api/dashboard")
    def dashboard(book: str | None = None, op: str = Depends(operator)) -> dict:
        rt = pick(book)
        app = rt.app

        def build():
            d = app.dashboard()
            d["book"] = next(b for b, r in desk.books.items() if r is rt)
            d["universe"] = list(rt.settings.universe)
            d["risk_config"] = {"version": app.risk_config.version, "hash": app.risk_config.content_hash(),
                                "approved_by": app.risk_config.approved_by,
                                "limits": {k: str(getattr(app.risk_config, k)) for k in (
                                    "risk_per_trade", "max_daily_loss", "max_order_value", "max_position_value",
                                    "max_portfolio_exposure", "max_concurrent_positions")}}
            d["scheduler"] = {"last_tick": rt.scheduler.last_tick, "errors": rt.scheduler.errors[-5:]}
            d["intraday"]["strategies"] = [{"label": sv.label, "validation": v.outcome.value}
                                           for sv, v in app.intraday.active.values()]
            d["intraday"]["strategy_pnl"] = {k: str(v) for k, v in app.intraday.strategy_pnl().items()}
            return d
        return locked(rt, build)

    @api.get("/api/proposals/{proposal_id}")
    def proposal(proposal_id: str, book: str | None = None, op: str = Depends(operator)) -> dict:
        rt = pick(book)

        def get():
            p = rt.app.swing.latest(proposal_id)
            return {**to_jsonable(p), "hash": p.content_hash(), "max_loss": p.max_loss,
                    "declined": rt.app.swing.declined.get(proposal_id)}
        return locked(rt, get)

    @api.get("/api/strategies")
    def strategies(book: str | None = None, op: str = Depends(operator)) -> dict:
        rt = pick(book)
        app = rt.app
        return locked(rt, lambda: {"versions": [
            {"hash": h, "label": v.label, "status": v.status.value, "failures": v.eligibility_failures,
             "params": v.params_dict, "evidence": app.strategies.evidence.get(h)} for h, v in app.strategies.versions.items()],
            "releases": list(app.strategies.releases.values())})

    @api.get("/api/audit")
    def audit(limit: int = 200, action: str | None = None, book: str | None = None, op: str = Depends(operator)) -> list:
        rt = pick(book)
        return locked(rt, lambda: [{"seq": e.seq, "ts": e.ts, "actor": e.actor, "action": e.action, "payload": e.payload}
                                   for e in rt.app.audit.events(action)[-min(limit, 2000):]])

    @api.get("/api/eod/{day}")
    def eod(day: date, book: str | None = None, op: str = Depends(operator)) -> dict:
        app = pick(book).app
        rep = app.persistence.store.latest("eod_report").get(day.isoformat()) if app.persistence else None
        if rep is None:
            raise HTTPException(404, "no report for that date")
        return to_jsonable(rep)

    # ---- swing approvals --------------------------------------------------------------------
    @api.post("/api/swing/{proposal_id}/approve")
    def approve(proposal_id: str, body: Approve, book: str | None = None, op: str = Depends(operator)) -> dict:
        rt = pick(book)
        return locked(rt, lambda: rt.app.swing.approve(proposal_id, body.version, body.hash, op))

    @api.post("/api/swing/{proposal_id}/decline")
    def decline(proposal_id: str, body: Reason, book: str | None = None, op: str = Depends(operator)) -> dict:
        rt = pick(book)
        locked(rt, rt.app.swing.decline, proposal_id, op, body.reason)
        return {"ok": True}

    @api.post("/api/swing/approvals/{approval_id}/execute")
    def execute(approval_id: str, book: str | None = None, op: str = Depends(operator)) -> dict:
        rt = pick(book)

        def run():
            r = rt.app.swing.execute(approval_id, actor=op)
            if isinstance(r, str):
                return {"ok": False, "message": r}
            return {"ok": r.order is not None and r.order.status.value != "REJECTED", "message": r.message,
                    "risk_reasons": r.decision.rejection_reasons,
                    "client_request_id": r.order.client_request_id if r.order else None}
        return locked(rt, run)

    # ---- book-scoped operator actions -------------------------------------------------------
    @api.post("/api/strategies/{version_hash}/release")
    def release(version_hash: str, body: Release, book: str | None = None, op: str = Depends(operator)) -> dict:
        rt = pick(book)
        return locked(rt, lambda: rt.app.strategies.release(version_hash, body.session_date, op))

    @api.post("/api/risk/approve")
    def approve_risk(book: str | None = None, op: str = Depends(operator)) -> dict:
        rt = pick(book)
        cfg = locked(rt, rt.app.approve_risk_config, op)
        return {"ok": True, "hash": rt.app.risk_config.content_hash(), "approved_at": cfg["approved_at"]}

    @api.post("/api/controls/kill-switch")
    def kill(body: Reason, book: str | None = None, op: str = Depends(operator)) -> dict:
        rt = pick(book)
        locked(rt, rt.app.monitor.activate_kill_switch, op, body.reason)
        return {"ok": True, "kill_switch": True}

    @api.post("/api/controls/kill-switch/reset")
    def kill_reset(body: Reason, book: str | None = None, op: str = Depends(operator)) -> dict:
        rt = pick(book)

        def run():
            if not rt.app.execution.reconciliation_ok:
                raise PermissionError("reconcile positions before resetting the kill switch")
            rt.app.monitor.reset_kill_switch(op, body.reason)
        locked(rt, run)
        return {"ok": True, "kill_switch": False}

    @api.post("/api/controls/pause")
    def pause(body: ModeReason, book: str | None = None, op: str = Depends(operator)) -> dict:
        rt = pick(book)
        locked(rt, rt.app.monitor.pause_entries, op, body.reason, body.mode)
        return {"ok": True}

    @api.post("/api/controls/resume")
    def resume(body: Reason, book: str | None = None, op: str = Depends(operator)) -> dict:
        rt = pick(book)
        locked(rt, rt.app.monitor.resume_entries, op, body.reason)
        return {"ok": True}

    @api.post("/api/square-off")
    def square_off(body: Reason, book: str | None = None, op: str = Depends(operator)) -> dict:
        rt = pick(book)

        def run():
            rt.app.audit.record(op, "SQUARE_OFF_REQUESTED", reason=body.reason)
            return rt.app.intraday.run_square_off()
        return locked(rt, run)

    @api.post("/api/positions/reconcile")
    def reconcile(book: str | None = None, op: str = Depends(operator)) -> dict:
        rt = pick(book)
        return {"mismatches": locked(rt, rt.app.execution.reconcile_positions)}

    @api.post("/api/alerts/{alert_id}/ack")
    def ack(alert_id: str, book: str | None = None, op: str = Depends(operator)) -> dict:
        rt = pick(book)
        a = locked(rt, rt.app.monitor.ack, alert_id, op)
        return {"ok": True, "alert_id": alert_id, "acked_by": a["acked_by"]}

    @api.post("/api/alerts/test")
    def test_alert(book: str | None = None, op: str = Depends(operator)) -> dict:
        rt = pick(book)
        if not rt.app.alert_channel_configured:
            raise HTTPException(409, "no alert channel configured (see docs/RUNBOOK.md §1)")
        locked(rt, rt.app.monitor.alert, "HIGH", f"Test alert from {op}. If you can read this on your phone, alerting works.")
        return {"ok": True, "message": "test alert sent; check your phone"}

    @api.post("/api/agent/run")
    def agent(body: AgentTask, op: str = Depends(operator)) -> dict:
        rt = desk.swing_book() or pick(None)
        try:
            from .agent import run_agent
            from .tools import ToolGateway
        except ImportError as e:  # pragma: no cover
            raise HTTPException(501, str(e)) from None
        env = "live" if rt.app.mode is OperatingMode.LIVE else "paper"
        rt.app.audit.record(op, "AGENT_RUN_REQUESTED", task=body.task[:500])
        try:
            run = run_agent(ToolGateway(rt.app, env), body.task)  # takes the lock per tool call, not for the whole run
        except ImportError:
            raise HTTPException(501, "install claudetrade[agent] and configure Anthropic credentials") from None
        return {"summary": run.final_text, "tool_calls": run.tool_calls, "stop_reason": run.stop_reason}

    return api
