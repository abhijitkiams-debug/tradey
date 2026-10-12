"""Shared capital ledger for intraday and swing (§FR-07).

Event-sourced and append-only: balances are always derived by replaying events, and a
correction is a new event. Every reservation is keyed by the order's ``client_request_id``,
so the same rupee can never back two orders. Reservations can only be created out of
genuinely uncommitted cash.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .models import ZERO, TradeMode


class InsufficientCapital(Exception):
    pass


class DuplicateReservation(Exception):
    pass


@dataclass(frozen=True)
class LedgerEvent:
    seq: int
    ts: datetime
    kind: str  # DEPOSIT | WITHDRAW | RESERVE | RELEASE | BUY_FILL | SELL_FILL | CHARGE | ADJUST
    amount: Decimal  # always positive; kind determines direction
    ref: str | None = None
    mode: TradeMode | None = None
    note: str = ""


class CapitalLedger:
    def __init__(self, clock: Callable[[], datetime]):
        self._clock = clock
        self._events: list[LedgerEvent] = []
        self._lock = threading.RLock()

    # ---- writes -------------------------------------------------------------------------
    def _append(self, kind: str, amount: Decimal, ref: str | None = None, mode: TradeMode | None = None, note: str = "") -> LedgerEvent:
        if amount < 0:
            raise ValueError("ledger amounts are non-negative; direction comes from kind")
        ev = LedgerEvent(len(self._events), self._clock(), kind, amount, ref, mode, note)
        self._events.append(ev)
        return ev

    def deposit(self, amount: Decimal, note: str = "") -> None:
        with self._lock:
            self._append("DEPOSIT", amount, note=note)

    def reserve(self, ref: str, amount: Decimal, mode: TradeMode) -> None:
        """Atomically earmark ``amount`` for order ``ref``. Fails rather than over-allocating."""
        with self._lock:
            if ref in self.reservations():
                raise DuplicateReservation(ref)
            if amount > self.available():
                raise InsufficientCapital(f"need {amount}, available {self.available()}")
            self._append("RESERVE", amount, ref, mode)

    def release(self, ref: str, note: str = "") -> Decimal:
        """Release whatever is still reserved for ``ref`` (cancel, reject, completed)."""
        with self._lock:
            outstanding = self.reservations().get(ref, ZERO)
            if outstanding > 0:
                self._append("RELEASE", outstanding, ref, note=note)
            return outstanding

    def record_buy_fill(self, ref: str, gross: Decimal, charges: Decimal, mode: TradeMode) -> None:
        with self._lock:
            # Consume the reservation first so the same money isn't counted twice.
            outstanding = self.reservations().get(ref, ZERO)
            consume = min(outstanding, gross + charges)
            if consume > 0:
                self._append("RELEASE", consume, ref, note="consumed by fill")
            self._append("BUY_FILL", gross, ref, mode)
            if charges > 0:
                self._append("CHARGE", charges, ref, mode)

    def record_sell_fill(self, ref: str, gross: Decimal, charges: Decimal, mode: TradeMode) -> None:
        with self._lock:
            outstanding = self.reservations().get(ref, ZERO)
            if outstanding > 0 and charges > 0:
                self._append("RELEASE", min(outstanding, charges), ref, note="consumed by fill")
            self._append("SELL_FILL", gross, ref, mode)
            if charges > 0:
                self._append("CHARGE", charges, ref, mode)

    def adjust(self, signed_amount: Decimal, note: str) -> None:
        """Traceable correction after reconciliation with the broker's funds view."""
        with self._lock:
            kind = "DEPOSIT" if signed_amount >= 0 else "WITHDRAW"
            self._append(kind, abs(signed_amount), note=f"ADJUST: {note}")

    # ---- reads --------------------------------------------------------------------------
    def cash(self) -> Decimal:
        total = ZERO
        for e in self._events:
            if e.kind in ("DEPOSIT", "SELL_FILL"):
                total += e.amount
            elif e.kind in ("WITHDRAW", "BUY_FILL", "CHARGE"):
                total -= e.amount
        return total

    def reservations(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for e in self._events:
            if e.kind == "RESERVE":
                out[e.ref] = out.get(e.ref, ZERO) + e.amount
            elif e.kind == "RELEASE":
                out[e.ref] = out.get(e.ref, ZERO) - e.amount
        return {k: v for k, v in out.items() if v > 0}

    def reserved(self, mode: TradeMode | None = None) -> Decimal:
        if mode is None:
            return sum(self.reservations().values(), ZERO)
        modes = {e.ref: e.mode for e in self._events if e.kind == "RESERVE"}
        return sum((v for k, v in self.reservations().items() if modes.get(k) == mode), ZERO)

    def available(self) -> Decimal:
        """Cash not already earmarked for pending orders. Never negative."""
        return max(self.cash() - self.reserved(), ZERO)

    def charges_paid(self) -> Decimal:
        return sum((e.amount for e in self._events if e.kind == "CHARGE"), ZERO)

    def events(self) -> list[LedgerEvent]:
        return list(self._events)
