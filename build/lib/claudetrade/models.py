"""Core domain types shared by every component.

Monetary values are ``Decimal`` rupees throughout so ledger arithmetic is exact.
Timestamps are timezone-aware (Asia/Kolkata for exchange-session logic).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
ZERO = Decimal("0")


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def canonical_json(obj: Any) -> str:
    """Deterministic JSON used for hashing (sorted keys, Decimal/datetime as str)."""

    def default(o: Any) -> Any:
        if isinstance(o, (Decimal, datetime)):
            return str(o)
        if isinstance(o, Enum):
            return o.value
        raise TypeError(f"not serialisable: {type(o)!r}")

    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=default)


def stable_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


class OperatingMode(str, Enum):
    RESEARCH = "RESEARCH"
    PAPER = "PAPER"
    LIVE = "LIVE"


class TradeMode(str, Enum):
    INTRADAY = "INTRADAY"
    SWING = "SWING"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"  # stop-loss limit
    SL_M = "SL_M"  # stop-loss market


class Product(str, Enum):
    """Broker product. MIS = intraday (auto square-off), CNC = delivery (cash, no leverage)."""

    MIS = "MIS"
    CNC = "CNC"


class OrderStatus(str, Enum):
    PENDING = "PENDING"  # created locally, not yet acknowledged
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"  # submission outcome not yet reconciled (e.g. timeout)

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.COMPLETED, OrderStatus.REJECTED, OrderStatus.CANCELLED)


@dataclass(frozen=True)
class Instrument:
    symbol: str
    exchange: str = "NSE"
    tick_size: Decimal = Decimal("0.05")
    lot_size: int = 1
    eligible: bool = True

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.symbol}"


@dataclass(frozen=True)
class Quote:
    symbol: str
    ltp: Decimal
    bid: Decimal
    ask: Decimal
    volume: int
    prev_close: Decimal
    source_ts: datetime
    ingest_ts: datetime
    source: str = "unknown"

    @property
    def spread_bps(self) -> Decimal:
        mid = (self.bid + self.ask) / 2
        if mid <= 0:
            return Decimal("Infinity")
        return (self.ask - self.bid) / mid * Decimal("10000")


@dataclass(frozen=True)
class OrderRequest:
    """A request to trade. Nothing is executed from this alone; it must pass the Risk Engine."""

    client_request_id: str
    symbol: str
    side: Side
    quantity: int
    order_type: OrderType
    product: Product
    mode: TradeMode
    limit_price: Decimal | None = None
    trigger_price: Decimal | None = None
    stop_loss: Decimal | None = None  # protective stop used for risk-per-trade sizing
    is_exit: bool = False  # reduces an existing position (square-off, stop, target)
    strategy_version: str | None = None
    proposal_id: str | None = None
    approval_id: str | None = None
    origin: str = "system"  # "claude", "strategy", "operator", "system"

    def fingerprint(self) -> str:
        """Hash of the economic content. A risk decision is bound to this hash."""
        d = asdict(self)
        d.pop("origin", None)
        return stable_hash(d)


@dataclass
class Order:
    client_request_id: str
    request: OrderRequest
    status: OrderStatus = OrderStatus.PENDING
    broker_order_id: str | None = None
    filled_quantity: int = 0
    average_price: Decimal = ZERO
    reject_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def remaining_quantity(self) -> int:
        return self.request.quantity - self.filled_quantity


@dataclass(frozen=True)
class Fill:
    fill_id: str
    client_request_id: str
    broker_order_id: str
    symbol: str
    side: Side
    quantity: int
    price: Decimal
    ts: datetime
    charges: Decimal = ZERO


@dataclass
class Position:
    symbol: str
    mode: TradeMode
    quantity: int = 0  # signed: +long / -short
    average_price: Decimal = ZERO
    realised_pnl: Decimal = ZERO
    stop_loss: Decimal | None = None
    target: Decimal | None = None
    opened_at: datetime | None = None
    reconciled_at: datetime | None = None

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    def unrealised_pnl(self, ltp: Decimal) -> Decimal:
        return (ltp - self.average_price) * self.quantity

    def market_value(self, ltp: Decimal) -> Decimal:
        return abs(ltp * self.quantity)


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class RiskDecision:
    decision_id: str
    client_request_id: str
    request_fingerprint: str
    approved: bool
    checks: tuple[CheckResult, ...]
    approved_quantity: int
    estimated_cost: Decimal
    reserved_amount: Decimal
    ts: datetime

    @property
    def rejection_reasons(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.passed]


@dataclass
class HealthStatus:
    broker_connected: bool = False
    market_data_ok: bool = False
    last_broker_check: datetime | None = None
    last_data_check: datetime | None = None
    reconciliation_ok: bool = True
    notes: list[str] = field(default_factory=list)
