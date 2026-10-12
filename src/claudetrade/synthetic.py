"""Synthetic NSE-like 1-minute bars for tests and demos.

This data is random and carries no information about real markets. Results computed on it
demonstrate that the pipeline works; they are never evidence that a strategy is profitable.
"""

from __future__ import annotations

import zlib
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from .models import IST


def trading_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def generate_bars(symbol: str, days: list[date], start_price: float = 500.0, seed: int = 0,
                  minutes: int = 375) -> pd.DataFrame:
    rng = np.random.default_rng(zlib.crc32(f"{symbol}:{seed}".encode()))
    frames = []
    price = start_price
    for d in days:
        regime = rng.choice(["trend_up", "trend_down", "range"], p=[0.3, 0.3, 0.4])
        drift = {"trend_up": 0.00012, "trend_down": -0.00012, "range": 0.0}[regime]
        vol = rng.uniform(0.0006, 0.0014)
        gap = rng.normal(0, 0.004)
        open_px = price * (1 + gap)
        rets = rng.normal(drift, vol, minutes)
        if regime == "range":  # pull back toward the open
            path = [open_px]
            for r in rets:
                path.append(path[-1] * (1 + r) - 0.02 * (path[-1] - open_px) / 10)
            close = np.array(path[1:])
        else:
            close = open_px * np.cumprod(1 + rets)
        opens = np.concatenate([[open_px], close[:-1]])
        wick = np.abs(rng.normal(0, vol / 2, minutes)) * close
        high = np.maximum(opens, close) + wick
        low = np.minimum(opens, close) - np.abs(rng.normal(0, vol / 2, minutes)) * close
        u = np.linspace(-1, 1, minutes)
        volume = (rng.uniform(800, 1500, minutes) * (1 + 2 * u**2)).astype(int)
        idx = pd.date_range(datetime.combine(d, time(9, 15), IST), periods=minutes, freq="1min")
        frames.append(pd.DataFrame({"open": opens, "high": high, "low": low, "close": close, "volume": volume}, index=idx))
        price = float(close[-1])
    return pd.concat(frames).round({"open": 2, "high": 2, "low": 2, "close": 2})


def universe(n_days: int = 60, start: date = date(2026, 6, 1), seed: int = 7) -> dict[str, pd.DataFrame]:
    days = trading_days(start, n_days)
    specs = {"ALPHA": 520.0, "BRAVO": 1240.0, "CHARLIE": 310.0, "DELTA": 880.0}
    return {s: generate_bars(s, days, p, seed) for s, p in specs.items()}
