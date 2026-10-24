"""Strategy contract (§FR-02).

A strategy is a pure function of one session's bars (only data up to and including the
signal bar may be used) plus an immutable parameter dict. It emits entry signals; the
backtester / live engine decides fills, sizing and exits. Each strategy declares the
assumptions it relies on so Day-T+1 validation can check they still hold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import Any, ClassVar

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Signal:
    bar_index: int  # signal is known at the CLOSE of this bar; earliest fill is the next bar's open
    side: str  # "BUY" only in V1 (no shorting)
    stop: float
    target: float | None
    reason: str = ""


@dataclass(frozen=True)
class StrategySpec:
    family: str
    description: str
    entry: str
    exit: str
    stop_logic: str
    sizing: str
    max_holding: str
    session_rules: str
    regime_assumption: str
    liquidity_filter: str
    eligible_instruments: str = "NSE cash equities passing min price, avg volume and spread filters"


class Strategy:
    family: ClassVar[str]
    spec: ClassVar[StrategySpec]
    default_params: ClassVar[dict[str, Any]]
    param_grid: ClassVar[dict[str, list[Any]]]

    def __init__(self, **params: Any):
        unknown = set(params) - set(self.default_params)
        if unknown:
            raise ValueError(f"unknown params for {self.family}: {sorted(unknown)}")
        self.params: dict[str, Any] = {**self.default_params, **params}

    def signals(self, day: pd.DataFrame) -> list[Signal]:
        raise NotImplementedError

    # helpers -------------------------------------------------------------------------
    @staticmethod
    def entry_window(day: pd.DataFrame, start: time, end: time) -> np.ndarray:
        t = day.index.time
        return np.array([(start <= x < end) for x in t])

    @staticmethod
    def atr(day: pd.DataFrame, n: int) -> pd.Series:
        prev = day["close"].shift(1)
        tr = pd.concat([day["high"] - day["low"], (day["high"] - prev).abs(), (day["low"] - prev).abs()], axis=1).max(axis=1)
        return tr.rolling(n, min_periods=1).mean()


@dataclass
class Registry:
    strategies: dict[str, type[Strategy]] = field(default_factory=dict)

    def register(self, cls: type[Strategy]) -> type[Strategy]:
        self.strategies[cls.family] = cls
        return cls


REGISTRY = Registry()
