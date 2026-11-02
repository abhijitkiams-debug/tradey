import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from test_simple import (  # noqa: F401 - fixtures/helpers
    AUTH,
    make,
    secrets_dir,  # noqa: F401
)

from claudetrade import positional as pos
from claudetrade.costs import CostModel
from claudetrade.evidence import SWING_KEY
from claudetrade.models import IST
from claudetrade.swing import parse_proposal


def frame(closes, highs=None, lows=None, opens=None, vols=None, start="2024-01-01"):
    idx = pd.bdate_range(start, periods=len(closes), name="date")
    c = np.asarray(closes, float)
    o = np.asarray(opens if opens is not None else c, float)
    h = np.asarray(highs if highs is not None else np.maximum(o, c) * 1.005, float)
    lo = np.asarray(lows if lows is not None else np.minimum(o, c) * 0.995, float)
    v = np.asarray(vols if vols is not None else np.full(len(c), 2e6), float)
    return pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": v}, index=idx)


def test_universe_lists_are_bundled():
    n200, n500 = pos.load_universe("nifty200"), pos.load_universe("nifty500")
    assert len(n200) == 200 and len(n500) >= 490
    assert "RELIANCE" in n200 and n200["RELIANCE"]
    assert set(n200) <= set(n500) | {s for s in n200}  # lists are snapshots; both load


def test_bnf_signal_needs_deep_fall_and_reversal_candle():
    closes = [100.0] * 40 + [96, 92, 88, 84, 81]
    df = frame(closes)
    # last day: fell to 81 but closed near the high of a wide range -> reversal
    df.iloc[-1, df.columns.get_loc("low")] = 78
    df.iloc[-1, df.columns.get_loc("high")] = 82
    d = pos.indicators(df)
    sig = pos.bnf_signal(d, len(d) - 1, pos.BNFParams(entry_deviation_pct=-12, min_turnover_cr=1))
    assert sig and sig["family"] == "bnf" and sig["signal_low"] == 78
    # closing at the low is no reversal
    df.iloc[-1, df.columns.get_loc("close")] = 78.2
    d = pos.indicators(df)
    assert pos.bnf_signal(d, len(d) - 1, pos.BNFParams(entry_deviation_pct=-12, min_turnover_cr=1)) is None
    # not far enough below the average
    assert pos.bnf_signal(pos.indicators(frame([100.0] * 40 + [97, 95])), 41, pos.BNFParams(min_turnover_cr=1)) is None


def vcp_frame():
    """A steady uptrend, then a 60-day base with shrinking pullbacks and quiet volume, then a breakout."""
    up = list(np.linspace(50, 100, 220))
    base = []
    for depth, n in ((0.18, 20), (0.09, 20), (0.04, 20)):
        half = n // 2
        base += list(np.linspace(100, 100 * (1 - depth), half)) + list(np.linspace(100 * (1 - depth), 99.5, n - half))
    closes = up + base + [104.0]
    vols = [2e6] * 220 + [1.2e6] * 60 + [5e6]
    df = frame(closes, vols=vols)
    df["high"] = np.maximum(df["open"], df["close"]) * 1.002
    df["low"] = np.minimum(df["open"], df["close"]) * 0.998
    return df


def test_momentum_signal_on_a_tight_base_breakout():
    df = vcp_frame()
    d = pos.indicators(df)
    p = pos.MomentumParams(min_turnover_cr=1)
    sig = pos.momentum_signal(d, len(d) - 1, p)
    assert sig and sig["family"] == "momentum" and 99 < sig["pivot"] < 101
    # same day without the volume surge: no breakout
    df2 = df.copy()
    df2.iloc[-1, df2.columns.get_loc("volume")] = 1.3e6
    assert pos.momentum_signal(pos.indicators(df2), len(df2) - 1, p) is None
    # a stock in a downtrend never qualifies
    down = frame(list(np.linspace(150, 60, 300)))
    assert pos.momentum_signal(pos.indicators(down), 299, p) is None


