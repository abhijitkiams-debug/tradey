"""Off-hours paper test: replay a past trading day through the full desk.

Runs the same code path as a live paper session, minute by minute on a simulated clock:
startup research (on days *before* the replay day only — no look-ahead), auto-PAPER switches,
pre-open, Day-T+1 validation at 09:20, signals and entries, simulated fills, stops/targets,
square-off from 15:00, and the 15:35 end-of-day report.

It uses its own throwaway database (``var/replay/<date>.db``), so it never touches the real
paper book and never counts toward the 72-hour live gate.
"""

from __future__ import annotations

import threading
import time as _time
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from .clock import SimClock
from .models import IST, OperatingMode, TradeMode
from .replay import quote_from_bar
from .settings import Settings

DataLoader = Callable[[date], tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]]


def groww_loader(settings: Settings) -> tuple[DataLoader, Callable[[date], bool]]:
    """(research history before the day, that day's 1-minute bars) from Groww."""
    from .app import system_clock
    from .broker.groww import RateLimiter
    from .feeds import GrowwFeed
    from .market_data import MarketDataService
    from .runtime import _groww_client

    client = _groww_client()
    feed = GrowwFeed(client, MarketDataService(system_clock, settings.risk, settings.session), system_clock,
                     limiter=RateLimiter())
    cache = Path(settings.research.cache_dir)

    def day_bars(day: date) -> dict[str, pd.DataFrame]:
        out = {}
        for s in settings.universe:
            df = feed.candles(s, datetime.combine(day, time(9, 15), IST), datetime.combine(day, time(15, 30), IST))
            if len(df):
                out[s] = df
        return out

    def load(day: date):
        research = {}
        for s in settings.universe:
            h = feed.history(s, settings.research.history_days, end=day - timedelta(days=1), cache_dir=cache)
            h = h[h.index.date < day]
            if len(h):
                research[s] = h
        return research, day_bars(day)

    def traded(day: date) -> bool:
        if not settings.universe:
            return False
        s = settings.universe[0]
        return len(feed.candles(s, datetime.combine(day, time(9, 15), IST), datetime.combine(day, time(15, 30), IST))) > 0

    return load, traded


def synthetic_loader(settings: Settings) -> tuple[DataLoader, Callable[[date], bool]]:
    from .synthetic import trading_days, universe

    syms = settings.universe or ("ALPHA", "BRAVO", "CHARLIE", "DELTA")

    def load(day: date):
        start = trading_days(day - timedelta(days=settings.research.history_days + 10), 1)[0]
        n = len([d for d in pd.bdate_range(start, day)])
        data = universe(n, start=start)
        data = dict(zip(syms, data.values(), strict=False))
        research = {s: df[df.index.date < day] for s, df in data.items()}
        today = {s: df[df.index.date == day] for s, df in data.items()}
        return research, {s: df for s, df in today.items() if len(df)}

    return load, (lambda d: d.weekday() < 5)


def _label_for(app, release_id: str) -> str:
    prefix = release_id.split("-")[1] if release_id.count("-") >= 2 else ""
    return next((v.label for h, v in app.strategies.versions.items() if prefix and h.startswith(prefix)), release_id)


def last_trading_day(now: datetime, traded: Callable[[date], bool]) -> date:
    d = now.date() if now.time() >= time(15, 30) else now.date() - timedelta(days=1)
    for _ in range(10):
        if d.weekday() < 5 and traded(d):
            return d
        d -= timedelta(days=1)
    raise RuntimeError("no trading day with data in the last 10 days")


def replay_day(settings: Settings, day: date, load: DataLoader, db_dir: str = "var/replay",
               speed_s: float = 0.0, serve: Callable[[Any], None] | None = None,
               progress: Callable[[str], None] = print) -> dict:
    from .desk import build_desk

    db = Path(db_dir) / f"{day.isoformat()}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    for f in db.parent.glob(db.name + "*"):
        f.unlink()  # fresh every run
    progress(f"replaying {day:%a %d %b %Y} into {db} (separate from the real paper book)")
    research, today = load(day)
    if not today:
        raise RuntimeError(f"no 1-minute data for {day}: market closed that day?")
    progress(f"history: {', '.join(f'{s} {len(df)} bars' for s, df in research.items())}")
    progress(f"replay day: {', '.join(f'{s} {len(df)} bars' for s, df in today.items())}")

    clock = SimClock(datetime.combine(day, time(8, 30), IST))
    rs = replace(settings, paper_db_path=str(db), live_enabled=False, data_source="none", agent_daily_review=False,
                 universe=tuple(today), mode=OperatingMode.PAPER)
    desk = build_desk(rs, clock=clock, research_loader=lambda: research, notifier=lambda a: None)
    app = desk.paper.app
    if serve is not None:
        threading.Thread(target=serve, args=(desk,), daemon=True).start()
    progress("research + auto_paper ...")
    out = desk.initial_research()
    progress(f"  {out}")

    prev_close = {s: Decimal(str(research[s]["close"].iat[-1])) if s in research and len(research[s])
                  else Decimal(str(df["open"].iat[0])) for s, df in today.items()}
    minutes = sorted({ts for df in today.values() for ts in df.index})
    t0 = _time.monotonic()
    for ts in minutes:
        now = ts.to_pydatetime() + timedelta(minutes=1)  # bar is complete one minute after it opens
        with app.lock:
            clock.set(now)
            for s, df in today.items():
                if ts in df.index:
                    app.market_data.ingest_quote(quote_from_bar(s, df.loc[ts], prev_close[s], now))
                    app.bars[s] = df[df.index <= ts]
        desk.run_pending()
        if speed_s:
            _time.sleep(speed_s)
    clock.set(datetime.combine(day, time(15, 40), IST))  # end-of-day report; stop before 16:00 research
    desk.run_pending()

    rep = (app.persistence.store.latest("eod_report").get(day.isoformat()) or {}) if app.persistence else {}
    orders = [o for o in app.execution.orders.values() if o.created_at and o.created_at.date() == day]
    fills = [e.payload for e in app.audit.events("FILL") if e.ts.date() == day]
    summary = {
        "day": day.isoformat(),
        "seconds": round(_time.monotonic() - t0, 1),
        "strategies": [{"label": r["label"], "mode": r["mode"], "validation_today": r["books"]["paper"]["validation"],
                        "pnl_today": r["books"]["paper"]["pnl_today"]} for r in desk.strategy_rows() if r["key"] != "SWING"],
        "validations": [{"strategy": _label_for(app, e.payload.get("release", "")), "outcome": e.payload["outcome"],
                         "reasons": list(e.payload["reasons"][:2])}
                        for e in app.audit.events("SESSION_VALIDATION") if e.ts.date() == day],
        "orders": len(orders),
        "entries": sum(1 for o in orders if not o.request.is_exit),
        "exits": sum(1 for o in orders if o.request.is_exit),
        "rejected_by_risk": sum(1 for e in app.audit.events("RISK_DECISION") if e.ts.date() == day and not e.payload["approved"]),
        "fills": [{k: f[k] for k in ("symbol", "side", "qty", "price", "realised")} for f in fills],
        "realised_pnl": str(app.portfolio.realised_pnl_on(day)),
        "charges": str(app.ledger.charges_paid()),
        "verified_flat": rep.get("verified_flat"),
        "square_off_failed": rep.get("square_off_failed"),
        "open_intraday_positions": len(app.portfolio.open_positions(TradeMode.INTRADAY)),
        "audit_chain_intact": app.audit.verify(),
        "db": str(db),
    }
    summary["_desk"] = desk
    return summary
