import os
import stat
from datetime import datetime

import pytest
from fake_groww import FakeGroww
from fastapi.testclient import TestClient

from claudetrade import secrets_store, simple
from claudetrade.clock import SimClock
from claudetrade.desk import StrategyMode, build_desk
from claudetrade.models import IST
from claudetrade.server import create_app
from claudetrade.settings import Settings

AUTH = {"Authorization": "Bearer kid-token"}


@pytest.fixture(autouse=True)
def secrets_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDETRADE_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setenv("CLAUDETRADE_RESTART_ON_SETUP", "0")
    for k in ("GROWW_API_KEY", "GROWW_TOTP_SECRET", "GROWW_API_SECRET", "GROWW_ACCESS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    simple._cache.clear()


def make(tmp_path, at=(10, 0), setup_needed=False):
    from test_desk import freeze

    clock = SimClock(datetime(2026, 11, 3, *at, tzinfo=IST))
    s = Settings(paper_db_path=str(tmp_path / "p.db"), universe=("ALPHA", "CHARLIE"), default_avg_daily_volume=1_000_000)
    desk = build_desk(s, clock=clock, groww_client=FakeGroww(), research_loader=lambda: {}, notifier=lambda a: None)
    desk.setup_needed = setup_needed
    freeze(desk)
    return desk, clock, TestClient(create_app(desk, token="kid-token"))


def test_plain_status_and_checks(tmp_path):
    desk, clock, c = make(tmp_path, at=(18, 0))
    data = c.get("/api/simple", headers=AUTH).json()
    sm = data["summary"]
    assert sm["headline"] == "Not practising yet. Press “Start practice”."
    assert sm["market"] == "Market is closed. It opens tomorrow at 9:15 AM."
    assert sm["pretend_money"] == "₹10,000.00" and sm["real_money"] == "OFF (not set up)"
    titles = {i["title"]: i["ok"] for i in data["checks"]}
    assert titles["Groww connected"] is True and titles["Practice strategies switched on"] is False


def test_big_buttons(tmp_path):
    desk, clock, c = make(tmp_path)
    r = c.post("/api/simple/start", headers=AUTH).json()
    assert r["message"].startswith("Practice is on.")
    assert any(t.mode is StrategyMode.PAPER for t in desk.toggles.values())
    assert c.post("/api/simple/pause", headers=AUTH).json()["message"].startswith("Paused")
    assert desk.paper.app.controls.entries_paused
    assert c.get("/api/simple", headers=AUTH).json()["summary"]["tone"] == "warn"
    c.post("/api/simple/resume", headers=AUTH)
    assert not desk.paper.app.controls.entries_paused
    c.post("/api/simple/emergency", headers=AUTH)
    assert desk.paper.app.controls.kill_switch
    assert "Clear emergency stop" in c.post("/api/simple/resume", headers=AUTH).json()["message"]
    c.post("/api/simple/clear-stop", headers=AUTH)
    assert not desk.paper.app.controls.kill_switch
    assert c.post("/api/simple/nope", headers=AUTH).status_code == 404


def test_setup_from_browser(tmp_path, monkeypatch):
    desk, clock, c = make(tmp_path, setup_needed=True)
    sm = c.get("/api/simple", headers=AUTH).json()
    assert sm["summary"]["setup_needed"] and "connect your Groww" in sm["summary"]["headline"]
    import claudetrade.broker.groww_auth as ga

    monkeypatch.setattr(ga, "check_credentials", lambda k, s: (False, "Groww said no."))
    r = c.post("/api/setup/groww", json={"api_key": "x" * 50, "totp_secret": "JBSWY3DPEHPK3PXP"}, headers=AUTH).json()
    assert r == {"ok": False, "message": "Groww said no."} and not secrets_store.has_groww()
    monkeypatch.setattr(ga, "check_credentials", lambda k, s: (True, "Connected to Groww."))
    r = c.post("/api/setup/groww", json={"api_key": "eyJ" + "a" * 200, "totp_secret": "jbsw y3dp ehpk 3pxp"}, headers=AUTH).json()
    assert r["ok"] and secrets_store.has_groww()
    saved = secrets_store.load_groww()
    assert saved["totp_secret"] == "JBSWY3DPEHPK3PXP"
    path = os.path.join(os.environ["CLAUDETRADE_SECRETS_DIR"], "groww.json")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    audit = [e for e in desk.paper.app.audit.events("GROWW_CREDENTIALS_SAVED")]
    assert audit and "JBSW" not in str(audit[0].payload)
    assert c.post("/api/setup/groww", json={"api_key": "x" * 50, "totp_secret": "JBSWY3DPEHPK3PXP"}).status_code == 401


def test_browser_saved_login_wins_over_env(monkeypatch):
    pytest.importorskip("growwapi")
    pytest.importorskip("pyotp")
    from claudetrade.broker.groww_auth import login_from_env

    monkeypatch.setenv("GROWW_API_KEY", "old-key-from-env")
    monkeypatch.setenv("GROWW_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    secrets_store.save_groww("new-key-from-browser", "KRSXG5CTMVRXEZLU")
    _login, flow = login_from_env()
    assert flow == "totp" and _login.__closure__ and any(
        getattr(cell, "cell_contents", None) == "new-key-from-browser" for cell in _login.__closure__)


def test_credentials_shape_rejected_before_calling_groww():
    pytest.importorskip("pyotp")
    from claudetrade.broker.groww_auth import check_credentials

    ok, msg = check_credentials("short", "JBSWY3DPEHPK3PXP")
    assert not ok and "TOTP token" in msg
    ok, msg = check_credentials("eyJ" + "a" * 200 + ".b.c", "not-base32-1890")
    assert not ok and "TOTP secret" in msg


def test_practice_runs_in_background(tmp_path):
    import time

    from claudetrade.settings import ResearchSettings

    desk, clock, c = make(tmp_path, at=(18, 0))
    desk.settings = Settings(universe=("ALPHA", "BRAVO"), default_avg_daily_volume=1_000_000, data_source="none",
                             auto_paper=("ORB",), research=ResearchSettings(history_days=40, train_days=14, test_days=6))
    os.chdir(tmp_path)
    assert "takes about a minute" in c.post("/api/practice", json={"day": "2026-10-30"}, headers=AUTH).json()["message"]
    for _ in range(240):
        st = c.get("/api/simple", headers=AUTH).json()["practice"]
        if st["state"] != "running":
            break
        time.sleep(0.5)
    assert st["state"] == "done", st
    assert st["result"]["sentence"].startswith("On Friday 30 Oct the strategies made")
    assert (" made ₹" in st["result"]["sentence"]) == st["result"]["positive"]  # words match the colour
    assert st["result"]["safety"].startswith("✅")
    assert st["result"]["real_data"] is False
