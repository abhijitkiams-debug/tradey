"""In-memory stand-in for growwapi.GrowwAPI: same method signatures, same exception class
names, response shapes per Groww's docs. Used to test the adapter without network access."""

from __future__ import annotations

import itertools


class GrowwAPIException(Exception):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.msg, self.code = msg, code


class GrowwAPINotFoundException(GrowwAPIException):
    def __init__(self):
        super().__init__("Not found", "404")


class GrowwAPITimeoutException(GrowwAPIException):
    def __init__(self):
        super().__init__("Timeout", "504")


class GrowwAPIRateLimitException(GrowwAPIException):
    def __init__(self):
        super().__init__("Rate limited", "429")


class GrowwAPIAuthenticationException(GrowwAPIException):
    def __init__(self):
        super().__init__("Auth", "401")


class FakeGroww:
    def __init__(self):
        self.orders = {}
        self.by_ref = {}
        self.trades = {}
        self.calls = []
        self.fail_next = {}  # method -> exception instance
        self.accept_then_timeout = False
        self.quotes = {}
        self.candles = {}
        self.holdings = []
        self._ids = itertools.count(1)
        self._tids = itertools.count(1)

    def _maybe_fail(self, name):
        self.calls.append(name)
        if name in self.fail_next:
            raise self.fail_next.pop(name)

    def place_order(self, validity, exchange, order_type, product, quantity, segment, trading_symbol,
                    transaction_type, order_reference_id=None, price=0.0, trigger_price=None, timeout=None):
        self._maybe_fail("place_order")
        if order_reference_id in self.by_ref:
            raise GrowwAPIException("Duplicate order reference id", "GA001")
        oid = f"GMK{next(self._ids):08d}"
        self.orders[oid] = {"groww_order_id": oid, "order_reference_id": order_reference_id, "trading_symbol": trading_symbol,
                            "order_status": "ACKED", "quantity": quantity, "filled_quantity": 0, "average_fill_price": 0,
                            "product": product, "transaction_type": transaction_type, "order_type": order_type,
                            "price": price, "remark": None}
        self.by_ref[order_reference_id] = oid
        self.trades[oid] = []
        if self.accept_then_timeout:
            self.accept_then_timeout = False
            raise GrowwAPITimeoutException()
        return {"groww_order_id": oid, "order_status": "ACKED", "order_reference_id": order_reference_id, "remark": None}

    # exchange simulation -------------------------------------------------------------------
    def execute(self, oid, qty, price, record_trade=True):
        o = self.orders[oid]
        o["filled_quantity"] += qty
        o["average_fill_price"] = price
        o["order_status"] = "EXECUTED" if o["filled_quantity"] == o["quantity"] else "ACKED"
        if record_trade:
            self.trades[oid].append({"groww_trade_id": f"T{next(self._tids)}", "price": price, "quantity": qty,
                                     "trade_date_time": "2026-10-13T10:00:30", "groww_order_id": oid})

    def get_order_detail(self, segment, groww_order_id, timeout=None):
        self._maybe_fail("get_order_detail")
        if groww_order_id not in self.orders:
            raise GrowwAPINotFoundException()
        return dict(self.orders[groww_order_id])

    def get_order_status_by_reference(self, segment, order_reference_id, timeout=None):
        self._maybe_fail("get_order_status_by_reference")
        oid = self.by_ref.get(order_reference_id)
        if oid is None:
            raise GrowwAPINotFoundException()
        o = self.orders[oid]
        return {k: o[k] for k in ("groww_order_id", "order_status", "remark", "filled_quantity", "order_reference_id")}

    def get_trade_list_for_order(self, groww_order_id, segment, page=0, page_size=25, timeout=None):
        self._maybe_fail("get_trade_list_for_order")
        return {"trade_list": self.trades[groww_order_id][page * page_size:(page + 1) * page_size]}

    def get_order_list(self, page=0, page_size=25, segment=None, timeout=None):
        items = list(self.orders.values())
        return {"order_list": items[page * page_size:(page + 1) * page_size]}

    def cancel_order(self, groww_order_id, segment, timeout=None):
        self._maybe_fail("cancel_order")
        self.orders[groww_order_id]["order_status"] = "CANCELLED"
        return {"groww_order_id": groww_order_id, "order_status": "CANCELLATION_REQUESTED"}

    def get_positions_for_user(self, segment=None, timeout=None):
        self._maybe_fail("get_positions_for_user")
        pos = {}
        for o in self.orders.values():
            if o["filled_quantity"]:
                k = (o["trading_symbol"], o["product"])
                p = pos.setdefault(k, {"trading_symbol": k[0], "product": k[1], "segment": "CASH", "exchange": "NSE",
                                       "credit_quantity": 0, "debit_quantity": 0})
                p["credit_quantity" if o["transaction_type"] == "BUY" else "debit_quantity"] += o["filled_quantity"]
        return {"positions": list(pos.values())}

    def get_holdings_for_user(self, timeout=None):
        return {"holdings": self.holdings}

    def get_available_margin_details(self, timeout=None):
        self._maybe_fail("get_available_margin_details")
        return {"clear_cash": 10000.0, "equity_margin_details": {"cnc_balance_available": 9876.5, "mis_balance_available": 9876.5}}

    def get_quote(self, trading_symbol, exchange, segment, timeout=None):
        self._maybe_fail("get_quote")
        return self.quotes[trading_symbol]

    def get_historical_candles(self, exchange, segment, groww_symbol, start_time, end_time, candle_interval, timeout=None):
        return {"candles": self.candles.get(groww_symbol, [])}
