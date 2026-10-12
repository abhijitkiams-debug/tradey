from datetime import date, datetime, time

import pandas as pd
import pytest

from claudetrade.backtest import BacktestConfig, EligibilityCriteria, _simulate_trade, run_backtest, walk_forward
from claudetrade.market_data import validate_bars
from claudetrade.models import IST
from claudetrade.strategies import REGISTRY, MeanReversion, OpeningRangeBreakout, Signal
from claudetrade.synthetic import universe


@pytest.fixture(scope="module")
def data():
    u = universe(n_days=24, start=date(2026, 6, 1))
    return {k: u[k] for k in ("ALPHA", "BRAVO")}


def bars(rows, start=time(9, 15)):
    idx = pd.date_range(datetime.combine(date(2026, 6, 1), start, IST), periods=len(rows), freq="1min")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


def test_all_three_families_registered():
    assert set(REGISTRY.strategies) == {"ORB", "VWAP_PULLBACK", "MEAN_REVERSION"}
    for cls in REGISTRY.strategies.values():
        assert cls.spec.regime_assumption and cls.spec.stop_logic and cls.param_grid


def test_fill_is_next_bar_open_never_signal_bar():
    df = bars([[100, 101, 99, 100.5, 1000], [102, 103, 101.5, 102.5, 1000], [102.5, 104, 102, 103, 1000]])
    j, k, qty, entry, exit_px, reason = _simulate_trade(df, Signal(0, "BUY", 99.0, 103.5), BacktestConfig(slippage_bps=0), time(15, 10), 10_000)
    assert j == 1 and entry == 102  # next bar's open, not the 100.5 signal close


def test_stop_assumed_first_when_bar_hits_both():
    df = bars([[100, 100, 100, 100, 1], [100, 100.2, 99.9, 100, 1], [100, 110, 90, 100, 1]])
    *_, exit_px, reason = _simulate_trade(df, Signal(0, "BUY", 95.0, 105.0), BacktestConfig(slippage_bps=0), time(15, 10), 10_000)
    assert reason == "stop" and exit_px == 95.0


def test_gap_through_stop_fills_at_open():
    df = bars([[100, 100, 100, 100, 1], [100, 100.2, 99.9, 100, 1], [92, 93, 91, 92, 1]])
    *_, exit_px, reason = _simulate_trade(df, Signal(0, "BUY", 95.0, 105.0), BacktestConfig(slippage_bps=0), time(15, 10), 10_000)
    assert reason == "stop" and exit_px == 92


def test_positions_closed_at_square_off():
    df = bars([[100, 100, 100, 100, 1]] * 4, start=time(15, 7))
    *_, reason = _simulate_trade(df, Signal(0, "BUY", 90.0, 200.0), BacktestConfig(slippage_bps=0), time(15, 10), 10_000)
    assert reason == "square_off"


@pytest.mark.parametrize("cls", list(REGISTRY.strategies.values()))
def test_no_look_ahead(cls, data):
    """Signals emitted on a truncated day must equal those on the full day: no future bars used."""
    day = data["ALPHA"][data["ALPHA"].index.date == date(2026, 6, 3)]
    full = cls().signals(day)
    for cut in (90, 180, 300):
        part = cls().signals(day.iloc[:cut])
        assert [s for s in full if s.bar_index < cut - 1] == [s for s in part if s.bar_index < cut - 1]


def test_costs_reduce_returns(data):
    base = run_backtest(MeanReversion(), data, BacktestConfig()).metrics
    pricey = run_backtest(MeanReversion(), data, BacktestConfig(cost_multiplier=2)).metrics
    assert base.trades == pricey.trades > 0
    assert pricey.net_pnl < base.net_pnl and pricey.total_costs == pytest.approx(2 * base.total_costs)


def test_backtests_are_reproducible(data):
    a = run_backtest(OpeningRangeBreakout(), data, BacktestConfig())
    b = run_backtest(OpeningRangeBreakout(), {k: v.copy() for k, v in data.items()}, BacktestConfig())
    assert a.run_id == b.run_id and a.metrics.as_dict() == b.metrics.as_dict()


def test_walk_forward_selects_in_sample_and_scores_out_of_sample(data):
    wf = walk_forward(MeanReversion, data, BacktestConfig(), train_days=12, test_days=6, min_train_trades=1)
    assert len(wf.folds) == 2
    for f in wf.folds:
        assert not set(f.train_days) & set(f.test_days)
        assert max(f.train_days) < min(f.test_days)
    assert set(wf.stress) == {"base", "slippage_x2", "slippage_x4", "costs_x1.5"}
    assert wf.stress["slippage_x4"].net_pnl <= wf.stress["base"].net_pnl
    wf2 = walk_forward(MeanReversion, data, BacktestConfig(), train_days=12, test_days=6, min_train_trades=1)
    assert wf.summary() == wf2.summary()


def test_eligibility_rejects_thin_evidence(data):
    wf = walk_forward(OpeningRangeBreakout, data, BacktestConfig(), train_days=12, test_days=6)
    ok, fails = EligibilityCriteria().evaluate(wf)
    assert not ok and any("OOS trades" in f for f in fails)


def test_bar_validation_detects_problems():
    df = bars([[100, 101, 99, 100, 10], [100, 99, 101, 100, 10]])  # high < low on bar 2
    assert any("inconsistent OHLC" in i for i in validate_bars(df))
    gap = pd.concat([bars([[100, 101, 99, 100, 10]] * 3), bars([[100, 101, 99, 100, 10]], start=time(9, 30))])
    assert any("missing candles" in i for i in validate_bars(gap))
    assert validate_bars(bars([[100, 101, 99, 100, 10]] * 5)) == []
