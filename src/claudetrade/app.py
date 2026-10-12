"""Composition root: wires the components together for a given operating mode.

``ClaudeTrade.paper(...)`` is the only constructor that produces an executing system in
this release. Live mode additionally needs a verified broker adapter, an operator-approved
risk config and a runtime ``LiveAuthorisation`` — none of which can come from Claude.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

from .audit import AuditLog
from .broker import PaperBroker
from .broker.base import BrokerAdapter
from .config import LiveAuthorisation, RiskConfig, SessionConfig
from .costs import CostModel
from .execution import ExecutionService
from .ledger import CapitalLedger
from .lifecycle import StrategyBook
from .market_data import MarketDataService
from .models import IST, OperatingMode, TradeMode
from .monitoring import IntradayEngine, Monitor
from .persistence import Persistence
from .portfolio import Portfolio
from .risk import RiskEngine, TradingControls
from .store import Store
from .swing import SwingDesk


def system_clock() -> datetime:
    return datetime.now(IST)


@dataclass
class ClaudeTrade:
    mode: OperatingMode
    clock: Callable[[], datetime]
    risk_config: RiskConfig
    session: SessionConfig
    costs: CostModel
    audit: AuditLog
    ledger: CapitalLedger
    portfolio: Portfolio
    market_data: MarketDataService
    controls: TradingControls
    monitor: Monitor
    broker: BrokerAdapter
    risk: RiskEngine
    execution: ExecutionService
    swing: SwingDesk
    strategies: StrategyBook
    intraday: IntradayEngine
    live_auth: LiveAuthorisation
    bars: dict[str, pd.DataFrame] = field(default_factory=dict)
    persistence: Persistence | None = None

    @classmethod
    def build(
        cls,
        mode: OperatingMode = OperatingMode.PAPER,
        clock: Callable[[], datetime] = system_clock,
        risk_config: RiskConfig | None = None,
        session: SessionConfig | None = None,
        costs: CostModel | None = None,
        broker: BrokerAdapter | None = None,
        audit_path: Path | None = None,
        store: Store | None = None,
    ) -> ClaudeTrade:
        risk_config = risk_config or RiskConfig()
        session = session or SessionConfig()
        costs = costs or CostModel()
        audit = AuditLog(clock, audit_path)
        ledger = CapitalLedger(clock)
        portfolio = Portfolio()
        md = MarketDataService(clock, risk_config, session)
        controls = TradingControls()
        monitor = Monitor(clock, audit, controls)
        if broker is None:
            if mode is OperatingMode.LIVE:
                raise PermissionError("live mode requires an explicitly supplied, verified broker adapter")
            broker = PaperBroker(lambda s: md._quotes.get(s), clock, costs, starting_funds=risk_config.starting_capital)
        elif mode is not OperatingMode.LIVE and not isinstance(broker, PaperBroker):
            raise PermissionError("a non-paper broker may only be attached in LIVE mode")
        live_auth = LiveAuthorisation()
        ex_holder: dict[str, ExecutionService] = {}
        risk = RiskEngine(
            config=risk_config, session=session, ledger=ledger, portfolio=portfolio, market_data=md,
            controls=controls, costs=costs, clock=clock, operating_mode=mode, live_auth=live_auth,
            broker_healthy=broker.is_healthy,
            reconciliation_ok=lambda: ex_holder["ex"].reconciliation_ok,
            seen_request_ids=lambda: ex_holder["ex"].seen_request_ids(),
            pending_entry_value=lambda: ex_holder["ex"].pending_entry_value(),
        )
        ex = ExecutionService(broker, risk, ledger, portfolio, audit, clock, monitor.alert)
        ex_holder["ex"] = ex
        swing = SwingDesk(md, ex, risk_config, costs, audit, clock)
        book = StrategyBook(audit, clock)
        app_bars: dict[str, pd.DataFrame] = {}
        engine = IntradayEngine(ex, md, monitor, risk_config, session, lambda s: app_bars.get(s), clock)
        app = cls(mode, clock, risk_config, session, costs, audit, ledger, portfolio, md, controls, monitor,
                  broker, risk, ex, swing, book, engine, live_auth, app_bars)
        restored: dict | None = None
        if store is not None:
            app.persistence = Persistence(store)
            if app.persistence.has_state():
                restored = app.persistence.restore(app)
            app.persistence.attach(app)
        if restored is None:
            ledger.deposit(risk_config.starting_capital, note="initial capital")
        audit.record("system", "STARTUP", mode=mode.value, risk_config=risk_config.version,
                     risk_config_hash=risk_config.content_hash(), live_enabled=False, restored=restored)
        if restored is not None:
            # Broker is the source of truth after a restart: reconcile before anything trades.
            ex.sync()
            ex.reconcile_positions()
        return app

    # ---- dashboard (§FR-11) -------------------------------------------------------------
    def dashboard(self) -> dict:
        now = self.clock()
        prices = {s: v.quote.ltp for s, v in self.market_data.snapshot(
            sorted({p.symbol for p in self.portfolio.open_positions()}), TradeMode.SWING).items() if v.quote}
        positions = [
            {"symbol": p.symbol, "mode": p.mode.value, "qty": p.quantity, "avg": str(p.average_price),
             "ltp": str(prices.get(p.symbol, "")), "stop": str(p.stop_loss) if p.stop_loss else None,
             "target": str(p.target) if p.target else None,
             "unrealised": str(p.unrealised_pnl(prices[p.symbol])) if p.symbol in prices else None,
             "reconciled_at": str(p.reconciled_at) if p.reconciled_at else None}
            for p in self.portfolio.open_positions()
        ]
        sv = self.intraday.strategy_version
        return {
            "as_of": now.isoformat(),
            "mode": self.mode.value,
            "live_authorised": self.live_auth.is_valid_for(self.risk_config),
            "portfolio": {
                "cash": str(self.ledger.cash()),
                "available": str(self.ledger.available()),
                "reserved": str(self.ledger.reserved()),
                "reserved_intraday": str(self.ledger.reserved(TradeMode.INTRADAY)),
                "reserved_swing": str(self.ledger.reserved(TradeMode.SWING)),
                "committed": str(self.portfolio.committed_capital()),
                "realised_pnl_today": str(self.portfolio.realised_pnl_on(now.date())),
                "unrealised_pnl": str(self.portfolio.unrealised_pnl(prices)),
                "charges_paid": str(self.ledger.charges_paid()),
                "positions": positions,
                "pending_orders": [
                    {"id": o.client_request_id, "symbol": o.request.symbol, "side": o.request.side.value,
                     "qty": o.request.quantity, "filled": o.filled_quantity, "status": o.status.value,
                     "mode": o.request.mode.value}
                    for o in self.execution.open_orders()
                ],
            },
            "intraday": {
                "strategy": sv.label if sv else None,
                "validation": self.intraday.validation.outcome.value if self.intraday.validation else None,
                "invalidated": self.intraday.invalidated,
                "daily_loss_limit": str(self.risk_config.max_daily_loss),
                "square_off": vars(self.intraday.square_off),
            },
            "swing": {
                "awaiting_approval": [{"id": p.proposal_id, "v": p.version, "symbol": p.symbol,
                                       "rec": p.recommendation.value, "expires": p.expires_at.isoformat()}
                                      for p in self.swing.pending()],
                "approvals": [{"id": a.approval_id, "proposal": a.proposal_id, "v": a.version,
                               "status": a.status.value, "by": a.approver, "note": a.note}
                              for a in self.swing.approvals.values()],
            },
            "research": {h[:10]: {"label": v.label, "status": v.status.value, "failures": list(v.eligibility_failures)}
                         for h, v in self.strategies.versions.items()},
            "operations": {
                "broker_healthy": self.broker.is_healthy(),
                "market_data_healthy": self.market_data.is_healthy(),
                "reconciliation_ok": self.execution.reconciliation_ok,
                "mismatches": self.execution.mismatches,
                "kill_switch": self.controls.kill_switch,
                "entries_paused": self.controls.entries_paused or bool(self.controls.paused_modes),
                "alerts": [{"ts": a.ts.isoformat(), "sev": a.severity, "msg": a.message} for a in self.monitor.alerts[-20:]],
                "audit_chain_intact": self.audit.verify(),
                "rejected_orders": [{"id": o.client_request_id, "reason": o.reject_reason}
                                    for o in self.execution.orders.values() if o.status.value == "REJECTED"][-20:],
            },
        }

    def authorise_live(self, operator: str) -> None:
        if self.mode is not OperatingMode.LIVE:
            raise PermissionError("system was not started in LIVE mode")
        self.live_auth.authorise(operator, self.clock(), self.risk_config)
        self.audit.record(operator, "LIVE_AUTHORISED", risk_config_hash=self.risk_config.content_hash())

    def deposit(self, amount: Decimal, operator: str) -> None:
        self.ledger.deposit(amount, note=f"by {operator}")
        self.audit.record(operator, "DEPOSIT", amount=amount)
