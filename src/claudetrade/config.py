"""Risk and session configuration.

Numerical limits here are *defaults for paper trading only*. The PRD (§13, §18) leaves
risk-per-trade, daily loss and exposure limits as open operator decisions, so a
``RiskConfig`` carries an ``approved_by`` stamp and the Risk Engine refuses live orders
under a config nobody has approved.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, time
from decimal import Decimal

from .models import stable_hash


@dataclass(frozen=True)
class SessionConfig:
    """NSE cash-market session. Cutoffs must be confirmed against Groww's current rules (§18 Q6)."""

    market_open: time = time(9, 15)
    market_close: time = time(15, 30)
    no_new_entries_after: time = time(14, 45)
    # Our own square-off starts well before the broker's auto square-off window.
    square_off_start: time = time(15, 0)
    square_off_deadline: time = time(15, 10)
    holidays: frozenset[str] = frozenset()  # ISO dates, loaded from the exchange calendar

    def is_trading_day(self, ts: datetime) -> bool:
        return ts.weekday() < 5 and ts.date().isoformat() not in self.holidays

    def is_open(self, ts: datetime) -> bool:
        return self.is_trading_day(ts) and self.market_open <= ts.time() < self.market_close


@dataclass(frozen=True)
class RiskConfig:
    version: str = "paper-default-1"
    starting_capital: Decimal = Decimal("10000")

    # Per-trade
    risk_per_trade: Decimal = Decimal("100")  # 1% of capital, paper default
    max_order_value: Decimal = Decimal("4000")
    max_position_value: Decimal = Decimal("4000")

    # Portfolio
    max_portfolio_exposure: Decimal = Decimal("9000")  # always keep a cash buffer
    max_concurrent_positions: int = 3
    max_daily_loss: Decimal = Decimal("300")  # realised + unrealised, 3% paper default

    # Market-quality filters
    max_spread_bps: Decimal = Decimal("15")
    min_price: Decimal = Decimal("20")
    max_gap_pct: Decimal = Decimal("5")  # |open/prev_close - 1| beyond which entries are blocked
    max_price_deviation_pct: Decimal = Decimal("1.0")  # limit price vs LTP sanity band
    min_avg_volume: int = 100_000

    # Data freshness (seconds) per mode
    max_quote_age_intraday_s: int = 10
    max_quote_age_swing_s: int = 120

    # Product rules (§FR-08: no unauthorised leverage / overnight exposure)
    allow_short_intraday: bool = False
    allowed_order_types: frozenset[str] = frozenset({"MARKET", "LIMIT", "SL", "SL_M"})
    cost_buffer_pct: Decimal = Decimal("0.5")  # extra reserve on top of estimated charges

    # Swing
    swing_proposal_ttl_minutes: int = 240
    swing_material_change_pct: Decimal = Decimal("1.5")

    # Release control
    approved_by: str | None = None
    approved_at: datetime | None = None

    @property
    def is_approved(self) -> bool:
        return bool(self.approved_by and self.approved_at)

    def content_hash(self) -> str:
        d = asdict(self)
        d.pop("approved_by")
        d.pop("approved_at")
        d["allowed_order_types"] = sorted(self.allowed_order_types)
        return stable_hash(d)

    def approve(self, operator: str, at: datetime) -> RiskConfig:
        return replace(self, approved_by=operator, approved_at=at)


@dataclass
class LiveAuthorisation:
    """Live trading is off unless an operator explicitly flips it (§5).

    It is deliberately not loadable from config files or environment variables: it can only
    be set at runtime by ``authorise``, and it is bound to the risk-config hash it was granted
    under, so changing limits revokes it.
    """

    enabled: bool = False
    operator: str | None = None
    granted_at: datetime | None = None
    risk_config_hash: str | None = None
    history: list[tuple[datetime, str, str]] = field(default_factory=list)

    def authorise(self, operator: str, at: datetime, risk_config: RiskConfig) -> None:
        if not risk_config.is_approved:
            raise PermissionError("risk configuration must be operator-approved before live mode")
        self.enabled = True
        self.operator = operator
        self.granted_at = at
        self.risk_config_hash = risk_config.content_hash()
        self.history.append((at, operator, "AUTHORISE"))

    def revoke(self, operator: str, at: datetime) -> None:
        self.enabled = False
        self.history.append((at, operator, "REVOKE"))

    def is_valid_for(self, risk_config: RiskConfig) -> bool:
        return self.enabled and self.risk_config_hash == risk_config.content_hash()
