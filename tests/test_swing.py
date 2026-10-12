from decimal import Decimal as D

import pytest
from conftest import fill, set_quote

from claudetrade.models import OrderStatus, TradeMode
from claudetrade.swing import ApprovalStatus, SchemaError


def proposal(**over):
    p = {
        "symbol": "ALPHA", "exchange": "NSE", "recommendation": "BUY",
        "entry_low": 498, "entry_high": 502, "quantity": 6, "stop_loss": 486,
        "target_low": 530, "target_high": 545, "horizon_days": 15,
        "rationale": "Higher lows above the 50-day average with improving volume.",
        "evidence": ["50DMA rising", "volume above 20d average on up days"],
        "risks": ["broad market weakness", "results due in 3 weeks"],
        "invalidation": ["daily close below 486"],
        "confidence_notes": "Moderate; based on price/volume only.",
    }
    p.update(over)
    return p


def test_valid_proposal_is_stored_with_system_reference_price(app):
    p = app.swing.submit_proposal(proposal())
    assert p.reference_price == D("500") and p.estimated_costs > 0
    assert p.max_loss == (D("502") - D("486")) * 6 + p.estimated_costs
    assert app.swing.pending() == [p]


@pytest.mark.parametrize("bad", [
    {"recommendation": "STRONG BUY"},
    {"stop_loss": 505},
    {"entry_low": 510, "entry_high": 500},
    {"quantity": 0},
    {"horizon_days": 2},
    {"rationale": "looks good"},
    {"risks": []},
    {"exchange": "BSE"},
    {"target_low": 500},
])
def test_schema_rejects_malformed_output(app, bad):
    with pytest.raises(SchemaError):
        app.swing.submit_proposal(proposal(**bad))
    assert app.audit.events("PROPOSAL_REJECTED")


def test_no_trade_is_a_valid_proposal_but_not_approvable(app):
    p = app.swing.submit_proposal(proposal(recommendation="NO-TRADE", entry_low=None, entry_high=None,
                                           stop_loss=None, target_low=None, target_high=None, quantity=0,
                                           evidence=[], invalidation=[]))
    with pytest.raises(PermissionError):
        app.swing.approve(p.proposal_id, p.version, p.content_hash(), "owner")


def test_approval_is_bound_to_exact_hash(app):
    p = app.swing.submit_proposal(proposal())
    with pytest.raises(PermissionError):
        app.swing.approve(p.proposal_id, p.version, "0" * 64, "owner")


def test_approved_proposal_executes_once(app):
    p = app.swing.submit_proposal(proposal())
    a = app.swing.approve(p.proposal_id, p.version, p.content_hash(), "owner")
    r = app.swing.execute(a.approval_id)
    assert r.order is not None and r.order.request.approval_id == a.approval_id
    fill(app)
    assert r.order.status is OrderStatus.COMPLETED
    assert app.portfolio.position("ALPHA", TradeMode.SWING).quantity == 6
    assert a.status is ApprovalStatus.CONSUMED
    assert "CONSUMED" in app.swing.execute(a.approval_id)


def test_revision_invalidates_prior_approval(app):
    p = app.swing.submit_proposal(proposal())
    a = app.swing.approve(p.proposal_id, p.version, p.content_hash(), "owner")
    p2 = app.swing.revise(p.proposal_id, {"quantity": 7}, actor="claude")
    assert p2.version == 2
    assert a.status is ApprovalStatus.INVALIDATED
    assert app.swing.execute(a.approval_id).startswith("approval INVALIDATED")


def test_expired_proposal_cannot_execute(app, clock):
    p = app.swing.submit_proposal(proposal())
    a = app.swing.approve(p.proposal_id, p.version, p.content_hash(), "owner")
    clock.advance(minutes=app.risk_config.swing_proposal_ttl_minutes + 1)
    set_quote(app, "ALPHA", 500)
    assert "expired" in app.swing.execute(a.approval_id)
    assert not app.execution.orders


def test_material_price_change_voids_approval(app):
    p = app.swing.submit_proposal(proposal())
    a = app.swing.approve(p.proposal_id, p.version, p.content_hash(), "owner")
    set_quote(app, "ALPHA", 510)  # +2% > 1.5% material-change threshold
    msg = app.swing.execute(a.approval_id)
    assert "material change" in msg and a.status is ApprovalStatus.INVALIDATED
    assert not app.execution.orders


def test_swing_and_intraday_share_one_capital_pool(app):
    from conftest import req

    p = app.swing.submit_proposal(proposal())
    a = app.swing.approve(p.proposal_id, p.version, p.content_hash(), "owner")
    app.swing.execute(a.approval_id)  # reserves ~₹3k (unfilled)
    before = app.ledger.available()
    assert before < D("7000")
    r = app.execution.execute(req(symbol="DELTA", qty=4, stop="790"))
    assert r.order is not None
    assert app.ledger.available() < before - D("3000")
