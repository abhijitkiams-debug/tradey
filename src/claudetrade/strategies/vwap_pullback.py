from __future__ import annotations

from datetime import time

import pandas as pd

from .base import REGISTRY, Signal, Strategy, StrategySpec


@REGISTRY.register
class VWAPTrendPullback(Strategy):
    family = "VWAP_PULLBACK"
    spec = StrategySpec(
        family="VWAP_PULLBACK",
        description="In an up-trending session, buy a pullback to VWAP that closes back above it.",
        entry="Price above a rising VWAP; a bar's low tags VWAP (within band) and closes above VWAP.",
        exit="Target at R-multiple; stop below VWAP by ATR multiple; time exit before square-off.",
        stop_logic="min(signal bar low, VWAP - atr_mult * ATR)",
        sizing="floor(risk budget / (entry - stop)), capped by available capital",
        max_holding="same session",
        session_rules="entries between start_after and entry_cutoff; max_trades per day",
        regime_assumption="intraday uptrend; fails in reversal / mean-reverting sessions",
        liquidity_filter="min price, min avg daily volume, max spread (Risk Engine)",
    )
    default_params = {"slope_bars": 10, "band_pct": 0.1, "atr_n": 14, "atr_mult": 0.5, "r_multiple": 2.0,
                      "start_after": "09:45", "entry_cutoff": "14:00", "max_trades": 2}
    param_grid = {"slope_bars": [10, 20], "atr_mult": [0.5, 1.0], "r_multiple": [1.5, 2.5]}

    def signals(self, day: pd.DataFrame) -> list[Signal]:
        p = self.params
        typical = (day["high"] + day["low"] + day["close"]) / 3
        vwap = (typical * day["volume"]).cumsum() / day["volume"].cumsum().replace(0, float("nan"))
        atr = self.atr(day, p["atr_n"])
        start, cutoff = time.fromisoformat(p["start_after"]), time.fromisoformat(p["entry_cutoff"])
        n = p["slope_bars"]
        # fraction of the previous n bars that closed above VWAP (excludes the current bar)
        above_frac = (day["close"] > vwap).astype(float).rolling(n).mean().shift(1).to_numpy()
        vw, at = vwap.to_numpy(), atr.to_numpy()
        lows, closes, times = day["low"].to_numpy(), day["close"].to_numpy(), day.index.time
        out: list[Signal] = []
        last = -10**9
        for i in range(n, len(day)):
            t = times[i]
            if t < start:
                continue
            if t >= cutoff or len(out) >= p["max_trades"]:
                break
            v, v_prev = vw[i], vw[i - n]
            if pd.isna(v) or pd.isna(v_prev) or v <= v_prev:
                continue
            lo, c = float(lows[i]), float(closes[i])
            prior_above = above_frac[i] > 0.7
            tagged = lo <= v * (1 + p["band_pct"] / 100)
            if prior_above and tagged and c > v and i - last > n:
                stop = min(lo, float(v) - p["atr_mult"] * float(at[i]))
                risk = c - stop
                if risk > 0:
                    out.append(Signal(i, "BUY", stop, c + p["r_multiple"] * risk, "VWAP pullback in uptrend"))
                    last = i
        return out
