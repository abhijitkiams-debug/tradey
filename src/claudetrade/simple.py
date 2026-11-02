"""Simple mode: everything a non-expert needs, in plain words.

* ``summary(desk)``: one status sentence, pretend-money balance, today's result, trades today.
* ``checks(desk)``: an "Is everything OK?" checklist, each item ✅/⚠️/❌ with what to do.
* actions: start practice, pause, resume, emergency stop, clear emergency stop.
* ``Practice``: replay a past trading day in the background (separate database).

Nothing here can turn on real-money trading; that stays in the Advanced dashboard behind its
own gates.
"""

from __future__ import annotations

import threading
import traceback
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

from .desk import Desk, StrategyMode
from .evidence import SWING_KEY
from .models import TradeMode

_cache: dict[str, tuple[datetime, Any]] = {}


def _cached(key: str, now: datetime, ttl_s: int, fn):
    hit = _cache.get(key)
    if hit and (now - hit[0]).total_seconds() < ttl_s:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def _fmt_time(t: time) -> str:
    return datetime.combine(datetime.today(), t).strftime("%-I:%M %p")


def market_state(desk: Desk) -> tuple[bool, str]:
    app = desk.paper.app
    now, ses = app.clock(), app.session
    if ses.is_open(now):
        return True, f"Market is open until {_fmt_time(ses.market_close)}."
    nxt = now if now.time() < ses.market_open else now + timedelta(days=1)
    while not ses.is_trading_day(nxt):
        nxt += timedelta(days=1)
    when = "today" if nxt.date() == now.date() else ("tomorrow" if nxt.date() == now.date() + timedelta(days=1)
                                                     else nxt.strftime("%A"))
    return False, f"Market is closed. It opens {when} at {_fmt_time(ses.market_open)}."


def _money(x: Decimal) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}₹{abs(x):,.2f}"


def summary(desk: Desk) -> dict[str, Any]:
    app = desk.paper.app
    now = app.clock()
    is_open, market = market_state(desk)
    prices = {p.symbol: v.quote.ltp for p in app.portfolio.open_positions()
              if (v := app.market_data.validate_quote(p.symbol, p.mode)).quote}
    equity = app.ledger.cash() + sum((Decimal(p.quantity) * prices.get(p.symbol, p.average_price)
                                      for p in app.portfolio.open_positions()), Decimal("0"))
    today = app.portfolio.realised_pnl_on(now.date()) + app.portfolio.unrealised_pnl(prices)
    trades = sum(1 for e in app.audit.events("FILL") if e.ts.date() == now.date())
    practising = [r for r in desk.strategy_rows() if r["key"] != SWING_KEY and r["mode"] == "PAPER"]
    c = app.controls
    setup = getattr(desk, "setup_needed", False)

    if setup:
        tone, headline = "warn", "Almost ready: connect your Groww account below to start practising."
    elif c.kill_switch:
        tone, headline = "bad", "Emergency stop is ON. No new trades. Press “Clear emergency stop” when it's safe."
    elif c.entries_paused or c.paused_modes:
        tone, headline = "warn", "Paused. No new trades until you press Resume."
    elif not practising:
        tone, headline = "warn", "Not practising yet. Press “Start practice”."
    elif is_open:
        tone, headline = "good", f"Practising with pretend money. Watching {len(app.market_data.instruments)} stocks."
    else:
        tone, headline = "good", "Ready. Practice trading will start automatically when the market opens."
    return {
        "as_of": now.isoformat(), "tone": tone, "headline": headline, "market": market, "market_open": is_open,
        "setup_needed": setup,
        "pretend_money": _money(equity), "start_money": _money(app.risk_config.starting_capital),
        "today_result": _money(today), "today_positive": today >= 0, "trades_today": trades,
        "open_trades": len(app.portfolio.open_positions(TradeMode.INTRADAY)),
        "practising": [r["family"] for r in practising],
        "paused": bool(c.entries_paused or c.paused_modes), "emergency_stop": c.kill_switch,
        "real_money": ("ON" if desk.live_master_on() else "OFF") if desk.live else "OFF (not set up)",
    }


