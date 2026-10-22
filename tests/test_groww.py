from datetime import datetime
from decimal import Decimal as D

import pytest
from conftest import req, set_quote
from fake_groww import FakeGroww, GrowwAPIAuthenticationException, GrowwAPIException, GrowwAPIRateLimitException

from claudetrade.app import ClaudeTrade
from claudetrade.broker.groww import GrowwBroker, RateLimiter, valid_reference
from claudetrade.config import RiskConfig
from claudetrade.feeds import GrowwFeed
from claudetrade.models import IST, Instrument, OperatingMode, OrderStatus, Product, TradeMode


@pytest.fixture
def fake():
    return FakeGroww()


@pytest.fixture
def live(clock, fake):
    """A LIVE-mode app on the fake Groww account, approved and authorised."""
    broker = GrowwBroker(fake, clock, limiter=RateLimiter(sleep=lambda s: None))
    cfg = RiskConfig().approve("owner", clock())
    app = ClaudeTrade.build(mode=OperatingMode.LIVE, clock=clock, broker=broker, risk_config=cfg)
    for sym in ("ALPHA", "CHARLIE"):
        app.market_data.register(Instrument(sym), avg_daily_volume=1_000_000)
    set_quote(app, "ALPHA", 500)
    set_quote(app, "CHARLIE", 250)
    broker.heartbeat()
    app.authorise_live("owner")
    return app


def test_reference_format():
    assert valid_reference("id-0123456789ab") and valid_reference("sw-ap-0123456789")
    assert not valid_reference("t-1") and not valid_reference("a-b-c-defgh") and not valid_reference("x" * 21)


def test_live_order_round_trip(live, fake):
    r = live.execution.execute(req(qty=5, cid="id-000000000001"))
    assert r.order.status is OrderStatus.OPEN and r.order.broker_order_id.startswith("GMK")
    sent = fake.orders[r.order.broker_order_id]
    assert sent["product"] == "MIS" and sent["order_reference_id"] == "id-000000000001"
    fake.execute(r.order.broker_order_id, 5, 500.05)
    live.execution.sync()
    assert r.order.status is OrderStatus.COMPLETED and r.order.filled_quantity == 5
    assert live.portfolio.position("ALPHA", TradeMode.INTRADAY).quantity == 5
    assert live.ledger.reserved() == 0
    assert live.execution.reconcile_positions() == []


def test_invalid_reference_never_sent(live, fake):
    r = live.execution.execute(req(qty=5, cid="short"))
    assert r.order.status is OrderStatus.REJECTED and "place_order" not in fake.calls


def test_timeout_after_acceptance_reconciles_by_reference(live, fake):
    fake.accept_then_timeout = True
    r = live.execution.execute(req(qty=5, cid="id-000000000002"))
    assert r.order.broker_order_id is not None
    assert fake.calls.count("place_order") == 1  # no blind resubmission
    assert "get_order_status_by_reference" in fake.calls


def test_not_found_waits_for_grace_period(live, fake, clock):
    fake.fail_next["place_order"] = GrowwAPIException("socket closed", "502")  # ambiguous
    r = live.execution.execute(req(qty=5, cid="id-000000000003"))
    assert r.order.status is OrderStatus.UNKNOWN  # too early to conclude
    assert live.execution.execute(req(symbol="CHARLIE", qty=2, stop="245", cid="id-000000000004")).order is None
    clock.advance(seconds=31)
    set_quote(live, "ALPHA", 500)
    live.broker.heartbeat()
    live.execution.sync()
    assert r.order.status is OrderStatus.REJECTED and live.ledger.reserved() == 0


def test_definitive_rejection_and_rate_limit(live, fake):
    fake.fail_next["place_order"] = GrowwAPIException("Insufficient funds", "GA005")
    r = live.execution.execute(req(qty=5, cid="id-000000000005"))
    assert r.order.status is OrderStatus.REJECTED and "Insufficient" in r.order.reject_reason
    fake.fail_next["place_order"] = GrowwAPIRateLimitException()
    r = live.execution.execute(req(qty=5, cid="id-000000000006"))
    assert r.order.status is OrderStatus.REJECTED and live.ledger.reserved() == 0


def test_status_terminal_only_when_trades_arrive(live, fake):
    r = live.execution.execute(req(qty=5, cid="id-000000000007"))
    fake.execute(r.order.broker_order_id, 5, 500.0, record_trade=False)  # status EXECUTED, trade list lagging
    live.execution.sync()
    assert r.order.status is OrderStatus.PARTIALLY_FILLED
    assert live.portfolio.position("ALPHA", TradeMode.INTRADAY).quantity == 0
    fake.trades[r.order.broker_order_id].append({"groww_trade_id": "T99", "price": 500.0, "quantity": 5,
                                                 "trade_date_time": "2026-10-13T10:00:30"})
    live.execution.sync()
    assert r.order.status is OrderStatus.COMPLETED
    assert live.portfolio.position("ALPHA", TradeMode.INTRADAY).quantity == 5


