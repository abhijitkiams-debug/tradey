from datetime import date, datetime

import pytest
from conftest import fill, req
from fake_groww import FakeGroww
from fastapi.testclient import TestClient
from test_lifecycle import LENIENT

from claudetrade.backtest import BacktestConfig, EligibilityCriteria, walk_forward
from claudetrade.broker.groww import RateLimiter
from claudetrade.clock import SimClock
from claudetrade.desk import LIVE_STRATEGY_CONFIRMATION, StrategyMode, ToggleError, build_desk
from claudetrade.evidence import SWING_KEY
from claudetrade.models import IST, OrderStatus, TradeMode
from claudetrade.server import create_app
from claudetrade.settings import Settings
from claudetrade.strategies import MeanReversion
from claudetrade.synthetic import universe

PRICES = {"ALPHA": 500, "CHARLIE": 250}
AUTH = {"Authorization": "Bearer desk-token", "X-Operator": "abhijit"}
CONFIRM = LIVE_STRATEGY_CONFIRMATION


def quotes(fake, clock):
    ms = int(clock().timestamp() * 1000)
    for s, px in PRICES.items():
        fake.quotes[s] = {"last_price": px, "bid_price": px - 0.05, "offer_price": px + 0.05, "volume": 10_000,
                          "day_change": 0.0, "last_trade_time": ms - 500}


def at(desk, clock, fake, day, h, m):
    clock.set(datetime(2026, 10, day, h, m, tzinfo=IST))
    quotes(fake, clock)
    return desk.run_pending()


def full_day(desk, clock, fake, day):
    for h, m in ((9, 25), (10, 0), (15, 1), (15, 2), (15, 40)):
        at(desk, clock, fake, day, h, m)


@pytest.fixture
def env(tmp_path):
    clock = SimClock(datetime(2026, 10, 13, 8, 0, tzinfo=IST))
    fake = FakeGroww()
    sent = []
    s = Settings(paper_db_path=str(tmp_path / "paper.db"), live_db_path=str(tmp_path / "live.db"), live_enabled=True,
                 universe=tuple(PRICES), default_avg_daily_volume=1_000_000)
    desk = build_desk(s, clock=clock, groww_client=fake, research_loader=lambda: {}, notifier=sent.append)
    desk.live.app.broker.limiter = RateLimiter(sleep=lambda s: None)
    return desk, clock, fake, s


def freeze(desk, criteria=LENIENT):
    wf = walk_forward(MeanReversion, {"ALPHA": universe(20, start=date(2026, 6, 1))["ALPHA"]}, BacktestConfig(),
                      train_days=12, test_days=6, min_train_trades=1)
    return desk.paper.app.strategies.freeze(wf, criteria, desk.paper.app.risk_config)


def test_books_keep_money_separate(env):
    desk, clock, fake, _ = env
    at(desk, clock, fake, 13, 10, 0)
    desk.paper.app.execution.execute(req(qty=2, stop="490"))
    fill(desk.paper.app)
    assert desk.paper.app.ledger.cash() < 10_000
    assert desk.live.app.ledger.cash() == 10_000
    assert not desk.live.app.portfolio.open_positions()
    assert fake.orders == {}  # nothing reached Groww


def test_one_poll_feeds_both_books(env):
    desk, clock, fake, _ = env
    at(desk, clock, fake, 13, 10, 0)
    assert fake.calls.count("get_quote") == len(PRICES)  # not once per book
    for rt in desk.books.values():
        assert rt.app.market_data.validate_quote("ALPHA", TradeMode.INTRADAY).valid


def test_paper_switch_activates_mid_session(env):
    desk, clock, fake, _ = env
    sv = freeze(desk)
    at(desk, clock, fake, 13, 10, 0)
    desk.set_mode(sv.content_hash, "PAPER", "operator:abhijit", reason="start trial")
    assert sv.content_hash in desk.paper.app.intraday.active
    assert sv.content_hash not in desk.live.app.intraday.active
    events = desk.paper.app.audit.events("STRATEGY_MODE")
    assert events[-1].payload["mode"] == "PAPER" and desk.live.app.audit.events("STRATEGY_MODE")


def test_ineligible_strategy_can_paper_trial_but_never_go_live(env):
    desk, clock, fake, _ = env
    sv = freeze(desk, EligibilityCriteria(min_oos_trades=10_000))
    at(desk, clock, fake, 13, 10, 0)
    desk.set_mode(sv.content_hash, "PAPER", "operator:abhijit")
    assert sv.content_hash in desk.paper.app.intraday.active  # paper-only trial
    row = next(r for r in desk.strategy_rows() if r["key"] == sv.content_hash)
    assert row["paper_trial"] and any("not eligible" in b for b in row["live_blockers"])
    with pytest.raises(ToggleError, match="not eligible"):
        desk.set_mode(sv.content_hash, "LIVE", "operator:abhijit", confirm=CONFIRM)


def test_live_needs_confirmation_master_and_own_paper_record(env):
    desk, clock, fake, _ = env
    sv = freeze(desk)
    at(desk, clock, fake, 13, 9, 25)
    with pytest.raises(ToggleError, match="type exactly"):
        desk.set_mode(sv.content_hash, "LIVE", "operator:abhijit")
    with pytest.raises(ToggleError, match="PAPER first"):
        desk.set_mode(sv.content_hash, "LIVE", "operator:abhijit", confirm=CONFIRM)
    desk.set_mode(sv.content_hash, "PAPER", "operator:abhijit")
    with pytest.raises(ToggleError) as e:
        desk.set_mode(sv.content_hash, "LIVE", "operator:abhijit", confirm=CONFIRM)
    assert "master switch is off" in str(e.value) and "72 h required" in str(e.value)
    assert desk.paper.app.audit.events("STRATEGY_MODE_DENIED")


