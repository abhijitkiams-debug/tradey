"""Market data from Groww (§A, §FR-01): live quotes, intraday 1-minute candles, history.

Quotes go through ``MarketDataService`` validation like any other source; this module only
translates. ``source_ts`` is the exchange's last-trade time, so an illiquid or halted stock
naturally shows up as stale and blocks entries.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from .broker.groww import SEGMENT, RateLimiter
from .market_data import MarketDataService, validate_bars
from .models import IST, Quote


def _d(v: Any) -> Decimal:
    return Decimal(str(v if v is not None else 0))


class GrowwFeed:
    def __init__(self, client: Any, md: MarketDataService, clock: Callable[[], datetime],
                 exchange: str = "NSE", limiter: RateLimiter | None = None, timeout_s: int = 5):
        self.client, self.md, self.clock = client, md, clock
        self.targets: list[MarketDataService] = [md]  # one poll feeds every book (paper and live)
        self.min_interval_s = 5.0
        self._last: dict[str, tuple[datetime, Quote]] = {}
        self._bars: dict[str, tuple[datetime, pd.DataFrame]] = {}
        self.exchange = exchange
        self.limiter = limiter or RateLimiter()
        self.timeout_s = timeout_s
        self.errors: dict[str, str] = {}

    def quote(self, symbol: str) -> Quote | None:
        self.limiter.acquire("live")
        q = self.client.get_quote(trading_symbol=symbol, exchange=self.exchange, segment=SEGMENT, timeout=self.timeout_s)
        last = _d(q.get("last_price"))
        if last <= 0:
            return None
        prev_close = last - _d(q.get("day_change"))  # derived: robust to ohlc.close semantics
        ltt = q.get("last_trade_time")
        src = datetime.fromtimestamp(ltt / 1000, IST) if isinstance(ltt, (int, float)) and ltt > 0 else None
        if src is None:
            return None  # no timestamp, no trust
        return Quote(symbol, last, _d(q.get("bid_price")), _d(q.get("offer_price")), int(q.get("volume") or 0),
                     prev_close, src, self.clock(), source="groww")

    def add_target(self, md: MarketDataService) -> None:
        if all(t is not md for t in self.targets):  # identity: MarketDataService compares by value
            self.targets.append(md)

    def poll(self, symbols: list[str]) -> int:
        """Refresh quotes into every target; returns how many succeeded. Failures mark the feed
        unhealthy. A symbol polled within ``min_interval_s`` is served from that poll."""
        ok = 0
        now = self.clock()
        for s in symbols:
            cached = self._last.get(s)
            if cached and (now - cached[0]).total_seconds() < self.min_interval_s:
                q = cached[1]
            else:
                try:
                    q = self.quote(s)
                except Exception as e:  # noqa: BLE001 - any failure just means no fresh quote
                    self.errors[s] = type(e).__name__
                    self._last.pop(s, None)
                    continue
                if q is None:
                    continue
                self._last[s] = (now, q)
            for md in self.targets:
                md.ingest_quote(q)
            self.errors.pop(s, None)
            ok += 1
        for md in self.targets:
            md.feed_connected = ok > 0 or not symbols
        return ok

    def candles(self, symbol: str, start: datetime, end: datetime, interval: str = "1minute") -> pd.DataFrame:
        self.limiter.acquire("live")
        r = self.client.get_historical_candles(
            exchange=self.exchange, segment=SEGMENT, groww_symbol=f"{self.exchange}-{symbol}",
            start_time=start.strftime("%Y-%m-%d %H:%M:%S"), end_time=end.strftime("%Y-%m-%d %H:%M:%S"),
            candle_interval=interval, timeout=30,
        )
        rows = r.get("candles") or []
        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame([c[:6] for c in rows], columns=["timestamp", "open", "high", "low", "close", "volume"])
        ts = pd.to_datetime(df["timestamp"].astype(str).str.replace("T", " "), errors="coerce")
        if ts.isna().all():  # epoch seconds variant
            ts = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST)
        elif ts.dt.tz is None:
            ts = ts.dt.tz_localize(IST)
        df.index = ts
        return df.drop(columns="timestamp").astype({"open": float, "high": float, "low": float, "close": float,
                                                     "volume": "int64"}).sort_index()

    def today_bars(self, symbol: str) -> pd.DataFrame:
        now = self.clock()
        cached = self._bars.get(symbol)
        if cached and (now - cached[0]).total_seconds() < self.min_interval_s:
            return cached[1]
        start = datetime.combine(now.date(), time(9, 15), IST)
        df = self.candles(symbol, start, now)
        # drop the bar that is still forming
        df = df[df.index <= pd.Timestamp(now) - pd.Timedelta(minutes=1)]
        self._bars[symbol] = (now, df)
        return df

    def history(self, symbol: str, days: int, end: date | None = None, cache_dir: Path | None = None) -> pd.DataFrame:
        """1-minute history in 30-day chunks (Groww's per-request limit), optionally cached as CSV."""
        end = end or self.clock().date()
        start = end - timedelta(days=days)
        cache = cache_dir / f"{symbol}.csv" if cache_dir else None
        if cache and cache.exists():
            cached = pd.read_csv(cache, parse_dates=["timestamp"]).set_index("timestamp")
            if cached.index.tz is None:
                cached.index = cached.index.tz_localize(IST)
            if len(cached) and cached.index.min().date() <= start and cached.index.max().date() >= end - timedelta(days=1):
                return cached[(cached.index.date >= start) & (cached.index.date <= end)]
        parts, cur = [], start
        while cur < end:
            nxt = min(cur + timedelta(days=29), end)
            parts.append(self.candles(symbol, datetime.combine(cur, time(9, 15), IST), datetime.combine(nxt, time(15, 30), IST)))
            cur = nxt + timedelta(days=1)
        df = pd.concat([p for p in parts if len(p)]) if any(len(p) for p in parts) else parts[0]
        df = df[~df.index.duplicated()].sort_index()
        df = df[(df.index.time >= time(9, 15)) & (df.index.time < time(15, 30))]
        if cache is not None and len(df):
            cache.parent.mkdir(parents=True, exist_ok=True)
            df.rename_axis("timestamp").to_csv(cache)
        return df

    def research_dataset(self, symbols: list[str], days: int, cache_dir: Path | None = None) -> tuple[dict[str, pd.DataFrame], dict[str, list[str]]]:
        data, issues = {}, {}
        for s in symbols:
            df = self.history(s, days, cache_dir=cache_dir)
            problems = validate_bars(df)
            if problems:
                issues[s] = problems[:5]
            if len(df):
                data[s] = df
        return data, issues
