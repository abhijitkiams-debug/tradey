"""Operator API and dashboard (§4 permissions, §FR-11 dashboard).

Every route except ``/healthz`` and the static dashboard shell requires the operator token
(``Authorization: Bearer ...``). If ``CLAUDETRADE_OPERATOR_TOKEN`` is unset the API fails
closed. The token is compared in constant time; only its SHA-256 is held in memory.

Operator-only actions live here, never in Claude's tool set: approving swing proposals,
releasing strategies, approving risk limits, authorising live trading, resuming entries and
resetting the kill switch.
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

from .models import OperatingMode, TradeMode
from .runtime import Runtime
from .tools import to_jsonable

LIVE_CONFIRMATION = "ENABLE LIVE TRADING"
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


def create_app(rt: Runtime, token: str | None = None) -> FastAPI:
    token = token if token is not None else os.environ.get("CLAUDETRADE_OPERATOR_TOKEN")
    token_hash = hashlib.sha256(token.encode()).digest() if token else None
    app = rt.app
    api = FastAPI(title="ClaudeTrade", docs_url=None, redoc_url=None)

    def operator(authorization: str = Header(default=""), x_operator: str = Header(default="owner")) -> str:
        if token_hash is None:
            raise HTTPException(503, "operator token not configured; API disabled")
        supplied = authorization.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(), token_hash):
            raise HTTPException(401, "invalid operator token")
        return f"operator:{x_operator[:40]}"

    def locked(fn, *a: Any, **kw: Any) -> Any:
        with app.lock:
            try:
                return to_jsonable(fn(*a, **kw))
            except (PermissionError, KeyError, ValueError) as e:
                raise HTTPException(409, str(e)) from None

    # ---- read ---------------------------------------------------------------------------
    @api.get("/healthz")
    def healthz() -> dict:
        last = rt.scheduler.last_tick
        return {"ok": True, "mode": app.mode.value, "last_tick": last.isoformat() if last else None}

    @api.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text()

    @api.get("/api/dashboard")
    def dashboard(op: str = Depends(operator)) -> dict:
        def build():
            d = app.dashboard()
            d["universe"] = list(rt.settings.universe)
            d["risk_config"] = {"version": app.risk_config.version, "hash": app.risk_config.content_hash(),
                                "approved_by": app.risk_config.approved_by,
                                "limits": {k: str(getattr(app.risk_config, k)) for k in (
                                    "risk_per_trade", "max_daily_loss", "max_order_value", "max_position_value",
                                    "max_portfolio_exposure", "max_concurrent_positions")}}
            d["scheduler"] = {"last_tick": rt.scheduler.last_tick, "errors": rt.scheduler.errors[-5:]}
            return d
        return locked(build)

    @api.get("/api/proposals/{proposal_id}")
    def proposal(proposal_id: str, op: str = Depends(operator)) -> dict:
        def get():
            p = app.swing.latest(proposal_id)
            return {**to_jsonable(p), "hash": p.content_hash(), "max_loss": p.max_loss,
                    "declined": app.swing.declined.get(proposal_id)}
        return locked(get)

    @api.get("/api/strategies")
    def strategies(op: str = Depends(operator)) -> dict:
        return locked(lambda: {"versions": [
            {"hash": h, "label": v.label, "status": v.status.value, "failures": v.eligibility_failures,
             "params": v.params_dict, "evidence": app.strategies.evidence.get(h)} for h, v in app.strategies.versions.items()],
            "releases": list(app.strategies.releases.values())})

    @api.get("/api/audit")
    def audit(limit: int = 200, action: str | None = None, op: str = Depends(operator)) -> list:
        return locked(lambda: [{"seq": e.seq, "ts": e.ts, "actor": e.actor, "action": e.action, "payload": e.payload}
                               for e in app.audit.events(action)[-min(limit, 2000):]])

    @api.get("/api/eod/{day}")
    def eod(day: date, op: str = Depends(operator)) -> dict:
        rep = app.persistence.store.latest("eod_report").get(day.isoformat()) if app.persistence else None
        if rep is None:
            raise HTTPException(404, "no report for that date")
        return to_jsonable(rep)

    # ---- swing approvals ----------------------------------------------------------------
    @api.post("/api/swing/{proposal_id}/approve")
    def approve(proposal_id: str, body: Approve, op: str = Depends(operator)) -> dict:
        return locked(lambda: app.swing.approve(proposal_id, body.version, body.hash, op))

    @api.post("/api/swing/{proposal_id}/decline")
    def decline(proposal_id: str, body: Reason, op: str = Depends(operator)) -> dict:
        locked(app.swing.decline, proposal_id, op, body.reason)
        return {"ok": True}

    @api.post("/api/swing/approvals/{approval_id}/execute")
    def execute(approval_id: str, op: str = Depends(operator)) -> dict:
        def run():
            r = app.swing.execute(approval_id, actor=op)
            if isinstance(r, str):
                return {"ok": False, "message": r}
            return {"ok": r.order is not None and r.order.status.value != "REJECTED", "message": r.message,
                    "risk_reasons": r.decision.rejection_reasons,
                    "client_request_id": r.order.client_request_id if r.order else None}
        return locked(run)

    # ---- strategy release / risk / live -------------------------------------------------
    @api.post("/api/strategies/{version_hash}/release")
    def release(version_hash: str, body: Release, op: str = Depends(operator)) -> dict:
        return locked(lambda: app.strategies.release(version_hash, body.session_date, op))

    @api.post("/api/risk/approve")
    def approve_risk(op: str = Depends(operator)) -> dict:
        cfg = locked(app.approve_risk_config, op)
        return {"ok": True, "hash": app.risk_config.content_hash(), "approved_at": cfg["approved_at"]}

    @api.post("/api/live/authorise")
    def authorise_live(body: LiveAuth, op: str = Depends(operator)) -> dict:
        if body.confirm != LIVE_CONFIRMATION:
            raise HTTPException(400, f"type exactly: {LIVE_CONFIRMATION}")
        if app.mode is not OperatingMode.LIVE:
            raise HTTPException(409, "process was not started in LIVE mode")
        locked(app.authorise_live, op)
        return {"ok": True, "live_authorised": True}

    @api.post("/api/live/revoke")
    def revoke_live(op: str = Depends(operator)) -> dict:
        def run():
            app.live_auth.revoke(op, app.clock())
            app.audit.record(op, "LIVE_REVOKED")
        locked(run)
        return {"ok": True, "live_authorised": False}

    # ---- safety controls ----------------------------------------------------------------
    @api.post("/api/controls/kill-switch")
    def kill(body: Reason, op: str = Depends(operator)) -> dict:
        locked(app.monitor.activate_kill_switch, op, body.reason)
        return {"ok": True, "kill_switch": True}

    @api.post("/api/controls/kill-switch/reset")
    def kill_reset(body: Reason, op: str = Depends(operator)) -> dict:
        def run():
            if not app.execution.reconciliation_ok:
                raise PermissionError("reconcile positions before resetting the kill switch")
            app.monitor.reset_kill_switch(op, body.reason)
        locked(run)
        return {"ok": True, "kill_switch": False}

    @api.post("/api/controls/pause")
    def pause(body: ModeReason, op: str = Depends(operator)) -> dict:
        locked(app.monitor.pause_entries, op, body.reason, body.mode)
        return {"ok": True}

    @api.post("/api/controls/resume")
    def resume(body: Reason, op: str = Depends(operator)) -> dict:
        locked(app.monitor.resume_entries, op, body.reason)
        return {"ok": True}

    @api.post("/api/square-off")
    def square_off(body: Reason, op: str = Depends(operator)) -> dict:
        def run():
            app.audit.record(op, "SQUARE_OFF_REQUESTED", reason=body.reason)
            return app.intraday.run_square_off()
        return locked(run)

    @api.post("/api/positions/reconcile")
    def reconcile(op: str = Depends(operator)) -> dict:
        return {"mismatches": locked(app.execution.reconcile_positions)}

    @api.post("/api/agent/run")
    def agent(body: AgentTask, op: str = Depends(operator)) -> dict:
        try:
            from .agent import run_agent
            from .tools import ToolGateway
        except ImportError as e:  # pragma: no cover
            raise HTTPException(501, str(e)) from None
        env = "live" if app.mode is OperatingMode.LIVE else "paper"
        app.audit.record(op, "AGENT_RUN_REQUESTED", task=body.task[:500])
        try:
            run = run_agent(ToolGateway(app, env), body.task)  # takes the lock per tool call, not for the whole run
        except ImportError:
            raise HTTPException(501, "install claudetrade[agent] and configure Anthropic credentials") from None
        return {"summary": run.final_text, "tool_calls": run.tool_calls, "stop_reason": run.stop_reason}

    return api