def checks(desk: Desk) -> list[dict[str, Any]]:
    app = desk.paper.app
    now = app.clock()
    out: list[dict[str, Any]] = []

    def item(ok: bool | None, title: str, detail: str = "", fix: str = "") -> None:
        out.append({"ok": ok, "title": title, "detail": detail, "fix": fix})

    if getattr(desk, "setup_needed", False):
        item(False, "Groww connected", "Not connected yet.", "Fill in the “Connect Groww” box below.")
    else:
        client = desk.paper.feed.client if desk.paper.feed else None

        def probe():
            if client is None:
                return ("err", "no data source")
            try:
                prof = client.get_user_profile(timeout=10) or {}
            except Exception as e:  # noqa: BLE001
                return ("err", f"{type(e).__name__}: {getattr(e, 'msg', '')}")
            return ("ok", prof)
        state, val = _cached("groww", now, 60, probe)
        if client is None:
            item(None, "Groww", "Not in use (practice uses made-up prices).")
        elif state == "err":
            item(False, "Groww connected", "Groww didn't accept the login.",
                 "Open “Connect Groww” below and enter a fresh TOTP token and secret.")
        if client is not None and state != "err":
            item(True, "Groww connected")
            nse = "CASH" in (val.get("active_segments") or []) and val.get("nse_enabled")
            item(bool(nse), "Can see NSE stock prices", "" if nse else "Groww says NSE isn't enabled for this account.",
                 "" if nse else "Check your Groww Trading API plan is active, then contact Groww support.")

    is_open, market = market_state(desk)
    if is_open:
        fresh = [q for q in app.market_data._quotes.values() if (now - q.source_ts).total_seconds() < 180]
        item(bool(fresh), "Live prices arriving", f"{len(fresh)} of {len(app.market_data.instruments)} stocks updated.",
             "" if fresh else "Check the internet connection. If it lasts, restart with “Stop” then “Start”.")
        last = desk.paper.scheduler.last_tick
        alive = last is not None and (now - last).total_seconds() < 180
        item(alive, "App is running", "" if alive else "The app hasn't checked the market recently.",
             "" if alive else "Make sure the computer isn't asleep, then restart with “Stop” then “Start”.")
    else:
        item(None, "Live prices", market)

    practising = [r for r in desk.strategy_rows() if r["key"] != SWING_KEY and r["mode"] == "PAPER"]
    item(bool(practising), "Practice strategies switched on",
         ", ".join(r["family"].replace("_", " ").title() for r in practising) or "None.",
         "" if practising else "Press “Start practice”. (Right after the first start, wait a few minutes for setup.)")
    item(not app.controls.kill_switch, "Emergency stop is off", "",
         "" if not app.controls.kill_switch else "Press “Clear emergency stop” when it's safe.")
    item(True if app.alert_channel_configured else None, "Phone alerts",
         "On." if app.alert_channel_configured else "Off (optional while practising).",
         "" if app.alert_channel_configured else "Needed only before real-money trading (see the guide).")
    return out


# ---- actions ---------------------------------------------------------------------------------
def start_practice(desk: Desk, operator: str) -> str:
    app = desk.paper.app
    if getattr(desk, "setup_needed", False):
        return "Connect Groww first (box below)."
    if app.controls.kill_switch:
        return "Emergency stop is on. Press “Clear emergency stop” first."
    vs = desk.versions()
    if not vs:
        return "Still getting ready (downloading price history). Try again in a few minutes."
    families = sorted({v.family for v in vs.values()})
    switched = []
    for fam in families:
        mine = sorted((v for v in vs.values() if v.family == fam), key=lambda v: v.frozen_at)
        if any(desk.mode(v.content_hash) is not StrategyMode.OFF for v in mine):
            continue
        desk.set_mode(mine[-1].content_hash, StrategyMode.PAPER, operator, reason="simple mode: start practice")
        switched.append(fam)
    with app.lock:
        if app.controls.entries_paused or app.controls.paused_modes:
            app.monitor.resume_entries(operator, "simple mode: start practice")
    return "Practice is on." + (f" Switched on: {', '.join(f.replace('_', ' ').title() for f in switched)}." if switched else "")


