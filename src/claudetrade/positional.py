"""Positional (multi-day, delivery) strategies on daily bars, plus a portfolio backtester.

Two opposite methods with the same discipline (cut losers fast, size by risk, follow rules):

* **BNF "kairi" (deviation) mean reversion**, adapted from Takashi Kotegawa's reported method:
  in a market-wide panic, buy liquid stocks that have fallen far below their 25-day average and
  show a reversal candle; sell into the bounce back toward the average.
* **Momentum breakout** (Minervini-style "trend template" + volatility contraction), the style
  J Law (Law Wai-Sum) is *reported* to follow. His exact rules are unpublished; this is an
  adaptation, not his system.
* **Combined**: one book that runs momentum in an uptrend, BNF in a panic, and stays in cash
  in between ("adapt to the market").

Everything is mechanical and testable. Signals are computed on a day's close and filled at the
next day's open; stops gap through (filled at the open when the open is already past the stop).
Costs use the delivery (CNC) ``CostModel``, the same one the live desk uses for estimates.

Known limits (also shown in the Lab page): data starts in 2020 (Groww), the index lists are
*today's* constituents (survivorship bias), circuit limits and news-driven ("justified") falls
are not modelled; the human review at approval is the quality filter.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .costs import CostModel
from .models import Product, Side

DATA_DIR = Path(__file__).parent / "data"
INDEX_SYMBOL = "NIFTY"  # Groww: NSE-NIFTY (Nifty 50)
COLUMNS = ["open", "high", "low", "close", "volume"]


# ---- universe -----------------------------------------------------------------------------------
def load_universe(name: str = "nifty200") -> dict[str, str]:
    """{symbol: industry} from the bundled NSE index list (snapshot; see data/README)."""
    path = DATA_DIR / f"{name}.csv"
    with path.open(newline="", encoding="utf-8") as f:
        return {r["Symbol"].strip(): r["Industry"].strip() for r in csv.DictReader(f) if r.get("Symbol")}


# ---- daily data ---------------------------------------------------------------------------------
def _clean_daily(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], name="date"))
    out = df.copy()
    idx = pd.DatetimeIndex(out.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    out.index = idx.normalize().rename("date")
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out = out[COLUMNS].astype(float)
    return out[(out["close"] > 0) & (out["high"] >= out["low"])]


def fetch_daily(feed: Any, symbol: str, start: date, end: date, cache_dir: Path | None = None) -> pd.DataFrame:
    """Daily candles from Groww in 180-day chunks (Groww's per-request limit), cached as CSV and
    topped up incrementally on later calls."""
    cache = cache_dir / f"{symbol}.csv" if cache_dir else None
    have = pd.DataFrame()
    if cache and cache.exists():
        have = pd.read_csv(cache, parse_dates=["date"]).set_index("date")
        have = _clean_daily(have)
    parts = [have] if len(have) else []
    fetch_from = start
    if len(have):
        first, last = have.index.min().date(), have.index.max().date()
        if first <= start + timedelta(days=7):
            fetch_from = last  # refetch the last day: it may have been a partial candle
        if last >= end and first <= start + timedelta(days=7):
            return have[(have.index.date >= start) & (have.index.date <= end)]
    cur = fetch_from
    from .models import IST

    while cur <= end:
        nxt = min(cur + timedelta(days=179), end)
        df = feed.candles(symbol, datetime.combine(cur, time(9, 15), IST), datetime.combine(nxt, time(15, 30), IST),
                          interval="1day")
        if len(df):
            parts.append(_clean_daily(df))
        cur = nxt + timedelta(days=1)
    out = _clean_daily(pd.concat(parts)) if parts else _clean_daily(pd.DataFrame())
    if cache is not None and len(out):
        cache.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(cache)
    return out[(out.index.date >= start) & (out.index.date <= end)]


def load_dataset(feed: Any, symbols: Iterable[str], start: date, end: date, cache_dir: Path | None = None,
                 progress: Callable[[str], None] | None = None) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, dict[str, str]]:
    """(stock data, Nifty index data, {symbol: error}) — a symbol that fails is skipped, not fatal."""
    say = progress or (lambda _m: None)
    errors: dict[str, str] = {}
    index = fetch_daily(feed, INDEX_SYMBOL, start, end, cache_dir)
    if index.empty:
        raise RuntimeError("no Nifty 50 index data from Groww (NSE-NIFTY)")
    data: dict[str, pd.DataFrame] = {}
    syms = list(symbols)
    for i, s in enumerate(syms, 1):
        try:
            df = fetch_daily(feed, s, start, end, cache_dir)
            if len(df) >= 60:
                data[s] = df
            else:
                errors[s] = "not enough history"
        except Exception as e:  # noqa: BLE001 - one bad symbol must not stop the run
            errors[s] = f"{type(e).__name__}: {str(getattr(e, 'msg', '') or e)[:80]}"
        if i % 10 == 0 or i == len(syms):
            say(f"downloaded {i}/{len(syms)} stocks")
    return data, index, errors


def synthetic_dataset(symbols: Iterable[str], start: date, end: date, seed: int = 7) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Made-up daily prices with trends and one market crash, for tests and offline demos."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start, end, name="date")
    n = len(days)
    mkt = rng.normal(0.0005, 0.009, n)
    crash = int(n * 0.35)
    mkt[crash:crash + 15] -= 0.025  # a sharp, market-wide panic
    mkt[crash + 15:crash + 35] += 0.012  # ...and the rebound
    index = _ohlc_from_returns(days, mkt, 10000.0, rng, vol_base=1e8)
    data = {}
    for s in symbols:
        beta = rng.uniform(0.7, 1.4)
        drift = rng.normal(0.0004, 0.0006)
        ret = beta * mkt + drift + rng.normal(0, 0.014, n)
        data[s] = _ohlc_from_returns(days, ret, float(rng.uniform(80, 2500)), rng, vol_base=float(rng.uniform(3e5, 5e6)))
    return data, index


def _ohlc_from_returns(days: pd.DatetimeIndex, ret: np.ndarray, p0: float, rng: np.random.Generator, vol_base: float) -> pd.DataFrame:
    close = p0 * np.exp(np.cumsum(ret))
    prev = np.concatenate([[p0], close[:-1]])
    opn = prev * np.exp(rng.normal(0, 0.004, len(close)))
    hi = np.maximum(opn, close) * (1 + np.abs(rng.normal(0, 0.006, len(close))))
    lo = np.minimum(opn, close) * (1 - np.abs(rng.normal(0, 0.006, len(close))))
    vol = vol_base * np.exp(rng.normal(0, 0.35, len(close))) * (1 + 3 * np.abs(ret) / 0.02)
    return pd.DataFrame({"open": opn, "high": hi, "low": lo, "close": close, "volume": vol.round()}, index=days)


# ---- indicators ---------------------------------------------------------------------------------
def indicators(df: pd.DataFrame, index_close: pd.Series | None = None) -> pd.DataFrame:
    d = df.copy()
    c = d["close"]
    for n in (20, 25, 50, 150, 200):
        d[f"sma{n}"] = c.rolling(n, min_periods=n).mean()
    d["dev25"] = (c - d["sma25"]) / d["sma25"] * 100  # BNF's kairi rate, %
    d["hi52"] = d["high"].rolling(250, min_periods=120).max()
    d["lo52"] = d["low"].rolling(250, min_periods=120).min()
    d["vol50"] = d["volume"].rolling(50, min_periods=20).mean().shift(1)  # excludes today
    d["vol10"] = d["volume"].rolling(10, min_periods=5).mean().shift(1)
    d["turnover50"] = (c * d["volume"]).rolling(50, min_periods=20).mean()
    d["ret126"] = c.pct_change(126)
    d["sma200_up"] = d["sma200"] > d["sma200"].shift(21)
    rng_ = (d["high"] - d["low"]).replace(0, np.nan)
    d["close_pos"] = ((c - d["low"]) / rng_).fillna(0.5)  # 0 = closed at low, 1 = at high
    d["ret1"] = c.pct_change()
    d["worst_day5"] = d["ret1"].rolling(5, min_periods=1).min()
    if index_close is not None:
        d["idx_ret126"] = index_close.reindex(d.index).ffill().pct_change(126)
    return d


# ---- strategy rules -----------------------------------------------------------------------------
@dataclass(frozen=True)
class BNFParams:
    entry_deviation_pct: float = -15.0  # stock close at least this far below its 25-DMA
    market_deviation_pct: float = -5.0  # panic regime: Nifty at least this far below its 25-DMA
    breadth_panic_pct: float = 60.0  # ...or this % of the universe more than 10% below their 25-DMA
    require_reversal: bool = True  # signal day closes in the upper half of its range
    max_single_day_drop_pct: float = 18.0  # skip one-day collapses (often news/results driven)
    stop_pct: float = 7.0  # stop: below the signal-day low, or this far below entry, whichever is hit first
    time_stop_days: int = 7
    min_turnover_cr: float = 10.0  # average daily turnover, ₹ crore


@dataclass(frozen=True)
class MomentumParams:
    min_above_low_pct: float = 30.0  # at least this far above the 52-week low
    max_below_high_pct: float = 25.0  # within this of the 52-week high
    base_days: int = 60  # the base (consolidation) the breakout comes out of
    max_base_depth_pct: float = 35.0
    max_last_contraction_pct: float = 10.0  # final pullback must be tight
    require_contraction: bool = True  # each of 3 pullbacks shallower than the last
    breakout_volume_x: float = 1.5  # breakout day volume vs 50-day average
    max_chase_pct: float = 5.0  # skip if the next open is this far above the pivot
    stop_pct: float = 8.0
    first_target_pct: float = 20.0  # sell half here
    trail_sma: int = 50  # trail the rest: exit on a close below this average
    market_breadth_pct: float = 50.0  # % of universe above their 50-DMA
    min_turnover_cr: float = 20.0


@dataclass(frozen=True)
class BacktestParams:
    strategy: str = "combined"  # bnf | momentum | combined
    capital: float = 10000.0
    risk_pct: float = 1.0  # % of equity risked per trade (momentum: half this after losses)
    max_positions: int = 5
    max_position_value: float = 0.0  # 0 = equity / max_positions
    slippage_bps: float = 10.0
    bnf: BNFParams = field(default_factory=BNFParams)
    momentum: MomentumParams = field(default_factory=MomentumParams)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> BacktestParams:
        raw = dict(raw or {})
        bnf = replace(BNFParams(), **{k: v for k, v in (raw.pop("bnf", None) or {}).items() if k in BNFParams.__dataclass_fields__})
        mom = replace(MomentumParams(), **{k: v for k, v in (raw.pop("momentum", None) or {}).items()
                                           if k in MomentumParams.__dataclass_fields__})
        top = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        p = replace(cls(), bnf=bnf, momentum=mom, **top)
        if p.strategy not in ("bnf", "momentum", "combined"):
            raise ValueError("strategy must be bnf, momentum or combined")
        if not (0 < p.risk_pct <= 5 and 1 <= p.max_positions <= 20 and p.capital > 0):
            raise ValueError("risk_pct must be 0-5, max_positions 1-20, capital > 0")
        return p


def market_regime(index: pd.DataFrame, panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Per day: Nifty deviation, breadth, and the regime (UPTREND / PANIC / CHOPPY)."""
    ix = indicators(index)
    closes = pd.DataFrame({s: d["close"] for s, d in panel.items()})
    sma50 = pd.DataFrame({s: d["sma50"] for s, d in panel.items()})
    dev25 = pd.DataFrame({s: d["dev25"] for s, d in panel.items()})
    valid = sma50.notna()
    above50 = ((closes > sma50) & valid).sum(axis=1) / valid.sum(axis=1).replace(0, np.nan) * 100
    v25 = dev25.notna()
    deep = ((dev25 < -10) & v25).sum(axis=1) / v25.sum(axis=1).replace(0, np.nan) * 100
    out = pd.DataFrame({"nifty": ix["close"], "nifty_dev25": ix["dev25"], "nifty_above_sma50": ix["close"] > ix["sma50"],
                        "breadth_above50": above50.reindex(ix.index), "breadth_deep_below25": deep.reindex(ix.index)})
    return out


def regime_label(row: pd.Series, bnf: BNFParams, mom: MomentumParams) -> str:
    if (row["nifty_dev25"] <= bnf.market_deviation_pct) or (row["breadth_deep_below25"] >= bnf.breadth_panic_pct):
        return "PANIC"
    if row["nifty_above_sma50"] and row["breadth_above50"] >= mom.market_breadth_pct:
        return "UPTREND"
    return "CHOPPY"


def bnf_signal(d: pd.DataFrame, i: int, p: BNFParams) -> dict[str, Any] | None:
    r = d.iloc[i]
    if not (r["dev25"] <= p.entry_deviation_pct):
        return None
    if r["turnover50"] < p.min_turnover_cr * 1e7:
        return None
    if p.require_reversal and r["close_pos"] < 0.5:
        return None
    if r["worst_day5"] * 100 <= -p.max_single_day_drop_pct:
        return None
    return {"family": "bnf", "signal_low": float(r["low"]), "score": -float(r["dev25"]),
            "why": f"{r['dev25']:.1f}% below its 25-day average, closed in the top {100 - r['close_pos'] * 100:.0f}% of the day's range"}


def _contractions(d: pd.DataFrame, i: int, base_days: int) -> list[float]:
    w = d.iloc[i - base_days:i]
    out = []
    for chunk in np.array_split(np.arange(len(w)), 3):
        seg = w.iloc[chunk]
        hi = seg["high"].max()
        out.append((hi - seg["low"].min()) / hi * 100 if hi > 0 else 0.0)
    return out


def momentum_signal(d: pd.DataFrame, i: int, p: MomentumParams) -> dict[str, Any] | None:
    if i < max(p.base_days, 200):
        return None
    r = d.iloc[i]
    if not (r["close"] > r["sma50"] > r["sma150"] > r["sma200"] and bool(r["sma200_up"])):
        return None
    if r["close"] < r["lo52"] * (1 + p.min_above_low_pct / 100) or r["close"] < r["hi52"] * (1 - p.max_below_high_pct / 100):
        return None
    if "idx_ret126" in d and not (r["ret126"] > r["idx_ret126"]):
        return None
    if r["turnover50"] < p.min_turnover_cr * 1e7:
        return None
    base = d.iloc[i - p.base_days:i]
    pivot = float(base["high"].max())
    depth = (pivot - float(base["low"].min())) / pivot * 100
    if depth > p.max_base_depth_pct or not (r["close"] > pivot):
        return None
    if not (r["volume"] >= p.breakout_volume_x * r["vol50"]):
        return None
    cons = _contractions(d, i, p.base_days)
    if p.require_contraction and not (cons[0] > cons[1] > cons[2] and cons[2] <= p.max_last_contraction_pct):
        return None
    if r["vol10"] >= r["vol50"] * 1.2:  # volume should dry up in the base, not swell
        return None
    return {"family": "momentum", "pivot": pivot, "score": float(r["ret126"] or 0),
            "why": (f"breakout above a {p.base_days}-day base at ₹{pivot:,.2f} on {r['volume'] / r['vol50']:.1f}× volume; "
                    f"pullbacks {cons[0]:.0f}% → {cons[1]:.0f}% → {cons[2]:.0f}%")}


def prefilter(d: pd.DataFrame, bnf: BNFParams, mom: MomentumParams) -> tuple[np.ndarray, np.ndarray]:
    """Cheap vectorised pre-checks, so the full per-day rules run only on candidate days."""
    b = (d["dev25"] <= bnf.entry_deviation_pct) & (d["turnover50"] >= bnf.min_turnover_cr * 1e7)
    pivot = d["high"].rolling(mom.base_days, min_periods=mom.base_days).max().shift(1)
    m = ((d["close"] > d["sma50"]) & (d["sma50"] > d["sma150"]) & (d["sma150"] > d["sma200"]) & (d["close"] > pivot)
         & (d["volume"] >= mom.breakout_volume_x * d["vol50"]) & (d["turnover50"] >= mom.min_turnover_cr * 1e7))
    return b.fillna(False).to_numpy(bool), m.fillna(False).to_numpy(bool)


# ---- backtester ---------------------------------------------------------------------------------
@dataclass
class _Pos:
    symbol: str
    family: str
    entry_date: date
    entry: float
    qty: int
    stop: float
    entry_cost: float
    signal_low: float = 0.0
    half_taken: bool = False
    days: int = 0
    realised: float = 0.0  # net P&L of partial exits
    orig_qty: int = 0


def _cost(costs: CostModel, side: Side, qty: int, price: float) -> float:
    return float(costs.charges(side, Product.CNC, qty, Decimal(str(round(price, 2)))))


def backtest(data: dict[str, pd.DataFrame], index: pd.DataFrame, params: BacktestParams | None = None,
             costs: CostModel | None = None, sectors: dict[str, str] | None = None,
             start: date | None = None, bnf_symbols: set[str] | None = None) -> dict[str, Any]:
    """Daily portfolio simulation. ``bnf_symbols`` limits BNF entries to a sub-universe
    (Nifty 200) when the data covers more (Nifty 500, for momentum)."""
    p = params or BacktestParams()
    costs = costs or CostModel()
    ix_close = _clean_daily(index)["close"]
    panel = {s: indicators(_clean_daily(df), ix_close) for s, df in data.items() if len(df) >= 60}
    reg = market_regime(_clean_daily(index), panel)
    days = [d for d in reg.index if start is None or d.date() >= start]
    if len(days) < 30:
        raise ValueError("not enough days to backtest")
    pos_by_day = {s: {d: k for k, d in enumerate(df.index)} for s, df in panel.items()}
    pre = {s: prefilter(df, p.bnf, p.momentum) for s, df in panel.items()}
    slip = p.slippage_bps / 10000

    cash, positions, trades, curve = p.capital, [], [], []
    pending: list[dict[str, Any]] = []  # signals from yesterday, to fill at today's open
    recent: list[bool] = []  # last closed trades won? (momentum sizing)
    regimes: dict[str, int] = {"UPTREND": 0, "PANIC": 0, "CHOPPY": 0}
    total_costs = 0.0

    def close_part(ps: _Pos, qty: int, px: float, day, reason: str) -> None:
        nonlocal cash, total_costs
        fill = px * (1 - slip)
        c = _cost(costs, Side.SELL, qty, fill)
        total_costs += c
        cash += qty * fill - c
        alloc_entry_cost = ps.entry_cost * qty / ps.orig_qty
        pnl = qty * (fill - ps.entry) - c - alloc_entry_cost
        ps.realised += pnl
        ps.qty -= qty
        if ps.qty == 0:
            win = ps.realised > 0
            if ps.family == "momentum":
                recent.append(win)
            trades.append({"symbol": ps.symbol, "family": ps.family, "sector": (sectors or {}).get(ps.symbol, ""),
                           "entry_date": ps.entry_date.isoformat(), "exit_date": day.date().isoformat(),
                           "entry": round(ps.entry, 2), "exit": round(fill, 2), "qty": ps.orig_qty,
                           "days": ps.days, "pnl": round(ps.realised, 2),
                           "pnl_pct": round(ps.realised / (ps.entry * ps.orig_qty) * 100, 2), "reason": reason})

    for day in days:
        # 1) fill yesterday's signals at today's open
        equity_open = curve[-1][1] if curve else p.capital  # yesterday's marked equity
        for sig in sorted(pending, key=lambda s: -s["score"]):
            if len(positions) >= p.max_positions:
                break
            s = sig["symbol"]
            if any(ps.symbol == s for ps in positions) or day not in pos_by_day[s]:
                continue
            row = panel[s].iloc[pos_by_day[s][day]]
            px = float(row["open"]) * (1 + slip)
            if sig["family"] == "momentum":
                if px > sig["pivot"] * (1 + p.momentum.max_chase_pct / 100):
                    continue
                stop = px * (1 - p.momentum.stop_pct / 100)
                risk_pct = p.risk_pct if (len(recent) < 2 or recent[-1] or recent[-2]) else p.risk_pct / 2
            else:
                stop = max(sig["signal_low"] * 0.999, px * (1 - p.bnf.stop_pct / 100))
                if stop >= px:  # opened below the signal low: setup already failed
                    continue
                risk_pct = p.risk_pct
            risk_rs = equity_open * risk_pct / 100
            cap = p.max_position_value or equity_open / p.max_positions
            qty = int(min(risk_rs / (px - stop), cap / px, cash / (px * 1.003)))
            if qty < 1:
                continue
            c = _cost(costs, Side.BUY, qty, px)
            total_costs += c
            cash -= qty * px + c
            positions.append(_Pos(s, sig["family"], day.date(), px, qty, stop, c, sig.get("signal_low", 0.0), orig_qty=qty))
        pending = []

        # 2) manage open positions on today's bar
        for ps in list(positions):
            k = pos_by_day[ps.symbol].get(day)
            if k is None:
                continue
            r = panel[ps.symbol].iloc[k]
            o, h, lo, c_ = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
            ps.days += 1 if ps.entry_date != day.date() else 0
            exit_reason = None
            if lo <= ps.stop:  # stop first: the conservative assumption when both are touched
                close_part(ps, ps.qty, min(o, ps.stop), day, "stop")
            elif ps.family == "bnf":
                sma = float(r["sma25"])
                half_tgt = ps.entry + 0.5 * (sma - ps.entry)
                if not ps.half_taken and sma > ps.entry and h >= half_tgt and ps.qty >= 2:
                    close_part(ps, ps.qty // 2, max(o, half_tgt), day, "half at 50% back to 25-DMA")
                    ps.half_taken = True
                if ps.qty and sma > ps.entry and h >= sma:
                    close_part(ps, ps.qty, max(o, sma), day, "back at 25-DMA")
                elif ps.qty and ps.days >= p.bnf.time_stop_days:
                    exit_reason = "time stop"
            else:
                tgt = ps.entry * (1 + p.momentum.first_target_pct / 100)
                if not ps.half_taken and h >= tgt and ps.qty >= 2:
                    close_part(ps, ps.qty // 2, max(o, tgt), day, f"half at +{p.momentum.first_target_pct:.0f}%")
                    ps.half_taken = True
                    ps.stop = max(ps.stop, ps.entry)  # the rest can no longer lose
                trail = r.get(f"sma{p.momentum.trail_sma}")
                if ps.qty and trail is not None and not math.isnan(trail) and c_ < float(trail):
                    exit_reason = f"close below {p.momentum.trail_sma}-DMA"
            if ps.qty and exit_reason:
                close_part(ps, ps.qty, c_, day, exit_reason)
            if ps.qty == 0:
                positions.remove(ps)

        # 3) today's regime and new signals (filled tomorrow)
        rr = reg.loc[day]
        label = regime_label(rr, p.bnf, p.momentum) if not rr.isna().any() else "CHOPPY"
        regimes[label] += 1
        want_bnf = label == "PANIC" and p.strategy in ("bnf", "combined")
        want_mom = label == "UPTREND" and p.strategy in ("momentum", "combined")
        if (want_bnf or want_mom) and len(positions) < p.max_positions:
            for s, d in panel.items():
                k = pos_by_day[s].get(day)
                if k is None or k < 30:
                    continue
                pb, pm = pre[s]
                allow_bnf = want_bnf and pb[k] and (bnf_symbols is None or s in bnf_symbols)
                sig = ((bnf_signal(d, k, p.bnf) if allow_bnf else None)
                       or (momentum_signal(d, k, p.momentum) if want_mom and pm[k] else None))
                if sig:
                    pending.append({**sig, "symbol": s})

        mtm = sum(ps.qty * float(panel[ps.symbol]["close"].iloc[pos_by_day[ps.symbol][day]])
                  for ps in positions if day in pos_by_day[ps.symbol])
        curve.append((day, cash + mtm, label))

    # close anything still open at the last close (marked, not a real exit)
    last = days[-1]
    for ps in list(positions):
        k = pos_by_day[ps.symbol].get(last)
        if k is not None:
            close_part(ps, ps.qty, float(panel[ps.symbol]["close"].iloc[k]), last, "still open at end (marked)")
    eq = pd.Series([e for _, e, _ in curve], index=[d for d, _, _ in curve])
    bench = reg["nifty"].reindex(eq.index).ffill()
    bench_eq = bench / bench.iloc[0] * p.capital
    return {
        "params": p.to_dict(), "cost_model": costs.version,
        "period": {"start": eq.index[0].date().isoformat(), "end": eq.index[-1].date().isoformat(), "days": len(eq)},
        "universe_size": len(panel),
        "metrics": metrics(eq, trades, total_costs, p.capital),
        "by_family": {f: trade_stats([t for t in trades if t["family"] == f]) for f in ("bnf", "momentum")},
        "benchmark": {"name": "Nifty 50 buy & hold", **_curve_stats(bench_eq, p.capital)},
        "regime_days": regimes,
        "yearly": _yearly(eq, bench_eq),
        "equity_curve": _downsample([(d.date().isoformat(), round(e, 2), round(float(b), 2))
                                     for (d, e, _), b in zip(curve, bench_eq, strict=False)]),
        "trades": trades[-500:],
        "limits": LIMITS,
    }


LIMITS = [
    "Groww price history starts in 2020, so this covers about 6 years (one big crash: March 2020).",
    "Stock lists are today's Nifty 200/500 members: companies that dropped out are missing, which flatters results.",
    "Upper/lower circuit limits are not modelled: a stock stuck at its lower circuit can't be sold at the stop.",
    "The 'quality filter' (fraud news, pledges, results collapses) can't be automated: you check that when approving.",
    "Delivery (CNC) only: no short selling. Costs include brokerage, STT, stamp, exchange, GST and DP charges.",
]


def _curve_stats(eq: pd.Series, capital: float) -> dict[str, float]:
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    final = float(eq.iloc[-1])
    peak = eq.cummax()
    dd = float(((eq - peak) / peak).min() * 100)
    cagr = ((final / capital) ** (1 / years) - 1) * 100 if final > 0 else -100.0
    return {"final": round(final, 2), "return_pct": round((final / capital - 1) * 100, 2), "cagr_pct": round(cagr, 2),
            "max_drawdown_pct": round(dd, 2)}


def trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0}
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_w, gross_l = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    return {"trades": len(trades), "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
            "avg_win_pct": round(float(np.mean([t["pnl_pct"] for t in wins])), 2) if wins else 0.0,
            "avg_loss_pct": round(float(np.mean([t["pnl_pct"] for t in losses])), 2) if losses else 0.0,
            "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else None,
            "net_pnl": round(gross_w - gross_l, 2), "avg_days": round(float(np.mean([t["days"] for t in trades])), 1)}


def metrics(eq: pd.Series, trades: list[dict[str, Any]], total_costs: float, capital: float) -> dict[str, Any]:
    out = {**_curve_stats(eq, capital), **trade_stats(trades), "total_costs": round(total_costs, 2)}
    daily = eq.pct_change().dropna()
    out["sharpe"] = round(float(daily.mean() / daily.std() * math.sqrt(252)), 2) if len(daily) > 2 and daily.std() > 0 else None
    return out


def _yearly(eq: pd.Series, bench: pd.Series) -> list[dict[str, Any]]:
    out = []
    for y in sorted(set(eq.index.year)):
        e, b = eq[eq.index.year == y], bench[bench.index.year == y]
        prev_e = eq[eq.index.year < y]
        prev_b = bench[bench.index.year < y]
        e0 = float(prev_e.iloc[-1]) if len(prev_e) else float(e.iloc[0])
        b0 = float(prev_b.iloc[-1]) if len(prev_b) else float(b.iloc[0])
        out.append({"year": int(y), "strategy_pct": round((float(e.iloc[-1]) / e0 - 1) * 100, 2),
                    "nifty_pct": round((float(b.iloc[-1]) / b0 - 1) * 100, 2)})
    return out


def _downsample(rows: list, n: int = 600) -> list:
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)] + [rows[-1]]


