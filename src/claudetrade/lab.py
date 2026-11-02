"""Strategy Lab: run the positional strategies (BNF, momentum, combined) from the browser.

* ``Lab.start_backtest(params)`` downloads daily history (cached, topped up incrementally) and
  backtests in a background thread; ``status()`` reports progress and the last result.
* ``Lab.start_scan(params)`` finds today's signals on the latest daily close.
* ``Lab.propose(desk, symbol, operator)`` turns a signal into a swing proposal in the book the
  SWING switch points at. It is *only a proposal*: a person approves it in the dashboard, and
  the usual risk checks and approval re-checks apply. Nothing here can place an order.

With no Groww connection the Lab runs on clearly-labelled made-up prices so the page can still
be tried out.
"""

from __future__ import annotations

import threading
import traceback
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import positional as pos
from .models import Instrument


class Lab:
    def __init__(self, cache_dir: str | Path = "var/daily", years: float = 7.0):
        self.cache_dir = Path(cache_dir)
        self.years = years
        self.state = "idle"  # idle | running | done | error
        self.task = ""
        self.log: list[str] = []
        self.result: dict[str, Any] | None = None
        self.scan: dict[str, Any] | None = None
        self.data_note = ""
        self.finished = 0  # bumps when any job ends, so the page knows to redraw
        self._lock = threading.Lock()
        self._data: tuple[dict, Any, dict] | None = None
        self._data_key: tuple | None = None

    # ---- data ---------------------------------------------------------------------------------
    def _load(self, desk: Any, universe: str) -> tuple[dict, Any, dict, bool]:
        sectors = pos.load_universe(universe)
        today = desk.paper.app.clock().date()
        start = today - timedelta(days=int(self.years * 365.25))
        feed = desk.paper.feed
        real = feed is not None and not getattr(desk, "setup_needed", False)
        key = (universe, today, real)
        if self._data is not None and self._data_key == key:
            return (*self._data, real)
        if real:
            self._say(f"Getting daily prices for {len(sectors)} stocks + Nifty from Groww (first time: ~{len(sectors) // 20} min; "
                      "later runs only fetch new days)")
            data, index, errors = pos.load_dataset(feed, sectors, start, today, self.cache_dir, progress=self._say)
            self.data_note = (f"Real Groww prices, {len(data)} stocks" + (f" ({len(errors)} skipped)" if errors else ""))
        else:
            data, index = pos.synthetic_dataset(list(sectors)[:120], date(2020, 1, 1), today)
            errors = {}
            self.data_note = "MADE-UP prices (Groww not connected): this only shows how the page works"
        self._data, self._data_key = (data, index, sectors), key
        return data, index, sectors, real

    def _say(self, msg: str) -> None:
        self.log.append(msg)
        del self.log[:-30]

    # ---- jobs ---------------------------------------------------------------------------------
    def _run(self, name: str, fn) -> str:
        with self._lock:
            if self.state == "running":
                return f"Busy: {self.task} is still running."
            self.state, self.task, self.log = "running", name, []
        def go():
            try:
                fn()
                self.state = "done"
            except Exception as e:  # noqa: BLE001
                self._say(traceback.format_exc()[-1200:])
                self._say(f"Failed: {type(e).__name__}: {e}")
                self.state = "error"
            self.finished += 1
        threading.Thread(target=go, name=f"claudetrade-lab-{name}", daemon=True).start()
        return f"Started {name}."

    def start_backtest(self, desk: Any, raw: dict[str, Any] | None = None, universe: str = "nifty200") -> str:
        params = pos.BacktestParams.from_dict(raw)
        self._check_universe(universe)

        def job():
            data, index, sectors, real = self._load(desk, universe)
            self._say(f"Backtesting {params.strategy} on {len(data)} stocks…")
            bnf_only = set(pos.load_universe("nifty200")) if universe != "nifty200" else None
            res = pos.backtest(data, index, params, costs=desk.paper.app.costs,
                               sectors=sectors, bnf_symbols=bnf_only)
            res["data"] = {"real": real, "note": self.data_note, "universe": universe}
            self.result = res
            self._say("Done.")
        return self._run("backtest", job)

    def start_scan(self, desk: Any, raw: dict[str, Any] | None = None, universe: str = "nifty200") -> str:
        params = pos.BacktestParams.from_dict(raw)
        self._check_universe(universe)

        def job():
            data, index, sectors, real = self._load(desk, universe)
            app = desk.paper.app
            equity = float(app.ledger.cash()) if real else params.capital
            bnf_only = set(pos.load_universe("nifty200")) if universe != "nifty200" else None
            res = pos.scan_today(data, index, params, equity=equity, max_order_value=float(app.risk_config.max_order_value),
                                 sectors=sectors, bnf_symbols=bnf_only)
            res["data"] = {"real": real, "note": self.data_note, "universe": universe}
            self.scan = res
            self._say(f"Found {len(res['signals'])} signal(s). Market: {res['regime']}.")
        return self._run("scan", job)

    @staticmethod
    def _check_universe(universe: str) -> None:
        if universe not in ("nifty200", "nifty500"):
            raise ValueError("universe must be nifty200 or nifty500")

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "task": self.task, "log": self.log[-8:], "result": self.result, "scan": self.scan,
                "data_note": self.data_note,
                "finished": self.finished}

    # ---- signal -> proposal -------------------------------------------------------------------
    def propose(self, desk: Any, symbol: str, operator: str) -> str:
        if not self.scan or not self.scan.get("data", {}).get("real"):
            return "Run “Find today's signals” on real Groww prices first."
        sig = next((s for s in self.scan["signals"] if s["symbol"] == symbol), None)
        if sig is None:
            return f"{symbol} is not in today's signals."
        if sig["quantity"] < 1:
            return f"{symbol}: one share costs more than your per-order limit, so it can't be proposed."
        rt = desk.swing_book()
        if rt is None:
            return "Swing proposals are OFF. In Advanced, set the SWING switch to PAPER first."
        app = rt.app
        if symbol not in app.market_data.instruments:
            app.market_data.register(Instrument(symbol))
        feed = desk.paper.feed
        if feed is not None:
            feed.add_target(app.market_data)
            feed.poll([symbol])  # the proposal is anchored to a current Groww quote
        from .swing import SchemaError

        with app.lock:
            try:
                p = app.swing.submit_proposal(pos.proposal_from_signal(sig), actor=f"{operator}:strategy-lab")
            except SchemaError as e:
                return f"Not accepted: {e}"
        book = "LIVE" if rt is desk.live else "PAPER"
        return f"Proposal {p.proposal_id} for {symbol} is waiting for your approval in Advanced ({book} book)."
