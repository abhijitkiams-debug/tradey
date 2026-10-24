"""Drive a paper session by replaying 1-minute bars as live quotes (§16 Phase 2)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pandas as pd

from .app import ClaudeTrade
from .clock import SimClock
from .models import IST, Instrument, Quote


def quote_from_bar(symbol: str, row: pd.Series, prev_close: Decimal, ts: datetime, spread_bps: Decimal = Decimal("4")) -> Quote:
    ltp = Decimal(str(row["close"]))
    half = (ltp * spread_bps / Decimal("20000")).quantize(Decimal("0.05")) or Decimal("0.05")
    return Quote(symbol, ltp, ltp - half, ltp + half, int(row["volume"]), prev_close, ts, ts, source="replay")


def load_universe(app: ClaudeTrade, data: dict[str, pd.DataFrame], avg_volume: int = 500_000) -> None:
    for sym in data:
        app.market_data.register(Instrument(sym), avg_daily_volume=avg_volume)


def replay_session(app: ClaudeTrade, clock: SimClock, data: dict[str, pd.DataFrame], day: date,
                   on_minute=None) -> None:
    """Feed one session minute by minute: quotes in, engine tick, broker fills."""
    history = {s: df[df.index.date < day] for s, df in data.items()}
    prev_close = {s: Decimal(str(h["close"].iat[-1])) if len(h) else Decimal(str(data[s]["open"].iat[0]))
                  for s, h in history.items()}
    todays = {s: df[df.index.date == day] for s, df in data.items()}
    minutes = sorted({ts for df in todays.values() for ts in df.index})
    for ts in minutes:
        clock.set(ts.to_pydatetime() + timedelta(minutes=1))
        for s, df in todays.items():
            if ts in df.index:
                app.market_data.ingest_quote(quote_from_bar(s, df.loc[ts], prev_close[s], clock.now))
                app.bars[s] = df[df.index <= ts]
        if hasattr(app.broker, "step"):  # paper broker simulates the exchange
            app.broker.step()
        app.intraday.tick()
        if on_minute:
            on_minute(ts)
        if clock.now.time() >= app.session.square_off_deadline and app.intraday.square_off.verified_flat:
            break


def session_start(day: date) -> datetime:
    return datetime.combine(day, time(9, 0), IST)
