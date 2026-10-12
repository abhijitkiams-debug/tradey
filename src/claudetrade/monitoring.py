"""Monitoring, alerts and the intraday session engine (§G, §FR-10, §14).

``IntradayEngine.tick`` is called on a schedule during the session. Each tick:
  1. syncs broker state and runs health checks (data freshness, broker, daily loss);
  2. manages exits for open intraday positions (stop / target);
  3. evaluates new entry signals from the *frozen, approved* strategy only;
  4. from the square-off start time, cancels entries, exits everything and verifies flat.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import pandas as pd

from .audit import AuditLog
from .config import RiskConfig, SessionConfig
from .execution import ExecutionService
from .lifecycle import SessionOutcome, SessionValidation, StrategyVersion
from .market_data import MarketDataService
from .models import OrderRequest, OrderType, Product, Side, TradeMode
from .risk import TradingControls, size_by_risk


@dataclass(frozen=True)
class Alert:
    ts: datetime
    severity: str  # INFO | HIGH | CRITICAL
    message: str


@dataclass
class Monitor:
    clock: Callable[[], datetime]
    audit: AuditLog
    controls: TradingControls
    alerts: list[Alert] = field(default_factory=list)
    notifier: Callable[[Alert], None] | None = None

    def alert(self, severity: str, message: str) -> None:
        a = Alert(self.clock(), severity, message)
        self.alerts.append(a)
        self.audit.record("monitor", "ALERT", severity=severity, message=message)
        if self.notifier:
            self.notifier(a)

    def activate_kill_switch(self, actor: str, reason: str) -> None:
        self.controls.kill_switch = True
        self.controls.reasons.append(reason)
        self.audit.record(actor, "KILL_SWITCH_ON", reason=reason)
        self.alert("CRITICAL", f"kill switch activated by {actor}: {reason}")

    def pause_entries(self, actor: str, reason: str, mode: TradeMode | None = None) -> None:
        if mode is None:
            self.controls.entries_paused = True
        else:
            self.controls.paused_modes.add(mode)
        self.controls.reasons.append(reason)
        self.audit.record(actor, "ENTRIES_PAUSED", reason=reason, mode=mode.value if mode else "ALL")

    def resume_entries(self, operator: str, reason: str) -> None:
        """Operator only. Kill switch requires a separate, explicit reset."""
        self.controls.entries_paused = False
        self.controls.paused_modes.clear()
        self.audit.record(operator, "ENTRIES_RESUMED", reason=reason)

    def reset_kill_switch(self, operator: str, reason: str) -> None:
        self.controls.kill_switch = False
        self.audit.record(operator, "KILL_SWITCH_OFF", reason=reason)


@dataclass
class SquareOffStatus:
    started: bool = False
    verified_flat: bool = False
    attempts: int = 0
    last_error: str | None = None


class IntradayEngine:
    def __init__(
        self,
        execution: ExecutionService,
        market_data: MarketDataService,
        monitor: Monitor,
        risk: RiskConfig,
        session: SessionConfig,
        bars_source: Callable[[str], pd.DataFrame],
        clock: Callable[[], datetime],
    ):
        self.ex = execution
        self.md = market_data
        self.monitor = monitor
        self.risk = risk
        self.session = session
        self.bars = bars_source
        self.clock = clock
        self.strategy_version: StrategyVersion | None = None
        self.validation: SessionValidation | None = None
        self.universe: list[str] = []
        self._acted_signals: set[tuple[str, int]] = set()
        self.square_off = SquareOffStatus()
        self.invalidated: str | None = None

    def activate(self, sv: StrategyVersion, validation: SessionValidation, universe: list[str]) -> bool:
        if validation.outcome is not SessionOutcome.APPROVED or validation.version_hash != sv.content_hash:
            self.monitor.alert("INFO", f"intraday not activated: {validation.outcome.value} {list(validation.reasons)}")
            return False
        self.strategy_version, self.validation, self.universe = sv, validation, universe
        self.ex.audit.record("engine", "INTRADAY_ACTIVATED", strategy=sv.label, universe=universe)
        return True

    def invalidate(self, reason: str) -> None:
        """Stop new entries for the rest of the session; existing positions follow the exit policy."""
        self.invalidated = reason
        self.monitor.pause_entries("engine", f"strategy invalidated: {reason}", TradeMode.INTRADAY)

    # ------------------------------------------------------------------------------------
    def tick(self) -> None:
        now = self.clock()
        self.ex.sync()
        self._health_checks(now)
        if now.time() >= self.session.square_off_start:
            self.run_square_off()
            return
        self._manage_exits()
        if self.strategy_version and not self.invalidated:
            self._entries(now)

    def _health_checks(self, now: datetime) -> None:
        if not self.ex.broker.is_healthy():
            self.monitor.pause_entries("monitor", "broker disconnected")
            self.monitor.alert("HIGH", "broker disconnected: new entries blocked")
        if not self.md.is_healthy():
            self.monitor.pause_entries("monitor", "market data unhealthy")
            self.monitor.alert("HIGH", "market data unhealthy: new entries blocked")
        prices = self._prices()
        day_pnl = self.ex.portfolio.realised_pnl_on(now.date()) + self.ex.portfolio.unrealised_pnl(prices)
        if day_pnl <= -self.risk.max_daily_loss and not self.monitor.controls.entries_paused:
            self.monitor.pause_entries("monitor", f"daily loss limit reached (₹{day_pnl:.2f})")
            self.monitor.alert("HIGH", f"daily loss limit reached: ₹{day_pnl:.2f}; no new risk today")

    def _prices(self) -> dict[str, Decimal]:
        out = {}
        for p in self.ex.portfolio.open_positions():
            v = self.md.validate_quote(p.symbol, p.mode)
            if v.quote is not None:
                out[p.symbol] = v.quote.ltp
        return out

    def _manage_exits(self) -> None:
        for pos in self.ex.portfolio.open_positions(TradeMode.INTRADAY):
            if any(o.request.is_exit and o.request.symbol == pos.symbol for o in self.ex.open_orders(TradeMode.INTRADAY)):
                continue
            v = self.md.validate_quote(pos.symbol, TradeMode.INTRADAY)
            if v.quote is None:
                continue
            ltp = v.quote.ltp
            reason = None
            if pos.stop_loss is not None and ltp <= pos.stop_loss:
                reason = "stop"
            elif pos.target is not None and ltp >= pos.target:
                reason = "target"
            if reason:
                self._exit(pos.symbol, abs(pos.quantity), reason)

    def _entries(self, now: datetime) -> None:
        sv = self.strategy_version
        strat = sv.instantiate()
        for sym in self.universe:
            bars = self.bars(sym)
            if bars is None or bars.empty:
                continue
            today = bars[bars.index.date == now.date()]
            # Only completed bars: drop the bar still forming
            today = today[today.index + pd.Timedelta(minutes=1) <= pd.Timestamp(now)]
            if len(today) < 2:
                continue
            last = len(today) - 1
            for sig in strat.signals(today):
                if sig.bar_index != last or (sym, sig.bar_index) in self._acted_signals:
                    continue
                self._acted_signals.add((sym, sig.bar_index))
                self._enter(sym, sig, sv)

    def _enter(self, sym: str, sig, sv: StrategyVersion) -> None:
        v = self.md.validate_quote(sym, TradeMode.INTRADAY)
        if not v.valid:
            return
        entry = v.quote.ask
        stop = Decimal(str(round(sig.stop, 2)))
        cash_cap = min(self.ex.ledger.available() * Decimal("0.98"), self.risk.max_order_value)
        qty = size_by_risk(self.risk.risk_per_trade, entry, stop, cash_cap)
        if qty <= 0:
            self.ex.audit.record("engine", "SIGNAL_SKIPPED", symbol=sym, reason="computed quantity is zero")
            return
        req = OrderRequest(
            client_request_id=f"id-{uuid.uuid4().hex[:12]}",
            symbol=sym, side=Side.BUY, quantity=qty, order_type=OrderType.MARKET, product=Product.MIS,
            mode=TradeMode.INTRADAY, stop_loss=stop, strategy_version=sv.label, origin="strategy",
        )
        res = self.ex.execute(req)
        if res.order is not None and sig.target is not None:
            self.ex.portfolio.position(sym, TradeMode.INTRADAY).target = Decimal(str(round(sig.target, 2)))

    def _exit(self, sym: str, qty: int, reason: str):
        req = OrderRequest(
            client_request_id=f"ix-{uuid.uuid4().hex[:12]}",
            symbol=sym, side=Side.SELL, quantity=qty, order_type=OrderType.MARKET, product=Product.MIS,
            mode=TradeMode.INTRADAY, is_exit=True, origin="system",
        )
        res = self.ex.execute(req)
        self.ex.audit.record("engine", "EXIT_REQUESTED", symbol=sym, qty=qty, reason=reason, result=res.message)
        return res

    # ------------------------------------------------------------------------------------
    def run_square_off(self) -> SquareOffStatus:
        """Cancel intraday entries, exit all intraday positions, verify flat at the broker.
        Never reports flat unless the broker's own position view agrees."""
        st = self.square_off
        if not st.started:
            st.started = True
            self.monitor.pause_entries("engine", "square-off window", TradeMode.INTRADAY)
            self.ex.audit.record("engine", "SQUARE_OFF_START")
        st.attempts += 1
        for o in self.ex.open_orders(TradeMode.INTRADAY):
            if not o.request.is_exit:
                self.ex.cancel(o.client_request_id, actor="square_off")
        self.ex.sync()
        for pos in self.ex.portfolio.open_positions(TradeMode.INTRADAY):
            pending_exit = sum(o.remaining_quantity for o in self.ex.open_orders(TradeMode.INTRADAY)
                               if o.request.is_exit and o.request.symbol == pos.symbol)
            remaining = abs(pos.quantity) - pending_exit
            if remaining > 0:
                res = self._exit(pos.symbol, remaining, "square_off")
                if res.order is None:
                    st.last_error = res.message
        self.ex.sync()
        return self.verify_flat()

    def verify_flat(self) -> SquareOffStatus:
        st = self.square_off
        try:
            broker_mis = {k: v for k, v in self.ex.broker.get_positions().items() if k[1] is Product.MIS and v != 0}
        except Exception as e:  # broker unreachable: we cannot claim flat
            broker_mis = {"unknown": str(e)}
        local_open = self.ex.portfolio.open_positions(TradeMode.INTRADAY)
        st.verified_flat = not broker_mis and not local_open
        now = self.clock()
        if st.verified_flat:
            self.ex.audit.record("engine", "SQUARE_OFF_VERIFIED_FLAT", attempts=st.attempts)
        elif now.time() >= self.session.square_off_deadline:
            st.last_error = f"not flat after deadline: broker={broker_mis} local={[p.symbol for p in local_open]}"
            self.monitor.activate_kill_switch("engine", "square-off failure")
            self.monitor.alert("CRITICAL", "SQUARE-OFF FAILED — operator action required: " + st.last_error)
        return st

    def end_of_day_report(self) -> dict:
        now = self.clock()
        orders = [o for o in self.ex.orders.values() if o.request.mode is TradeMode.INTRADAY]
        return {
            "date": now.date().isoformat(),
            "strategy": self.strategy_version.label if self.strategy_version else None,
            "validation": self.validation.outcome.value if self.validation else None,
            "orders": len(orders),
            "rejected": sum(o.status.value == "REJECTED" for o in orders),
            "realised_pnl": str(self.ex.portfolio.realised_pnl_on(now.date())),
            "charges_paid_total": str(self.ex.ledger.charges_paid()),
            "verified_flat": self.square_off.verified_flat,
            "reconciliation_ok": self.ex.reconciliation_ok,
            "alerts": [a.message for a in self.monitor.alerts if a.ts.date() == now.date()],
        }


__all__ = ["Alert", "Monitor", "IntradayEngine", "SquareOffStatus"]
