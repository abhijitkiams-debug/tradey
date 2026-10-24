"""Trading desk: the paper book and the live book in one service, with a per-strategy switch.

Each strategy version, and the Claude swing desk ("SWING"), has one mode:

    OFF ── PAPER ── LIVE

* **Money is never shared.** The paper book trades virtual capital through the paper broker
  (filled against live Groww quotes); the live book trades the real Groww account. Each has
  its own ledger, positions, risk engine, kill switch and database.
* **Going up is gated.** PAPER needs a frozen version (a not-yet-eligible one runs as a
  paper-only *trial*). LIVE needs, all at once: the live book running with the master switch
  on, an ELIGIBLE version, the strategy's *own* paper record (72 h since it was switched to
  PAPER and its last 3 paper sessions clean, read from the hash-verified paper audit log),
  an alert channel, and the typed confirmation.
* **Going down is immediate.** LIVE→PAPER/OFF stops new entries at once; positions already
  open keep their stops and square-off, because exits never need authorisation.
* Revoking the master switch drops every LIVE strategy back to PAPER.
* A toggle is a standing release: each morning the scheduler releases and validates every
  switched-on strategy for that day (Day-T+1 validation still applies). Switching on during
  the session validates it straight away.

Every change is audited in both books; the paper audit log is the evidence of record.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, time
from enum import Enum
from pathlib import Path
from typing import Any

from .evidence import SWING_KEY, GateResult, LiveGate
from .evidence import evaluate as evaluate_gate
from .lifecycle import StrategyStatus, StrategyVersion
from .models import OperatingMode
from .runtime import Runtime, build_runtime
from .settings import Settings

LIVE_STRATEGY_CONFIRMATION = "ENABLE LIVE TRADING"


class StrategyMode(str, Enum):
    OFF = "OFF"
    PAPER = "PAPER"
    LIVE = "LIVE"


@dataclass
class Toggle:
    key: str  # strategy version hash, or "SWING"
    label: str
    mode: StrategyMode
    changed_by: str
    changed_at: datetime
    reason: str = ""


class ToggleError(PermissionError):
    pass


class Desk:
    def __init__(self, paper: Runtime, live: Runtime | None, clock: Callable[[], datetime], live_gate: LiveGate | None = None):
        self.paper, self.live, self.clock = paper, live, clock
        self.books: dict[str, Runtime] = {"paper": paper, **({"live": live} if live else {})}
        self.store = paper.app.persistence.store if paper.app.persistence else None
        default_db = paper.app.persistence.store.path if paper.app.persistence else None
        self.live_gate = live_gate or (live.app.live_gate if live else None) or LiveGate()
        if self.live_gate.paper_evidence_db is None and default_db:
            self.live_gate = replace(self.live_gate, paper_evidence_db=default_db)
        if live is not None:
            live.app.live_gate = self.live_gate
        self.toggles: dict[str, Toggle] = {}
        self._stop = threading.Event()
        self._gate_cache: dict[str, tuple[datetime, GateResult]] = {}
        self._load()
        paper.scheduler.strategy_source = lambda: self._wanted(StrategyMode.PAPER)
        if live is not None:
            live.scheduler.strategy_source = lambda: self._wanted(StrategyMode.LIVE)

    # ---- state -----------------------------------------------------------------------------
    def _load(self) -> None:
        if self.store is None:
            return
        for key, d in self.store.latest("toggle").items():
            self.toggles[key] = Toggle(**{**d, "mode": StrategyMode(d["mode"])})
        if self.live is None or not self.live.app.live_auth.enabled:
            # After a restart live authorisation is gone, so nothing may stay LIVE.
            for t in self.toggles.values():
                if t.mode is StrategyMode.LIVE:
                    self._apply(t.key, t.label, StrategyMode.PAPER, "system", "live not authorised after restart")

    def mode(self, key: str) -> StrategyMode:
        t = self.toggles.get(key)
        return t.mode if t else StrategyMode.OFF

    def versions(self) -> dict[str, StrategyVersion]:
        out = dict(self.paper.app.strategies.versions)
        if self.live:
            for h, sv in self.live.app.strategies.versions.items():
                out.setdefault(h, sv)
        return out

    def _wanted(self, mode: StrategyMode) -> list[tuple[StrategyVersion, str, bool]]:
        vs = self.versions()
        out = []
        for key, t in self.toggles.items():
            if key != SWING_KEY and t.mode is mode and key in vs:
                sv = vs[key]
                out.append((sv, t.changed_by, mode is StrategyMode.PAPER and sv.status is not StrategyStatus.ELIGIBLE))
        return out

    def live_master_on(self) -> bool:
        return bool(self.live and self.live.app.live_auth.is_valid_for(self.live.app.risk_config))

    def swing_book(self) -> Runtime | None:
        m = self.mode(SWING_KEY)
        if m is StrategyMode.LIVE and self.live is not None:
            return self.live
        if m is StrategyMode.PAPER:
            return self.paper
        return None

    # ---- gate ------------------------------------------------------------------------------
    def gate(self, key: str, fresh: bool = False) -> GateResult:
        now = self.clock()
        cached = self._gate_cache.get(key)
        if cached and not fresh and (now - cached[0]).total_seconds() < 60:
            return cached[1]
        channel = (self.live or self.paper).app.alert_channel_configured
        if self.store is not None:
            with self.paper.app.lock:
                self.store.checkpoint_wal()  # same process: flush so the read-only evidence view is current
        res = evaluate_gate(self.live_gate, now, channel, strategy_key=key)
        self._gate_cache[key] = (now, res)
        return res

    def live_blockers(self, key: str) -> list[str]:
        out = []
        if self.live is None:
            return ["live book not enabled (live_enabled = true and Groww credentials)"]
        if not self.live_master_on():
            out.append("live master switch is off")
        if not self.live.app.risk_config.is_approved:
            out.append("live risk limits not approved")
        if key != SWING_KEY:
            sv = self.versions().get(key)
            if sv is None:
                out.append("unknown strategy version")
            elif sv.status is not StrategyStatus.ELIGIBLE:
                out.append(f"not eligible: {'; '.join(sv.eligibility_failures[:2])}")
        if self.mode(key) is not StrategyMode.PAPER:
            out.append("switch it to PAPER first; LIVE needs its own paper record")
        out += self.gate(key).reasons
        return out

    # ---- switching -------------------------------------------------------------------------
    def set_mode(self, key: str, target: StrategyMode | str, operator: str, confirm: str | None = None,
                 reason: str = "") -> Toggle:
        target = StrategyMode(target)
        if key == SWING_KEY:
            label = "Swing — Claude proposals"
        else:
            sv = self.versions().get(key)
            if sv is None:
                raise ToggleError("unknown strategy version")
            label = sv.label
        current = self.mode(key)
        if target is current:
            return self.toggles.get(key) or Toggle(key, label, current, operator, self.clock())
        if target is StrategyMode.LIVE:
            if confirm != LIVE_STRATEGY_CONFIRMATION:
                raise ToggleError(f"type exactly: {LIVE_STRATEGY_CONFIRMATION}")
            self._gate_cache.pop(key, None)
            blockers = self.live_blockers(key)
            if blockers:
                self._audit(operator, "STRATEGY_MODE_DENIED", key=key, label=label, mode=target.value, reasons=blockers)
                raise ToggleError("cannot go LIVE: " + "; ".join(blockers))
        return self._apply(key, label, target, operator, reason)

    def _apply(self, key: str, label: str, target: StrategyMode, operator: str, reason: str) -> Toggle:
        previous = self.mode(key)
        t = Toggle(key, label, target, operator, self.clock(), reason)
        self.toggles[key] = t
        if self.store is not None:
            self.store.put("toggle", key, t)
        self._audit(operator, "STRATEGY_MODE", key=key, label=label, mode=target.value, previous=previous.value,
                    reason=reason)
        if self.store is not None:
            self.store.checkpoint_wal()  # the paper audit log is the gate's evidence; make it readable now
        if key != SWING_KEY:
            for book, rt in self.books.items():
                if book.upper() != target.value and key in rt.app.intraday.active:
                    with rt.app.lock:
                        rt.app.intraday.deactivate(key, f"switched to {target.value} by {operator}")
            self._activate_now(key, target)
        return t

    def _activate_now(self, key: str, target: StrategyMode) -> None:
        """Mid-session switch-on: release and validate in the target book right away."""
        rt = {StrategyMode.PAPER: self.paper, StrategyMode.LIVE: self.live}.get(target)
        if rt is None:
            return
        now = self.clock()
        ses = rt.app.session
        if not (ses.is_trading_day(now) and time(9, 20) <= now.time() < ses.no_new_entries_after):
            return
        with rt.app.lock:
            rt.scheduler._last_validate = now
            rt.scheduler.validate()

    def _audit(self, actor: str, action: str, **payload: Any) -> None:
        for rt in self.books.values():
            with rt.app.lock:
                rt.app.audit.record(actor, action, **payload)

    def authorise_live(self, operator: str) -> None:
        if self.live is None:
            raise ToggleError("live book not enabled")
        with self.live.app.lock:
            self.live.app.authorise_live(operator)

    def revoke_live(self, operator: str, reason: str = "master switch off") -> None:
        if self.live is None:
            return
        with self.live.app.lock:
            self.live.app.live_auth.revoke(operator, self.clock())
            self.live.app.audit.record(operator, "LIVE_REVOKED", reason=reason)
        for key, t in list(self.toggles.items()):
            if t.mode is StrategyMode.LIVE:
                self._apply(key, t.label, StrategyMode.PAPER, operator, f"live master switch off: {reason}")

    # ---- automation from config -------------------------------------------------------------
    def auto_paper_check(self) -> list[str]:
        """Switch configured families to PAPER when they have a version and none is on yet.
        Never replaces a version already running (that would restart its 72 h clock)."""
        families = getattr(self, "settings", None) and self.settings.auto_paper or ()
        switched = []
        vs = self.versions()
        for fam in families:
            mine = sorted((v for v in vs.values() if v.family == fam), key=lambda v: v.frozen_at)
            if not mine or any(self.mode(v.content_hash) is not StrategyMode.OFF for v in mine):
                continue
            newest = mine[-1]
            self.set_mode(newest.content_hash, StrategyMode.PAPER, "config:auto_paper",
                          reason=f"auto_paper includes {fam}")
            switched.append(newest.label)
        return switched

    def initial_research(self) -> dict:
        """Startup research when no strategies exist yet: compute outside the lock, freeze inside."""
        sched = self.paper.scheduler
        out, results = sched.compute_research()
        with self.paper.app.lock:
            sched.after_research = None
            try:
                out = sched.freeze_research(out, results)
            finally:
                sched.after_research = self.auto_paper_check
        out["auto_paper"] = self.auto_paper_check()
        self.paper.app.audit.record("desk", "INITIAL_RESEARCH", result=out)
        return out

    # ---- views -----------------------------------------------------------------------------
    def strategy_rows(self) -> list[dict[str, Any]]:
        vs = self.versions()
        # Research freezes a new version every day; show the ones switched on plus the newest per family.
        newest: dict[str, str] = {}
        for h, v in sorted(vs.items(), key=lambda kv: kv[1].frozen_at):
            newest[v.family] = h
        shown = [h for h in vs if self.mode(h) is not StrategyMode.OFF or newest.get(vs[h].family) == h]
        rows = []
        for key in [SWING_KEY, *shown]:
            sv = vs.get(key)
            t = self.toggles.get(key)
            g = self.gate(key) if self.mode(key) is not StrategyMode.OFF else None
            per_book = {}
            for book, rt in self.books.items():
                eng = rt.app.intraday
                per_book[book] = {
                    "active": key in eng.active,
                    "validation": eng.active[key][1].outcome.value if key in eng.active else None,
                    "pnl_today": str(eng.strategy_pnl().get(sv.label, "0")) if sv else None,
                }
            mode = self.mode(key)
            rows.append({
                "key": key,
                "label": sv.label if sv else "Swing — Claude proposals",
                "family": sv.family if sv else "SWING",
                "eligible": (sv.status is StrategyStatus.ELIGIBLE) if sv else True,
                "eligibility_failures": list(sv.eligibility_failures) if sv else [],
                "mode": mode.value,
                "changed_by": t.changed_by if t else None,
                "changed_at": t.changed_at if t else None,
                "paper_trial": bool(sv and sv.status is not StrategyStatus.ELIGIBLE and mode is StrategyMode.PAPER),
                "gate": {"passed": g.passed, "paper_hours": round(g.paper_hours, 1), "clean_sessions": g.clean_sessions,
                         "required_hours": self.live_gate.min_paper_hours,
                         "required_sessions": self.live_gate.min_clean_sessions, "reasons": g.reasons} if g else None,
                "live_blockers": self.live_blockers(key) if mode is StrategyMode.PAPER else [],
                "books": per_book,
            })
        return rows

    def overview(self) -> dict[str, Any]:
        return {
            "as_of": self.clock(),
            "books": list(self.books),
            "live_enabled": self.live is not None,
            "live_master": self.live_master_on(),
            "live_risk_approved": bool(self.live and self.live.app.risk_config.is_approved),
            "strategies": self.strategy_rows(),
            "swing_book": next((b for b, rt in self.books.items() if rt is self.swing_book()), None),
        }

    # ---- running -----------------------------------------------------------------------------
    def run_pending(self) -> dict[str, list[str]]:
        return {book: rt.scheduler.run_pending() for book, rt in self.books.items()}

    def loop(self, tick_seconds: int) -> None:
        while not self._stop.is_set():
            self.run_pending()
            self._stop.wait(tick_seconds)

    def start(self, tick_seconds: int) -> threading.Thread:
        settings = getattr(self, "settings", None)
        if settings is not None:
            self.paper.scheduler.after_research = self.auto_paper_check
            if settings.research_on_start and not self.versions():
                def bootstrap():
                    try:
                        self.initial_research()
                    except Exception as e:  # noqa: BLE001 - surfaced on the dashboard, service keeps running
                        self.paper.app.monitor.alert("HIGH", f"startup research failed: {type(e).__name__}: {e}")
                threading.Thread(target=bootstrap, name="claudetrade-research", daemon=True).start()
            else:
                self.auto_paper_check()
        th = threading.Thread(target=self.loop, args=(tick_seconds,), name="claudetrade-desk", daemon=True)
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()
        for rt in self.books.values():
            rt.stop()

    @classmethod
    def single(cls, rt: Runtime) -> Desk:
        """Wrap one runtime (paper or live) — used by tests and single-book deployments."""
        d = cls.__new__(cls)
        d.paper = rt if rt.app.mode is not OperatingMode.LIVE else None
        d.live = rt if rt.app.mode is OperatingMode.LIVE else None
        d.clock = rt.app.clock
        d.books = {"live" if d.live else "paper": rt}
        d.store = rt.app.persistence.store if rt.app.persistence else None
        d.live_gate = rt.app.live_gate or LiveGate()
        d.toggles = {}
        d._stop = threading.Event()
        d._gate_cache = {}
        if d.paper is None:  # live-only: there is no paper book to route to
            d.paper = rt
        return d


def build_desk(settings: Settings, clock: Callable[[], datetime] | None = None, groww_client: Any | None = None,
               research_loader: Callable[[], dict] | None = None, notifier: Callable[[Any], None] | None = None) -> Desk:
    from .app import system_clock

    clock = clock or system_clock
    paper_settings = replace(settings, mode=OperatingMode.PAPER, db_path=settings.paper_db_path)
    paper = build_runtime(paper_settings, clock=clock, groww_client=groww_client, research_loader=research_loader,
                          notifier=notifier)
    live = None
    if settings.live_enabled:
        client = groww_client or (paper.feed.client if paper.feed else None)
        gate = replace(settings.live_gate, paper_evidence_db=str(Path(settings.paper_db_path).resolve()))
        live_settings = replace(settings, mode=OperatingMode.LIVE, db_path=settings.live_db_path, data_source="none",
                                agent_daily_review=False, live_gate=gate)
        live = build_runtime(live_settings, clock=clock, groww_client=client, research_loader=lambda: {},
                             notifier=notifier)
        if paper.feed is not None:
            paper.feed.add_target(live.app.market_data)
            live.scheduler.feed = paper.feed
    desk = Desk(paper, live, clock)
    desk.settings = settings
    # Claude's after-close review files proposals into whichever book the swing switch points at.
    if paper.scheduler.agent_runner is not None or settings.agent_daily_review:
        def review(task: str):
            rt = desk.swing_book()
            if rt is None:
                return None
            from .agent import run_agent
            from .tools import ToolGateway

            env = "live" if rt is desk.live else "paper"
            return run_agent(ToolGateway(rt.app, env, research_data=paper.scheduler.research_loader()
                                         if paper.scheduler.research_loader else None), task)
        paper.scheduler.agent_runner = review
    return desk