def test_auth_failure_marks_broker_unhealthy(live, fake):
    fake.fail_next["get_available_margin_details"] = GrowwAPIAuthenticationException()
    assert live.broker.heartbeat() is False
    assert not live.broker.is_healthy()
    assert "broker_healthy" in live.execution.execute(req(qty=5, cid="id-000000000008")).message


def test_health_expires_without_heartbeat(live, clock):
    clock.advance(seconds=120)
    assert not live.broker.is_healthy()


def test_positions_include_holdings_and_respect_external(live, fake):
    fake.holdings = [{"trading_symbol": "INFY", "quantity": 10}]
    assert live.broker.get_positions() == {("INFY", Product.CNC): 10}
    # an unmanaged external holding does not block trading
    assert live.execution.reconcile_positions() == []


def test_cancel_and_funds(live, fake):
    from claudetrade.models import OrderType

    r = live.execution.execute(req(qty=5, cid="id-000000000009", order_type=OrderType.LIMIT, limit="498"))
    live.execution.cancel("id-000000000009")
    assert r.order.status is OrderStatus.CANCELLED and live.ledger.reserved() == 0
    assert live.broker.get_available_funds() == D("9876.5")


def test_cancel_failure_is_reported(live, fake):
    r = live.execution.execute(req(qty=5, cid="id-000000000010"))
    fake.fail_next["cancel_order"] = GrowwAPIException("Order already executed", "GA010")
    assert "cancel failed" in live.execution.cancel(r.order.client_request_id)


def test_rate_limiter_blocks_bursts():
    t = [0.0]
    waits = []
    rl = RateLimiter(now=lambda: t[0], sleep=lambda s: (waits.append(s), t.__setitem__(0, t[0] + s)))
    for _ in range(25):
        rl.acquire("orders")
    assert waits and t[0] >= 2.0  # 25 orders at 10/s need >= 2 seconds


def test_live_requires_authorisation_per_risk_config(clock, fake):
    broker = GrowwBroker(fake, clock, limiter=RateLimiter(sleep=lambda s: None))
    app = ClaudeTrade.build(mode=OperatingMode.LIVE, clock=clock, broker=broker)
    with pytest.raises(PermissionError):
        app.authorise_live("owner")  # default config isn't operator-approved


def test_paper_mode_refuses_groww_broker(clock, fake):
    with pytest.raises(PermissionError):
        ClaudeTrade.build(clock=clock, broker=GrowwBroker(fake, clock))


def test_feed_translates_quotes_and_candles(app, fake, clock):
    now_ms = int(clock().timestamp() * 1000)
    fake.quotes["ALPHA"] = {"last_price": 501.5, "bid_price": 501.45, "offer_price": 501.55, "volume": 12000,
                            "day_change": 1.5, "last_trade_time": now_ms - 2000}
    fake.quotes["BRAVO"] = {"last_price": 0}
    feed = GrowwFeed(fake, app.market_data, clock, limiter=RateLimiter(sleep=lambda s: None))
    feed.min_interval_s = 0  # no poll cache: each poll hits the API
    assert feed.poll(["ALPHA", "BRAVO"]) == 1
    v = app.market_data.validate_quote("ALPHA", TradeMode.INTRADAY)
    assert v.valid and v.quote.prev_close == D("500.0") and v.quote.source == "groww"
    fake.quotes["ALPHA"]["last_trade_time"] = now_ms - 60_000
    feed.poll(["ALPHA"])
    assert not app.market_data.validate_quote("ALPHA", TradeMode.INTRADAY).valid  # stale last trade
    fake.candles["NSE-ALPHA"] = [["2026-10-13T09:15:00", 500, 501, 499, 500.5, 1000, None],
                                 ["2026-10-13T09:16:00", 500.5, 502, 500, 501.5, 1500, None]]
    bars = feed.today_bars("ALPHA")
    assert list(bars.columns) == ["open", "high", "low", "close", "volume"] and len(bars) == 2
    assert bars.index[0] == datetime(2026, 10, 13, 9, 15, tzinfo=IST)


def test_feed_failure_marks_data_unhealthy(app, fake, clock):
    fake.fail_next["get_quote"] = GrowwAPIException("down", "503")
    feed = GrowwFeed(fake, app.market_data, clock, limiter=RateLimiter(sleep=lambda s: None))
    assert feed.poll(["ALPHA"]) == 0
    assert not app.market_data.is_healthy()
