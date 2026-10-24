"""Deterministic Risk Engine (§D, §FR-08).

Every order, whatever its origin (Claude, a strategy rule, an operator), passes through
``RiskEngine.validate``. The engine never trusts the caller's view of the world: it reads
account, market and health state itself and fails closed if anything it needs is missing.

Exits (square-off, stop-loss, target) are treated differently from entries: they are
allowed through a kill switch, daily-loss breach or stale data, because blocking an exit
increases risk. They are still checked for being genuine reductions of an existing position.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .config import LiveAuthorisation, RiskConfig, SessionConfig
from .costs import CostModel
from .ledger import CapitalLedger
from .market_data import MarketDataService, detect_gap_pct
from .models import (
    ZERO,
    CheckResult,
    OperatingMode,
    OrderRequest,
    OrderType,
    Product,
    RiskDecision,
    Side,
    TradeMode,
)
from .portfolio import Portfolio


@dataclass
class TradingControls:
    """Operator / monitoring safety switches. Claude can pause entries but never resume them."""

    kill_switch: bool = False
    entries_paused: bool = False
    paused_modes: set[TradeMode] = field(default_factory=set)
    reasons: list[str] = field(default_factory=list)

    def entries_blocked(self, mode: TradeMode) -> str | None:
        if self.kill_switch:
            return "kill switch active"
        if self.entries_paused:
            return "new entries paused"
        if mode in self.paused_modes:
            return f"{mode.value} entries paused"
        return None


def size_by_risk(risk_budget: Decimal, entry: Decimal, stop: Decimal, cash_limit: Decimal, lot_size: int = 1) -> int:
    """``floor(risk budget / risk per share)``, further capped by cash. Zero means don't trade."""
    per_share = abs(entry - stop)
    if per_share <= 0 or entry <= 0:
        return 0
    qty = min(math.floor(risk_budget / per_share), math.floor(cash_limit / entry))
    return max(qty - qty % lot_size, 0)


