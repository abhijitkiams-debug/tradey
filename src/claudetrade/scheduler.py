"""Session scheduler (§9 durable scheduler, §11 key workflow).

A single loop calls ``run_pending()``. Every daily job is idempotent: its completion is
recorded in the store as ``job_run/<date>:<job>``, so a restart mid-day neither skips nor
repeats work. Each job runs inside a guard: an exception pauses new entries, raises an
alert and is recorded, but never kills the loop.

Daily timeline (IST, defaults from ``SessionConfig``):
  09:05  preopen    broker heartbeat, reconciliation, load today's release
  09:20+ validate   Day-T+1 validation of today's released strategy (re-tried while DEFERRED)
  ticks  session    quotes, bars, broker sync, exits, entries, swing stop checks
  15:00+            square-off (driven by the engine inside ticks) until verified flat
  15:35  eod        reconciliation + end-of-day report stored
  16:00  research   history refresh, walk-forward, freeze candidates (release stays manual)
  16:30  review     optional Claude swing review -> proposals awaiting approval
"""

from __future__ import annotations

import threading
import traceback
import uuid
from collections.abc import Callable
from datetime import datetime, time
from typing import Any

from .app import ClaudeTrade
from .backtest import BacktestConfig, walk_forward
from .lifecycle import SessionOutcome
from .models import OrderRequest, OrderType, Product, Side, TradeMode
from .settings import Settings
from .strategies import REGISTRY


