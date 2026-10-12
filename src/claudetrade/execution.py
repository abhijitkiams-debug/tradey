"""Execution gateway and order management (§E, §FR-09, §14).

``ExecutionService.execute`` is the only path to a broker. It runs the Risk Engine
itself (callers cannot hand it a pre-made approval), reserves capital atomically, submits
with the client request id as the broker-side reference, and treats a timeout as
"outcome unknown" — reconciling with the broker before anything else may happen.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .audit import AuditLog
from .broker.base import BrokerAdapter, BrokerError, BrokerRejected, BrokerTimeout, BrokerUnavailable
from .ledger import CapitalLedger, DuplicateReservation, InsufficientCapital
from .models import ZERO, Order, OrderRequest, OrderStatus, Product, RiskDecision, Side, TradeMode
from .portfolio import Portfolio
from .risk import RiskEngine


@dataclass(frozen=True)
class ExecutionResult:
    order: Order | None
    decision: RiskDecision
    message: str

    @property
    def accepted(self) -> bool:
        return self.order is not None and self.order.status not in (OrderStatus.REJECTED,)


class ExecutionService:
    def __init__(
        self,
        broker: BrokerAdapter,
        risk: RiskEngine,
        ledger: CapitalLedger,
        portfolio: Portfolio,
        audit: AuditLog,
        clock: Callable[[], datetime],
        alert: Callable[[str, str], None],
    ):
        self.broker = broker
        self.risk = risk
        self.ledger = ledger
        self.portfolio = portfolio
        self.audit = audit
        self.clock = clock
        self.alert = alert
        self.orders: dict[str, Order] = {}
        self.decisions: dict[str, RiskDecision] = {}
        self._lock = threading.RLock()
        self._seen_fill_ids: set[str] = set()
        self.reconciliation_ok = True
        self.mismatches: list[str] = []

    # wired into the RiskEngine
    def seen_request_ids(self) -> set[str]:
        return set(self.orders) | set(self.decisions)

    def pending_entry_value(self) -> Decimal:
        total = ZERO
        for o in self.orders.values():
            if not o.request.is_exit and not o.status.is_terminal:
                px = o.request.limit_price
                if px is None:
                    qv = self.risk.md.validate_quote(o.request.symbol, o.request.mode)
                    px = qv.quote.ltp if qv.quote else ZERO
                total += px * o.remaining_quantity
        return total

    # ------------------------------------------------------------------------------------
    def execute(self, req: OrderRequest) -> ExecutionResult:
        with self._lock:
            decision = self.risk.validate(req)
            self.audit.record(req.origin, "RISK_DECISION", request_id=req.client_request_id, approved=decision.approved,
                              reasons=decision.rejection_reasons, fingerprint=decision.request_fingerprint,
                              symbol=req.symbol, side=req.side.value, qty=req.quantity, mode=req.mode.value)
            if req.client_request_id not in self.decisions:
                self.decisions[req.client_request_id] = decision
            if not decision.approved:
                return ExecutionResult(None, decision, "rejected by risk engine: " + "; ".join(decision.rejection_reasons))

            if not req.is_exit:
                try:
                    self.ledger.reserve(req.client_request_id, decision.reserved_amount, req.mode)
                except (InsufficientCapital, DuplicateReservation) as e:
                    self.audit.record("execution", "RESERVE_FAILED", request_id=req.client_request_id, error=str(e))
                    return ExecutionResult(None, decision, f"capital reservation failed: {e}")

            now = self.clock()
            order = Order(req.client_request_id, req, OrderStatus.PENDING, created_at=now, updated_at=now)
            self.orders[req.client_request_id] = order
            try:
                order.broker_order_id = self.broker.place_order(req)
                order.status = OrderStatus.OPEN
                msg = "submitted"
            except BrokerRejected as e:
                self._terminal(order, OrderStatus.REJECTED, str(e))
                msg = f"broker rejected: {e}"
            except BrokerUnavailable as e:
                self._terminal(order, OrderStatus.REJECTED, f"not sent: {e}")
                msg = f"broker unavailable: {e}"
            except BrokerTimeout as e:
                order.status = OrderStatus.UNKNOWN
                self.audit.record("execution", "ORDER_TIMEOUT", request_id=req.client_request_id, error=str(e))
                self.reconcile_order(order)  # never resubmit blindly
                msg = f"timeout; reconciled status {order.status.value}"
            order.updated_at = self.clock()
            self.audit.record("execution", "ORDER_SUBMIT", request_id=req.client_request_id,
                              broker_order_id=order.broker_order_id, status=order.status.value, message=msg)
            self.sync()
            return ExecutionResult(order, decision, msg)

    def reconcile_order(self, order: Order) -> None:
        """Resolve an UNKNOWN submission from the broker's own records."""
        try:
            state = self.broker.find_by_client_id(order.client_request_id)
        except BrokerError as e:
            self.reconciliation_ok = False
            self.alert("HIGH", f"cannot reconcile {order.client_request_id}: {e}; new entries blocked")
            return
        if state is None:
            # Broker has no record under our reference: the request never arrived.
            self._terminal(order, OrderStatus.REJECTED, "not received by broker (reconciled after timeout)")
            return
        order.broker_order_id = state.broker_order_id
        # Leave it non-terminal so the next sync adopts the broker status *and* applies fills.
        order.status = OrderStatus.OPEN
        self.audit.record("execution", "ORDER_RECONCILED", request_id=order.client_request_id,
                          broker_order_id=state.broker_order_id, status=state.status.value)

    def cancel(self, client_request_id: str, actor: str = "system") -> str:
        with self._lock:
            o = self.orders.get(client_request_id)
            if o is None:
                return "unknown order"
            if o.status.is_terminal:
                return f"already {o.status.value}"
            if o.broker_order_id is None:
                return "no broker order id; reconcile first"
            try:
                self.broker.cancel_order(o.broker_order_id)
            except BrokerError as e:
                self.audit.record(actor, "CANCEL_FAILED", request_id=client_request_id, error=str(e))
                return f"cancel failed: {e}"
            self.audit.record(actor, "CANCEL_REQUESTED", request_id=client_request_id)
            self.sync()
            return f"status {o.status.value}"

    # ------------------------------------------------------------------------------------
    def sync(self) -> None:
        """Pull order states and fills from the broker; apply new fills exactly once."""
        with self._lock:
            for o in list(self.orders.values()):
                if o.status.is_terminal:
                    continue
                if o.status is OrderStatus.UNKNOWN:
                    self.reconcile_order(o)
                if o.broker_order_id is None:
                    continue
                try:
                    st = self.broker.get_order(o.broker_order_id)
                except BrokerError as e:
                    self.alert("HIGH", f"order sync failed: {e}")
                    return
                if st is None:
                    continue
                for f in st.fills:
                    if f.fill_id in self._seen_fill_ids:
                        continue
                    self._seen_fill_ids.add(f.fill_id)
                    self._apply_fill(o, f)
                o.filled_quantity = st.filled_quantity
                o.average_price = st.average_price
                if st.status != o.status:
                    o.status = st.status
                    o.reject_reason = st.reject_reason
                    o.updated_at = self.clock()
                if o.status.is_terminal:
                    released = self.ledger.release(o.client_request_id, note=o.status.value)
                    if released:
                        self.audit.record("execution", "RESERVATION_RELEASED", request_id=o.client_request_id, amount=released)

    def _apply_fill(self, o: Order, f) -> None:
        mode = o.request.mode
        gross = f.price * f.quantity
        if f.side is Side.BUY:
            self.ledger.record_buy_fill(o.client_request_id, gross, f.charges, mode)
        else:
            self.ledger.record_sell_fill(o.client_request_id, gross, f.charges, mode)
        realised = self.portfolio.apply_fill(f, mode)
        pos = self.portfolio.position(f.symbol, mode)
        if not o.request.is_exit and o.request.stop_loss is not None:
            pos.stop_loss = o.request.stop_loss
        self.audit.record("broker", "FILL", request_id=o.client_request_id, fill_id=f.fill_id, symbol=f.symbol,
                          side=f.side.value, qty=f.quantity, price=f.price, charges=f.charges, realised=realised)

    def _terminal(self, order: Order, status: OrderStatus, reason: str) -> None:
        order.status = status
        order.reject_reason = reason
        order.updated_at = self.clock()
        self.ledger.release(order.client_request_id, note=reason)
        self.audit.record("execution", f"ORDER_{status.value}", request_id=order.client_request_id, reason=reason)

    # ------------------------------------------------------------------------------------
    def reconcile_positions(self) -> list[str]:
        """Compare the local book with broker-reported positions (§FR-09, §14 capital mismatch)."""
        with self._lock:
            try:
                broker_pos = self.broker.get_positions()
            except BrokerError as e:
                self.reconciliation_ok = False
                self.alert("HIGH", f"position reconciliation failed: {e}")
                return [str(e)]
            local: dict[tuple[str, Product], int] = {}
            for p in self.portfolio.open_positions():
                key = (p.symbol, Product.MIS if p.mode is TradeMode.INTRADAY else Product.CNC)
                local[key] = local.get(key, 0) + p.quantity
            mismatches = [
                f"{sym}/{prod.value}: local {local.get((sym, prod), 0)} vs broker {broker_pos.get((sym, prod), 0)}"
                for sym, prod in set(local) | set(broker_pos)
                if local.get((sym, prod), 0) != broker_pos.get((sym, prod), 0)
            ]
            unresolved = [o.client_request_id for o in self.orders.values() if o.status is OrderStatus.UNKNOWN]
            mismatches += [f"order {cid} status unknown" for cid in unresolved]
            self.mismatches = mismatches
            self.reconciliation_ok = not mismatches
            self.portfolio.mark(self.clock())
            self.audit.record("execution", "RECONCILIATION", ok=self.reconciliation_ok, mismatches=mismatches)
            if mismatches:
                self.alert("HIGH", "reconciliation mismatch; new entries blocked: " + "; ".join(mismatches))
            return mismatches

    def open_orders(self, mode: TradeMode | None = None) -> list[Order]:
        return [o for o in self.orders.values() if not o.status.is_terminal and (mode is None or o.request.mode == mode)]
