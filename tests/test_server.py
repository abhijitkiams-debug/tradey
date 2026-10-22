from datetime import datetime

import pytest
from conftest import fill, set_quote
from fake_groww import FakeGroww
from fastapi.testclient import TestClient
from test_lifecycle import LENIENT
from test_swing import proposal

from claudetrade.backtest import BacktestConfig, walk_forward
from claudetrade.broker.groww import RateLimiter
from claudetrade.clock import SimClock
from claudetrade.models import IST, OperatingMode, OrderStatus
from claudetrade.runtime import build_runtime
from claudetrade.server import LIVE_CONFIRMATION, create_app
from claudetrade.settings import Settings
from claudetrade.strategies import MeanReversion
from claudetrade.synthetic import universe

TOKEN = "s3cret-operator-token"
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-Operator": "abhijit"}


def make(tmp_path, mode=OperatingMode.PAPER):
    clock = SimClock(datetime(2026, 10, 13, 10, 0, tzinfo=IST))
    fake = FakeGroww()
    s = Settings(mode=mode, db_path=str(tmp_path / "ct.db"), universe=("ALPHA", "CHARLIE", "BRAVO", "DELTA"),
                 default_avg_daily_volume=1_000_000, data_source="none")
    rt = build_runtime(s, clock=clock, groww_client=fake, research_loader=lambda: {})
    if mode is OperatingMode.LIVE:
        rt.app.broker.limiter = RateLimiter(sleep=lambda s: None)
    for sym, px in (("ALPHA", 500), ("CHARLIE", 250), ("BRAVO", 1000), ("DELTA", 800)):
        set_quote(rt.app, sym, px)
    return rt, TestClient(create_app(rt, token=TOKEN)), clock


@pytest.fixture
def env(tmp_path):
    return make(tmp_path)


def test_auth_required_and_fails_closed(env, tmp_path):
    rt, c, _ = env
    assert c.get("/healthz").status_code == 200
    assert c.get("/").status_code == 200
    assert c.get("/api/dashboard").status_code == 401
    assert c.get("/api/dashboard", headers={"Authorization": "Bearer wrong"}).status_code == 401
    no_token = TestClient(create_app(rt, token=""))
    assert no_token.get("/api/dashboard", headers=AUTH).status_code == 503
    assert c.get("/api/dashboard", headers=AUTH).json()["mode"] == "PAPER"


def test_swing_approval_flow_via_api(env):
    rt, c, _ = env
    p = rt.app.swing.submit_proposal(proposal())
    d = c.get("/api/dashboard", headers=AUTH).json()
    assert [x["id"] for x in d["swing"]["awaiting_approval"]] == [p.proposal_id]
    detail = c.get(f"/api/proposals/{p.proposal_id}", headers=AUTH).json()
    bad = c.post(f"/api/swing/{p.proposal_id}/approve", json={"version": 1, "hash": "0" * 64}, headers=AUTH)
    assert bad.status_code == 409
    ok = c.post(f"/api/swing/{p.proposal_id}/approve", json={"version": detail["version"], "hash": detail["hash"]}, headers=AUTH)
    assert ok.status_code == 200 and ok.json()["approver"] == "operator:abhijit"
    ex = c.post(f"/api/swing/approvals/{ok.json()['approval_id']}/execute", headers=AUTH).json()
    assert ex["ok"], ex
    fill(rt.app)
    assert rt.app.execution.orders[ex["client_request_id"]].status is OrderStatus.COMPLETED
    again = c.post(f"/api/swing/approvals/{ok.json()['approval_id']}/execute", headers=AUTH).json()
    assert again["ok"] is False and "CONSUMED" in again["message"]


def test_decline_removes_from_queue_and_persists(env, tmp_path):
    rt, c, clock = env
    p = rt.app.swing.submit_proposal(proposal())
    assert c.post(f"/api/swing/{p.proposal_id}/decline", json={"reason": "earnings next week"}, headers=AUTH).status_code == 200
    assert c.get("/api/dashboard", headers=AUTH).json()["swing"]["awaiting_approval"] == []
    rt.stop()
    rt2 = build_runtime(rt.settings, clock=clock, groww_client=FakeGroww(), research_loader=lambda: {})
    assert rt2.app.swing.declined == {p.proposal_id: "earnings next week"}


def test_kill_switch_and_resume_controls(env):
    rt, c, _ = env
    assert c.post("/api/controls/kill-switch", json={"reason": "manual test"}, headers=AUTH).json()["kill_switch"]
    assert rt.app.controls.kill_switch
    assert c.post("/api/controls/kill-switch/reset", json={"reason": "all clear"}, headers=AUTH).status_code == 200
    c.post("/api/controls/pause", json={"reason": "lunch", "mode": "INTRADAY"}, headers=AUTH)
    assert rt.app.controls.paused_modes
    c.post("/api/controls/resume", json={"reason": "back"}, headers=AUTH)
    assert not rt.app.controls.paused_modes
    actors = {e.actor for e in rt.app.audit.events() if e.action in ("KILL_SWITCH_ON", "ENTRIES_RESUMED")}
    assert actors == {"operator:abhijit"}


def test_strategy_release_via_api(env):
    rt, c, clock = env
    wf = walk_forward(MeanReversion, {"ALPHA": universe(20)["ALPHA"]}, BacktestConfig(), train_days=12, test_days=6,
                      min_train_trades=1)
    sv = rt.app.strategies.freeze(wf, LENIENT, rt.app.risk_config)
    r = c.post(f"/api/strategies/{sv.content_hash}/release", json={"session_date": "2026-10-14"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["approved_by"] == "operator:abhijit"
    versions = c.get("/api/strategies", headers=AUTH).json()["versions"]
    assert versions[0]["status"] == "ELIGIBLE"


def test_live_needs_live_process_approved_limits_and_typed_confirmation(env, tmp_path):
    _, c, _ = env
    assert c.post("/api/live/authorise", json={"confirm": LIVE_CONFIRMATION}, headers=AUTH).status_code == 409  # paper process

    rt, lc, _ = make(tmp_path / "live", OperatingMode.LIVE)
    assert lc.post("/api/live/authorise", json={"confirm": "yes"}, headers=AUTH).status_code == 400
    assert lc.post("/api/live/authorise", json={"confirm": LIVE_CONFIRMATION}, headers=AUTH).status_code == 409  # limits unapproved
    assert lc.post("/api/risk/approve", headers=AUTH).status_code == 200
    blocked = lc.post("/api/live/authorise", json={"confirm": LIVE_CONFIRMATION}, headers=AUTH)
    assert blocked.status_code == 409 and "live gate" in blocked.json()["detail"]  # no paper evidence yet
    rt.app.live_gate = None  # gate itself is covered in test_live_gate.py
    assert lc.post("/api/live/authorise", json={"confirm": LIVE_CONFIRMATION}, headers=AUTH).json()["live_authorised"]
    assert rt.app.live_auth.is_valid_for(rt.app.risk_config)
    lc.post("/api/live/revoke", headers=AUTH)
    assert not rt.app.live_auth.enabled


def test_audit_and_eod_endpoints(env):
    rt, c, clock = env
    rows = c.get("/api/audit?limit=5", headers=AUTH).json()
    assert rows and rows[-1]["seq"] == len(rt.app.audit.events()) - 1
    assert c.get("/api/eod/2026-10-13", headers=AUTH).status_code == 404
