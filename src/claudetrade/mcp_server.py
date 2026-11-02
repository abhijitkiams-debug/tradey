"""ClaudeTrade MCP server: lets Claude (Desktop / Claude Code) operate the running desk.

It talks to the desk's own operator API (http://127.0.0.1:8000 inside the container), so every
action goes through the same checks and audit trail as the dashboard. Actions are recorded as
``operator:claude-mcp``.

What Claude can do here:
  * read everything: status, strategies (with paper-record progress and live blockers), book
    details, proposals, alerts, research, audit trail;
  * switch strategies OFF or PAPER;
  * use the safety brakes: pause entries, kill switch, square-off, reconcile, live master OFF.

What stays human-only (dashboard): anything that adds risk or stands in for the operator's
judgement — switching a strategy to LIVE, turning the live master switch ON, approving or
executing swing trades, resuming entries, resetting the kill switch, approving limits, and
acknowledging alerts (an acknowledgement means a person has acted).

Run: ``claudetrade mcp`` (stdio). See deploy/README.md for the Claude Desktop / Code config.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Literal

Call = Callable[[str, str, dict | None], Any]

INSTRUCTIONS = """You are connected to the operator's ClaudeTrade desk (NSE cash equities, Groww).
Two books with separate money: 'paper' (virtual) and 'live' (the operator's Groww account).
Read state with the read tools before acting, and say what you read. You may switch strategies
OFF or PAPER and use safety brakes (pause, kill switch, square-off, reconcile, live master off),
always with a clear reason. You cannot turn anything LIVE, approve trades, resume entries, reset
the kill switch or acknowledge alerts: tell the operator to do those in the dashboard
(http://127.0.0.1:8000). Never guess prices or balances; report what the tools return."""


def http_call(base: str | None = None, token: str | None = None) -> Call:
    base = (base or os.environ.get("CLAUDETRADE_API_URL") or "http://127.0.0.1:8000").rstrip("/")
    token = token or os.environ.get("CLAUDETRADE_OPERATOR_TOKEN", "")

    def call(method: str, path: str, body: dict | None = None) -> Any:
        req = urllib.request.Request(
            base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token}", "X-Operator": "claude-mcp", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail")
            except Exception:  # noqa: BLE001
                detail = e.reason
            return {"ok": False, "status": e.code, "error": detail}
        except urllib.error.URLError as e:
            return {"ok": False, "error": f"desk not reachable at {base}: {e.reason}. Is the service running?"}
    return call


def _book(book: str) -> str:
    return "?book=" + ("live" if book == "live" else "paper")


def build_server(call: Call | None = None):
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    call = call or http_call()
    srv = MCPServer(name="claudetrade", instructions=INSTRUCTIONS)
    READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    BRAKE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    # ---- read -------------------------------------------------------------------------------
    @srv.tool(annotations=READ)
    def status() -> dict:
        """Desk overview: which books exist, live master switch, every strategy's mode, paper-record
        progress toward LIVE, and each book's health (kill switch, entries, broker, data, alerts)."""
        desk = call("GET", "/api/desk", None)
        if not isinstance(desk, dict) or desk.get("ok") is False:
            return desk
        books = {}
        for b in desk.get("books", []):
            d = call("GET", "/api/dashboard" + _book(b), None)
            if isinstance(d, dict) and "operations" in d:
                o, p = d["operations"], d["portfolio"]
                books[b] = {"kill_switch": o["kill_switch"], "entries_paused": o["entries_paused"],
                            "broker_healthy": o["broker_healthy"], "market_data_healthy": o["market_data_healthy"],
                            "reconciliation_ok": o["reconciliation_ok"], "unacked_critical": len(o.get("unacked_critical", [])),
                            "cash": p["cash"], "available": p["available"], "realised_pnl_today": p["realised_pnl_today"],
                            "open_positions": len(p["positions"]), "pending_orders": len(p["pending_orders"]),
                            "scheduler_last_tick": d.get("scheduler", {}).get("last_tick")}
        return {"as_of": desk.get("as_of"), "live_enabled": desk.get("live_enabled"), "live_master": desk.get("live_master"),
                "swing_book": desk.get("swing_book"), "books": books,
                "strategies": [{"key": r["key"], "label": r["label"], "mode": r["mode"], "eligible": r["eligible"],
                                "paper_trial": r["paper_trial"], "gate": r["gate"], "live_blockers": r["live_blockers"],
                                "today": r["books"]} for r in desk.get("strategies", [])]}

    @srv.tool(annotations=READ)
    def book_details(book: Literal["paper", "live"] = "paper") -> dict:
        """Full dashboard data for one book: portfolio, positions, pending orders, intraday strategies
        and P&L, swing proposals/approvals, risk limits, alerts, reconciliation, scheduler errors."""
        return call("GET", "/api/dashboard" + _book(book), None)

    @srv.tool(annotations=READ)
    def proposal(proposal_id: str, book: Literal["paper", "live"] = "paper") -> dict:
        """One swing proposal in full (rationale, evidence, risks, max loss, expiry, hash)."""
        return call("GET", f"/api/proposals/{proposal_id}" + _book(book), None)

    @srv.tool(annotations=READ)
    def research(book: Literal["paper", "live"] = "paper") -> dict:
        """Frozen strategy versions with eligibility, failures and walk-forward evidence."""
        return call("GET", "/api/strategies" + _book(book), None)

    @srv.tool(annotations=READ)
    def strategy_gate(key: str) -> dict:
        """A strategy's paper record toward LIVE (hours, clean sessions) and what still blocks LIVE."""
        return call("GET", f"/api/strategies/{key}/gate", None)

    @srv.tool(annotations=READ)
    def audit_trail(book: Literal["paper", "live"] = "paper", limit: int = 50, action: str | None = None) -> list | dict:
        """Recent audit events (who did what, when). Optional filter by action, e.g. RISK_DECISION, FILL, ALERT."""
        q = _book(book) + f"&limit={max(1, min(int(limit), 500))}" + (f"&action={action}" if action else "")
        return call("GET", "/api/audit" + q, None)

    # ---- allowed changes -------------------------------------------------------------------
    @srv.tool(annotations=BRAKE)
    def set_strategy_mode(key: str, mode: Literal["OFF", "PAPER"], reason: str) -> dict:
        """Switch a strategy (or 'SWING') to OFF or PAPER. Switching down from LIVE is immediate and
        allowed. LIVE cannot be set from here: the operator does that in the dashboard."""
        return call("POST", f"/api/strategies/{key}/mode", {"mode": mode, "reason": f"[claude] {reason}"})

    @srv.tool(annotations=BRAKE)
    def pause_new_entries(reason: str, book: Literal["paper", "live"] = "paper",
                          trade_mode: Literal["INTRADAY", "SWING"] | None = None) -> dict:
        """Stop new entries in a book (optionally only INTRADAY or SWING). Only the operator can resume."""
        body = {"reason": f"[claude] {reason}", **({"mode": trade_mode} if trade_mode else {})}
        return call("POST", "/api/controls/pause" + _book(book), body)

    @srv.tool(annotations=BRAKE)
    def kill_switch(reason: str, book: Literal["paper", "live", "all"] = "all") -> dict:
        """Emergency stop: no new entries. Exits and square-off keep working. Only the operator can reset it."""
        if book == "all":
            return call("POST", "/api/controls/kill-all", {"reason": f"[claude] {reason}"})
        return call("POST", "/api/controls/kill-switch" + _book(book), {"reason": f"[claude] {reason}"})

    @srv.tool(annotations=BRAKE)
    def square_off(reason: str, book: Literal["paper", "live"] = "paper") -> dict:
        """Exit all intraday positions in a book now and verify flat with the broker."""
        return call("POST", "/api/square-off" + _book(book), {"reason": f"[claude] {reason}"})

    @srv.tool(annotations=BRAKE)
    def reconcile(book: Literal["paper", "live"] = "paper") -> dict:
        """Compare the book with the broker's positions; returns any mismatches."""
        return call("POST", "/api/positions/reconcile" + _book(book), None)

    @srv.tool(annotations=BRAKE)
    def live_master_off(reason: str) -> dict:
        """Turn OFF the live master switch: every LIVE strategy drops to PAPER. Turning it ON is human-only."""
        out = call("POST", "/api/live/revoke", None)
        if isinstance(out, dict):
            out["reason_recorded_by_claude"] = reason
        return out

    return srv


def main() -> None:
    build_server().run("stdio")
