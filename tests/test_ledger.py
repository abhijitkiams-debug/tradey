from datetime import datetime
from decimal import Decimal as D

import pytest

from claudetrade.ledger import CapitalLedger, DuplicateReservation, InsufficientCapital
from claudetrade.models import IST, TradeMode


@pytest.fixture
def ledger():
    led = CapitalLedger(lambda: datetime(2026, 10, 13, 10, tzinfo=IST))
    led.deposit(D("10000"))
    return led


def test_reservations_reduce_available(ledger):
    ledger.reserve("a", D("4000"), TradeMode.INTRADAY)
    ledger.reserve("b", D("3000"), TradeMode.SWING)
    assert ledger.available() == D("3000")
    assert ledger.reserved(TradeMode.SWING) == D("3000")


def test_cannot_over_allocate(ledger):
    ledger.reserve("a", D("8000"), TradeMode.INTRADAY)
    with pytest.raises(InsufficientCapital):
        ledger.reserve("b", D("2001"), TradeMode.SWING)


def test_same_order_cannot_reserve_twice(ledger):
    ledger.reserve("a", D("100"), TradeMode.INTRADAY)
    with pytest.raises(DuplicateReservation):
        ledger.reserve("a", D("100"), TradeMode.INTRADAY)


def test_fill_consumes_reservation_and_release_returns_rest(ledger):
    ledger.reserve("a", D("2600"), TradeMode.INTRADAY)
    ledger.record_buy_fill("a", D("2500"), D("3.5"), TradeMode.INTRADAY)
    assert ledger.cash() == D("7496.5")
    assert ledger.reservations()["a"] == D("96.5")
    ledger.release("a")
    assert ledger.available() == D("7496.5")


def test_ledger_is_append_only(ledger):
    n = len(ledger.events())
    ledger.adjust(D("-5"), "broker reported extra charge")
    assert len(ledger.events()) == n + 1
    assert ledger.cash() == D("9995")
