from datetime import datetime

import pytest
from conftest import fill, req
from fake_groww import FakeGroww
from test_lifecycle import LENIENT

from claudetrade.backtest import BacktestConfig, walk_forward
from claudetrade.clock import SimClock
from claudetrade.models import IST, TradeMode
from claudetrade.runtime import build_runtime
from claudetrade.settings import Settings, load_settings
from claudetrade.strategies import MeanReversion
from claudetrade.synthetic import universe


def at(clock, h, m, day=13):
    clock.set(datetime(2026, 10, day, h, m, tzinfo=IST))


def feed_quotes(fake, clock, prices):
    ms = int(clock().timestamp() * 1000)
    for s, px in prices.items():
        fake.quotes[s] = {"last_price": px, "bid_price": px - 0.05, "offer_price": px + 0.05, "volume": 10000,
                          "day_change": 0.0, "last_trade_time": ms - 1000}


@pytest.fixture
def rt(tmp_path):
    clock = SimClock(datetime(2026, 10, 13, 8, 0, tzinfo=IST))
    fake = FakeGroww()
    s = Settings(db_path=str(tmp_path / "ct.db"), universe=("ALPHA", "CHARLIE"), default_avg_daily_volume=1_000_000)
    r = build_runtime(s, clock=clock, groww_client=fake, research_loader=lambda: {})
    return r, clock, fake


def test_no_release_means_no_intraday_trading(rt):
    r, clock, fake = rt
    at(clock, 9, 25)
    feed_quotes(fake, clock, {"ALPHA": 500, "CHARLIE": 250})
    ran = r.scheduler.run_pending()
    assert ran[:3] == ["preopen", "validate", "tick"]
    assert r.app.audit.events("NO_RELEASE")
    assert r.app.intraday.strategy_version is None
    assert r.scheduler.done("validate", clock())


def test_jobs_are_idempotent_across_restart(rt, tmp_path):
    r, clock, fake = rt
    at(clock, 16, 5)
    ran = r.scheduler.run_pending()
    assert {"preopen", "eod", "research"} <= set(ran)
    r.stop()
    r2 = build_runtime(r.settings, clock=clock, groww_client=fake, research_loader=lambda: {})
    assert r2.scheduler.run_pending() == []  # nothing re-runs after restart
    at(clock, 16, 5, day=14)
    assert "preopen" in r2.scheduler.run_pending()


def test_weekend_does_nothing(rt):
    r, clock, _ = rt
    at(clock, 10, 0, day=17)
    assert r.scheduler.run_pending() == []


def test_released_strategy_validates_and_activates(rt):
    r, clock, fake = rt
    app = r.app
    data = universe(n_days=20)
    wf = walk_forward(MeanReversion, {"ALPHA": data["ALPHA"]}, BacktestConfig(), train_days=12, test_days=6, min_train_trades=1)
    sv = app.strategies.freeze(wf, LENIENT, app.risk_config)
    app.strategies.release(sv.content_hash, clock().date(), "owner")
    at(clock, 9, 25)
    feed_quotes(fake, clock, {"ALPHA": 500, "CHARLIE": 250})
    r.scheduler.run_pending()
    assert app.intraday.strategy_version is sv


def test_square_off_and_eod_report(rt):
    r, clock, fake = rt
    app = r.app
    at(clock, 10, 0)
    feed_quotes(fake, clock, {"ALPHA": 500, "CHARLIE": 250})
    r.scheduler.run_pending()
    app.execution.execute(req(qty=5))
    fill(app)
    at(clock, 15, 1)
    feed_quotes(fake, clock, {"ALPHA": 501, "CHARLIE": 250})
    r.scheduler.run_pending()  # exits submitted
    at(clock, 15, 2)
    feed_quotes(fake, clock, {"ALPHA": 501, "CHARLIE": 250})
    r.scheduler.run_pending()  # paper broker fills, verified flat
    assert app.intraday.square_off.verified_flat
    at(clock, 15, 40)
    r.scheduler.run_pending()
    report = app.persistence.store.latest("eod_report")["2026-10-13"]
    assert report["verified_flat"] is True and report["mismatches"] == []


def test_swing_stop_exits_automatically(rt):
    from test_swing import proposal

    r, clock, fake = rt
    app = r.app
    at(clock, 10, 0)
    feed_quotes(fake, clock, {"ALPHA": 500, "CHARLIE": 250})
    r.scheduler.run_pending()
    p = app.swing.submit_proposal(proposal())
    a = app.swing.approve(p.proposal_id, p.version, p.content_hash(), "owner")
    app.swing.execute(a.approval_id)
    fill(app)
    at(clock, 11, 0)
    feed_quotes(fake, clock, {"ALPHA": 485, "CHARLIE": 250})
    r.scheduler.run_pending()
    at(clock, 11, 1)
    feed_quotes(fake, clock, {"ALPHA": 485, "CHARLIE": 250})
    r.scheduler.run_pending()
    assert app.portfolio.position("ALPHA", TradeMode.SWING).is_flat
    assert any("swing stop hit" in al.message for al in app.monitor.alerts)


def test_job_failure_pauses_entries_but_loop_survives(rt):
    r, clock, fake = rt
    at(clock, 10, 0)

    def boom():
        raise RuntimeError("feed exploded")

    r.scheduler.tick = boom
    r.scheduler.run_pending()
    assert r.app.controls.entries_paused and r.app.audit.events("JOB_FAILED")


def test_settings_file(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('mode = "PAPER"\nuniverse = ["reliance", "infy"]\n[risk]\nrisk_per_trade = 80\n'
                 '[session]\nno_new_entries_after = "14:30"\n[external_holdings]\nINFY = 10\n')
    s = load_settings(f)
    assert s.universe == ("RELIANCE", "INFY") and str(s.risk.risk_per_trade) == "80"
    assert s.session.no_new_entries_after.hour == 14 and s.external_holdings == {"INFY": 10}
    f.write_text('[risk]\napproved_by = "me"\n')
    with pytest.raises(ValueError, match="operator action"):
        load_settings(f)
    f.write_text('lever = 5\n')
    with pytest.raises(ValueError, match="unknown"):
        load_settings(f)


def test_risk_approval_bound_to_config_hash(rt):
    r, clock, fake = rt
    r.app.approve_risk_config("owner")
    assert r.app.risk_config.is_approved
    r.stop()
    same = build_runtime(r.settings, clock=clock, groww_client=fake, research_loader=lambda: {})
    assert same.app.risk_config.is_approved
    same.stop()
    from dataclasses import replace
    from decimal import Decimal

    changed = replace(r.settings, risk=replace(r.settings.risk, max_daily_loss=Decimal("900")))
    other = build_runtime(changed, clock=clock, groww_client=fake, research_loader=lambda: {})
    assert not other.app.risk_config.is_approved
