import sqlite3
from decimal import Decimal as D

import pytest
from conftest import fill, req, set_quote
from test_swing import proposal

from claudetrade.app import ClaudeTrade
from claudetrade.models import Instrument, OrderStatus, TradeMode
from claudetrade.store import Store
from claudetrade.swing import ApprovalStatus


def build(clock, path):
    a = ClaudeTrade.build(clock=clock, store=Store(path))
    for sym in ("ALPHA", "BRAVO", "CHARLIE", "DELTA"):
        a.market_data.register(Instrument(sym), avg_daily_volume=1_000_000)
    for sym, px in (("ALPHA", "500"), ("BRAVO", "1000"), ("CHARLIE", "250"), ("DELTA", "800")):
        set_quote(a, sym, px)
    return a


@pytest.fixture
def db(tmp_path):
    return tmp_path / "ct.db"


def test_full_state_survives_restart(clock, db):
    a = build(clock, db)
    a.execution.execute(req(qty=5))
    fill(a)
    p = a.swing.submit_proposal(proposal(symbol="CHARLIE", entry_low=248, entry_high=252, stop_loss=242,
                                         target_low=270, target_high=280, quantity=8))
    ap = a.swing.approve(p.proposal_id, p.version, p.content_hash(), "owner")
    pending = a.swing.submit_proposal(proposal())
    a.monitor.activate_kill_switch("owner", "testing restart")
    before = a.dashboard()["portfolio"]
    a.persistence.store.close()

    b = build(clock, db)
    after = b.dashboard()["portfolio"]
    for k in ("cash", "available", "reserved", "committed", "realised_pnl_today", "charges_paid"):
        assert after[k] == before[k], k
    pos = b.portfolio.position("ALPHA", TradeMode.INTRADAY)
    assert pos.quantity == 5 and pos.stop_loss == D("490")
    assert b.controls.kill_switch is True                       # safety state survives restart
    assert b.live_auth.enabled is False                          # live never auto-restores
    assert b.swing.approvals[ap.approval_id].status is ApprovalStatus.ACTIVE
    assert b.swing.latest(pending.proposal_id).content_hash() == pending.content_hash()
    assert b.audit.verify() and b.audit.events("STARTUP")[-1].payload["restored"]["orders"] == 1
    assert b.execution.reconciliation_ok, b.execution.mismatches  # paper broker restored too
    # duplicate protection spans restarts
    old_id = next(iter(a.execution.orders))
    assert "not_duplicate" in b.execution.execute(req(cid=old_id)).message


def test_no_double_counting_after_restart(clock, db):
    a = build(clock, db)
    a.execution.execute(req(qty=5))
    fill(a)
    cash = a.ledger.cash()
    a.persistence.store.close()
    b = build(clock, db)
    b.execution.sync()
    b.broker.step()
    b.execution.sync()
    assert b.ledger.cash() == cash
    assert b.portfolio.position("ALPHA", TradeMode.INTRADAY).quantity == 5


def test_crash_mid_submission_reconciles_instead_of_resubmitting(clock, db):
    a = build(clock, db)

    def crash(_req):
        raise SystemExit("process killed during broker call")

    a.broker.place_order = crash
    with pytest.raises(SystemExit):
        a.execution.execute(req(qty=5, cid="crash-0001"))
    a.persistence.store.close()

    b = build(clock, db)
    o = b.execution.orders["crash-0001"]
    assert o.status is OrderStatus.REJECTED and "not received" in o.reject_reason
    assert b.ledger.reserved() == 0  # reservation released, nothing resubmitted
    assert not b.broker.list_orders()


def test_tables_are_append_only(db, clock):
    a = build(clock, db)
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE audit SET actor='x' WHERE seq=0")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM records")
    a.persistence.store.close()


def test_tampered_audit_refuses_to_start(db, clock):
    a = build(clock, db)
    a.persistence.store.close()
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER audit_no_update")
    conn.execute("UPDATE audit SET actor='mallory' WHERE seq=0")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="audit chain"):
        build(clock, db)


def test_altered_proposal_voids_approval_on_restore(db, clock):
    a = build(clock, db)
    p = a.swing.submit_proposal(proposal())
    ap = a.swing.approve(p.proposal_id, p.version, p.content_hash(), "owner")
    a.persistence.store.close()
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER records_no_update")
    conn.execute("UPDATE records SET body=replace(body, '\"quantity\":6', '\"quantity\":60') WHERE kind='proposal'")
    conn.commit()
    conn.close()
    b = build(clock, db)
    assert b.swing.approvals[ap.approval_id].status is ApprovalStatus.INVALIDATED