class Scheduler:
    def __init__(self, app: ClaudeTrade, settings: Settings, feed: Any | None = None,
                 research_loader: Callable[[], dict] | None = None, agent_runner: Callable[[str], Any] | None = None):
        self.app = app
        self.settings = settings
        self.feed = feed
        self.research_loader = research_loader
        self.agent_runner = agent_runner
        self.store = app.persistence.store if app.persistence else None
        self._mem_done: set[str] = set()
        self._stop = threading.Event()
        self.last_tick: datetime | None = None
        self.errors: list[str] = []

    # ---- durable job bookkeeping ---------------------------------------------------------
    def _key(self, job: str, now: datetime) -> str:
        return f"{now.date().isoformat()}:{job}"

    def done(self, job: str, now: datetime) -> bool:
        k = self._key(job, now)
        if k in self._mem_done:
            return True
        if self.store is not None and k in self.store.latest("job_run"):
            self._mem_done.add(k)
            return True
        return False

    def _mark(self, job: str, now: datetime, result: Any) -> None:
        k = self._key(job, now)
        self._mem_done.add(k)
        if self.store is not None:
            self.store.put("job_run", k, {"at": now, "result": result})

    def _guard(self, job: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - a job failure must not stop the loop
            msg = f"job {job} failed: {type(e).__name__}: {e}"
            self.errors.append(msg)
            self.app.monitor.pause_entries("scheduler", msg)
            self.app.monitor.alert("HIGH", msg)
            self.app.audit.record("scheduler", "JOB_FAILED", job=job, error=msg, trace=traceback.format_exc()[-2000:])
            return None

    # ---- main entry ------------------------------------------------------------------------
    def run_pending(self) -> list[str]:
        with self.app.lock:
            return self._run_pending()

    def _run_pending(self) -> list[str]:
        app, ses = self.app, self.app.session
        now = app.clock()
        ran: list[str] = []
        if not ses.is_trading_day(now):
            return ran
        t = now.time()

        if t >= time(9, 5) and not self.done("preopen", now):
            self._guard("preopen", self.preopen)
            self._mark("preopen", now, "ok")
            ran.append("preopen")

        if ses.market_open <= t < ses.market_close:
            if t >= time(9, 20) and not self.done("validate", now):
                outcome = self._guard("validate", self.validate)
                if outcome != SessionOutcome.DEFERRED.value:
                    self._mark("validate", now, outcome)
                ran.append("validate")
            self._guard("tick", self.tick)
            ran.append("tick")

        if t >= time(15, 35) and not self.done("eod", now):
            self._mark("eod", now, self._guard("eod", self.eod))
            ran.append("eod")
        if t >= time(16, 0) and not self.done("research", now):
            self._mark("research", now, self._guard("research", self.research))
            ran.append("research")
        if t >= time(16, 30) and self.settings.agent_daily_review and self.agent_runner and not self.done("review", now):
            self._mark("review", now, self._guard("review", self.review))
            ran.append("review")
        return ran

    # ---- jobs -------------------------------------------------------------------------------
    def preopen(self) -> None:
        eng = self.app.intraday
        if eng.validation is None or eng.validation.ts.date() != self.app.clock().date():
            eng.reset_for_day()
        if hasattr(self.app.broker, "heartbeat"):
            self.app.broker.heartbeat()
        self.app.execution.sync()
        self.app.execution.reconcile_positions()
        self.app.audit.record("scheduler", "PREOPEN", broker_healthy=self.app.broker.is_healthy())

    def validate(self) -> str:
        app = self.app
        today = app.clock().date()
        releases = [r for r in app.strategies.releases.values() if r.effective_session == today]
        if not releases:
            app.audit.record("scheduler", "NO_RELEASE", date=today.isoformat(),
                             note="no strategy released for today: intraday stands down")
            return "NO_RELEASE"
        rel = sorted(releases, key=lambda r: r.approved_at)[-1]
        self._refresh_quotes()
        v = app.strategies.validate_for_session(rel, app.market_data, list(self.settings.universe), app.risk_config, app.session)
        if v.outcome is SessionOutcome.APPROVED:
            app.intraday.activate(app.strategies.versions[rel.version_hash], v, list(self.settings.universe))
        return v.outcome.value

    def _refresh_quotes(self) -> None:
        if self.feed is None:
            return
        symbols = sorted(set(self.settings.universe) | {p.symbol for p in self.app.portfolio.open_positions()})
        self.feed.poll(symbols)

    def tick(self) -> None:
        app = self.app
        self.last_tick = app.clock()
        if hasattr(app.broker, "heartbeat"):
            app.broker.heartbeat()
        self._refresh_quotes()
        if self.feed is not None and app.intraday.strategy_version is not None:
            for s in self.settings.universe:
                try:
                    app.bars[s] = self.feed.today_bars(s)
                except Exception as e:  # noqa: BLE001 - missing bars just mean no signal this tick
                    app.audit.record("scheduler", "BARS_UNAVAILABLE", symbol=s, error=type(e).__name__)
        if hasattr(app.broker, "step"):
            app.broker.step()  # paper broker simulates the exchange against live quotes
        app.intraday.tick()
        self._swing_stops()

    def _swing_stops(self) -> None:
        """A swing stop-loss was part of the approved plan, so breaching it exits automatically.
        Targets only alert: taking profit is a new decision for the operator."""
        app = self.app
        for pos in app.portfolio.open_positions(TradeMode.SWING):
            if any(o.request.is_exit and o.request.symbol == pos.symbol for o in app.execution.open_orders(TradeMode.SWING)):
                continue
            v = app.market_data.validate_quote(pos.symbol, TradeMode.SWING)
            if v.quote is None:
                continue
            if pos.stop_loss is not None and v.quote.ltp <= pos.stop_loss:
                req = OrderRequest(f"sx-{uuid.uuid4().hex[:12]}", pos.symbol, Side.SELL, abs(pos.quantity), OrderType.MARKET,
                                   Product.CNC, TradeMode.SWING, is_exit=True, origin="system")
                res = app.execution.execute(req)
                app.monitor.alert("HIGH", f"swing stop hit on {pos.symbol} at {v.quote.ltp}: {res.message}")
            elif pos.target is not None and v.quote.ltp >= pos.target:
                key = f"target:{pos.symbol}:{app.clock().date()}"
                if key not in self._mem_done:
                    self._mem_done.add(key)
                    app.monitor.alert("INFO", f"swing target reached on {pos.symbol} at {v.quote.ltp}; review exit")

    def eod(self) -> dict:
        app = self.app
        app.execution.sync()
        mismatches = app.execution.reconcile_positions()
        report = {**app.intraday.end_of_day_report(), "mismatches": mismatches}
        if self.store is not None:
            self.store.put("eod_report", app.clock().date().isoformat(), report)
        app.audit.record("scheduler", "EOD_REPORT", verified_flat=report["verified_flat"], mismatches=mismatches)
        return {"verified_flat": report["verified_flat"], "mismatches": len(mismatches)}

    def research(self) -> dict:
        if self.research_loader is None:
            return {"skipped": "no research data source"}
        data = self.research_loader()
        if not data:
            return {"skipped": "empty dataset"}
        app, rs = self.app, self.settings.research
        cfg = BacktestConfig(capital=float(app.risk_config.starting_capital), risk_per_trade=float(app.risk_config.risk_per_trade),
                             max_position_value=float(app.risk_config.max_position_value),
                             max_concurrent=app.risk_config.max_concurrent_positions,
                             square_off=app.session.square_off_deadline.strftime("%H:%M"))
        out = {}
        for cls in REGISTRY.strategies.values():
            try:
                wf = walk_forward(cls, data, cfg, app.costs, train_days=rs.train_days, test_days=rs.test_days)
            except ValueError as e:
                out[cls.family] = f"skipped: {e}"
                continue
            sv = app.strategies.freeze(wf, self.settings.eligibility, app.risk_config)
            out[cls.family] = sv.status.value
        return out

    def review(self) -> dict:
        run = self.agent_runner(
            "After-close review. Check portfolio state, today's alerts and the universe "
            f"{list(self.settings.universe)}. Propose at most one swing trade if the evidence supports it, "
            "otherwise say why no trade is appropriate.")
        return {"tool_calls": len(getattr(run, "tool_calls", [])), "stop": getattr(run, "stop_reason", None)}

    # ---- loop --------------------------------------------------------------------------------
    def loop(self, sleep: Callable[[float], bool] | None = None) -> None:
        wait = sleep or self._stop.wait
        while not self._stop.is_set():
            self.run_pending()
            wait(self.settings.tick_seconds)

    def start(self) -> threading.Thread:
        th = threading.Thread(target=self.loop, name="claudetrade-scheduler", daemon=True)
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()
