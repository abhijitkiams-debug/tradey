"""Broker adapter contract (§E, §FR-09).

The Execution Service only talks to brokers through this interface, so paper and live
adapters are interchangeable and the same reconciliation logic runs against both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from ..models import Fill, OrderRequest, OrderStatus, Product


class BrokerError(Exception):
    pass


class BrokerRejected(BrokerError):
    """The broker definitively refused the order."""


class BrokerTimeout(BrokerError):
    """Outcome unknown: the order may or may not have reached the broker. Reconcile first."""


class BrokerUnavailable(BrokerError):
    """Request definitely not delivered (e.g. connection refused before send)."""


@dataclass
class BrokerOrderState:
    broker_order_id: str
    client_request_id: str
    status: OrderStatus
    quantity: int
    filled_quantity: int
    average_price: Decimal
    reject_reason: str | None = None
    fills: list[Fill] = field(default_factory=list)


class BrokerAdapter(Protocol):
    name: str

    def is_healthy(self) -> bool: ...

    def place_order(self, request: OrderRequest) -> str:
        """Submit and return the broker order id. Must carry ``client_request_id`` as the
        broker-side reference so a retried request can be detected and deduplicated."""
        ...

    def cancel_order(self, broker_order_id: str) -> None: ...

    def get_order(self, broker_order_id: str) -> BrokerOrderState | None: ...

    def find_by_client_id(self, client_request_id: str) -> BrokerOrderState | None: ...

    def list_orders(self) -> list[BrokerOrderState]: ...

    def get_positions(self) -> dict[tuple[str, Product], int]: ...

    def get_available_funds(self) -> Decimal: ...
