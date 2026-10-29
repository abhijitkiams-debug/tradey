"""Groww broker adapter (§E, §FR-09) built on the official ``growwapi`` Python SDK.

Mapping decisions, from the SDK source (growwapi 1.5.0) and Groww's Trade API docs:

* Every order carries our ``client_request_id`` as Groww's ``order_reference_id``
  (8-20 chars, alphanumeric, at most two hyphens). After a timeout we look the order up with
  ``get_order_status_by_reference``; only a clear not-found, after a grace period, is treated
  as "never received". Anything else leaves the order UNKNOWN, which blocks new entries.
* Errors are classified as definitive rejections (bad request / FAILURE payload / rate-limited
  before processing) or ambiguous (timeouts, connection drops, 5xx). Ambiguous errors on
  submission raise ``BrokerTimeout`` so the execution layer reconciles instead of retrying.
* An order is reported terminal only once its trade list accounts for the filled quantity,
  so fills can never be dropped because Groww's trade list lags its status.
* Rate limits are enforced client-side per Groww's documented categories.

Still to verify against a real account before a live pilot (see README, "Live pilot
checklist"): reference look-up behaviour for unknown ids, positions/holdings quantity
semantics, trade-list key names, and charges (estimated locally until a contract note
source is wired).

Credentials come from the environment or a secrets manager at runtime. They are never
logged, put in prompts, or stored in the database.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..costs import CostModel
from ..models import IST, Fill, OrderRequest, OrderStatus, Product, Side
from .base import BrokerOrderState, BrokerRejected, BrokerTimeout, BrokerUnavailable

SEGMENT = "CASH"
REFERENCE_RE = re.compile(r"^[A-Za-z0-9-]{8,20}$")

OPEN_STATES = {"NEW", "ACKED", "TRIGGER_PENDING", "APPROVED", "CANCELLATION_REQUESTED", "MODIFICATION_REQUESTED"}
DONE_STATES = {"EXECUTED", "COMPLETED", "DELIVERY_AWAITED"}
REJECT_STATES = {"REJECTED", "FAILED"}


def valid_reference(ref: str) -> bool:
    return bool(REFERENCE_RE.match(ref)) and ref.count("-") <= 2


class RateLimiter:
    """Sliding-window limits per category: (per second, per minute)."""

    LIMITS = {"orders": (10, 250), "live": (10, 300), "non_trading": (20, 500)}

    def __init__(self, now: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self._now, self._sleep = now, sleep
        self._hits: dict[str, deque[float]] = {k: deque() for k in self.LIMITS}
        self._lock = threading.Lock()

    def acquire(self, category: str) -> None:
        per_s, per_m = self.LIMITS[category]
        while True:
            with self._lock:
                now = self._now()
                q = self._hits[category]
                while q and now - q[0] >= 60:
                    q.popleft()
                last_s = sum(1 for t in q if now - t < 1)
                if last_s < per_s and len(q) < per_m:
                    q.append(now)
                    return
                wait = (1 - (now - q[-per_s])) if last_s >= per_s else (60 - (now - q[0]))
            self._sleep(max(wait, 0.01))


def _err_kind(e: Exception) -> str:
    """Classify SDK / transport errors without importing the SDK (keeps it optional)."""
    name = type(e).__name__
    code = str(getattr(e, "code", ""))
    if name in ("GrowwAPITimeoutException", "Timeout", "ReadTimeout", "ConnectTimeout") or code == "504":
        return "ambiguous"
    if name in ("ConnectionError", "ChunkedEncodingError") or code.startswith("5"):
        return "ambiguous"
    if name in ("GrowwAPIAuthenticationException", "GrowwAPIAuthorisationException") or code in ("401", "403"):
        return "auth"
    if name == "GrowwAPIRateLimitException" or code == "429":
        return "rate_limited"
    if name == "GrowwAPINotFoundException" or code == "404":
        return "not_found"
    if name.startswith("GrowwAPI"):
        return "rejected"  # explicit FAILURE payload or 400: the broker refused it
    return "ambiguous"


class GrowwBroker:
    name = "groww"
    reference_lookup_grace_s = 30

    def __init__(self, client: Any, clock: Callable[[], datetime], costs: CostModel | None = None,
                 exchange: str = "NSE", timeout_s: int = 8, limiter: RateLimiter | None = None,
                 healthy_window_s: int = 90):
        self.client = client
        self.clock = clock
        self.costs = costs or CostModel()
        self.exchange = exchange
        self.timeout_s = timeout_s
        self.limiter = limiter or RateLimiter()
        self.healthy_window_s = healthy_window_s
        self._last_ok: datetime | None = None
        self._auth_failed = False
        self._products: dict[str, Product] = {}  # groww order id -> product, for charge estimates
        self.last_error: str | None = None

    # ---- construction from the environment ------------------------------------------------
    @classmethod
    def from_env(cls, clock: Callable[[], datetime], **kw: Any) -> GrowwBroker:
        """Credentials from GROWW_* env vars; the session re-logs in by itself (see groww_auth)."""
        from .groww_auth import RefreshingGrowwClient, login_from_env

        login, flow = login_from_env()
        return cls(RefreshingGrowwClient(login, clock, flow), clock, **kw)

    # ---- plumbing -------------------------------------------------------------------------
    def _call(self, category: str, fn: str, **kwargs: Any) -> dict:
        self.limiter.acquire(category)
        try:
            out = getattr(self.client, fn)(timeout=self.timeout_s, **kwargs)
        except Exception as e:
            kind = _err_kind(e)
            self.last_error = f"{fn}: {type(e).__name__}"  # never include payloads (may echo tokens)
            from .groww_trace import log_error

            log_error(fn, e)
            if kind == "auth":
                self._auth_failed = True
            e.kind = kind  # type: ignore[attr-defined]
            raise
        self._last_ok = self.clock()
        self._auth_failed = False
        return out or {}

    def is_healthy(self) -> bool:
        if self._auth_failed or self._last_ok is None:
            return False
        return (self.clock() - self._last_ok).total_seconds() <= self.healthy_window_s

    def heartbeat(self) -> bool:
        try:
            self._call("non_trading", "get_available_margin_details")
        except Exception:
            return False
        return True

    # ---- orders ---------------------------------------------------------------------------
    def place_order(self, request: OrderRequest) -> str:
        ref = request.client_request_id
        if not valid_reference(ref):
            raise BrokerRejected(f"client_request_id {ref!r} is not a valid Groww order_reference_id")
        if request.product not in (Product.MIS, Product.CNC):
            raise BrokerRejected(f"product {request.product} not permitted")
        try:
            resp = self._call(
                "orders", "place_order",
                validity="DAY", exchange=self.exchange, order_type=request.order_type.value,
                product=request.product.value, quantity=request.quantity, segment=SEGMENT,
                trading_symbol=request.symbol, transaction_type=request.side.value, order_reference_id=ref,
                price=float(request.limit_price) if request.limit_price is not None else 0.0,
                trigger_price=float(request.trigger_price) if request.trigger_price is not None else None,
            )
        except Exception as e:
            kind = getattr(e, "kind", "ambiguous")
            if kind in ("rejected",):
                raise BrokerRejected(str(getattr(e, "msg", e))) from None
            if kind in ("rate_limited", "auth"):
                raise BrokerUnavailable(f"not accepted: {kind}") from None
            raise BrokerTimeout(f"outcome unknown ({type(e).__name__})") from None
        boid = resp.get("groww_order_id")
        status = str(resp.get("order_status", "")).upper()
        if not boid:
            raise BrokerTimeout("no order id in response; reconcile by reference")
        self._products[boid] = request.product
        if status in REJECT_STATES:
            raise BrokerRejected(resp.get("remark") or status)
        return boid

    def cancel_order(self, broker_order_id: str) -> None:
        try:
            self._call("orders", "cancel_order", groww_order_id=broker_order_id, segment=SEGMENT)
        except Exception as e:
            if getattr(e, "kind", "") == "rejected":
                raise BrokerRejected(str(getattr(e, "msg", e))) from None
            raise BrokerUnavailable(f"cancel failed: {type(e).__name__}") from None

    def get_order(self, broker_order_id: str) -> BrokerOrderState | None:
        try:
            d = self._call("non_trading", "get_order_detail", segment=SEGMENT, groww_order_id=broker_order_id)
        except Exception as e:
            if getattr(e, "kind", "") == "not_found":
                return None
            raise BrokerUnavailable(f"order detail failed: {type(e).__name__}") from None
        return self._state_from_detail(d)

    def find_by_client_id(self, client_request_id: str) -> BrokerOrderState | None:
        try:
            s = self._call("non_trading", "get_order_status_by_reference", segment=SEGMENT,
                           order_reference_id=client_request_id)
        except Exception as e:
            if getattr(e, "kind", "") == "not_found":
                return None
            raise BrokerUnavailable(f"reference lookup failed: {type(e).__name__}") from None
        boid = s.get("groww_order_id")
        if not boid:
            return None
        return self.get_order(boid)

    def list_orders(self) -> list[BrokerOrderState]:
        out, page = [], 0
        while True:
            r = self._call("non_trading", "get_order_list", page=page, page_size=25, segment=SEGMENT)
            items = r.get("order_list") or []
            out += [self._state_from_detail(d, fetch_trades=False) for d in items]
            if len(items) < 25 or page > 40:
                return out
            page += 1

    def _trades(self, boid: str) -> list[dict]:
        out, page = [], 0
        while True:
            r = self._call("non_trading", "get_trade_list_for_order", groww_order_id=boid, segment=SEGMENT,
                           page=page, page_size=25)
            items = r.get("trade_list") or r.get("trades") or []
            out += items
            if len(items) < 25 or page > 40:
                return out
            page += 1

    def _state_from_detail(self, d: dict, fetch_trades: bool = True) -> BrokerOrderState:
        boid = d["groww_order_id"]
        qty = int(d.get("quantity") or 0)
        filled = int(d.get("filled_quantity") or 0)
        raw = str(d.get("order_status", "")).upper()
        product = Product(d["product"]) if d.get("product") in ("MIS", "CNC") else self._products.get(boid, Product.MIS)
        side = Side(d.get("transaction_type", "BUY"))
        fills: list[Fill] = []
        if fetch_trades and filled > 0:
            for t in self._trades(boid):
                tq, tp = int(t["quantity"]), Decimal(str(t["price"]))
                fills.append(Fill(
                    fill_id=f"G{t['groww_trade_id']}", client_request_id=d.get("order_reference_id") or "",
                    broker_order_id=boid, symbol=d.get("trading_symbol") or t.get("trading_symbol"), side=side,
                    quantity=tq, price=tp, ts=_parse_ts(t.get("trade_date_time") or t.get("created_at"), self.clock),
                    charges=self.costs.charges(side, product, tq, tp),  # estimate until contract notes are wired
                ))
        traded = sum(f.quantity for f in fills)
        if raw in REJECT_STATES:
            status = OrderStatus.REJECTED
        elif raw == "CANCELLED":
            status = OrderStatus.CANCELLED
        elif raw in DONE_STATES:
            status = OrderStatus.COMPLETED
        else:
            status = OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.OPEN
        if fetch_trades and status.is_terminal and traded < filled:
            # Trade list lags the order status: stay non-terminal so the missing fills get picked up.
            status = OrderStatus.PARTIALLY_FILLED
            filled = traded
        avg = Decimal(str(d.get("average_fill_price") or 0))
        return BrokerOrderState(boid, d.get("order_reference_id") or "", status, qty, filled,
                                avg, d.get("remark") if status is OrderStatus.REJECTED else None, fills)

    # ---- positions / funds ----------------------------------------------------------------
    def get_positions(self) -> dict[tuple[str, Product], int]:
        try:
            pos = self._call("non_trading", "get_positions_for_user", segment=SEGMENT).get("positions") or []
            hold = self._call("non_trading", "get_holdings_for_user").get("holdings") or []
        except Exception as e:
            raise BrokerUnavailable(f"positions failed: {type(e).__name__}") from None
        out: dict[tuple[str, Product], int] = {}
        for p in pos:
            if p.get("product") not in ("MIS", "CNC"):
                continue
            net_today = int(p.get("credit_quantity") or 0) - int(p.get("debit_quantity") or 0)
            key = (p["trading_symbol"], Product(p["product"]))
            out[key] = out.get(key, 0) + net_today
        for h in hold:
            key = (h["trading_symbol"], Product.CNC)
            out[key] = out.get(key, 0) + int(Decimal(str(h.get("quantity") or 0)))
        return {k: v for k, v in out.items() if v != 0}

    def get_available_funds(self) -> Decimal:
        m = self._call("non_trading", "get_available_margin_details")
        eq = m.get("equity_margin_details") or {}
        return Decimal(str(eq.get("cnc_balance_available", m.get("clear_cash", 0))))


def _parse_ts(v: Any, clock: Callable[[], datetime]) -> datetime:
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, IST)
    if isinstance(v, str) and v:
        try:
            ts = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return ts if ts.tzinfo else ts.replace(tzinfo=IST)
        except ValueError:
            pass
    return clock()
