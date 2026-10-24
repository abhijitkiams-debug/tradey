"""Market data ingestion and validation (§FR-01).

Live quotes and historical bars are kept separate. Every quote records its source and
ingestion timestamps; ``snapshot()`` reports per-symbol validity so the Risk Engine can
fail closed on stale or inconsistent data.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd

from .config import RiskConfig, SessionConfig
from .models import Instrument, Quote, TradeMode


@dataclass(frozen=True)
class QuoteValidation:
    symbol: str
    valid: bool
    issues: tuple[str, ...]
    quote: Quote | None
    age_seconds: float | None


@dataclass
class MarketDataService:
    clock: Callable[[], datetime]
    risk: RiskConfig
    session: SessionConfig
    instruments: dict[str, Instrument] = field(default_factory=dict)
    _quotes: dict[str, Quote] = field(default_factory=dict)
    _avg_volume: dict[str, int] = field(default_factory=dict)
    feed_connected: bool = True

    def register(self, instrument: Instrument, avg_daily_volume: int | None = None) -> None:
        self.instruments[instrument.symbol] = instrument
        if avg_daily_volume is not None:
            self._avg_volume[instrument.symbol] = avg_daily_volume

    def ingest_quote(self, quote: Quote) -> None:
        self._quotes[quote.symbol] = quote

    def avg_volume(self, symbol: str) -> int | None:
        return self._avg_volume.get(symbol)

    def validate_quote(self, symbol: str, mode: TradeMode) -> QuoteValidation:
        issues: list[str] = []
        q = self._quotes.get(symbol)
        if not self.feed_connected:
            issues.append("market data feed disconnected")
        if symbol not in self.instruments:
            issues.append("unknown instrument")
        if q is None:
            return QuoteValidation(symbol, False, tuple(issues + ["no quote"]), None, None)
        now = self.clock()
        age = (now - q.source_ts).total_seconds()
        max_age = self.risk.max_quote_age_intraday_s if mode is TradeMode.INTRADAY else self.risk.max_quote_age_swing_s
        if age > max_age:
            issues.append(f"stale quote: {age:.1f}s old > {max_age}s")
        if age < -2:
            issues.append(f"quote timestamp {-age:.1f}s in the future")
        if q.ltp <= 0 or q.bid <= 0 or q.ask <= 0:
            issues.append("non-positive price")
        if q.bid > q.ask:
            issues.append("crossed book (bid > ask)")
        if q.ask > 0 and q.bid > 0 and not (q.bid * Decimal("0.98") <= q.ltp <= q.ask * Decimal("1.02")):
            issues.append("LTP inconsistent with bid/ask")
        if not self.session.is_open(now):
            issues.append("market session closed")
        return QuoteValidation(symbol, not issues, tuple(issues), q, age)

    def snapshot(self, symbols: list[str], mode: TradeMode) -> dict[str, QuoteValidation]:
        return {s: self.validate_quote(s, mode) for s in symbols}

    def is_healthy(self) -> bool:
        return self.feed_connected


def validate_bars(df: pd.DataFrame, freq: str = "1min", session: SessionConfig | None = None) -> list[str]:
    """Check a single-instrument OHLCV frame indexed by timestamp. Returns a list of issues
    (empty list == valid). Used both before backtests and before live signal evaluation."""
    issues: list[str] = []
    required = {"open", "high", "low", "close", "volume"}
    if missing := required - set(df.columns):
        return [f"missing columns: {sorted(missing)}"]
    if df.empty:
        return ["no bars"]
    if not df.index.is_monotonic_increasing:
        issues.append("timestamps not monotonic")
    if df.index.has_duplicates:
        issues.append(f"{int(df.index.duplicated().sum())} duplicate timestamps")
    if df[["open", "high", "low", "close"]].isna().any().any():
        issues.append("NaN prices")
    if (df[["open", "high", "low", "close"]] <= 0).any().any():
        issues.append("non-positive prices")
    bad = (df["high"] < df[["open", "close", "low"]].max(axis=1)) | (df["low"] > df[["open", "close", "high"]].min(axis=1))
    if bad.any():
        issues.append(f"{int(bad.sum())} bars with inconsistent OHLC")
    if (df["volume"] < 0).any():
        issues.append("negative volume")
    # Missing candles inside each session day
    step = pd.Timedelta(freq)
    for day, g in df.groupby(df.index.date):
        gaps = g.index.to_series().diff().dropna()
        missing = int(((gaps / step).round() - 1).clip(lower=0).sum())
        if missing:
            issues.append(f"{day}: {missing} missing candles")
    return issues


def detect_gap_pct(open_price: Decimal, prev_close: Decimal) -> Decimal:
    if prev_close <= 0:
        return Decimal("Infinity")
    return abs(open_price / prev_close - 1) * 100


def staleness(ts: datetime, now: datetime) -> timedelta:
    return now - ts
