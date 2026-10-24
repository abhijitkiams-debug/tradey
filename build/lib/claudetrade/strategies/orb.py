from __future__ import annotations

from datetime import time

import pandas as pd

from .base import REGISTRY, Signal, Strategy, StrategySpec


@REGISTRY.register
class OpeningRangeBreakout(Strategy):
    family = "ORB"
    spec = StrategySpec(
        family="ORB",
        description="Long breakout above the opening range high, confirmed on bar close.",
        entry="Close above OR high after the range is complete, with volume above its intraday average.",
        exit="Target at R-multiple of initial risk, stop at OR low (or OR mid), else time exit before square-off.",
        stop_logic="OR low or OR midpoint (param stop_at)",
        sizing="floor(risk budget / (entry - stop)), capped by available capital",
        max_holding="same session",
        session_rules="entries only between OR end and entry_cutoff; one trade per symbol per day",
        regime_assumption="trending / directional sessions; underperforms in choppy, range-bound days",
        liquidity_filter="min price, min avg daily volume, max spread (Risk Engine)",
    )
    default_params = {"or_minutes": 15, "stop_at": "low", "r_multiple": 2.0, "vol_mult": 1.2,
                      "entry_cutoff": "13:30", "max_or_pct": 2.5}
    param_grid = {"or_minutes": [15, 30], "stop_at": ["low", "mid"], "r_multiple": [1.5, 2.0, 3.0]}

    def signals(self, day: pd.DataFrame) -> list[Signal]:
        p = self.params
        start = day.index[0]
        or_end = start + pd.Timedelta(minutes=p["or_minutes"])
        rng = day[day.index < or_end]
        if len(rng) < 2:
            return []
        hi, lo = float(rng["high"].max()), float(rng["low"].min())
        if (hi - lo) / lo * 100 > p["max_or_pct"]:
            return []  # opening range too wide: risk per share too large / gap day
        stop = lo if p["stop_at"] == "low" else (hi + lo) / 2
        cutoff = time.fromisoformat(p["entry_cutoff"])
        vol = day["volume"].to_numpy()
        vol_avg = day["volume"].expanding().mean().to_numpy()
        close = day["close"].to_numpy()
        times = day.index.time
        for i in range(len(rng), len(day)):
            if times[i] >= cutoff:
                break
            c = float(close[i])
            if c > hi and vol[i] >= p["vol_mult"] * vol_avg[i]:
                risk = c - stop
                if risk <= 0:
                    return []
                return [Signal(i, "BUY", stop, c + p["r_multiple"] * risk, "close above opening range high")]
        return []