def pause(desk: Desk, operator: str) -> str:
    for rt in desk.books.values():
        with rt.app.lock:
            rt.app.monitor.pause_entries(operator, "simple mode: pause")
    return "Paused. No new trades. Trades already open keep their safety stops."


def resume(desk: Desk, operator: str) -> str:
    app = desk.paper.app
    if app.controls.kill_switch:
        return "Emergency stop is on. Press “Clear emergency stop” first."
    with app.lock:
        app.monitor.resume_entries(operator, "simple mode: resume")
    return "Resumed."


def emergency_stop(desk: Desk, operator: str) -> str:
    for rt in desk.books.values():
        with rt.app.lock:
            rt.app.monitor.activate_kill_switch(operator, "simple mode: emergency stop")
            rt.app.audit.record(operator, "SQUARE_OFF_REQUESTED", reason="simple mode: emergency stop")
            rt.app.intraday.run_square_off()
    return "Emergency stop ON. All open day-trades are being closed."


def clear_stop(desk: Desk, operator: str) -> str:
    msgs = []
    for name, rt in desk.books.items():
        with rt.app.lock:
            mism = rt.app.execution.reconcile_positions()
            if mism:
                msgs.append(f"{name}: positions don't match the broker yet ({len(mism)}); try again shortly")
                continue
            if rt.app.controls.kill_switch:
                rt.app.monitor.reset_kill_switch(operator, "simple mode: clear emergency stop")
    return "; ".join(msgs) or "Emergency stop cleared. Press Resume or Start practice to trade again."


# ---- practice on a past day ------------------------------------------------------------------
class Practice:
    def __init__(self) -> None:
        self.state = "idle"
        self.result: dict[str, Any] | None = None
        self.log: list[str] = []
        self._lock = threading.Lock()

    def start(self, desk: Desk, day=None) -> str:
        with self._lock:
            if self.state == "running":
                return "A practice run is already going."
            self.state, self.result, self.log = "running", None, []
        threading.Thread(target=self._run, args=(desk, day), name="claudetrade-practice", daemon=True).start()
        return "Practice started. It takes about a minute."

    def _run(self, desk: Desk, day) -> None:
        from .replay_day import groww_loader, last_trading_day, replay_day, synthetic_loader
        from .secrets_store import has_groww

        try:
            s = desk.settings
            real = s.data_source == "groww" and has_groww() and not getattr(desk, "setup_needed", False)
            load, traded = groww_loader(s) if real else synthetic_loader(s)
            day = day or last_trading_day(desk.paper.app.clock(), traded)
            out = replay_day(s, day, load, progress=self.log.append)
            out.pop("_desk", None)
            pnl = Decimal(out["realised_pnl"])
            safe = out["verified_flat"] and out["open_intraday_positions"] == 0
            self.result = {
                "day": day.strftime("%A %d %b %Y"), "real_data": real,
                "trades": out["entries"], "result": _money(pnl), "positive": pnl >= 0, "fees": _money(Decimal(out["charges"])),
                "ended_safely": bool(safe),
                "sentence": (f"On {day:%A %d %b} the strategies made {out['entries']} practice trade(s). "
                             f"Result: {'made' if pnl >= 0 else 'lost'} {_money(abs(pnl))} "
                             f"(after {_money(Decimal(out['charges']))} in fees)."),
                "safety": ("✅ Everything was closed safely by 3:10 PM." if safe
                           else "⚠️ Something stayed open. Look in Advanced."),
                "details": out,
            }
            self.state = "done"
        except Exception as e:  # noqa: BLE001
            self.log.append(traceback.format_exc()[-1500:])
            self.result = {"sentence": f"Practice couldn't run: {e}"}
            self.state = "error"

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "result": self.result, "log": self.log[-6:]}
