from datetime import date, datetime

import pytest
from fake_groww import FakeGroww, GrowwAPIAuthenticationException

from claudetrade.broker.groww_auth import RefreshingGrowwClient
from claudetrade.cli import main
from claudetrade.clock import SimClock
from claudetrade.desk import StrategyMode, build_desk
from claudetrade.models import IST
from claudetrade.settings import Settings
from claudetrade.synthetic import universe


class Expiring(FakeGroww):
    def __init__(self, n):
        super().__init__()
        self.n, self.expired = n, False

    def get_available_margin_details(self, timeout=None):
        if self.expired:
            raise GrowwAPIAuthenticationException()
        return super().get_available_margin_details(timeout)


def test_relogs_in_on_auth_failure_and_retries():
    clock = SimClock(datetime(2026, 10, 26, 10, 0, tzinfo=IST))
    made = []

    def login():
        made.append(Expiring(len(made)))
        return made[-1]

    c = RefreshingGrowwClient(login, clock, "totp", min_relogin_gap_s=0)
    assert c.get_available_margin_details()["clear_cash"] == 10000.0 and c.logins == 1
    made[-1].expired = True
    assert c.get_available_margin_details()["clear_cash"] == 10000.0  # transparently re-logged in
    assert c.logins == 2


def test_refreshes_each_morning_before_open():
    clock = SimClock(datetime(2026, 10, 26, 8, 0, tzinfo=IST))
    c = RefreshingGrowwClient(lambda: FakeGroww(), clock, "totp")
    c.get_available_margin_details()
    clock.set(datetime(2026, 10, 26, 8, 30, tzinfo=IST))
    c.get_available_margin_details()
    assert c.logins == 1
    clock.set(datetime(2026, 10, 26, 8, 46, tzinfo=IST))
    c.get_available_margin_details()
    assert c.logins == 2  # crossed 08:45
    clock.set(datetime(2026, 10, 26, 14, 0, tzinfo=IST))
    c.get_available_margin_details()
    assert c.logins == 2


def test_relogin_is_rate_limited_and_failure_is_not_swallowed():
    clock = SimClock(datetime(2026, 10, 26, 10, 0, tzinfo=IST))
    made = []

    def login():
        made.append(Expiring(len(made)))
        made[-1].expired = True
        return made[-1]

    c = RefreshingGrowwClient(login, clock, "totp", min_relogin_gap_s=300)
    with pytest.raises(GrowwAPIAuthenticationException):
        c.get_available_margin_details()
    assert c.logins == 1  # did not hammer the token endpoint


def test_startup_research_and_auto_paper(tmp_path):
    clock = SimClock(datetime(2026, 10, 26, 9, 0, tzinfo=IST))
    data = {k: v for k, v in universe(60, start=date(2026, 7, 1)).items() if k in ("ALPHA", "CHARLIE")}
    s = Settings(paper_db_path=str(tmp_path / "p.db"), universe=("ALPHA", "CHARLIE"), default_avg_daily_volume=1_000_000,
                 auto_paper=("MEAN_REVERSION", "ORB"))
    desk = build_desk(s, clock=clock, groww_client=FakeGroww(), research_loader=lambda: data, notifier=lambda a: None)
    out = desk.initial_research()
    assert set(out) >= {"ORB", "VWAP_PULLBACK", "MEAN_REVERSION", "auto_paper"}
    on = {desk.versions()[k].family for k, t in desk.toggles.items() if t.mode is StrategyMode.PAPER}
    assert on == {"MEAN_REVERSION", "ORB"}
    assert all(t.changed_by == "config:auto_paper" for t in desk.toggles.values())
    # next day's research freezes new versions, but the running ones are not replaced
    first = {k for k, t in desk.toggles.items() if t.mode is StrategyMode.PAPER}
    clock.set(datetime(2026, 10, 27, 16, 5, tzinfo=IST))
    with desk.paper.app.lock:
        desk.paper.scheduler.research()
    assert {k for k, t in desk.toggles.items() if t.mode is StrategyMode.PAPER} == first
    rows = desk.strategy_rows()
    assert len([r for r in rows if r["family"] == "MEAN_REVERSION"]) == 2  # running + newest
    assert not any(t.mode is StrategyMode.LIVE for t in desk.toggles.values())


def test_doctor_reports_missing_pieces(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text('universe = ["ITC"]\npaper_db_path = "%s"\nlive_db_path = "%s"\n'
                   '[research]\ncache_dir = "%s"\n' % (tmp_path / "p.db", tmp_path / "l.db", tmp_path / "h"))
    monkeypatch.delenv("CLAUDETRADE_OPERATOR_TOKEN", raising=False)
    for k in ("GROWW_API_KEY", "GROWW_ACCESS_TOKEN", "GROWW_TOTP_SECRET", "GROWW_API_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert main(["doctor", "--config", str(cfg)]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] operator token" in out and "NOT READY" in out


def test_mid_session_switch_retries_until_data_arrives(tmp_path):
    from test_desk import freeze

    clock = SimClock(datetime(2026, 10, 26, 11, 0, tzinfo=IST))  # after the 10:30 morning deadline
    fake = FakeGroww()
    s = Settings(paper_db_path=str(tmp_path / "p.db"), universe=("ALPHA", "CHARLIE"), default_avg_daily_volume=1_000_000)
    desk = build_desk(s, clock=clock, groww_client=fake, research_loader=lambda: {}, notifier=lambda a: None)
    desk.run_pending()  # day's validate job runs with nothing released
    sv = freeze(desk)
    desk.set_mode(sv.content_hash, "PAPER", "operator:abhijit")  # no quotes yet -> deferred, not rejected
    assert sv.content_hash not in desk.paper.app.intraday.active
    clock.set(datetime(2026, 10, 26, 11, 6, tzinfo=IST))
    ms = int(clock().timestamp() * 1000)
    for sym, px in (("ALPHA", 500), ("CHARLIE", 250)):
        fake.quotes[sym] = {"last_price": px, "bid_price": px - 0.05, "offer_price": px + 0.05, "volume": 1,
                            "day_change": 0, "last_trade_time": ms - 500}
    assert "validate" in desk.run_pending()["paper"]
    assert sv.content_hash in desk.paper.app.intraday.active
