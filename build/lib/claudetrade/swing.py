"""Swing proposals and explicit, parameter-bound approvals (§FR-06, §16 Phase 3).

* A proposal is validated against a versioned schema before it is stored.
* Any edit creates a new version; an approval names one version and the SHA-256 of its
  exact contents, so it cannot authorise anything else.
* Before execution the approval is re-checked: expiry, single use, account state, and
  whether the market has moved materially since the proposal. A material change voids
  the approval; a fresh proposal and approval are required.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from .audit import AuditLog
from .config import RiskConfig
from .costs import CostModel
from .execution import ExecutionResult, ExecutionService
from .market_data import MarketDataService
from .models import OrderRequest, OrderType, Product, Side, TradeMode, stable_hash, to_decimal

SCHEMA_VERSION = "swing-proposal/1"


class Recommendation(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    NO_TRADE = "NO-TRADE"


class SchemaError(ValueError):
    pass


@dataclass(frozen=True)
class SwingProposal:
    proposal_id: str
    version: int
    symbol: str
    exchange: str
    recommendation: Recommendation
    entry_low: Decimal | None
    entry_high: Decimal | None
    quantity: int
    stop_loss: Decimal | None
    target_low: Decimal | None
    target_high: Decimal | None
    horizon_days: int
    estimated_costs: Decimal
    reference_price: Decimal
    rationale: str
    evidence: tuple[str, ...]
    risks: tuple[str, ...]
    invalidation: tuple[str, ...]
    confidence_notes: str
    created_at: datetime
    expires_at: datetime
    schema_version: str = SCHEMA_VERSION

    def content_hash(self) -> str:
        return stable_hash(asdict(self))

    @property
    def actionable(self) -> bool:
        return self.recommendation in (Recommendation.BUY, Recommendation.SELL)

    @property
    def max_loss(self) -> Decimal | None:
        if self.entry_high is None or self.stop_loss is None:
            return None
        return (self.entry_high - self.stop_loss) * self.quantity + self.estimated_costs


def parse_proposal(raw: dict[str, Any], *, now: datetime, ttl: timedelta, reference_price: Decimal,
                   costs: CostModel, proposal_id: str | None = None, version: int = 1) -> SwingProposal:
    """Validate Claude's JSON output. Anything malformed raises ``SchemaError``; nothing is
    filled in by guessing. Reference price and costs come from the system, not from Claude."""
    errs: list[str] = []

    def dec(key: str, required: bool) -> Decimal | None:
        v = raw.get(key)
        if v is None:
            if required:
                errs.append(f"{key} is required")
            return None
        try:
            d = to_decimal(v)
        except (InvalidOperation, ValueError):
            errs.append(f"{key} must be a number")
            return None
        if d <= 0:
            errs.append(f"{key} must be > 0")
        return d

    def strs(key: str, required: bool = True) -> tuple[str, ...]:
        v = raw.get(key, [])
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
            errs.append(f"{key} must be a list of non-empty strings")
            return ()
        if required and not v:
            errs.append(f"{key} must not be empty")
        return tuple(v)

    try:
        rec = Recommendation(raw.get("recommendation"))
    except ValueError:
        errs.append("recommendation must be one of BUY, SELL, HOLD, NO-TRADE")
        rec = Recommendation.NO_TRADE
    actionable = rec in (Recommendation.BUY, Recommendation.SELL)
    symbol = raw.get("symbol")
    if not isinstance(symbol, str) or not symbol.isupper():
        errs.append("symbol must be an upper-case NSE symbol")
    exchange = raw.get("exchange", "NSE")
    if exchange != "NSE":
        errs.append("only NSE cash equities are supported in V1")
    entry_low, entry_high = dec("entry_low", actionable), dec("entry_high", actionable)
    stop = dec("stop_loss", rec is Recommendation.BUY)
    t_low, t_high = dec("target_low", False), dec("target_high", False)
    qty = raw.get("quantity", 0)
    if not isinstance(qty, int) or isinstance(qty, bool) or qty < 0 or (actionable and qty == 0):
        errs.append("quantity must be a positive integer for BUY/SELL")
    horizon = raw.get("horizon_days")
    if not isinstance(horizon, int) or not (5 <= horizon <= 31):
        errs.append("horizon_days must be an integer between 5 and 31")
    rationale = raw.get("rationale")
    if not isinstance(rationale, str) or len(rationale.strip()) < 20:
        errs.append("rationale must explain the trade (>= 20 chars)")
    evidence = strs("evidence", actionable)
    risks = strs("risks", True)
    invalidation = strs("invalidation", actionable)
    conf = raw.get("confidence_notes", "")
    if not isinstance(conf, str):
        errs.append("confidence_notes must be a string")
    if entry_low and entry_high and entry_low > entry_high:
        errs.append("entry_low > entry_high")
    if rec is Recommendation.BUY and stop and entry_low and stop >= entry_low:
        errs.append("stop_loss must be below the entry range for BUY")
    if rec is Recommendation.BUY and t_low and entry_high and t_low <= entry_high:
        errs.append("target must be above the entry range for BUY")
    if errs:
        raise SchemaError("; ".join(errs))

    est = Decimal("0")
    if actionable and entry_high:
        exit_px = t_low or entry_high
        est = costs.round_trip(Product.CNC, qty, entry_high, exit_px)
    return SwingProposal(
        proposal_id=proposal_id or f"sp-{uuid.uuid4().hex[:10]}",
        version=version,
        symbol=symbol,
        exchange=exchange,
        recommendation=rec,
        entry_low=entry_low,
        entry_high=entry_high,
        quantity=qty,
        stop_loss=stop,
        target_low=t_low,
        target_high=t_high,
        horizon_days=horizon,
        estimated_costs=est,
        reference_price=reference_price,
        rationale=rationale.strip(),
        evidence=evidence,
        risks=risks,
        invalidation=invalidation,
        confidence_notes=conf,
        created_at=now,
        expires_at=now + ttl,
    )


class ApprovalStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CONSUMED = "CONSUMED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


@dataclass
class Approval:
    approval_id: str
    proposal_id: str
    version: int
    proposal_hash: str
    approver: str
    approved_at: datetime
    status: ApprovalStatus = ApprovalStatus.ACTIVE
    note: str = ""


@dataclass
class SwingDesk:
    market_data: MarketDataService
    execution: ExecutionService
    risk_config: RiskConfig
    costs: CostModel
    audit: AuditLog
    clock: Callable[[], datetime]
    proposals: dict[str, list[SwingProposal]] = field(default_factory=dict)
    approvals: dict[str, Approval] = field(default_factory=dict)
    declined: dict[str, str] = field(default_factory=dict)  # proposal_id -> reason

    def submit_proposal(self, raw: dict[str, Any], actor: str = "claude") -> SwingProposal:
        symbol = raw.get("symbol")
        qv = self.market_data.validate_quote(symbol, TradeMode.SWING) if isinstance(symbol, str) else None
        if qv is None or qv.quote is None:
            self.audit.record(actor, "PROPOSAL_REJECTED", symbol=symbol, reason="no market data for symbol")
            raise SchemaError("no validated market data for symbol; cannot anchor proposal")
        try:
            p = parse_proposal(raw, now=self.clock(), ttl=timedelta(minutes=self.risk_config.swing_proposal_ttl_minutes),
                               reference_price=qv.quote.ltp, costs=self.costs)
        except SchemaError as e:
            self.audit.record(actor, "PROPOSAL_REJECTED", symbol=symbol, reason=str(e))
            raise
        self.proposals[p.proposal_id] = [p]
        self.audit.record(actor, "PROPOSAL_CREATED", proposal_id=p.proposal_id, version=p.version, hash=p.content_hash(),
                          symbol=p.symbol, recommendation=p.recommendation.value)
        return p

    def revise(self, proposal_id: str, changes: dict[str, Any], actor: str) -> SwingProposal:
        """Edits produce a new version and invalidate approvals of earlier versions."""
        current = self.latest(proposal_id)
        raw = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(current).items()}
        raw["recommendation"] = current.recommendation.value
        for k in ("evidence", "risks", "invalidation"):
            raw[k] = list(raw[k])
        raw.update(changes)
        qv = self.market_data.validate_quote(current.symbol, TradeMode.SWING)
        ref = qv.quote.ltp if qv.quote else current.reference_price
        p = parse_proposal(raw, now=self.clock(), ttl=timedelta(minutes=self.risk_config.swing_proposal_ttl_minutes),
                           reference_price=ref, costs=self.costs, proposal_id=proposal_id, version=current.version + 1)
        self.proposals[proposal_id].append(p)
        for a in self.approvals.values():
            if a.proposal_id == proposal_id and a.status is ApprovalStatus.ACTIVE:
                a.status, a.note = ApprovalStatus.INVALIDATED, f"superseded by v{p.version}"
        self.audit.record(actor, "PROPOSAL_REVISED", proposal_id=proposal_id, version=p.version, hash=p.content_hash())
        return p

    def latest(self, proposal_id: str) -> SwingProposal:
        if proposal_id not in self.proposals:
            raise KeyError(proposal_id)
        return self.proposals[proposal_id][-1]

    def approve(self, proposal_id: str, version: int, proposal_hash: str, approver: str) -> Approval:
        """Called only from an authenticated operator action — never from a Claude tool."""
        p = self.latest(proposal_id)
        now = self.clock()
        if p.version != version or p.content_hash() != proposal_hash:
            raise PermissionError("approval must reference the latest proposal version and its exact hash")
        if not p.actionable:
            raise PermissionError(f"{p.recommendation.value} proposals are informational, nothing to approve")
        if now >= p.expires_at:
            raise PermissionError("proposal expired")
        a = Approval(f"ap-{uuid.uuid4().hex[:10]}", proposal_id, version, proposal_hash, approver, now)
        self.approvals[a.approval_id] = a
        self.audit.record(approver, "PROPOSAL_APPROVED", approval_id=a.approval_id, proposal_id=proposal_id,
                          version=version, hash=proposal_hash)
        return a

    def execute(self, approval_id: str, actor: str = "operator") -> ExecutionResult | str:
        a = self.approvals.get(approval_id)
        if a is None:
            return "unknown approval"
        if a.status is not ApprovalStatus.ACTIVE:
            return f"approval {a.status.value}: {a.note}"
        p = self.latest(a.proposal_id)
        now = self.clock()
        problems: list[str] = []
        if p.version != a.version or p.content_hash() != a.proposal_hash:
            problems.append("proposal changed since approval")
        if now >= p.expires_at:
            a.status = ApprovalStatus.EXPIRED
            self.audit.record(actor, "APPROVAL_EXPIRED", approval_id=approval_id)
            return "proposal expired; a renewed proposal and approval are required"
        qv = self.market_data.validate_quote(p.symbol, TradeMode.SWING)
        if not qv.valid:
            return "market data not valid: " + "; ".join(qv.issues)  # retryable, approval kept
        ltp = qv.quote.ltp
        drift = abs(ltp / p.reference_price - 1) * 100
        if drift > self.risk_config.swing_material_change_pct:
            problems.append(f"price moved {drift:.2f}% since proposal (> {self.risk_config.swing_material_change_pct}%)")
        if p.recommendation is Recommendation.BUY and not (p.entry_low <= ltp <= p.entry_high):
            problems.append(f"LTP {ltp} outside approved entry range {p.entry_low}-{p.entry_high}")
        if p.recommendation is Recommendation.BUY and p.stop_loss is not None and ltp <= p.stop_loss:
            problems.append("price at or below stop-loss")
        if problems:
            a.status, a.note = ApprovalStatus.INVALIDATED, "; ".join(problems)
            self.audit.record(actor, "APPROVAL_INVALIDATED", approval_id=approval_id, reasons=problems)
            return "approval invalidated (material change): " + a.note

        is_buy = p.recommendation is Recommendation.BUY
        req = OrderRequest(
            client_request_id=f"sw-{a.approval_id}",  # one approval -> at most one order, ever
            symbol=p.symbol,
            side=Side.BUY if is_buy else Side.SELL,
            quantity=p.quantity,
            order_type=OrderType.LIMIT,
            product=Product.CNC,
            mode=TradeMode.SWING,
            limit_price=p.entry_high if is_buy else p.entry_low,
            stop_loss=p.stop_loss if is_buy else None,
            is_exit=not is_buy,
            proposal_id=p.proposal_id,
            approval_id=a.approval_id,
            origin=actor,
        )
        result = self.execution.execute(req)
        if result.order is not None:
            a.status, a.note = ApprovalStatus.CONSUMED, f"order {req.client_request_id}"
            pos = self.execution.portfolio.position(p.symbol, TradeMode.SWING)
            pos.target = p.target_low
        self.audit.record(actor, "APPROVAL_EXECUTED", approval_id=approval_id, result=result.message)
        return result

    def decline(self, proposal_id: str, operator: str, reason: str) -> None:
        self.latest(proposal_id)
        self.declined[proposal_id] = reason
        self.audit.record(operator, "PROPOSAL_DECLINED", proposal_id=proposal_id, reason=reason)

    def pending(self) -> list[SwingProposal]:
        approved = {a.proposal_id for a in self.approvals.values() if a.status is not ApprovalStatus.INVALIDATED}
        approved |= set(self.declined)
        now = self.clock()
        return [v[-1] for pid, v in self.proposals.items() if pid not in approved and v[-1].actionable and v[-1].expires_at > now]