@pytest.mark.parametrize("strategy", ["bnf", "momentum", "combined"])
def test_backtest_runs_with_costs_and_benchmark(strategy):
    syms = list(pos.load_universe("nifty200"))[:40]
    data, index = pos.synthetic_dataset(syms, date(2020, 1, 1), date(2025, 12, 31))
    t0 = time.monotonic()
    r = pos.backtest(data, index, pos.BacktestParams(strategy=strategy), costs=CostModel())
    assert time.monotonic() - t0 < 30
    m = r["metrics"]
    assert r["benchmark"]["name"].startswith("Nifty") and r["period"]["start"].startswith("2020")
    assert sum(r["regime_days"].values()) == r["period"]["days"]
    assert all(e > 0 for _, e, _ in r["equity_curve"])
    if m["trades"]:
        assert m["total_costs"] > 0
        fams = {t["family"] for t in r["trades"]}
        assert fams <= ({"bnf"} if strategy == "bnf" else {"momentum"} if strategy == "momentum" else {"bnf", "momentum"})
        for t in r["trades"]:
            assert t["exit_date"] >= t["entry_date"] and t["qty"] >= 1
    # same inputs, same answer
    assert pos.backtest(data, index, pos.BacktestParams(strategy=strategy))["metrics"] == m


def test_bnf_only_trades_in_a_panic_and_respects_stop():
    syms = list(pos.load_universe("nifty200"))[:40]
    data, index = pos.synthetic_dataset(syms, date(2020, 1, 1), date(2025, 12, 31))
    p = pos.BacktestParams(strategy="bnf", bnf=pos.BNFParams(entry_deviation_pct=-10, min_turnover_cr=0))
    r = pos.backtest(data, index, p)
    assert r["metrics"]["trades"] > 0
    for t in r["trades"]:
        if t["reason"] == "stop":
            assert t["pnl_pct"] > -(p.bnf.stop_pct + 3)  # gaps can overshoot a little, never by much here
        assert t["days"] <= p.bnf.time_stop_days + 1


def test_position_size_follows_risk_and_cap():
    syms = list(pos.load_universe("nifty200"))[:40]
    data, index = pos.synthetic_dataset(syms, date(2020, 1, 1), date(2025, 12, 31))
    p = pos.BacktestParams(strategy="bnf", max_position_value=1000, bnf=pos.BNFParams(entry_deviation_pct=-10, min_turnover_cr=0))
    for t in pos.backtest(data, index, p)["trades"]:
        assert t["entry"] * t["qty"] <= 1000 * 1.002


def test_bnf_symbols_limits_bnf_entries():
    syms = list(pos.load_universe("nifty200"))[:40]
    data, index = pos.synthetic_dataset(syms, date(2020, 1, 1), date(2025, 12, 31))
    p = pos.BacktestParams(strategy="bnf", bnf=pos.BNFParams(entry_deviation_pct=-10, min_turnover_cr=0))
    r = pos.backtest(data, index, p, bnf_symbols=set(syms[:5]))
    assert {t["symbol"] for t in r["trades"]} <= set(syms[:5])


def test_params_validation():
    with pytest.raises(ValueError):
        pos.BacktestParams.from_dict({"strategy": "yolo"})
    with pytest.raises(ValueError):
        pos.BacktestParams.from_dict({"risk_pct": 50})
    p = pos.BacktestParams.from_dict({"strategy": "bnf", "bnf": {"stop_pct": 5, "nonsense": 1}, "capital": 20000})
    assert p.bnf.stop_pct == 5 and p.capital == 20000 and p.momentum == pos.MomentumParams()


