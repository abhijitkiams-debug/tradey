"""Deployment settings loaded from a TOML file (see ``config/claudetrade.example.toml``).

The file describes limits and universe; it can never *approve* them. Risk-config approval
and live authorisation are operator actions recorded in the store and bound to the config's
content hash, so editing the file silently revokes them.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, replace
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from .backtest import EligibilityCriteria
from .config import RiskConfig, SessionConfig
from .models import OperatingMode


@dataclass(frozen=True)
class ResearchSettings:
    history_days: int = 120
    train_days: int = 40
    test_days: int = 10
    cache_dir: str = "var/history"


@dataclass(frozen=True)
class Settings:
    mode: OperatingMode = OperatingMode.PAPER
    data_source: str = "groww"  # groww | none
    db_path: str = "var/claudetrade.db"
    universe: tuple[str, ...] = ()
    avg_daily_volume: dict[str, int] = field(default_factory=dict)
    default_avg_daily_volume: int | None = None  # None: unknown -> liquidity check fails closed
    external_holdings: dict[str, int] = field(default_factory=dict)
    risk: RiskConfig = field(default_factory=RiskConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    research: ResearchSettings = field(default_factory=ResearchSettings)
    eligibility: EligibilityCriteria = field(default_factory=EligibilityCriteria)
    agent_daily_review: bool = False
    tick_seconds: int = 15


def _coerce(cls: type, raw: dict[str, Any], forbidden: tuple[str, ...] = ()) -> Any:
    out = {}
    known = {f.name: f for f in fields(cls)}
    for k, v in raw.items():
        if k in forbidden:
            raise ValueError(f"'{k}' cannot be set from a file; it is an operator action")
        if k not in known:
            raise ValueError(f"unknown setting {cls.__name__}.{k}")
        default = getattr(cls(), k)
        if isinstance(default, Decimal):
            v = Decimal(str(v))
        elif isinstance(default, time):
            v = time.fromisoformat(v)
        elif isinstance(default, frozenset):
            v = frozenset(v)
        elif isinstance(default, tuple):
            v = tuple(v)
        out[k] = v
    return replace(cls(), **out)


def load_settings(path: str | Path | None) -> Settings:
    if path is None:
        return Settings()
    raw = tomllib.loads(Path(path).read_text())
    top = {k: v for k, v in raw.items() if not isinstance(v, dict) or k in ("avg_daily_volume", "external_holdings")}
    s = Settings(
        mode=OperatingMode(top.pop("mode", "PAPER")),
        risk=_coerce(RiskConfig, raw.get("risk", {}), forbidden=("approved_by", "approved_at")),
        session=_coerce(SessionConfig, raw.get("session", {})),
        research=_coerce(ResearchSettings, raw.get("research", {})),
        eligibility=_coerce(EligibilityCriteria, raw.get("eligibility", {})),
    )
    for k in ("universe",):
        if k in top:
            top[k] = tuple(s_.upper() for s_ in top[k])
    unknown = set(top) - {f.name for f in fields(Settings)}
    if unknown:
        raise ValueError(f"unknown settings: {sorted(unknown)}")
    return replace(s, **top)
