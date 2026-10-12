"""Assembles a running system from ``Settings``: store, broker, data feed, scheduler, Claude.

PAPER mode with ``data_source = "groww"`` paper-trades against live Groww quotes (the paper
broker fills against them) — the PRD's Phase 2 setup. LIVE mode swaps in the Groww broker,
but orders still require an approved risk config *and* a runtime operator authorisation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .app import ClaudeTrade, system_clock
from .models import Instrument, OperatingMode
from .scheduler import Scheduler
from .settings import Settings
from .store import Store


@dataclass
class Runtime:
    settings: Settings
    app: ClaudeTrade
    scheduler: Scheduler
    feed: Any | None

    def start(self) -> None:
        self.scheduler.start()

    def stop(self) -> None:
        self.scheduler.stop()
        if self.app.persistence:
            self.app.persistence.store.close()


def _groww_client():
    from .broker.groww import GrowwBroker

    return GrowwBroker.from_env(system_clock).client


def build_runtime(settings: Settings, clock: Callable[[], datetime] = system_clock,
                  groww_client: Any | None = None, research_loader: Callable[[], dict] | None = None) -> Runtime:
    store = Store(settings.db_path)
    broker = None
    client = groww_client
    if settings.data_source == "groww" or settings.mode is OperatingMode.LIVE:
        client = client or _groww_client()
    if settings.mode is OperatingMode.LIVE:
        from .broker.groww import GrowwBroker

        broker = GrowwBroker(client, clock)
    app = ClaudeTrade.build(mode=settings.mode, clock=clock, risk_config=settings.risk, session=settings.session,
                            broker=broker, store=store)
    app.execution.external_cnc = dict(settings.external_holdings)
    for sym in settings.universe:
        vol = settings.avg_daily_volume.get(sym, settings.default_avg_daily_volume)
        app.market_data.register(Instrument(sym), avg_daily_volume=vol)

    feed = None
    if settings.data_source == "groww" and client is not None:
        from .feeds import GrowwFeed

        feed = GrowwFeed(client, app.market_data, clock)
        if research_loader is None:
            cache = Path(settings.research.cache_dir)

            def research_loader() -> dict:
                data, issues = feed.research_dataset(list(settings.universe), settings.research.history_days, cache)
                if issues:
                    app.audit.record("research", "DATA_ISSUES", issues=issues)
                return data

    agent_runner = None
    if settings.agent_daily_review:
        from .agent import run_agent
        from .tools import ToolGateway

        env = "live" if settings.mode is OperatingMode.LIVE else "paper"

        def agent_runner(task: str):
            return run_agent(ToolGateway(app, env, research_data=research_loader() if research_loader else None), task)

    sched = Scheduler(app, settings, feed=feed, research_loader=research_loader, agent_runner=agent_runner)
    return Runtime(settings, app, sched, feed)
