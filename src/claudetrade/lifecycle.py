"""Strategy identity, release control and the Day-T / Day-T+1 lifecycle (§FR-05, §FR-12).

Day T  : research -> walk-forward -> eligibility -> ``freeze`` (immutable, hashed).
Operator: ``release`` a frozen ELIGIBLE version for one specific session.
Day T+1: ``validate_for_session`` returns APPROVED / DEFERRED / REJECTED / EXPIRED.
          Only the exact frozen version can trade; nothing re-tunes parameters to force a trade.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from enum import Enum
from typing import Any

from . import __version__
from .audit import AuditLog
from .backtest import EligibilityCriteria, WalkForwardResult
from .config import RiskConfig, SessionConfig
from .market_data import MarketDataService, detect_gap_pct
from .models import TradeMode, stable_hash
from .strategies.base import REGISTRY, Strategy


class StrategyStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    RETIRED = "RETIRED"


class SessionOutcome(str, Enum):
    APPROVED = "APPROVED"
    DEFERRED = "DEFERRED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


def code_version(cls: type[Strategy]) -> str:
    return f"{__version__}+{stable_hash(inspect.getsource(cls))[:12]}"


@dataclass(frozen=True)
class StrategyVersion:
    strategy_id: str
    version: int
    family: str
    params: tuple[tuple[str, Any], ...]  # sorted, immutable
    code_version: str
    dataset_id: str
    cost_model_hash: str
    risk_config_hash: str
    evidence_hash: str  # hash of the walk-forward summary used to decide eligibility
    status: StrategyStatus
    eligibility_failures: tuple[str, ...]
    frozen_at: datetime

    @property
    def params_dict(self) -> dict[str, Any]:
        return dict(self.params)

    @property
    def content_hash(self) -> str:
        d = asdict(self)
        d.pop("frozen_at")
        return stable_hash(d)

    @property
    def label(self) -> str:
        return f"{self.strategy_id}@v{self.version}#{self.content_hash[:10]}"

    def instantiate(self) -> Strategy:
        cls = REGISTRY.strategies[self.family]
        if code_version(cls) != self.code_version:
            raise RuntimeError(f"strategy code changed since {self.label} was frozen; re-validate as a new version")
        return cls(**self.params_dict)


@dataclass(frozen=True)
class Release:
    release_id: str
    version_hash: str
    effective_session: date
    approved_by: str
    approved_at: datetime
    paper_only: bool = False  # trial of a not-yet-eligible version: the live book never accepts these


@dataclass(frozen=True)
class SessionValidation:
    outcome: SessionOutcome
    reasons: tuple[str, ...]
    version_hash: str | None
    ts: datetime


@dataclass
class StrategyBook:
    audit: AuditLog
    clock: Callable[[], datetime]
    versions: dict[str, StrategyVersion] = field(default_factory=dict)
    releases: dict[str, Release] = field(default_factory=dict)
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)

    def freeze(self, wf: WalkForwardResult, criteria: EligibilityCriteria, risk: RiskConfig) -> StrategyVersion:
        cls = REGISTRY.strategies[wf.family]
        ok, fails = criteria.evaluate(wf)
        summary = {**wf.summary(), "criteria": asdict(criteria), "eligible": ok, "failures": fails}
        prior = [v for v in self.versions.values() if v.strategy_id == wf.family]
        sv = StrategyVersion(
            strategy_id=wf.family,
            version=len(prior) + 1,
            family=wf.family,
            params=tuple(sorted({**cls.default_params, **wf.final_params}.items())),
            code_version=code_version(cls),
            dataset_id=wf.dataset_id,
            cost_model_hash=wf.cost_model_hash,
            risk_config_hash=risk.content_hash(),
            evidence_hash=stable_hash(summary),
            status=StrategyStatus.ELIGIBLE if ok else StrategyStatus.INELIGIBLE,
            eligibility_failures=tuple(fails),
            frozen_at=self.clock(),
        )
        self.versions[sv.content_hash] = sv
        self.evidence[sv.content_hash] = summary
        self.audit.record("research", "STRATEGY_FROZEN", label=sv.label, status=sv.status.value, failures=fails)
        return sv

    def release(self, version_hash: str, session: date, operator: str, paper_only: bool = False) -> Release:
        """Operator action (directly, or standing via a strategy toggle). Claude has no tool that reaches this."""
        sv = self.versions.get(version_hash)
        if sv is None:
            raise KeyError("unknown strategy version")
        if sv.status is not StrategyStatus.ELIGIBLE and not paper_only:
            raise PermissionError(f"{sv.label} is {sv.status.value}: {list(sv.eligibility_failures)}")
        suffix = "-paper" if paper_only else ""
        r = Release(f"rel-{version_hash[:8]}-{session.isoformat()}{suffix}", version_hash, session, operator,
                    self.clock(), paper_only)
        self.releases[r.release_id] = r
        self.audit.record(operator, "STRATEGY_RELEASED", label=sv.label, session=session.isoformat())
        return r

    def validate_for_session(
        self,
        release: Release,
        md: MarketDataService,
        universe: list[str],
        risk: RiskConfig,
        session: SessionConfig,
        advisory_flags: list[str] | None = None,
        defer_deadline: time = time(10, 30),
    ) -> SessionValidation:
        """Day-T+1 gate. Claude's advisory flags (e.g. adverse news) can only downgrade the
        outcome to DEFERRED / REJECTED, never upgrade it."""
        now = self.clock()
        reasons: list[str] = []
        sv = self.versions.get(release.version_hash)

        def done(outcome: SessionOutcome) -> SessionValidation:
            v = SessionValidation(outcome, tuple(reasons), release.version_hash, now)
            self.audit.record("lifecycle", "SESSION_VALIDATION", release=release.release_id, outcome=outcome.value, reasons=reasons)
            return v

        if release.effective_session != now.date():
            reasons.append(f"release is for {release.effective_session}, today is {now.date()}")
            return done(SessionOutcome.EXPIRED)
        if sv is None or (sv.status is not StrategyStatus.ELIGIBLE and not release.paper_only):
            reasons.append("strategy version missing or not eligible")
            return done(SessionOutcome.REJECTED)
        try:
            sv.instantiate()
        except RuntimeError as e:
            reasons.append(str(e))
            return done(SessionOutcome.REJECTED)
        if sv.risk_config_hash != risk.content_hash():
            reasons.append("risk configuration changed since the strategy was frozen")
            return done(SessionOutcome.REJECTED)
        if not session.is_trading_day(now):
            reasons.append("not a trading day")
            return done(SessionOutcome.EXPIRED)

        transient: list[str] = []
        valid = {s: v for s, v in md.snapshot(universe, TradeMode.INTRADAY).items()}
        good = [s for s, v in valid.items() if v.valid]
        if len(good) < max(1, len(universe) // 2):
            transient.append(f"only {len(good)}/{len(universe)} instruments have valid live data")
        gapped = [s for s in good if detect_gap_pct(valid[s].quote.ltp, valid[s].quote.prev_close) > risk.max_gap_pct]
        if good and len(gapped) > len(good) // 2:
            transient.append(f"broad overnight gap: {len(gapped)} of {len(good)} instruments beyond {risk.max_gap_pct}%")
        if not session.is_open(now):
            transient.append("market not open yet")
        for f in advisory_flags or []:
            transient.append(f"advisory: {f}")

        if transient:
            reasons.extend(transient)
            if now.time() >= defer_deadline:
                reasons.append(f"conditions not met by {defer_deadline}; standing down for the session")
                return done(SessionOutcome.REJECTED)
            return done(SessionOutcome.DEFERRED)
        reasons.append(f"{sv.label} cleared for {now.date()}")
        return done(SessionOutcome.APPROVED)
