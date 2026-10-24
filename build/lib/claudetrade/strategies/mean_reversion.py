from __future__ import annotations

from datetime import time

import pandas as pd

from .base import REGISTRY, Signal, Strategy, StrategySpec


@REGISTRY.register
class MeanReversion(Strategy):
    family = "MEAN_REVERSION"
    spec = StrategySpec(
        family="MEAN_REVERSION",
        description="Buy a statistically stretched intraday dip, target a return to the rolling mean.",
        entry="z-score of close vs rolling mean below -z_entry, and the bar closes up (first sign of turn).",
        exit="Target = rolling mean at signal time; stop = entry - atr_mult * ATR; time exit before square-off.",
        stop_logic="entry - atr_mult * ATR",
        sizing="floor(risk budget / (entry - stop)), capped by available capital",
        max_holding="same session",
        session_rules="entries between start_after and entry_cutoff; max_trades per day",
        regime_assumption="range-bound / mean-reverting sessions; dangerous in strong trend-down days",
        liquidity_filter="min price, min avg daily volume, max spread (Risk Engine)",
    )
    default_params = {"window": 20, "z_entry": 2.0, "atr_n": 14, "atr_mult": 1.5,
                      "start_after": "09:45", "entry_cutoff": "14:15", "max_trades": 2}
    param_grid = {"window": [20, 40], "z_entry": [1.5, 2.0, 2.5], "atr_mult": [1.0, 1.5]}

    def signals(self, day: pd.DataFrame) -> list[Signal]:
        p = self.params
        c = day["close"]
        mean = c.rolling(p["window"]).mean()
        sd = c.rolling(p["window"]).std()
        z = (c - mean) / sd
        atr = self.atr(day, p["atr_n"])
        start, cutoff = time.fromisoformat(p["start_after"]), time.fromisoformat(p["entry_cutoff"])
        zz, cc, oo = z.to_numpy(), c.to_numpy(), day["open"].to_numpy()
        mm, at, times = mean.to_numpy(), atr.to_numpy(), day.index.time
        out: list[Signal] = []
        last = -10**9
        for i in range(p["window"], len(day)):
            t = times[i]
            if t < start:
                continue
            if t >= cutoff or len(out) >= p["max_trades"]:
                break
            if pd.isna(zz[i]) or zz[i] > -p["z_entry"]:
                continue
            if cc[i] <= oo[i] or i - last <= p["window"]:
                continue
            entry = float(cc[i])
            stop = entry - p["atr_mult"] * float(at[i])
            target = float(mm[i])
            if stop < entry < target:
                out.append(Signal(i, "BUY", stop, target, f"z={zz[i]:.2f}"))
                last = i
        return out
