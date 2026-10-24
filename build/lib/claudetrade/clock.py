from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class SimClock:
    """Controllable clock for paper sessions, replays and tests."""

    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> datetime:
        self.now += timedelta(**kw)
        return self.now

    def set(self, ts: datetime) -> None:
        self.now = ts