class RiskEngine:
    def __init__(
        self,
        config: RiskConfig,
        session: SessionConfig,
        ledger: CapitalLedger,
        portfolio: Portfolio,
        market_data: MarketDataService,
        controls: TradingControls,
        costs: CostModel,
        clock: Callable[[], datetime],
        operating_mode: OperatingMode,
        live_auth: LiveAuthorisation,
        broker_healthy: Callable[[], bool],
        reconciliation_ok: Callable[[], bool],
        seen_request_ids: Callable[[], set[str]],
        pending_entry_value: Callable[[], Decimal] = lambda: ZERO,
    ):
        self.config = config
        self.session = session
        self.ledger = ledger
        self.portfolio = portfolio
        self.md = market_data
        self.controls = controls
        self.costs = costs
        self.clock = clock
        self.operating_mode = operating_mode
        self.live_auth = live_auth
        self.broker_healthy = broker_healthy
        self.reconciliation_ok = reconciliation_ok
        self.seen_request_ids = seen_request_ids
        self.pending_entry_value = pending_entry_value

    # ------------------------------------------------------------------------------------
    def validate(self, req: OrderRequest) -> RiskDecision:
        checks: list[CheckResult] = []
        now = self.clock()
        entry = not req.is_exit
        cfg = self.config

        def check(name: str, ok: bool, detail: str = "") -> bool:
            checks.append(CheckResult(name, bool(ok), "" if ok else detail))
            return bool(ok)

        # --- mode / authorisation -------------------------------------------------------
        if self.operating_mode is OperatingMode.RESEARCH:
            check("operating_mode", False, "research mode never executes orders")
        elif self.operating_mode is OperatingMode.LIVE:
            check("risk_config_approved", cfg.is_approved, "live trading requires an operator-approved risk config")
            if not req.is_exit:
                # Exits only ever reduce a real position that exists because of an earlier authorised
                # entry; blocking them after a revoke or restart would leave real risk unmanaged.
                check("live_authorised", self.live_auth.is_valid_for(cfg), "live mode not explicitly authorised for this risk config")
        else:
            check("operating_mode", True)

        # --- request shape --------------------------------------------------------------
        instrument = self.md.instruments.get(req.symbol)
        check("instrument_known", instrument is not None, f"{req.symbol} not in instrument master")
        check("instrument_eligible", instrument is not None and instrument.eligible, "instrument not eligible")
        check("quantity_positive", req.quantity > 0, "quantity must be > 0")
        if instrument is not None and instrument.lot_size > 1:
            check("lot_size", req.quantity % instrument.lot_size == 0, f"quantity not a multiple of {instrument.lot_size}")
        check("order_type_allowed", req.order_type.value in cfg.allowed_order_types, f"{req.order_type.value} not allowed")
        if req.order_type in (OrderType.LIMIT, OrderType.SL):
            check("limit_price_present", req.limit_price is not None and req.limit_price > 0, "limit price required")
        if req.order_type in (OrderType.SL, OrderType.SL_M):
            check("trigger_price_present", req.trigger_price is not None and req.trigger_price > 0, "trigger price required")
        check("not_duplicate", req.client_request_id not in self.seen_request_ids(), "client_request_id already used")

        expected_product = Product.MIS if req.mode is TradeMode.INTRADAY else Product.CNC
        check("product_matches_mode", req.product is expected_product,
              f"{req.mode.value} must use {expected_product.value} (no unauthorised leverage or overnight exposure)")

        # --- health / controls ----------------------------------------------------------
        check("broker_healthy", self.broker_healthy(), "broker disconnected or unhealthy")
        if entry:
            blocked = self.controls.entries_blocked(req.mode)
            check("entries_enabled", blocked is None, blocked or "")
            check("reconciliation_ok", self.reconciliation_ok(), "unresolved broker/ledger mismatch")
            check("market_data_healthy", self.md.is_healthy(), "market data feed unhealthy")

        # --- session --------------------------------------------------------------------
        t = now.time()
        check("session_open", self.session.is_open(now), "market closed")
        if entry and req.mode is TradeMode.INTRADAY:
            check("before_entry_cutoff", t < self.session.no_new_entries_after,
                  f"no new intraday entries after {self.session.no_new_entries_after}")

        # --- position semantics ---------------------------------------------------------
        pos = self.portfolio.position(req.symbol, req.mode)
        if req.is_exit:
            reduces = (pos.quantity > 0 and req.side is Side.SELL) or (pos.quantity < 0 and req.side is Side.BUY)
            check("exit_reduces_position", reduces, "exit order does not reduce an existing position")
            check("exit_not_oversized", req.quantity <= abs(pos.quantity), "exit quantity exceeds position")
        else:
            if req.side is Side.SELL:
                allowed = req.mode is TradeMode.INTRADAY and cfg.allow_short_intraday
                check("no_short_selling", allowed, "short entries not permitted")
            if pos.quantity != 0 and (pos.quantity > 0) != (req.side is Side.BUY):
                check("no_flip", False, "entry would flip an existing position; exit first")

        # --- market quality -------------------------------------------------------------
        qv = self.md.validate_quote(req.symbol, req.mode)
        if entry:
            check("market_data_valid", qv.valid, "; ".join(qv.issues))
        ltp = qv.quote.ltp if qv.quote else None
        ref_price = req.limit_price or ltp
        if entry and qv.quote is not None:
            q = qv.quote
            check("min_price", q.ltp >= cfg.min_price, f"price {q.ltp} below minimum {cfg.min_price}")
            check("spread", q.spread_bps <= cfg.max_spread_bps, f"spread {q.spread_bps:.1f}bps > {cfg.max_spread_bps}")
            gap = detect_gap_pct(q.ltp, q.prev_close)
            check("price_gap", gap <= cfg.max_gap_pct, f"move vs prev close {gap:.2f}% > {cfg.max_gap_pct}%")
            avg_vol = self.md.avg_volume(req.symbol)
            check("liquidity", avg_vol is not None and avg_vol >= cfg.min_avg_volume, f"avg volume {avg_vol} below {cfg.min_avg_volume}")
            if req.limit_price is not None:
                dev = abs(req.limit_price / q.ltp - 1) * 100
                check("limit_price_band", dev <= cfg.max_price_deviation_pct,
                      f"limit {req.limit_price} deviates {dev:.2f}% from LTP {q.ltp}")
        if ref_price is None:
            check("reference_price", False, "no price available to value the order")
            return self._decide(req, checks, ZERO, ZERO, now)

        order_value = ref_price * req.quantity
        est_charges = self.costs.charges(req.side, req.product, req.quantity, ref_price)

        # --- sizing / exposure / loss limits (entries only) -----------------------------
        reserve = ZERO
        if entry:
            if check("stop_loss_present", req.stop_loss is not None and req.stop_loss > 0, "entries must carry a protective stop"):
                per_share = (ref_price - req.stop_loss) if req.side is Side.BUY else (req.stop_loss - ref_price)
                if check("stop_loss_side", per_share > 0, "stop-loss on the wrong side of entry"):
                    trade_risk = per_share * req.quantity
                    check("risk_per_trade", trade_risk <= cfg.risk_per_trade,
                          f"risk ₹{trade_risk:.2f} > budget ₹{cfg.risk_per_trade}")
            check("max_order_value", order_value <= cfg.max_order_value, f"order ₹{order_value:.2f} > ₹{cfg.max_order_value}")
            pos_after = (abs(pos.quantity) + req.quantity) * ref_price
            check("max_position_value", pos_after <= cfg.max_position_value, f"position ₹{pos_after:.2f} > ₹{cfg.max_position_value}")

            prices = {s: v.quote.ltp for s, v in self.md.snapshot([p.symbol for p in self.portfolio.open_positions()], req.mode).items() if v.quote}
            exposure_after = self.portfolio.gross_exposure(prices) + self.pending_entry_value() + order_value
            check("max_portfolio_exposure", exposure_after <= cfg.max_portfolio_exposure,
                  f"exposure ₹{exposure_after:.2f} > ₹{cfg.max_portfolio_exposure}")
            opening_new = pos.quantity == 0
            n_open = len(self.portfolio.open_positions())
            check("max_concurrent_positions", not opening_new or n_open < cfg.max_concurrent_positions,
                  f"{n_open} open positions, limit {cfg.max_concurrent_positions}")

            day_pnl = self.portfolio.realised_pnl_on(now.date()) + self.portfolio.unrealised_pnl(prices)
            check("daily_loss_limit", day_pnl > -cfg.max_daily_loss, f"day P&L ₹{day_pnl:.2f} at/below -₹{cfg.max_daily_loss}")

            reserve = (order_value + est_charges) * (1 + cfg.cost_buffer_pct / 100)
            if req.side is Side.SELL:  # intraday short: block margin-equivalent cash; no leverage
                reserve = order_value + est_charges
            available = self.ledger.available()
            check("capital_available", reserve <= available, f"needs ₹{reserve:.2f}, uncommitted ₹{available:.2f}")
        else:
            reserve = est_charges

        return self._decide(req, checks, est_charges, reserve, now)

    def _decide(self, req: OrderRequest, checks: list[CheckResult], est: Decimal, reserve: Decimal, now: datetime) -> RiskDecision:
        approved = bool(checks) and all(c.passed for c in checks)
        return RiskDecision(
            decision_id=f"rd-{uuid.uuid4().hex[:12]}",
            client_request_id=req.client_request_id,
            request_fingerprint=req.fingerprint(),
            approved=approved,
            checks=tuple(checks),
            approved_quantity=req.quantity if approved else 0,
            estimated_cost=est,
            reserved_amount=reserve.quantize(Decimal("0.01")) if approved else ZERO,
            ts=now,
        )
