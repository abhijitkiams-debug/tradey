"""Command-line entry points.

  claudetrade research   [--data DIR] [--days N]   walk-forward all strategy families, freeze results
  claudetrade paper-demo [--days N]                research -> (maybe) release -> replayed paper session
  claudetrade agent "<task>" [--data DIR]          run Claude against the paper gateway (needs anthropic)
  claudetrade serve --config FILE                  scheduler + operator API/dashboard (paper or live)

Without --data, synthetic bars are used. Synthetic results prove the pipeline runs; they
say nothing about real markets.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

from .app import ClaudeTrade
from .backtest import BacktestConfig, EligibilityCriteria, walk_forward
from .clock import SimClock
from .lifecycle import SessionOutcome, StrategyStatus
from .market_data import validate_bars
from .models import IST
from .replay import load_universe, replay_session, session_start
from .strategies import REGISTRY
from .synthetic import universe


def load_csv_dir(path: Path) -> dict[str, pd.DataFrame]:
    """One CSV per symbol: SYMBOL.csv with timestamp,open,high,low,close,volume (IST)."""
    out = {}
    for f in sorted(path.glob("*.csv")):
        df = pd.read_csv(f, parse_dates=["timestamp"]).set_index("timestamp").sort_index()
        if df.index.tz is None:
            df.index = df.index.tz_localize(IST)
        out[f.stem.upper()] = df[["open", "high", "low", "close", "volume"]]
    return out


def _data(args) -> tuple[dict[str, pd.DataFrame], bool]:
    if args.data:
        return load_csv_dir(Path(args.data)), False
    return universe(args.days), True


def research(app: ClaudeTrade, data: dict[str, pd.DataFrame], synthetic: bool) -> list:
    for sym, df in data.items():
        issues = validate_bars(df)
        if issues:
            print(f"[data] {sym}: {issues[:3]}{' ...' if len(issues) > 3 else ''}")
    cfg = BacktestConfig(capital=float(app.risk_config.starting_capital), risk_per_trade=float(app.risk_config.risk_per_trade),
                         max_position_value=float(app.risk_config.max_position_value),
                         max_concurrent=app.risk_config.max_concurrent_positions)
    out = []
    print(f"\n{'family':16} {'OOS trades':>10} {'OOS net ₹':>10} {'PF':>6} {'maxDD%':>7} {'2x slip ₹':>10}  status")
    for cls in REGISTRY.strategies.values():
        wf = walk_forward(cls, data, cfg, app.costs)
        sv = app.strategies.freeze(wf, EligibilityCriteria(), app.risk_config)
        s = wf.summary()
        print(f"{cls.family:16} {s['oos_trades']:>10} {s['oos_net_pnl']:>10} {str(s['oos_profit_factor']):>6} "
              f"{s['oos_max_dd_pct']:>7} {s['stress_net_pnl']['slippage_x2']:>10}  {sv.status.value}")
        for f in sv.eligibility_failures:
            print(f"{'':18}- {f}")
        out.append(sv)
    if synthetic:
        print("\nNOTE: synthetic data — these numbers demonstrate the pipeline, not a market edge.")
    return out


def cmd_research(args) -> int:
    data, synthetic = _data(args)
    app = ClaudeTrade.build()
    research(app, data, synthetic)
    return 0


def cmd_paper_demo(args) -> int:
    data, synthetic = _data(args)
    days = sorted({d for df in data.values() for d in df.index.date})
    research_data = {s: df[df.index.date < days[-1]] for s, df in data.items()}
    session_day = days[-1]
    clock = SimClock(session_start(session_day) - timedelta(hours=16))
    app = ClaudeTrade.build(clock=clock)
    load_universe(app, data)
    print(f"Day T research on {len(days) - 1} sessions ({'synthetic' if synthetic else 'supplied'} data)")
    versions = research(app, research_data, synthetic)
    eligible = [v for v in versions if v.status is StrategyStatus.ELIGIBLE]
    clock.set(session_start(session_day))
    if not eligible:
        print("\nNo strategy met the eligibility criteria → intraday stands down for", session_day, "(no trade is a valid outcome).")
    else:
        best = eligible[0]
        rel = app.strategies.release(best.content_hash, session_day, operator="demo-operator")
        print(f"\nOperator released {best.label} for {session_day}")
        clock.set(session_start(session_day).replace(hour=9, minute=20))
        # seed quotes for validation
        from decimal import Decimal

        from .replay import quote_from_bar
        for s, df in data.items():
            today = df[df.index.date == session_day]
            prev = df[df.index.date < session_day]["close"].iat[-1]
            app.market_data.ingest_quote(quote_from_bar(s, today.iloc[4], Decimal(str(prev)), clock.now))
        v = app.strategies.validate_for_session(rel, app.market_data, list(data), app.risk_config, app.session)
        print(f"Day T+1 validation: {v.outcome.value} {list(v.reasons)}")
        if v.outcome is SessionOutcome.APPROVED:
            app.intraday.activate(best, v, list(data))
    replay_session(app, clock, data, session_day)
    print("\nEnd-of-day report:")
    print(json.dumps(app.intraday.end_of_day_report(), indent=2, default=str))
    print(f"Audit events: {len(app.audit.events())}, chain intact: {app.audit.verify()}")
    return 0


def cmd_agent(args) -> int:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        print("Install the agent extra: pip install 'claudetrade[agent]'", file=sys.stderr)
        return 2
    from .agent import run_agent
    from .tools import ToolGateway

    data, _ = _data(args)
    app = ClaudeTrade.build()
    load_universe(app, data)
    gw = ToolGateway(app, "paper", research_data=data)
    run = run_agent(gw, args.task)
    print(run.final_text)
    print(f"\n[{len(run.tool_calls)} tool calls, stop={run.stop_reason}]")
    return 0


def cmd_groww_check(args) -> int:
    """Read-only checks against your Groww account (RUNBOOK §5a). Places no orders."""
    from .app import system_clock
    from .broker.groww import GrowwBroker

    b = GrowwBroker.from_env(system_clock)
    print("heartbeat (margin endpoint):", "ok" if b.heartbeat() else f"FAILED {b.last_error}")
    try:
        r = b.find_by_client_id("id-000000000000")
        print("unknown reference lookup:", "not found (expected)" if r is None else f"UNEXPECTED: {r}")
    except Exception as e:  # noqa: BLE001 - report whatever the API does
        print(f"unknown reference lookup: raised {type(e).__name__} ({b.last_error}) — report this; timeouts will stay UNKNOWN")
    print("available funds (CNC):", b.get_available_funds())
    print("positions + holdings:", b.get_positions() or "none")
    return 0


def cmd_doctor(args) -> int:
    """Pre-flight checks before (re)starting the service. Read-only: places no orders."""
    import os
    from datetime import datetime, timedelta

    from .app import system_clock
    from .settings import load_settings
    from .strategies import REGISTRY

    results: list[tuple[str, str, str]] = []

    def rec(status: str, name: str, detail: str = "") -> None:
        results.append((status, name, detail))
        print(f"[{status:4}] {name}{': ' + detail if detail else ''}")

    try:
        s = load_settings(args.config)
        rec("PASS", "config", f"{args.config}: universe {list(s.universe)}, live_enabled={s.live_enabled}")
    except Exception as e:  # noqa: BLE001
        rec("FAIL", "config", str(e))
        return 1
    r = s.risk
    rec("INFO", "limits", f"risk/trade ₹{r.risk_per_trade}, daily loss ₹{r.max_daily_loss}, per order ₹{r.max_order_value}")
    if not s.universe:
        rec("FAIL", "universe", "empty: add NSE symbols to trade")
    bad = [f for f in s.auto_paper if f not in REGISTRY.strategies]
    rec("FAIL" if bad else ("PASS" if s.auto_paper else "INFO"), "auto_paper",
        f"unknown families {bad}" if bad else (", ".join(s.auto_paper) or "none: switch strategies to PAPER on the dashboard"))
    tok = os.environ.get("CLAUDETRADE_OPERATOR_TOKEN", "")
    rec("PASS" if len(tok) >= 24 else "FAIL", "operator token", "set" if len(tok) >= 24 else "set CLAUDETRADE_OPERATOR_TOKEN (24+ chars)")
    chan = bool(os.environ.get("CLAUDETRADE_TELEGRAM_BOT_TOKEN") and os.environ.get("CLAUDETRADE_TELEGRAM_CHAT_ID")) or \
        bool(os.environ.get("CLAUDETRADE_ALERT_WEBHOOK_URL"))
    rec("PASS" if chan else "WARN", "alert channel", "configured" if chan else "none: alerts only on the dashboard; required before live")
    if chan and args.send_test_alert:
        from .monitoring import Alert
        from .notify import notifier_from_env

        n = notifier_from_env("ClaudeTrade doctor")
        n.background = False
        n(Alert(system_clock(), "HIGH", "Doctor test alert: if this reached your phone, alerts work.", "al-doctor"))
        rec("PASS" if n.sent else "FAIL", "test alert", "sent" if n.sent else "delivery failed")
    for d in (Path(s.paper_db_path).parent, Path(s.live_db_path).parent, Path(s.research.cache_dir)):
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write-test"
            probe.write_text("ok")
            probe.unlink()
            rec("PASS", "writable", str(d.resolve()))
        except OSError as e:
            rec("FAIL", "writable", f"{d}: {e}")
    now = system_clock()
    if not s.session.holidays:
        rec("WARN", "holidays", "no NSE holidays configured: the scheduler will treat them as trading days")
    rec("INFO", "clock", f"{now:%a %d %b %H:%M} IST, trading day={s.session.is_trading_day(now)}, open now={s.session.is_open(now)}")

    if s.data_source != "groww" and not s.live_enabled:
        rec("WARN", "groww", "data_source is not groww: paper trading has no live prices")
    else:
        try:
            from .broker.groww import GrowwBroker, RateLimiter
            from .broker.groww_auth import RefreshingGrowwClient, diagnose_login, login_from_env
            from .feeds import GrowwFeed
            from .market_data import MarketDataService

            login, flow = login_from_env()
            steps = diagnose_login() if flow == "totp" else []
            for st in steps:
                rec(*st)
            if any(st[0] == "FAIL" and st[1] == "token exchange" for st in steps):
                raise RuntimeError("Groww login failed (see token exchange above); skipping quote/history checks")
            client = RefreshingGrowwClient(login, system_clock, flow)
            b = GrowwBroker(client, system_clock)
            rec("PASS" if b.heartbeat() else "FAIL", "groww login", f"flow={flow}" + (f" ({b.last_error})" if b.last_error else ""))
            if flow == "static_token":
                rec("WARN", "groww refresh", "GROWW_ACCESS_TOKEN cannot refresh itself; use GROWW_API_KEY + GROWW_TOTP_SECRET")
            elif flow == "api_secret":
                rec("WARN", "groww refresh", "API-secret logins need your approval on Groww Cloud every day before 08:45 IST")
            md = MarketDataService(system_clock, s.risk, s.session)
            feed = GrowwFeed(client, md, system_clock, limiter=RateLimiter())
            for sym in s.universe:
                try:
                    q = feed.quote(sym)
                    age = (now - q.source_ts).total_seconds() / 60 if q else None
                    rec("PASS" if q else "FAIL", f"quote {sym}", f"₹{q.ltp}, last trade {age:.0f} min ago" if q else "no price")
                except Exception as e:  # noqa: BLE001
                    rec("FAIL", f"quote {sym}", type(e).__name__)
            if s.universe:
                sym = s.universe[0]
                end = now
                start = end - timedelta(days=7)
                df = feed.candles(sym, datetime.combine(start.date(), datetime.min.time()), end)
                rec("PASS" if len(df) else "FAIL", "history", f"{sym}: {len(df)} one-minute candles in the last 7 days")
        except Exception as e:  # noqa: BLE001
            rec("FAIL", "groww", f"{type(e).__name__}: {e}")
    fails = sum(1 for st, *_ in results if st == "FAIL")
    print(f"\n{'READY' if not fails else 'NOT READY'}: {fails} failure(s), {sum(1 for st, *_ in results if st == 'WARN')} warning(s)")
    return 1 if fails else 0


def cmd_serve(args) -> int:
    import uvicorn

    from .desk import build_desk
    from .server import create_app
    from .settings import load_settings

    settings = load_settings(args.config)
    desk = build_desk(settings)
    desk.start(settings.tick_seconds)
    try:
        uvicorn.run(create_app(desk), host=args.host, port=args.port, log_level="info")
    finally:
        desk.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="claudetrade")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("research", cmd_research), ("paper-demo", cmd_paper_demo), ("agent", cmd_agent)):
        p = sub.add_parser(name)
        p.add_argument("--data", help="directory of SYMBOL.csv files")
        p.add_argument("--days", type=int, default=60)
        if name == "agent":
            p.add_argument("task")
        p.set_defaults(fn=fn)
    gc = sub.add_parser("groww-check", help="read-only checks against your Groww account")
    gc.set_defaults(fn=cmd_groww_check)
    dc = sub.add_parser("doctor", help="pre-flight checks (config, token, alerts, Groww login, quotes, history)")
    dc.add_argument("--config", required=True)
    dc.add_argument("--send-test-alert", action="store_true")
    dc.set_defaults(fn=cmd_doctor)
    sp = sub.add_parser("serve", help="run scheduler + operator API/dashboard")
    sp.add_argument("--config", default=None, help="TOML settings file")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.set_defaults(fn=cmd_serve)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
