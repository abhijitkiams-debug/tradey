import json

import pytest
from conftest import fill, req
from test_swing import proposal

from claudetrade.tools import FORBIDDEN_ACTIONS, TOOLS, ToolGateway


@pytest.fixture
def gw(app):
    return ToolGateway(app, "paper")


def test_tool_surface_matches_contract():
    names = {t["name"] for t in TOOLS}
    assert names == {"get_market_snapshot", "get_portfolio_state", "get_strategy_results", "propose_strategy",
                     "validate_strategy", "propose_swing_trade", "validate_order", "request_order", "get_order_status",
                     "cancel_order", "get_positions", "request_square_off", "pause_new_entries", "get_system_health"}
    for bad in FORBIDDEN_ACTIONS:
        assert not any(bad in n for n in names)


def test_definitions_are_strict_and_api_safe(gw):
    defs = gw.tool_definitions()
    blob = json.dumps(defs)
    for k in ("minimum", "maxItems", "minLength", "pattern"):
        assert f'"{k}"' not in blob
    for d in defs:
        assert d["strict"] is True and d["input_schema"]["additionalProperties"] is False


def test_research_environment_is_read_and_research_only(app):
    gw = ToolGateway(app, "research")
    names = {d["name"] for d in gw.tool_definitions()}
    assert "request_order" not in names and "propose_swing_trade" not in names
    r = gw.call("request_order", {"kind": "exit", "symbol": "ALPHA", "mode": "INTRADAY", "quantity": 1})
    assert r["ok"] is False and "not permitted" in r["error"]


def test_inputs_are_schema_validated(gw):
    assert "unexpected fields" in gw.call("get_portfolio_state", {"approve": True})["error"]
    assert "invalid input" in gw.call("validate_order", {"symbol": "ALPHA", "side": "BUY", "quantity": -3, "mode": "INTRADAY"})["error"]
    assert "invalid input" in gw.call("get_market_snapshot", {"symbols": ["alpha;drop"], "mode": "SWING"})["error"]
    assert gw.call("nonexistent", {})["error"] == "unknown tool"
    assert gw.app.audit.events("TOOL_DENIED")


def test_validate_order_is_a_dry_run(gw, app):
    r = gw.call("validate_order", {"symbol": "ALPHA", "side": "BUY", "quantity": 5, "mode": "INTRADAY", "stop_loss": 490})
    assert r["ok"] and r["approved"] and r["dry_run"]
    assert app.ledger.reserved() == 0 and not app.execution.orders


def test_claude_cannot_open_intraday_positions(gw):
    r = gw.call("request_order", {"kind": "entry", "symbol": "ALPHA", "mode": "INTRADAY", "quantity": 5})
    assert r["ok"] is False and "invalid input" in r["error"]


def test_claude_cannot_execute_swing_without_approval(gw, app):
    p = gw.call("propose_swing_trade", proposal())
    assert p["ok"] and p["status"] == "AWAITING_OPERATOR_APPROVAL"
    r = gw.call("request_order", {"kind": "approved_swing", "approval_id": "ap-forged"})
    assert r["ok"] is False and not app.execution.orders


def test_claude_can_execute_after_operator_approval(gw, app):
    p = gw.call("propose_swing_trade", proposal())
    sp = app.swing.latest(p["proposal_id"])
    a = app.swing.approve(sp.proposal_id, sp.version, sp.content_hash(), "owner")
    r = gw.call("request_order", {"kind": "approved_swing", "approval_id": a.approval_id})
    assert r["ok"] and r["risk_approved"] and "not a fill" in r["note"]
    fill(app)
    st = gw.call("get_order_status", {"client_request_id": r["client_request_id"]})
    assert st["status"] == "COMPLETED" and st["filled"] == 6


def test_claude_exit_and_safety_tools(gw, app):
    app.execution.execute(req(qty=5))
    fill(app)
    r = gw.call("request_order", {"kind": "exit", "symbol": "ALPHA", "mode": "INTRADAY", "quantity": 5})
    assert r["ok"], r
    assert gw.call("pause_new_entries", {"reason": "feed looks wrong"})["ok"]
    assert app.controls.entries_paused
    assert not hasattr(gw, "_t_resume_entries")
    health = gw.call("get_system_health", {})
    assert health["entries_paused"] is True and health["audit_chain_intact"] is True


def test_invalid_claude_proposal_returns_error_not_exception(gw):
    r = gw.call("propose_swing_trade", {**proposal(), "stop_loss": 600})
    assert r["ok"] is False and "stop_loss" in r["error"]


def test_audit_chain_detects_tampering(gw, app):
    gw.call("get_portfolio_state", {})
    assert app.audit.verify()
    ev = app.audit._events[1]
    object.__setattr__(ev, "actor", "someone-else")
    assert not app.audit.verify()


def test_claude_cannot_cancel_exits(gw, app):
    app.execution.execute(req(qty=5))
    fill(app)
    from claudetrade.models import OrderType, Side

    ex = app.execution.execute(req(side=Side.SELL, qty=5, exit_=True, order_type=OrderType.LIMIT, limit="504"))
    r = gw.call("cancel_order", {"client_request_id": ex.order.client_request_id})
    assert r["ok"] is False and ex.order.status.value == "OPEN"
