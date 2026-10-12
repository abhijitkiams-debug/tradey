"""Transaction-cost model for NSE cash equities.

These rates are *assumptions* for estimation and backtesting. They must be checked
against Groww's current fee schedule and statutory rates before live use (§9 integration
dependency). Every backtest records the ``CostModel`` it used, so results stay reproducible
when rates change.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal

from .models import Product, Side, stable_hash

PAISE = Decimal("0.01")


@dataclass(frozen=True)
class CostModel:
    version: str = "nse-eq-estimate-2026-10"
    brokerage_pct: Decimal = Decimal("0.0005")  # 0.05% per executed order...
    brokerage_cap: Decimal = Decimal("20")  # ...capped at ₹20
    brokerage_min: Decimal = Decimal("5")
    stt_intraday_sell_pct: Decimal = Decimal("0.00025")
    stt_delivery_pct: Decimal = Decimal("0.001")  # both sides
    exchange_txn_pct: Decimal = Decimal("0.0000297")
    sebi_pct: Decimal = Decimal("0.000001")  # ₹10 per crore
    stamp_intraday_buy_pct: Decimal = Decimal("0.00003")
    stamp_delivery_buy_pct: Decimal = Decimal("0.00015")
    gst_pct: Decimal = Decimal("0.18")
    dp_charge_per_sell: Decimal = Decimal("16")  # delivery sells only, incl. GST, estimate

    def content_hash(self) -> str:
        return stable_hash(asdict(self))

    def charges(self, side: Side, product: Product, quantity: int, price: Decimal) -> Decimal:
        turnover = price * quantity
        if turnover <= 0:
            return Decimal("0")
        brokerage = min(max(turnover * self.brokerage_pct, self.brokerage_min), self.brokerage_cap)
        exch = turnover * self.exchange_txn_pct
        sebi = turnover * self.sebi_pct
        gst = (brokerage + exch + sebi) * self.gst_pct
        if product is Product.MIS:
            stt = turnover * self.stt_intraday_sell_pct if side is Side.SELL else Decimal("0")
            stamp = turnover * self.stamp_intraday_buy_pct if side is Side.BUY else Decimal("0")
            dp = Decimal("0")
        else:
            stt = turnover * self.stt_delivery_pct
            stamp = turnover * self.stamp_delivery_buy_pct if side is Side.BUY else Decimal("0")
            dp = self.dp_charge_per_sell if side is Side.SELL else Decimal("0")
        total = brokerage + exch + sebi + gst + stt + stamp + dp
        return total.quantize(PAISE, rounding=ROUND_HALF_UP)

    def round_trip(self, product: Product, quantity: int, entry: Decimal, exit_: Decimal) -> Decimal:
        return self.charges(Side.BUY, product, quantity, entry) + self.charges(Side.SELL, product, quantity, exit_)
