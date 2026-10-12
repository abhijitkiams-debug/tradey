from datetime import datetime
from decimal import Decimal as D

from conftest import fill, req, set_quote

from claudetrade.app import ClaudeTrade
from claudetrade.config import RiskConfig
from claudetrade.models import IST, Instrument, OperatingMode, Product, Side, TradeMode
from claudetrade.risk import size_by_risk


def failed(decision):
    return {c.name for c in decision.checks if not c.passed}


def test_valid_entry_is_approved(app):
    d = app.risk.validate(req(qty=5))
    assert d.approved, d.rejection_reasons
    assert d.reserved_amount > D("2500")  # order value + charges + buffer


def test_entry_requires_protective_stop(app):
    assert "stop_loss_present" in failed(app.risk.validate(req(stop=None)))


def test_stop_on_wrong_side(app):
    assert "stop_loss_side" in failed(app.risk.validate(req(stop="510")))


def test_risk_per_trade_limit(app):
    # 20 shares * ₹10 risk = ₹200 > ₹100 budget
    assert "risk_per_trade" in failed(app.risk.validate(req(qty=20, stop="490")))


def test_max_order_value(app):
    assert "max_order_value" in failed(app.risk.validate(req(symbol="BRAVO", qty=5, stop="995")))


def test_stale_data_blocks_entries(app, clock):
    clock.advance(seconds=30)
    assert "market_data_valid" in failed(app.risk.validate(req()))


def test_stale_data_does_not_block_exits(app, clock):
    app.execution.execute(req(qty=5))
    fill(app)
    clock.advance(seconds=30)
    d = app.risk.validate(req(side=Side.SELL, qty=5, exit_=True))
    assert d.approved, d.rejection_reasons


def test_research_mode_never_executes(clock):
    a = ClaudeTrade.build(mode=OperatingMode.RESEARCH, clock=clock)
    a.market_data.register(Instrument("ALPHA"), 1_000_000)
    set_quote(a, "ALPHA", 500)
    assert "operating_mode" in failed(a.risk.validate(req()))


def test_live_mode_requires_explicit_authorisation(clock):
    from claudetrade.broker import PaperBroker

    a = ClaudeTrade.build(mode=OperatingMode.LIVE, clock=clock, broker=PaperBroker(lambda s: None, clock))
    a.market_data.register(Instrument("ALPHA"), 1_000_000)
    set_quote(a, "ALPHA", 500)
    f = failed(a.risk.validate(req()))
    assert {"risk_config_approved", "live_authorised"} <= f


def test_live_authorisation_bound_to_risk_config(clock):
    from claudetrade.config import LiveAuthorisation

    cfg = RiskConfig().approve("owner", clock())
    auth = LiveAuthorisation()
    auth.authorise("owner", clock(), cfg)
    assert auth.is_valid_for(cfg)
    loosened = RiskConfig(max_daily_loss=D("5000")).approve("owner", clock())
    assert not auth.is_valid_for(loosened)


def test_unapproved_config_cannot_be_authorised(clock):
    import pytest

    from claudetrade.config import LiveAuthorisation

    with pytest.raises(PermissionError):
        LiveAuthorisation().authorise("owner", clock(), RiskConfig())


def test_live_mode_cannot_build_without_broker(clock):
    import pytest

    with pytest.raises(PermissionError):
        ClaudeTrade.build(mode=OperatingMode.LIVE, clock=clock)


def test_kill_switch_blocks_entries_but_not_exits(app):
    app.execution.execute(req(qty=5))
    fill(app)
    app.monitor.activate_kill_switch("operator", "test")
    assert "entries_enabled" in failed(app.risk.validate(req(symbol="CHARLIE", stop="245")))
    assert app.risk.validate(req(side=Side.SELL, qty=5, exit_=True)).approved


def test_product_must_match_mode(app):
    # CNC on an intraday trade would create overnight exposure
    assert "product_matches_mode" in failed(app.risk.validate(req(product=Product.CNC)))
    assert "product_matches_mode" in failed(app.risk.validate(req(mode=TradeMode.SWING, product=Product.MIS)))


def test_short_entries_blocked(app):
    assert "no_short_selling" in failed(app.risk.validate(req(side=Side.SELL, stop="510")))


def test_exit_must_reduce_position(app):
    assert "exit_reduces_position" in failed(app.risk.validate(req(side=Side.SELL, exit_=True)))


def test_no_intraday_entries_after_cutoff(app, clock):
    clock.set(datetime(2026, 10, 13, 14, 50, tzinfo=IST))
    set_quote(app, "ALPHA", 500)
    assert "before_entry_cutoff" in failed(app.risk.validate(req()))


def test_market_closed(app, clock):
    clock.set(datetime(2026, 10, 17, 11, 0, tzinfo=IST))  # Saturday
    set_quote(app, "ALPHA", 500)
    assert "session_open" in failed(app.risk.validate(req()))


def test_spread_gap_and_price_band(app):
    set_quote(app, "ALPHA", 500, spread="2.00")
    assert "spread" in failed(app.risk.validate(req()))
    set_quote(app, "ALPHA", 500, prev_close=450)
    assert "price_gap" in failed(app.risk.validate(req()))
    set_quote(app, "ALPHA", 500)
    from claudetrade.models import OrderType

    assert "limit_price_band" in failed(app.risk.validate(req(order_type=OrderType.LIMIT, limit="520", stop="515")))


def test_concurrent_position_limit(app):
    for sym, stop in (("ALPHA", "490"), ("CHARLIE", "245"), ("DELTA", "790")):
        r = app.execution.execute(req(symbol=sym, qty=2, stop=stop))
        assert r.order is not None, r.message
    fill(app)
    set_quote(app, "BRAVO", 300)
    assert "max_concurrent_positions" in failed(app.risk.validate(req(symbol="BRAVO", qty=2, stop="295")))


def test_daily_loss_limit_blocks_new_risk(app):
    app.execution.execute(req(qty=8, stop="490"))
    fill(app)
    set_quote(app, "ALPHA", 455)  # -₹360 unrealised on 8 shares
    set_quote(app, "CHARLIE", 250)
    assert "daily_loss_limit" in failed(app.risk.validate(req(symbol="CHARLIE", stop="245")))


def test_capital_cannot_be_double_committed(app):
    app.execution.execute(req(symbol="ALPHA", qty=7, stop="490"))   # ~₹3.5k reserved
    app.execution.execute(req(symbol="DELTA", qty=4, stop="790"))   # ~₹3.2k reserved
    # neither has filled yet: both reservations count against available capital
    d = app.risk.validate(req(symbol="CHARLIE", qty=14, stop="244"))
    assert "capital_available" in failed(d) or "max_portfolio_exposure" in failed(d)


def test_duplicate_request_id_rejected(app):
    r = req(cid="same-id")
    app.execution.execute(r)
    assert "not_duplicate" in failed(app.risk.validate(r))


def test_unknown_instrument(app):
    assert "instrument_known" in failed(app.risk.validate(req(symbol="ZZZ")))


def test_size_by_risk():
    assert size_by_risk(D("100"), D("500"), D("490"), D("10000")) == 10
    assert size_by_risk(D("100"), D("500"), D("490"), D("2000")) == 4  # capped by cash
    assert size_by_risk(D("100"), D("500"), D("350"), D("10000")) == 0  # too wide -> no trade
    assert size_by_risk(D("100"), D("500"), D("500"), D("10000")) == 0