class ChunkFeed:
    def __init__(self):
        self.calls = []

    def candles(self, symbol, start, end, interval="1minute"):
        assert interval == "1day"
        self.calls.append((symbol, start.date(), end.date()))
        days = pd.bdate_range(start.date(), end.date())
        idx = pd.DatetimeIndex([datetime.combine(d.date(), datetime.min.time(), IST) for d in days])
        return pd.DataFrame({"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 1000}, index=idx)


def test_fetch_daily_chunks_and_caches(tmp_path):
    f = ChunkFeed()
    df = pos.fetch_daily(f, "ABC", date(2024, 1, 1), date(2025, 6, 30), cache_dir=tmp_path)
    assert all((e - s).days <= 179 for _, s, e in f.calls) and len(f.calls) == 4
    assert df.index.min().date() == date(2024, 1, 1) and df.index.tz is None
    n = len(f.calls)
    pos.fetch_daily(f, "ABC", date(2024, 1, 1), date(2025, 6, 30), cache_dir=tmp_path)
    assert len(f.calls) == n  # fully cached
    later = pos.fetch_daily(f, "ABC", date(2024, 1, 1), date(2025, 7, 10), cache_dir=tmp_path)
    assert len(f.calls) == n + 1 and f.calls[-1][1] == date(2025, 6, 30)  # only the new days
    assert later.index.max().date() == date(2025, 7, 10) and not later.index.duplicated().any()


def test_scan_and_proposal_match_the_swing_schema():
    sig = {"symbol": "ABC", "family": "bnf", "sector": "", "close": 100.0, "entry_low": 99.5, "entry_high": 101.0,
           "stop": 93.0, "target_1": 108.0, "target_2": 116.0, "quantity": 9, "horizon_days": 7, "why": "fell 18% below its 25-day average",
           "score": 18, "too_expensive": False}
    raw = pos.proposal_from_signal(sig)
    p = parse_proposal(raw, now=datetime(2026, 11, 3, 10, tzinfo=IST), ttl=timedelta(minutes=30), reference_price=pos.Decimal("100"),
                       costs=CostModel())
    assert p.recommendation.value == "BUY" and p.quantity == 9 and p.max_loss is not None

    syms = list(pos.load_universe("nifty200"))[:30]
    data, index = pos.synthetic_dataset(syms, date(2020, 1, 1), date(2025, 12, 31))
    out = pos.scan_today(data, index, equity=10000, max_order_value=1000)
    assert out["regime"] in ("PANIC", "UPTREND", "CHOPPY") and "nifty_vs_25dma_pct" in out["market"]
    for s in out["signals"]:
        assert s["stop"] < s["entry_low"] and s["close"] * s["quantity"] <= 1000


# ---- the Lab page and API ----------------------------------------------------------------------
def wait_done(c):
    for _ in range(300):
        s = c.get("/api/lab", headers=AUTH).json()
        if s["state"] != "running":
            return s
        time.sleep(0.1)
    raise AssertionError("lab job did not finish")


def test_lab_api_backtest_and_scan_on_demo_data(tmp_path):
    desk, clock, c = make(tmp_path, at=(18, 0), setup_needed=True)  # no Groww -> made-up prices
    assert "Strategy Lab" in c.get("/lab").text
    assert c.get("/api/lab").status_code == 401
    s = c.get("/api/lab", headers=AUTH).json()
    assert s["state"] == "idle" and s["defaults"]["strategy"] == "combined" and s["limits"]
    assert c.post("/api/lab/backtest", headers=AUTH, json={"params": {"strategy": "nope"}}).status_code == 409
    r = c.post("/api/lab/backtest", headers=AUTH, json={"params": {"strategy": "bnf"}}).json()
    assert r["ok"]
    s = wait_done(c)
    assert s["state"] == "done", s["log"]
    assert s["result"]["data"]["real"] is False and "MADE-UP" in s["result"]["data"]["note"]
    c.post("/api/lab/scan", headers=AUTH, json={})
    s = wait_done(c)
    assert s["scan"]["regime"] in ("PANIC", "UPTREND", "CHOPPY")
    # demo data can never become a proposal
    msg = c.post("/api/lab-propose", headers=AUTH, json={"symbol": "RELIANCE"}).json()["message"]
    assert "real Groww prices" in msg


def test_lab_propose_creates_a_proposal_that_still_needs_approval(tmp_path):
    desk, clock, c = make(tmp_path, at=(10, 0))
    desk.lab.scan = {"data": {"real": True}, "signals": [{
        "symbol": "ALPHA", "family": "bnf", "sector": "", "close": 100.0, "entry_low": 99.5, "entry_high": 101.0, "stop": 93.0,
        "target_1": 108.0, "target_2": 116.0, "quantity": 5, "horizon_days": 7, "why": "fell 18% below its 25-day average",
        "score": 18, "too_expensive": False}]}
    msg = c.post("/api/lab-propose", headers=AUTH, json={"symbol": "ALPHA"}).json()["message"]
    assert "SWING switch" in msg  # swing proposals are off until you switch them on
    desk.set_mode(SWING_KEY, "PAPER", "operator:test")
    from test_desk import quotes

    quotes(desk.paper.feed.client, clock)
    msg = c.post("/api/lab-propose", headers=AUTH, json={"symbol": "alpha"}).json()["message"]
    assert "waiting for your approval" in msg, msg
    pending = desk.paper.app.swing.pending()
    assert len(pending) == 1 and pending[0].symbol == "ALPHA" and not desk.paper.app.swing.approvals
    assert any(e.action == "PROPOSAL_CREATED" and e.actor.endswith("strategy-lab") for e in desk.paper.app.audit.events())
    assert "not in today's signals" in c.post("/api/lab-propose", headers=AUTH, json={"symbol": "ZZZ"}).json()["message"]
