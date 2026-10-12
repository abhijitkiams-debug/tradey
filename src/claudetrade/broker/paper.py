"""Simulated broker for paper trading and failure-injection tests (§16 Phase 2).

Fills are simulated against the current quote with configurable slippage. Fault injection
covers the scenarios the PRD requires us to survive: rejects, partial fills, timeouts where
the order *was* accepted, timeouts where it was not, and disconnects.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..costs import CostModel
from ..models import Fill, OrderRequest, OrderStatus, OrderType, Product, Quote, Side
from .base import BrokerOrderState, BrokerRejected, BrokerTimeout, BrokerUnavailable


@dataclass
class FaultPlan:
    reject_next: str | None = None
    timeout_next_accepted: bool = False  # order reaches broker, but the response is lost
    timeout_next_lost: bool = False  # request lost before reaching broker
    partial_fill_ratio: Decimal | None = None  # fill this fraction on next step, rest stays open
    disconnected: bool = False


@dataclass
class _PaperOrder:
    state: BrokerOrderState
    request: OrderRequest


class PaperBroker:
    name = "paper"

    def __init__(
        self,
        quote_source: Callable[[str], Quote | None],
        clock: Callable[[], datetime],
        costs: CostModel | None = None,
        slippage_bps: Decimal = Decimal("2"),
        starting_funds: Decimal = Decimal("10000"),
    ):
        self._quote = quote_source
        self._clock = clock
        self._costs = costs or CostModel()
        self.slippage_bps = slippage_bps
        self.faults = FaultPlan()
        self._orders: dict[str, _PaperOrder] = {}
        self._by_client: dict[str, str] = {}
        self._positions: dict[tuple[str, Product], int] = {}
        self._funds = starting_funds
        self._ids = itertools.count(1)
        self._fill_ids = itertools.count(1)

    # ---- adapter interface -------------------------------------------------------------
    def is_healthy(self) -> bool:
        return not self.faults.disconnected

    def place_order(self, request: OrderRequest) -> str:
        if self.faults.disconnected:
            raise BrokerUnavailable("paper broker disconnected")
        if self.faults.timeout_next_lost:
            self.faults.timeout_next_lost = False
            raise BrokerTimeout("request timed out (not delivered)")
        # Broker-side idempotency on the client reference
        if request.client_request_id in self._by_client:
            raise BrokerRejected(f"duplicate order reference {request.client_request_id}")
        boid = f"PB{next(self._ids):06d}"
        state = BrokerOrderState(boid, request.client_request_id, OrderStatus.OPEN, request.quantity, 0, Decimal("0"))
        if self.faults.reject_next:
            state.status = OrderStatus.REJECTED
            state.reject_reason = self.faults.reject_next
            self.faults.reject_next = None
        self._orders[boid] = _PaperOrder(state, request)
        self._by_client[request.client_request_id] = boid
        if self.faults.timeout_next_accepted:
            self.faults.timeout_next_accepted = False
            raise BrokerTimeout("response lost after broker accepted the order")
        if state.status is OrderStatus.REJECTED:
            raise BrokerRejected(state.reject_reason or "rejected")
        return boid

    def cancel_order(self, broker_order_id: str) -> None:
        if self.faults.disconnected:
            raise BrokerUnavailable("paper broker disconnected")
        o = self._orders.get(broker_order_id)
        if o is None:
            raise BrokerRejected("unknown order")
        if o.state.status.is_terminal:
            raise BrokerRejected(f"order already {o.state.status.value}")
        o.state.status = OrderStatus.CANCELLED

    def get_order(self, broker_order_id: str) -> BrokerOrderState | None:
        if self.faults.disconnected:
            raise BrokerUnavailable("paper broker disconnected")
        o = self._orders.get(broker_order_id)
        return o.state if o else None

    def find_by_client_id(self, client_request_id: str) -> BrokerOrderState | None:
        boid = self._by_client.get(client_request_id)
        return self.get_order(boid) if boid else None

    def list_orders(self) -> list[BrokerOrderState]:
        if self.faults.disconnected:
            raise BrokerUnavailable("paper broker disconnected")
        return [o.state for o in self._orders.values()]

    def get_positions(self) -> dict[tuple[str, Product], int]:
        if self.faults.disconnected:
            raise BrokerUnavailable("paper broker disconnected")
        return {k: v for k, v in self._positions.items() if v != 0}

    def get_available_funds(self) -> Decimal:
        return self._funds

    # ---- persistence (paper only: a real broker is the source of truth) --------------------
    def snapshot(self) -> dict:
        return {
            "orders": [{"state": o.state, "request": o.request} for o in self._orders.values()],
            "positions": [[sym, prod.value, q] for (sym, prod), q in self._positions.items()],
            "funds": self._funds,
            "next_id": len(self._orders) + 1,
            "next_fill": sum(len(o.state.fills) for o in self._orders.values()) + 1,
        }

    def restore(self, snap: dict, decode_order) -> None:
        self._orders.clear()
        self._by_client.clear()
        for item in snap["orders"]:
            state, request = decode_order(item)
            self._orders[state.broker_order_id] = _PaperOrder(state, request)
            self._by_client[state.client_request_id] = state.broker_order_id
        self._positions = {(sym, Product(prod)): q for sym, prod, q in snap["positions"]}
        self._funds = snap["funds"]
        self._ids = itertools.count(snap["next_id"])
        self._fill_ids = itertools.count(snap["next_fill"])

    # ---- simulation --------------------------------------------------------------------
    def step(self) -> list[Fill]:
        """Advance the simulated exchange: try to fill every working order once."""
        if self.faults.disconnected:
            return []
        fills: list[Fill] = []
        for o in self._orders.values():
            st = o.state
            if st.status not in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED):
                continue
            q = self._quote(o.request.symbol)
            if q is None:
                continue
            px = self._fill_price(o.request, q)
            if px is None:
                continue
            remaining = st.quantity - st.filled_quantity
            qty = remaining
            if self.faults.partial_fill_ratio is not None:
                qty = max(1, int(remaining * self.faults.partial_fill_ratio))
                self.faults.partial_fill_ratio = None
            fill = Fill(
                fill_id=f"F{next(self._fill_ids):07d}",
                client_request_id=st.client_request_id,
                broker_order_id=st.broker_order_id,
                symbol=o.request.symbol,
                side=o.request.side,
                quantity=qty,
                price=px,
                ts=self._clock(),
                charges=self._costs.charges(o.request.side, o.request.product, qty, px),
            )
            st.average_price = (st.average_price * st.filled_quantity + px * qty) / (st.filled_quantity + qty)
            st.filled_quantity += qty
            st.fills.append(fill)
            st.status = OrderStatus.COMPLETED if st.filled_quantity == st.quantity else OrderStatus.PARTIALLY_FILLED
            key = (o.request.symbol, o.request.product)
            signed = qty if o.request.side is Side.BUY else -qty
            self._positions[key] = self._positions.get(key, 0) + signed
            self._funds -= px * signed + fill.charges
            fills.append(fill)
        return fills

    def _fill_price(self, req: OrderRequest, q: Quote) -> Decimal | None:
        slip = self.slippage_bps / Decimal("10000")
        buy = req.side is Side.BUY
        if req.order_type in (OrderType.SL, OrderType.SL_M):
            trig = req.trigger_price
            triggered = (q.ltp >= trig) if buy else (q.ltp <= trig)
            if not triggered:
                return None
        if req.order_type in (OrderType.MARKET, OrderType.SL_M):
            base = q.ask if buy else q.bid
            return (base * (1 + slip if buy else 1 - slip)).quantize(Decimal("0.05"))
        limit = req.limit_price
        if buy and q.ask <= limit:
            return min(q.ask, limit)
        if not buy and q.bid >= limit:
            return max(q.bid, limit)
        return None