# ---- today's signals ----------------------------------------------------------------------------
def scan_today(data: dict[str, pd.DataFrame], index: pd.DataFrame, params: BacktestParams | None = None,
               equity: float | None = None, max_order_value: float | None = None,
               sectors: dict[str, str] | None = None, bnf_symbols: set[str] | None = None) -> dict[str, Any]:
    """Signals on the latest completed daily bar, sized like the backtest, ready to become
    swing proposals (which a human must approve)."""
    p = params or BacktestParams()
    ix_close = _clean_daily(index)["close"]
    panel = {s: indicators(_clean_daily(df), ix_close) for s, df in data.items() if len(df) >= 60}
    reg = market_regime(_clean_daily(index), panel)
    rr = reg.iloc[-1]
    label = regime_label(rr, p.bnf, p.momentum) if not rr.isna().any() else "CHOPPY"
    want_bnf = label == "PANIC" and p.strategy in ("bnf", "combined")
    want_mom = label == "UPTREND" and p.strategy in ("momentum", "combined")
    equity = equity or p.capital
    out = []
    asof = reg.index[-1]
    for s, d in panel.items():
        if d.index[-1] != asof:
            continue
        k = len(d) - 1
        allow_bnf = want_bnf and (bnf_symbols is None or s in bnf_symbols)
        sig = (bnf_signal(d, k, p.bnf) if allow_bnf else None) or (momentum_signal(d, k, p.momentum) if want_mom else None)
        if not sig:
            continue
        r = d.iloc[k]
        close = float(r["close"])
        if sig["family"] == "bnf":
            stop = max(sig["signal_low"] * 0.999, close * (1 - p.bnf.stop_pct / 100))
            tgt1, tgt2 = close + 0.5 * (float(r["sma25"]) - close), float(r["sma25"])
            horizon = max(5, p.bnf.time_stop_days)
        else:
            stop = close * (1 - p.momentum.stop_pct / 100)
            tgt1, tgt2 = close * (1 + p.momentum.first_target_pct / 100), close * 1.25
            horizon = 20
        cap = min(x for x in (max_order_value, p.max_position_value or equity / p.max_positions) if x)
        qty = int(min(equity * p.risk_pct / 100 / max(close - stop, 0.01), cap / close))
        out.append({"symbol": s, "family": sig["family"], "sector": (sectors or {}).get(s, ""), "close": round(close, 2),
                    "entry_low": round(close * 0.995, 2), "entry_high": round(close * 1.01, 2), "stop": round(stop, 2),
                    "target_1": round(tgt1, 2), "target_2": round(tgt2, 2), "quantity": qty, "horizon_days": horizon,
                    "why": sig["why"], "score": round(sig["score"], 3),
                    "too_expensive": qty < 1})
    out.sort(key=lambda x: -x["score"])
    return {"as_of": asof.date().isoformat(), "regime": label,
            "market": {"nifty": round(float(rr["nifty"]), 2), "nifty_vs_25dma_pct": round(float(rr["nifty_dev25"]), 2),
                       "nifty_above_50dma": bool(rr["nifty_above_sma50"]),
                       "stocks_above_50dma_pct": round(float(rr["breadth_above50"]), 1),
                       "stocks_10pct_below_25dma_pct": round(float(rr["breadth_deep_below25"]), 1)},
            "signals": out[:20]}


def proposal_from_signal(sig: dict[str, Any]) -> dict[str, Any]:
    """A swing proposal (swing-proposal/1 schema) from a scanner signal. It still needs your
    approval in the dashboard, and passes the same risk checks as any other proposal."""
    name = "BNF panic-reversion" if sig["family"] == "bnf" else "Momentum breakout"
    return {
        "symbol": sig["symbol"], "exchange": "NSE", "recommendation": "BUY",
        "entry_low": sig["entry_low"], "entry_high": sig["entry_high"], "quantity": int(sig["quantity"]),
        "stop_loss": sig["stop"], "target_low": sig["target_1"], "target_high": sig["target_2"],
        "horizon_days": int(min(max(sig["horizon_days"], 5), 31)),
        "rationale": f"{name} (Strategy Lab scanner): {sig['why']}.",
        "evidence": [f"Strategy Lab {sig['family']} rules on the daily close", sig["why"]],
        "risks": ["Rules-based signal: check the news (fraud, pledges, results) before approving",
                  "Circuit limits or a gap down can fill below the stop"],
        "invalidation": [f"Close below ₹{sig['stop']:,.2f}"],
        "confidence_notes": "Mechanical signal; see the Lab backtest for how these rules did historically.",
    }
