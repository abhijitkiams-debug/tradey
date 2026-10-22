import sqlite3
from datetime import datetime

from conftest import fill, req
from fake_groww import FakeGroww
from fastapi.testclient import TestClient

from claudetrade.broker.groww import RateLimiter
from claudetrade.clock import SimClock
from claudetrade.evidence import LiveGate, evaluate
from claudetrade.models import IST, OperatingMode, TradeMode
from claudetrade.notify import Notifier, format_alert
from claudetrade.runtime import build_runtime
from claudetrade.server import LIVE_CONFIRMATION, create_app
from claudetrade.settings import Settings

AUTH = {"Authorization": "Bearer tok-123456"}
PRICES = {"ALPHA": 500, "CHARLIE": 250}


def quotes(fake, clock):
    ms = int(clock().timestamp() * 1000)
    for s, px in PRICES.items():
        fake.quotes[s] = {"last_price": px, "bid_price": px - 0.05, "offer_price": px + 0.05, "volume": 10_000,
                          "day_change": 0.0, "last_trade_time": ms - 500}


def run_day(rt, clock, fake, day, trade=True, break_square_off=False):
    for h, m in ((9, 25), (10, 0), (15, 1), (15, 2), (15, 11), (15, 20), (15, 21), (15, 40)):
        clock.set(datetime(2026, 10, day, h, m, tzinfo=IST))
        quotes(fake, clock)
        if break_square_off and (h, m) == (15, 1):
            rt.app.broker.faults.disconnected = True
        if (h, m) == (15, 20):
            rt.app.broker.faults.disconnected = False
        rt.scheduler.run_pending()  # polls fresh quotes first
        if (h, m) == (10, 0) and trade:
            r = rt.app.execution.execute(req(qty=1, stop="490"))
            assert r.order is not None, r.message
            fill(rt.app)


def paper(tmp_path, days, dirty_day=None, notifier=None):
    clock = SimClock(datetime(2026, 10, days[0], 8, 0, tzinfo=IST))
    fake = FakeGroww()
    s = Settings(db_path=str(tmp_path / "paper.db"), universe=tuple(PRICES), default_avg_daily_volume=1_000_000)
    rt = build_runtime(s, clock=clock, groww_client=fake, research_loader=lambda: {}, notifier=notifier or (lambda a: None))
    for d in days:
        run_day(rt, clock, fake, d, break_square_off=(d == dirty_day))
    return rt, clock


def gate_for(tmp_path, **kw):
    return LiveGate(paper_evidence_db=str(tmp_path / "paper.db"), **kw)


def test_three_clean_sessions_over_72h_pass(tmp_path):
    rt, clock = paper(tmp_path, [13, 14, 15])
    rt.stop()
    clock.set(datetime(2026, 10, 16, 9, 0, tzinfo=IST))
    res = evaluate(gate_for(tmp_path), clock(), alert_channel_configured=True)
    assert res.passed, res.reasons
    assert res.clean_sessions == 3 and res.paper_hours >= 72
    assert all(s["orders"] >= 1 for s in res.sessions)


def test_under_72_hours_fails(tmp_path):
    rt, clock = paper(tmp_path, [13, 14, 15])
    rt.stop()
    clock.set(datetime(2026, 10, 15, 16, 0, tzinfo=IST))  # 56 h after first start
    res = evaluate(gate_for(tmp_path), clock(), True)
    assert not res.passed and any("72 h required" in r for r in res.reasons)


def test_unclean_recent_session_fails(tmp_path):
    rt, clock = paper(tmp_path, [13, 14, 15], dirty_day=15)
    rt.stop()
    clock.set(datetime(2026, 10, 16, 9, 0, tzinfo=IST))
    res = evaluate(gate_for(tmp_path), clock(), True)
    assert not res.passed and any("NOT clean" in r for r in res.reasons)
    assert [s["clean"] for s in res.sessions] == [True, True, False]


def test_no_alert_channel_or_evidence_fails(tmp_path):
    clock = datetime(2026, 10, 16, 9, 0, tzinfo=IST)
    res = evaluate(LiveGate(), clock, False)
    assert not res.passed and len(res.reasons) == 2
    assert not evaluate(gate_for(tmp_path), clock, True).passed  # file missing


def test_editing_snapshot_table_does_not_fool_gate(tmp_path):
    rt, clock = paper(tmp_path, [13, 14, 15], dirty_day=15)
    rt.stop()
    conn = sqlite3.connect(tmp_path / "paper.db")
    conn.execute("DROP TRIGGER records_no_update")
    conn.execute("UPDATE records SET body=replace(body, '\"square_off_failed\":true', '\"square_off_failed\":false')")
    conn.commit()
    conn.close()
    clock.set(datetime(2026, 10, 16, 9, 0, tzinfo=IST))
    assert not evaluate(gate_for(tmp_path), clock(), True).passed


def test_tampered_evidence_rejected(tmp_path):
    rt, clock = paper(tmp_path, [13, 14, 15], dirty_day=15)
    rt.stop()
    conn = sqlite3.connect(tmp_path / "paper.db")
    conn.execute("DROP TRIGGER audit_no_update")
    conn.execute("UPDATE audit SET payload=replace(payload, '\"square_off_failed\":true', '\"square_off_failed\":false') "
                 "WHERE action='EOD_REPORT'")
    conn.commit()
    conn.close()
    clock.set(datetime(2026, 10, 16, 9, 0, tzinfo=IST))
    res = evaluate(gate_for(tmp_path), clock(), True)
    assert not res.passed and "does not verify" in res.reasons[0]


