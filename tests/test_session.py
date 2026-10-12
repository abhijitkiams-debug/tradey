from datetime import date, datetime
from decimal import Decimal as D

from conftest import fill, req, set_quote
from test_lifecycle import LENIENT

from claudetrade.app import ClaudeTrade
from claudetrade.backtest import BacktestConfig, walk_forward
from claudetrade.clock import SimClock
from claudetrade.lifecycle import SessionOutcome
from claudetrade.models import IST, OrderStatus, Product, TradeMode
from claudetrade.replay import load_universe, quote_from_bar, replay_session, session_start
from claudetrade.strategies import MeanReversion
from claudetrade.synthetic import universe


def _at(app, clock, h, m):
    clock.set(datetime(2026, 10, 13, h, m, tzinfo=IST))
    for s, px in (("ALPHA", 500), ("BRAVO", 1000), ("CHARLIE", 250), ("DELTA", 800)):
        set_quote(app, s, px)


def test_square_off_exits_and_verifies_flat(app, clock):
    app.execution.execute(req(qty=5))
    app.execution.execute(req(symbol="CHARLIE", qty=4, stop="245"))
    fill(app)
    resting = app.execution.execute(req(symbol="DELTA", qty=2, stop="790", order_type=__import__("claudetrade.models", fromlist=["OrderType"]).OrderType.LIMIT, limit="795"))
    _at(app, clock, 15, 0)
    app.intraday.tick()
    assert resting.order.status is OrderStatus.CANCELLED  # outstanding entries cancelled
    assert not app.intraday.square_off.verified_flat  # exits submitted, not yet filled
    app.broker.step()
    app.intraday.tick()
    st = app.intraday.square_off
    assert st.verified_flat
    assert app.portfolio.is_flat(TradeMode.INTRADAY)
    assert not {k: v for k, v in app.broker.get_positions().items() if k[1] is Product.MIS}
    assert app.audit.events("SQUARE_OFF_VERIFIED_FLAT")
    # no new intraday entries after square-off has started
    _at(app, clock, 15, 1)
    assert app.execution.execute(req(symbol="BRAVO", qty=1, stop="990")).order is None


def test_square_off_failure_escalates_and_never_reports_flat(app, clock):
    app.execution.execute(req(qty=5))
    fill(app)
    _at(app, clock, 15, 0)
    app.broker.faults.disconnected = True
    app.intraday.tick()
    _at(app, clock, 15, 10)
    st = app.intraday.run_square_off()
    assert not st.verified_flat
    assert app.controls.kill_switch
    assert any(a.severity == "CRITICAL" and "SQUARE-OFF FAILED" in a.message for a in app.monitor.alerts)
    assert app.dashboard()["intraday"]["square_off"]["verified_flat"] is False


def test_swing_positions_are_not_squared_off(app, clock):
    from test_swing import proposal

    p = app.swing.submit_proposal(proposal())
    a = app.swing.approve(p.proposal_id, p.version, p.content_hash(), "owner")
    app.swing.execute(a.approval_id)
    fill(app)
    _at(app, clock, 15, 0)
    app.intraday.tick()
    assert app.intraday.square_off.verified_flat
    assert app.portfolio.position("ALPHA", TradeMode.SWING).quantity == 6


def test_daily_loss_pauses_entries(app, clock):
    app.execution.execute(req(qty=8))
    fill(app)
    set_quote(app, "ALPHA", 455)
    app.intraday.tick()
    assert app.controls.entries_paused
    assert any("daily loss" in a.message for a in app.monitor.alerts)


def test_full_replayed_paper_session_ends_flat():
    data = universe(n_days=30, start=date(2026, 6, 1))
    days = sorted({d for d in data["ALPHA"].index.date})
    research = {s: df[df.index.date < days[20]] for s, df in data.items()}
    clock = SimClock(session_start(days[20]))
    app = ClaudeTrade.build(clock=clock)
    load_universe(app, data)
    wf = walk_forward(MeanReversion, research, BacktestConfig(), train_days=14, test_days=6, min_train_trades=1)
    sv = app.strategies.freeze(wf, LENIENT, app.risk_config)
    # first later session on which the frozen strategy actually signals (research never sees it)
    day = next(d for d in days[20:] if any(sv.instantiate().signals(df[df.index.date == d]) for df in data.values()))
    clock.set(session_start(day))
    rel = app.strategies.release(sv.content_hash, day, "owner")
    clock.set(datetime.combine(day, datetime.min.time(), IST).replace(hour=9, minute=20))
    for s, df in data.items():
        today = df[df.index.date == day]
        prev = D(str(df[df.index.date < day]["close"].iat[-1]))
        app.market_data.ingest_quote(quote_from_bar(s, today.iloc[4], prev, clock.now))
    v = app.strategies.validate_for_session(rel, app.market_data, list(data), app.risk_config, app.session)
    assert v.outcome is SessionOutcome.APPROVED, v.reasons
    assert app.intraday.activate(sv, v, list(data))
    replay_session(app, clock, data, day)
    report = app.intraday.end_of_day_report()
    assert report["orders"] > 0, "fixture should exercise real entries and exits"
    assert report["verified_flat"] is True
    assert app.portfolio.is_flat(TradeMode.INTRADAY)
    entries = [o for o in app.execution.orders.values() if not o.request.is_exit]
    assert entries and all(o.request.strategy_version == sv.label for o in entries)
    assert any(o.request.is_exit for o in app.execution.orders.values())
    assert len(app.audit.events("RISK_DECISION")) == len(app.execution.orders) + sum(
        1 for e in app.audit.events("RISK_DECISION") if not e.payload["approved"])
    assert app.audit.verify()
    assert app.ledger.reserved() == 0
