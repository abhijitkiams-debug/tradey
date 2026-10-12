from decimal import Decimal as D

from conftest import fill, req, set_quote

from claudetrade.models import OrderStatus, OrderType, Product, Side, TradeMode


def test_order_lifecycle_and_ledger(app):
    r = app.execution.execute(req(qty=5))
    assert r.order.status is OrderStatus.OPEN
    assert app.ledger.reserved() > 0
    fill(app)
    assert r.order.status is OrderStatus.COMPLETED
    assert app.ledger.reserved() == 0  # remainder released after completion
    pos = app.portfolio.position("ALPHA", TradeMode.INTRADAY)
    assert pos.quantity == 5 and pos.stop_loss == D("490")
    assert app.ledger.cash() < D("10000") - D("2500")  # paid price + charges
    assert app.execution.reconcile_positions() == []


def test_timeout_after_acceptance_is_reconciled_not_resubmitted(app):
    app.broker.faults.timeout_next_accepted = True
    r = app.execution.execute(req(qty=5, cid="tmo-1"))
    assert r.order.broker_order_id is not None
    fill(app)
    assert r.order.status is OrderStatus.COMPLETED
    assert len([o for o in app.broker.list_orders() if o.client_request_id == "tmo-1"]) == 1
    assert app.portfolio.position("ALPHA", TradeMode.INTRADAY).quantity == 5
    # a naive retry with the same id is stopped by the risk engine (and the broker)
    again = app.execution.execute(req(qty=5, cid="tmo-1"))
    assert again.order is None and "not_duplicate" in again.message


def test_timeout_before_delivery_releases_capital(app):
    app.broker.faults.timeout_next_lost = True
    r = app.execution.execute(req(qty=5))
    assert r.order.status is OrderStatus.REJECTED
    assert "not received" in r.order.reject_reason
    assert app.ledger.reserved() == 0 and app.ledger.available() == D("10000")


def test_broker_reject_releases_reservation(app):
    app.broker.faults.reject_next = "RMS: insufficient margin"
    r = app.execution.execute(req(qty=5))
    assert r.order.status is OrderStatus.REJECTED
    assert app.ledger.available() == D("10000")
    assert "REJECTED" in [e.action.split("_")[-1] for e in app.audit.events()]


def test_partial_fill_then_cancel(app):
    app.broker.faults.partial_fill_ratio = D("0.4")
    r = app.execution.execute(req(qty=5))
    fill(app)
    assert r.order.status is OrderStatus.PARTIALLY_FILLED and r.order.filled_quantity == 2
    assert app.ledger.reserved() > 0  # remaining quantity still reserved
    app.execution.cancel(r.order.client_request_id)
    assert r.order.status is OrderStatus.CANCELLED
    assert app.ledger.reserved() == 0
    assert app.portfolio.position("ALPHA", TradeMode.INTRADAY).quantity == 2
    assert app.execution.reconcile_positions() == []


def test_fills_are_applied_exactly_once(app):
    app.execution.execute(req(qty=5))
    fill(app)
    cash = app.ledger.cash()
    app.execution.sync()
    app.execution.sync()
    assert app.ledger.cash() == cash
    assert app.portfolio.position("ALPHA", TradeMode.INTRADAY).quantity == 5


def test_round_trip_realises_pnl_net_of_charges(app):
    app.execution.execute(req(qty=5))
    fill(app)
    set_quote(app, "ALPHA", 510)
    app.execution.execute(req(side=Side.SELL, qty=5, exit_=True))
    fill(app)
    pos = app.portfolio.position("ALPHA", TradeMode.INTRADAY)
    assert pos.is_flat
    pnl = app.portfolio.realised_pnl_on(app.clock().date())
    assert D("0") < pnl < D("50")  # ~₹50 gross less slippage and charges
    assert app.ledger.cash() == D("10000") + pnl


def test_disconnect_blocks_entries(app):
    app.broker.faults.disconnected = True
    r = app.execution.execute(req())
    assert r.order is None and "broker_healthy" in r.message


def test_position_mismatch_blocks_new_entries(app):
    app.execution.execute(req(qty=5))
    fill(app)
    app.broker._positions[("ALPHA", Product.MIS)] = 7  # broker reports something we didn't book
    assert app.execution.reconcile_positions()
    assert app.execution.reconciliation_ok is False
    r = app.execution.execute(req(symbol="CHARLIE", qty=2, stop="245"))
    assert r.order is None and "reconciliation_ok" in r.message
    assert any(a.severity == "HIGH" for a in app.monitor.alerts)


def test_every_order_has_a_risk_decision_in_audit(app):
    app.execution.execute(req(qty=5))
    app.execution.execute(req(qty=500))  # rejected
    decisions = app.audit.events("RISK_DECISION")
    assert len(decisions) == 2 and [d.payload["approved"] for d in decisions] == [True, False]
    assert app.audit.verify()


def test_limit_order_waits_for_price(app):
    r = app.execution.execute(req(order_type=OrderType.LIMIT, limit="498", qty=5))
    fill(app)
    assert r.order.status is OrderStatus.OPEN
    set_quote(app, "ALPHA", "497.9")
    fill(app)
    assert r.order.status is OrderStatus.COMPLETED and r.order.average_price <= D("498")
