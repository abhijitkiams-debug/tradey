"""Position book and P&L (§F). Positions are keyed by (symbol, mode) so intraday and swing
holdings in the same stock are tracked and squared off independently."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from .models import ZERO, Fill, Position, Side, TradeMode


class Portfolio:
    def __init__(self) -> None:
        self.positions: dict[tuple[str, TradeMode], Position] = {}
        self._realised_by_day: dict[date, Decimal] = {}
        self._seen_fills: set[str] = set()

    def position(self, symbol: str, mode: TradeMode) -> Position:
        key = (symbol, mode)
        if key not in self.positions:
            self.positions[key] = Position(symbol=symbol, mode=mode)
        return self.positions[key]

    def apply_fill(self, fill: Fill, mode: TradeMode) -> Decimal:
        """Update the position; return realised P&L (gross of charges) from this fill.
        Idempotent per fill_id so replays after reconnection don't double count."""
        if fill.fill_id in self._seen_fills:
            return ZERO
        self._seen_fills.add(fill.fill_id)
        pos = self.position(fill.symbol, mode)
        signed = fill.quantity if fill.side is Side.BUY else -fill.quantity
        realised = ZERO
        if pos.quantity == 0 or (pos.quantity > 0) == (signed > 0):
            new_qty = pos.quantity + signed
            pos.average_price = (pos.average_price * abs(pos.quantity) + fill.price * abs(signed)) / abs(new_qty)
            pos.quantity = new_qty
            if pos.opened_at is None:
                pos.opened_at = fill.ts
        else:
            closing = min(abs(signed), abs(pos.quantity))
            direction = 1 if pos.quantity > 0 else -1
            realised = (fill.price - pos.average_price) * closing * direction
            pos.quantity += signed
            if pos.quantity == 0:
                pos.average_price = ZERO
                pos.opened_at = None
                pos.stop_loss = None
                pos.target = None
            elif (pos.quantity > 0) != (direction > 0):
                # flipped through zero: remainder opens at fill price
                pos.average_price = fill.price
                pos.opened_at = fill.ts
        realised -= fill.charges
        pos.realised_pnl += realised
        day = fill.ts.date()
        self._realised_by_day[day] = self._realised_by_day.get(day, ZERO) + realised
        return realised

    def open_positions(self, mode: TradeMode | None = None) -> list[Position]:
        return [p for p in self.positions.values() if not p.is_flat and (mode is None or p.mode == mode)]

    def realised_pnl_on(self, day: date) -> Decimal:
        return self._realised_by_day.get(day, ZERO)

    def unrealised_pnl(self, prices: dict[str, Decimal]) -> Decimal:
        return sum((p.unrealised_pnl(prices[p.symbol]) for p in self.open_positions() if p.symbol in prices), ZERO)

    def gross_exposure(self, prices: dict[str, Decimal]) -> Decimal:
        total = ZERO
        for p in self.open_positions():
            px = prices.get(p.symbol, p.average_price)
            total += p.market_value(px)
        return total

    def committed_capital(self) -> Decimal:
        return sum((abs(p.quantity) * p.average_price for p in self.open_positions()), ZERO)

    def is_flat(self, mode: TradeMode) -> bool:
        return not self.open_positions(mode)

    def mark(self, ts: datetime) -> None:
        for p in self.positions.values():
            p.reconciled_at = ts