def go_live(desk, clock, fake, sv):
    """Three clean paper days with the strategy running, then 72 h, master on, LIVE."""
    at(desk, clock, fake, 13, 9, 0)
    desk.set_mode(sv.content_hash, "PAPER", "operator:abhijit")
    for d in (13, 14, 15):
        full_day(desk, clock, fake, d)
    at(desk, clock, fake, 16, 9, 30)
    desk.live.app.approve_risk_config("operator:abhijit")
    desk.authorise_live("operator:abhijit")
    return desk.set_mode(sv.content_hash, "LIVE", "operator:abhijit", confirm=CONFIRM, reason="72h clean")


def test_full_path_to_live_and_back(env):
    desk, clock, fake, _ = env
    sv = freeze(desk)
    t = go_live(desk, clock, fake, sv)
    assert t.mode is StrategyMode.LIVE
    h = sv.content_hash
    assert h in desk.live.app.intraday.active and h not in desk.paper.app.intraday.active
    g = desk.gate(h)
    assert g.passed and g.clean_sessions == 3

    # a live position opened by this strategy...
    at(desk, clock, fake, 16, 10, 0)
    r = desk.live.app.execution.execute(req(qty=1, stop="490", cid="id-desk00000001"))
    assert r.order is not None, r.message
    fake.execute(r.order.broker_order_id, 1, 500.0)
    desk.live.app.execution.sync()
    assert desk.live.app.portfolio.position("ALPHA", TradeMode.INTRADAY).quantity == 1

    # ...switching down is immediate: no new live entries, back in paper
    desk.set_mode(h, "PAPER", "operator:abhijit", reason="pull back")
    assert h not in desk.live.app.intraday.active and h in desk.paper.app.intraday.active

    # master off: exits of real positions still go through at square-off
    desk.revoke_live("operator:abhijit")
    at(desk, clock, fake, 16, 15, 1)
    exits = [o for o in desk.live.app.execution.orders.values() if o.request.is_exit]
    assert exits and exits[0].status is not OrderStatus.REJECTED
    assert exits[0].broker_order_id in fake.orders


def test_master_off_drops_live_strategies_to_paper(env):
    desk, clock, fake, _ = env
    sv = freeze(desk)
    go_live(desk, clock, fake, sv)
    desk.revoke_live("operator:abhijit", "testing")
    assert desk.mode(sv.content_hash) is StrategyMode.PAPER
    assert not desk.live_master_on()


def test_restart_restores_toggles_but_never_live(env):
    desk, clock, fake, s = env
    sv = freeze(desk)
    go_live(desk, clock, fake, sv)
    other = freeze(desk)
    desk.stop()
    desk2 = build_desk(s, clock=clock, groww_client=fake, research_loader=lambda: {}, notifier=lambda a: None)
    assert desk2.mode(sv.content_hash) is StrategyMode.PAPER  # live auth is gone after restart
    assert desk2.mode(other.content_hash) is StrategyMode.OFF
    assert desk2.toggles[sv.content_hash].reason == "live not authorised after restart"


def test_swing_routing(env):
    desk, clock, fake, _ = env
    assert desk.swing_book() is None
    desk.set_mode(SWING_KEY, "PAPER", "operator:abhijit")
    assert desk.swing_book() is desk.paper
    with pytest.raises(ToggleError):
        desk.set_mode(SWING_KEY, "LIVE", "operator:abhijit", confirm=CONFIRM)


def test_desk_api(env):
    desk, clock, fake, _ = env
    sv = freeze(desk)
    at(desk, clock, fake, 13, 10, 0)
    c = TestClient(create_app(desk, token="desk-token"))
    ov = c.get("/api/desk", headers=AUTH).json()
    assert ov["books"] == ["paper", "live"] and ov["live_master"] is False
    assert {r["key"] for r in ov["strategies"]} == {SWING_KEY, sv.content_hash}
    r = c.post(f"/api/strategies/{sv.content_hash}/mode", json={"mode": "PAPER", "reason": "trial"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["toggle"]["changed_by"] == "operator:abhijit"
    bad = c.post(f"/api/strategies/{sv.content_hash}/mode", json={"mode": "LIVE", "confirm": CONFIRM}, headers=AUTH)
    assert bad.status_code == 409 and "cannot go LIVE" in bad.json()["detail"]
    d = c.get("/api/dashboard?book=paper", headers=AUTH).json()
    assert d["book"] == "paper" and d["intraday"]["strategies"][0]["label"] == sv.label
    assert c.get("/api/dashboard?book=live", headers=AUTH).json()["mode"] == "LIVE"
    assert c.get("/api/dashboard?book=nope", headers=AUTH).status_code == 404
    assert c.post("/api/controls/kill-all", json={"reason": "drill"}, headers=AUTH).status_code == 200
    assert desk.paper.app.controls.kill_switch and desk.live.app.controls.kill_switch
    gate = c.get(f"/api/strategies/{sv.content_hash}/gate", headers=AUTH).json()
    assert gate["passed"] is False and gate["live_blockers"]
