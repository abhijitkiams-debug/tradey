"""Cost-aware intraday backtester, walk-forward validation and stress tests (§FR-02, §FR-04).

Bias controls:
* signals use only bars up to the signal bar's close; fills happen at the *next* bar's open
  (no same-bar fills);
* if a bar touches both stop and target, the stop is assumed to fill first;
* stops that gap through fill at the bar open, not the stop price;
* every position is closed at the square-off time, never carried overnight;
* parameters are selected on in-sample windows only and scored on the following unseen window.

Survivorship bias is a property of the dataset: the caller must supply the instrument
universe as it existed on each date. The dataset id recorded with each run makes that auditable.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import asdict, dataclass, field
from datetime import time
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from .costs import CostModel
from .models import Product, stable_hash
from .strategies.base import Strategy


@dataclass(frozen=True)
class BacktestConfig:
    capital: float = 10_000.0
    risk_per_trade: float = 100.0
    max_position_value: float = 4_000.0
    max_concurrent: int = 3
    slippage_bps: float = 3.0
    cost_multiplier: float = 1.0
    square_off: str = "15:10"

    def content_hash(self) -> str:
        return stable_hash(asdict(self))


@dataclass(frozen=True)
class Trade:
    symbol: str
    day: str
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    qty: int
    entry: float
    exit: float
    gross: float
    costs: float
    exit_reason: str
    regime: str

    @property
    def net(self) -> float:
        return self.gross - self.costs


@dataclass
class Metrics:
    trades: int
    net_pnl: float
    net_return_pct: float
    gross_pnl: float
    total_costs: float
    max_drawdown_pct: float
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    sharpe: float | None  # None when too few days to be meaningful
    exposure_pct: float
    days: int
    by_regime: dict[str, dict[str, float]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BacktestResult:
    strategy: str
    params: dict[str, Any]
    dataset_id: str
    config: BacktestConfig
    cost_model_hash: str
    trades: list[Trade]
    metrics: Metrics

    @property
    def run_id(self) -> str:
        return stable_hash({"strategy": self.strategy, "params": self.params, "dataset": self.dataset_id,
                            "config": self.config.content_hash(), "costs": self.cost_model_hash})[:16]


def dataset_id(data: dict[str, pd.DataFrame]) -> str:
    parts = {}
    for sym in sorted(data):
        df = data[sym]
        parts[sym] = int(pd.util.hash_pandas_object(df[["open", "high", "low", "close", "volume"]], index=True).sum() % (2**63))
    return stable_hash(parts)[:16]


def classify_regime(day: pd.DataFrame) -> str:
    o, c = float(day["open"].iat[0]), float(day["close"].iat[-1])
    rng = float(day["high"].max() - day["low"].min())
    if rng <= 0:
        return "flat"
    efficiency = abs(c - o) / rng
    if efficiency >= 0.5:
        return "trend_up" if c > o else "trend_down"
    return "range"


_PREP_CACHE: dict[int, tuple[dict, dict]] = {}
_SIGNAL_CACHE: dict[tuple, list] = {}


def _prepare(data: dict[str, pd.DataFrame]) -> dict[Any, dict[str, tuple[pd.DataFrame, str]]]:
    """Split once into per-day frames (walk-forward re-runs many parameter sets on the same data)."""
    key = id(data)
    cached = _PREP_CACHE.get(key)
    if cached is not None and cached[0] is data:
        return cached[1]
    by_day: dict[Any, dict[str, tuple[pd.DataFrame, str]]] = {}
    for sym, df in data.items():
        for d, g in df.groupby(df.index.date):
            by_day.setdefault(d, {})[sym] = (g, classify_regime(g))
    if len(_PREP_CACHE) > 8:
        _PREP_CACHE.clear()
    _PREP_CACHE[key] = (data, by_day)
    return by_day


def _signals(strategy: Strategy, sym: str, d: Any, g: pd.DataFrame) -> list:
    # Signals depend only on (strategy, params, that day's bars) — memoise across stress runs and folds.
    k = (strategy.family, tuple(sorted(strategy.params.items())), sym, d, id(g))
    if k not in _SIGNAL_CACHE:
        if len(_SIGNAL_CACHE) > 200_000:
            _SIGNAL_CACHE.clear()
        _SIGNAL_CACHE[k] = strategy.signals(g)
    return _SIGNAL_CACHE[k]


def _simulate_trade(day: pd.DataFrame, sig, cfg: BacktestConfig, sq: time, equity: float) -> tuple | None:
    j = sig.bar_index + 1
    if j >= len(day) or day.index[j].time() >= sq:
        return None
    slip = cfg.slippage_bps / 10_000
    o, h, lo = day["open"].to_numpy(), day["high"].to_numpy(), day["low"].to_numpy()
    entry = o[j] * (1 + slip)
    if entry <= sig.stop:
        return None  # gapped through the stop before we could enter
    per_share = entry - sig.stop
    qty = min(math.floor(cfg.risk_per_trade / per_share), math.floor(min(cfg.max_position_value, equity) / entry))
    if qty <= 0:
        return None
    times = day.index.time
    for k in range(j, len(day)):
        if times[k] >= sq:
            return j, k, qty, entry, o[k] * (1 - slip), "square_off"
        if lo[k] <= sig.stop:
            px = min(o[k], sig.stop) if k > j else sig.stop
            return j, k, qty, entry, px * (1 - slip), "stop"
        if sig.target is not None and h[k] >= sig.target:
            return j, k, qty, entry, sig.target, "target"
    k = len(day) - 1
    return j, k, qty, entry, float(day["close"].iat[k]) * (1 - slip), "end_of_data"


def run_backtest(strategy: Strategy, data: dict[str, pd.DataFrame], cfg: BacktestConfig,
                 costs: CostModel | None = None, days: list | None = None) -> BacktestResult:
    costs = costs or CostModel()
    sq = time.fromisoformat(cfg.square_off)
    prepared = _prepare(data)
    by_day = {d: v for d, v in prepared.items() if days is None or d in days}
    trades: list[Trade] = []
    equity = cfg.capital
    daily: list[float] = []
    bars_in_pos = 0
    total_bars = 0
    for d in sorted(by_day):
        candidates = []
        for sym, (g, regime) in by_day[d].items():
            total_bars += len(g)
            for sig in _signals(strategy, sym, d, g):
                sim = _simulate_trade(g, sig, cfg, sq, equity)
                if sim:
                    candidates.append((g.index[sim[0]], sym, g, sim, regime))
        candidates.sort(key=lambda x: (x[0], x[1]))
        open_until: list[tuple[pd.Timestamp, float, str]] = []
        day_pnl = 0.0
        for ts, sym, g, (j, k, qty, entry, exit_px, reason), regime in candidates:
            open_until = [x for x in open_until if x[0] > ts]
            if len(open_until) >= cfg.max_concurrent or any(x[2] == sym for x in open_until):
                continue
            if sum(x[1] for x in open_until) + qty * entry > equity:
                continue
            c = float(costs.round_trip(Product.MIS, qty, Decimal(str(round(entry, 2))), Decimal(str(round(exit_px, 2))))) * cfg.cost_multiplier
            tr = Trade(sym, str(d), g.index[j], g.index[k], qty, entry, exit_px, (exit_px - entry) * qty, c, reason, regime)
            trades.append(tr)
            open_until.append((g.index[k], qty * entry, sym))
            bars_in_pos += k - j + 1
            day_pnl += tr.net
        equity += day_pnl
        daily.append(day_pnl)
    return BacktestResult(strategy.family, dict(strategy.params), dataset_id(data), cfg, costs.content_hash(), trades,
                          compute_metrics(trades, daily, cfg.capital, bars_in_pos, total_bars))


def compute_metrics(trades: list[Trade], daily: list[float], capital: float, bars_in_pos: int = 0, total_bars: int = 0) -> Metrics:
    nets = np.array([t.net for t in trades]) if trades else np.array([])
    wins, losses = nets[nets > 0], nets[nets <= 0]
    equity = capital + np.cumsum(daily) if daily else np.array([capital])
    peak = np.maximum.accumulate(np.concatenate([[capital], equity]))
    dd = (np.concatenate([[capital], equity]) - peak) / peak
    sharpe = None
    if len(daily) >= 20:
        r = np.array(daily) / capital
        if r.std(ddof=1) > 0:
            sharpe = float(r.mean() / r.std(ddof=1) * math.sqrt(252))
    by_regime: dict[str, dict[str, float]] = {}
    for reg in sorted({t.regime for t in trades}):
        rn = np.array([t.net for t in trades if t.regime == reg])
        by_regime[reg] = {"trades": int(len(rn)), "net_pnl": float(rn.sum()), "win_rate": float((rn > 0).mean())}
    gross_loss = -losses.sum()
    return Metrics(
        trades=len(trades),
        net_pnl=float(nets.sum()) if len(nets) else 0.0,
        net_return_pct=float(nets.sum() / capital * 100) if len(nets) else 0.0,
        gross_pnl=float(sum(t.gross for t in trades)),
        total_costs=float(sum(t.costs for t in trades)),
        max_drawdown_pct=float(-dd.min() * 100),
        win_rate=float(len(wins) / len(nets)) if len(nets) else 0.0,
        avg_win=float(wins.mean()) if len(wins) else 0.0,
        avg_loss=float(losses.mean()) if len(losses) else 0.0,
        profit_factor=float(wins.sum() / gross_loss) if gross_loss > 0 else (float("inf") if len(wins) else 0.0),
        sharpe=sharpe,
        exposure_pct=float(bars_in_pos / total_bars * 100) if total_bars else 0.0,
        days=len(daily),
        by_regime=by_regime,
    )


# ---- walk-forward ------------------------------------------------------------------------
@dataclass
class Fold:
    train_days: tuple
    test_days: tuple
    chosen_params: dict[str, Any]
    in_sample: Metrics
    out_of_sample: Metrics


@dataclass
class WalkForwardResult:
    family: str
    folds: list[Fold]
    oos_trades: list[Trade]
    oos_metrics: Metrics
    final_params: dict[str, Any]
    stress: dict[str, Metrics]
    top_trades_removed: Metrics
    dataset_id: str
    cost_model_hash: str
    config: BacktestConfig

    def summary(self) -> dict[str, Any]:
        m = self.oos_metrics
        return {
            "family": self.family,
            "folds": len(self.folds),
            "oos_trades": m.trades,
            "oos_net_pnl": round(m.net_pnl, 2),
            "oos_profit_factor": round(m.profit_factor, 3) if math.isfinite(m.profit_factor) else None,
            "oos_max_dd_pct": round(m.max_drawdown_pct, 2),
            "oos_win_rate": round(m.win_rate, 3),
            "oos_positive_folds": sum(f.out_of_sample.net_pnl > 0 for f in self.folds),
            "final_params": self.final_params,
            "stress_net_pnl": {k: round(v.net_pnl, 2) for k, v in self.stress.items()},
            "net_without_top_5pct_trades": round(self.top_trades_removed.net_pnl, 2),
            "dataset_id": self.dataset_id,
            "cost_model_hash": self.cost_model_hash[:16],
        }


def _grid(cls: type[Strategy]):
    keys = list(cls.param_grid)
    for combo in itertools.product(*(cls.param_grid[k] for k in keys)):
        yield dict(zip(keys, combo, strict=True))


def walk_forward(cls: type[Strategy], data: dict[str, pd.DataFrame], cfg: BacktestConfig, costs: CostModel | None = None,
                 train_days: int = 40, test_days: int = 10, min_train_trades: int = 10) -> WalkForwardResult:
    costs = costs or CostModel()
    all_days = sorted({d for df in data.values() for d in df.index.date})
    if len(all_days) < train_days + test_days:
        raise ValueError(f"need at least {train_days + test_days} days, have {len(all_days)}")
    folds: list[Fold] = []
    start = 0
    while start + train_days + test_days <= len(all_days):
        tr = tuple(all_days[start:start + train_days])
        te = tuple(all_days[start + train_days:start + train_days + test_days])
        best, best_score, best_m = None, -float("inf"), None
        for params in _grid(cls):
            r = run_backtest(cls(**params), data, cfg, costs, days=set(tr))
            m = r.metrics
            if m.trades < min_train_trades:
                continue
            score = m.net_pnl - 0.5 * m.max_drawdown_pct / 100 * cfg.capital  # penalise drawdown
            if score > best_score:
                best, best_score, best_m = params, score, m
        if best is None:  # nothing traded enough in-sample: fall back to defaults, recorded as such
            best = {}
            best_m = run_backtest(cls(), data, cfg, costs, days=set(tr)).metrics
        oos = run_backtest(cls(**best), data, cfg, costs, days=set(te))
        folds.append(Fold(tr, te, {**cls.default_params, **best}, best_m, oos.metrics))
        start += test_days
    # Concatenate OOS trades fold by fold (each fold's params were chosen without seeing it)
    oos_trades: list[Trade] = []
    daily: list[float] = []
    for f in folds:
        r = run_backtest(cls(**{k: f.chosen_params[k] for k in cls.default_params}), data, cfg, costs, days=set(f.test_days))
        oos_trades += r.trades
        per_day = {str(d): 0.0 for d in f.test_days}
        for t in r.trades:
            per_day[t.day] += t.net
        daily += list(per_day.values())
    oos_m = compute_metrics(oos_trades, daily, cfg.capital)
    final = folds[-1].chosen_params
    stress = {}
    for name, slip_mult, cost_mult in [("base", 1, 1.0), ("slippage_x2", 2, 1.0), ("slippage_x4", 4, 1.0), ("costs_x1.5", 1, 1.5)]:
        scfg = BacktestConfig(**{**asdict(cfg), "slippage_bps": cfg.slippage_bps * slip_mult, "cost_multiplier": cost_mult})
        stress[name] = run_backtest(cls(**final), data, scfg, costs).metrics
    # Robustness: drop the best 5% of OOS trades — a strategy living on a few outliers is fragile
    k = max(1, int(len(oos_trades) * 0.05)) if oos_trades else 0
    trimmed = sorted(oos_trades, key=lambda t: t.net)[:-k] if k else []
    trimmed_m = compute_metrics(trimmed, [sum(t.net for t in trimmed)], cfg.capital)
    return WalkForwardResult(cls.family, folds, oos_trades, oos_m, final, stress, trimmed_m,
                             dataset_id(data), costs.content_hash(), cfg)


@dataclass(frozen=True)
class EligibilityCriteria:
    """Placeholder thresholds. PRD §18 Q4: the operator must approve the real ones."""

    min_oos_trades: int = 30
    min_oos_profit_factor: float = 1.2
    min_oos_net_pnl: float = 0.0
    max_oos_drawdown_pct: float = 10.0
    min_positive_fold_ratio: float = 0.6
    must_survive_stress: tuple[str, ...] = ("slippage_x2", "costs_x1.5")
    must_survive_top_trade_removal: bool = True

    def evaluate(self, wf: WalkForwardResult) -> tuple[bool, list[str]]:
        m = wf.oos_metrics
        fails: list[str] = []
        if m.trades < self.min_oos_trades:
            fails.append(f"OOS trades {m.trades} < {self.min_oos_trades}")
        if m.profit_factor < self.min_oos_profit_factor:
            fails.append(f"OOS profit factor {m.profit_factor:.2f} < {self.min_oos_profit_factor}")
        if m.net_pnl <= self.min_oos_net_pnl:
            fails.append(f"OOS net P&L {m.net_pnl:.2f} <= {self.min_oos_net_pnl}")
        if m.max_drawdown_pct > self.max_oos_drawdown_pct:
            fails.append(f"OOS max drawdown {m.max_drawdown_pct:.1f}% > {self.max_oos_drawdown_pct}%")
        pos = sum(f.out_of_sample.net_pnl > 0 for f in wf.folds) / max(len(wf.folds), 1)
        if pos < self.min_positive_fold_ratio:
            fails.append(f"positive OOS folds {pos:.0%} < {self.min_positive_fold_ratio:.0%}")
        for s in self.must_survive_stress:
            if wf.stress[s].net_pnl <= 0:
                fails.append(f"unprofitable under stress '{s}'")
        if self.must_survive_top_trade_removal and wf.top_trades_removed.net_pnl <= 0:
            fails.append("unprofitable once the best 5% of trades are removed")
        return not fails, fails