def test_live_authorisation_end_to_end(tmp_path):
    prt, clock = paper(tmp_path, [13, 14, 15])
    prt.stop()
    clock.set(datetime(2026, 10, 16, 9, 30, tzinfo=IST))
    fake = FakeGroww()
    sent = []
    s = Settings(mode=OperatingMode.LIVE, db_path=str(tmp_path / "live.db"), universe=tuple(PRICES),
                 default_avg_daily_volume=1_000_000, data_source="none", live_gate=gate_for(tmp_path))
    rt = build_runtime(s, clock=clock, groww_client=fake, research_loader=lambda: {}, notifier=sent.append)
    rt.app.broker.limiter = RateLimiter(sleep=lambda s: None)
    c = TestClient(create_app(rt, token="tok-123456"))
    assert c.get("/api/live/gate", headers=AUTH).json()["passed"]
    c.post("/api/risk/approve", headers=AUTH)
    assert c.post("/api/live/authorise", json={"confirm": LIVE_CONFIRMATION}, headers=AUTH).json()["live_authorised"]
    assert rt.app.audit.events("LIVE_GATE_CHECK")[-1].payload["passed"]


def test_critical_alert_repeats_until_acknowledged(tmp_path):
    sent = []
    rt, clock = paper(tmp_path, [13], notifier=sent.append)
    clock.set(datetime(2026, 10, 13, 16, 0, tzinfo=IST))
    a = rt.app.monitor.alert("CRITICAL", "test emergency")
    n0 = len(sent)
    clock.set(datetime(2026, 10, 13, 16, 1, tzinfo=IST))
    rt.scheduler.run_pending()
    assert len(sent) == n0  # not yet: repeats every 2 minutes
    clock.set(datetime(2026, 10, 13, 16, 3, tzinfo=IST))
    assert "escalate" in rt.scheduler.run_pending()
    assert a.sends >= 2
    c = TestClient(create_app(rt, token="tok-123456"))
    assert c.get("/api/dashboard", headers=AUTH).json()["operations"]["unacked_critical"]
    assert c.post(f"/api/alerts/{a.alert_id}/ack", headers=AUTH).json()["acked_by"].startswith("operator:")
    clock.set(datetime(2026, 10, 13, 16, 10, tzinfo=IST))
    assert "escalate" not in rt.scheduler.run_pending()


def test_square_off_failure_escalates_once_with_instructions(tmp_path):
    sent = []
    rt, clock = paper(tmp_path, [13], dirty_day=13, notifier=sent.append)
    crit = [a for a in rt.app.monitor.alerts if a.severity == "CRITICAL" and "SQUARE-OFF FAILED" in a.message]
    assert len(crit) == 1  # escalated once, not every tick
    assert "Exit each one at market" in crit[0].message and "ALPHA" in crit[0].message
    assert rt.app.controls.kill_switch
    # flat again after reconnect -> INFO follow-up, kill switch stays on
    assert any("now verified FLAT" in a.message for a in rt.app.monitor.alerts)
    assert rt.app.portfolio.is_flat(TradeMode.INTRADAY)
    report = rt.app.persistence.store.latest("eod_report")["2026-10-13"]
    assert report["verified_flat"] and report["square_off_failed"]  # flat later, but the day still counts as failed


def test_unacked_alert_survives_restart(tmp_path):
    rt, clock = paper(tmp_path, [13])
    a = rt.app.monitor.alert("CRITICAL", "still open")
    rt.stop()
    sent = []
    rt2 = build_runtime(rt.settings, clock=clock, groww_client=FakeGroww(), research_loader=lambda: {}, notifier=sent.append)
    assert [x.alert_id for x in rt2.app.monitor.unacked_critical()] == [a.alert_id]
    rt2.scheduler.run_pending()
    assert sent and sent[-1].alert_id == a.alert_id  # re-sent promptly after restart


def test_notifier_filters_and_survives_channel_failure():
    from claudetrade.monitoring import Alert

    got, errors = [], []

    def broken(text, alert):
        raise OSError("telegram down")

    n = Notifier([broken, lambda t, a: got.append(t)], "HIGH", "CT", on_error=lambda ch, e: errors.append(ch), background=False)
    n(Alert(datetime(2026, 10, 13, 15, 10, tzinfo=IST), "INFO", "fyi", "al-1"))
    a = Alert(datetime(2026, 10, 13, 15, 10, tzinfo=IST), "CRITICAL", "act now", "al-2", sends=3)
    n(a)
    assert len(got) == 1 and "repeat 2" in got[0] and "al-2" in got[0] and errors == ["broken"]
    assert "CRITICAL" in format_alert(a, "CT")


def test_operator_limits_in_example_config():
    from claudetrade.settings import load_settings

    s = load_settings("config/claudetrade.example.toml")
    assert (str(s.risk.risk_per_trade), str(s.risk.max_daily_loss), str(s.risk.max_order_value)) == ("100", "300", "1000")
    assert s.live_gate.min_paper_hours == 72 and s.live_gate.min_clean_sessions == 3


def test_send_test_alert(tmp_path):
    sent = []
    rt, clock = paper(tmp_path, [13], notifier=sent.append)
    c = TestClient(create_app(rt, token="tok-123456"))
    assert c.post("/api/alerts/test", headers=AUTH).json()["ok"] and "Test alert" in sent[-1].message
    rt.app.alert_channel_configured = False
    assert c.post("/api/alerts/test", headers=AUTH).status_code == 409
