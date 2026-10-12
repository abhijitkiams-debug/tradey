from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from claudetrade.app import ClaudeTrade
from claudetrade.clock import SimClock
from claudetrade.models import IST, Instrument, OrderRequest, OrderType, Product, Quote, Side, TradeMode

D = Decimal


@pytest.fixture
def clock():
    return SimClock(datetime(2026, 10, 13, 10, 0, tzinfo=IST))  # a Tuesday, mid-session


@pytest.fixture
def app(clock):
    a = ClaudeTrade.build(clock=clock)
    for sym in ("ALPHA", "BRAVO", "CHARLIE", "DELTA"):
        a.market_data.register(Instrument(sym), avg_daily_volume=1_000_000)
    for sym, px in (("ALPHA", "500"), ("BRAVO", "1000"), ("CHARLIE", "250"), ("DELTA", "800")):
        set_quote(a, sym, px)
    return a


def set_quote(app, symbol, ltp, prev_close=None, spread="0.10", age_s=0):
    ltp = D(str(ltp))
    half = D(spread) / 2
    ts = app.clock()
    from datetime import timedelta

    app.market_data.ingest_quote(Quote(symbol, ltp, ltp - half, ltp + half, 10_000,
                                       D(str(prev_close)) if prev_close else ltp, ts - timedelta(seconds=age_s), ts, "test"))


_n = 0


def req(symbol="ALPHA", side=Side.BUY, qty=5, mode=TradeMode.INTRADAY, stop="490", exit_=False,
        order_type=OrderType.MARKET, limit=None, product=None, cid=None):
    global _n
    _n += 1
    return OrderRequest(
        client_request_id=cid or f"t-{_n}",
        symbol=symbol, side=side, quantity=qty, order_type=order_type,
        product=product or (Product.MIS if mode is TradeMode.INTRADAY else Product.CNC),
        mode=mode, limit_price=D(limit) if limit else None,
        stop_loss=D(stop) if (stop and not exit_) else None, is_exit=exit_,
    )


def fill(app):
    app.broker.step()
    app.execution.sync()
