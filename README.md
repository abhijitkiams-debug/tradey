# ClaudeTrade

AI-assisted trading for NSE cash equities with ₹10,000 capital. Claude does the research,
risk rules are enforced in code, and every broker order is checked and reconciled.
Implements PRD v1.0 Phases 1–3 (foundation, paper trading, swing approvals) and the
machinery for the Phase 4 live pilot. **Paper trading on live Groww data works out of the
box. Live orders need you to approve risk limits, start in LIVE mode and type an explicit
authorisation, and the pilot checklist below should be completed first.**

> Research intelligently. Validate independently. Execute deterministically. Reconcile continuously.

## Run it

```bash
pip install -e ".[server,groww,agent]"
cp config/claudetrade.example.toml config/claudetrade.toml   # universe, limits; live_enabled = true when ready
export CLAUDETRADE_OPERATOR_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
export GROWW_API_KEY=... GROWW_API_SECRET=...            # or GROWW_ACCESS_TOKEN / GROWW_TOTP_SECRET
claudetrade serve --config config/claudetrade.toml       # http://127.0.0.1:8000, sign in with the token
```

Or with Docker: copy `.env.example` to `.env`, put your config at `config/claudetrade.toml`, then run
`docker compose up -d`.

### The dashboard: one switch per strategy

`serve` runs the **desk**, which has two books with completely separate money:

| Book | Money | Broker |
|---|---|---|
| **Paper** | Virtual ₹10,000 | Simulated fills against live Groww prices |
| **Live** (`live_enabled = true`) | Your Groww account | Groww API |

Every frozen strategy version, plus "Swing: Claude proposals", gets a switch: **OFF · PAPER · LIVE**.

- **OFF → PAPER:** any time. A strategy that hasn't passed the eligibility checks runs as a *paper-only trial*.
- **PAPER → LIVE:** needs everything below. The LIVE button stays disabled and lists what's missing:
  - the strategy is eligible;
  - the live master switch is on, with approved live limits;
  - *this strategy's own* paper record: 72 h since it was switched to PAPER, and its last 3 paper sessions clean;
  - an alert channel;
  - typing `ENABLE LIVE TRADING`.
- **LIVE → PAPER/OFF:** immediate. New live entries stop at once; open live positions keep their stops and square-off.
- **Master switch off, or a restart:** every LIVE strategy drops back to PAPER automatically.

A switch is a standing release: each morning every switched-on strategy is validated for that day
before it can trade. Switching on during the session validates it straight away.

The **Paper / Live tabs** show each book's portfolio, positions, orders, swing approvals, intraday
P&L by strategy, alerts and audit trail, with its own controls: kill switch, pause/resume, square-off,
reconcile, approve limits. **Kill ALL books** stops both at once.

What happens each trading day (`scheduler.py`, times in IST):

| Time | Job |
|---|---|
| 09:05 | Pre-open: broker heartbeat, reconcile positions, reset yesterday's intraday state |
| 09:20+ | Day-T+1 validation of the strategy *you* released for today. With no release, intraday stands down |
| Session | Every `tick_seconds`: Groww quotes and 1-min candles, broker sync, stop/target exits, entries from the frozen strategy, swing stop-loss exits |
| 15:00–15:10 | Square-off, then broker-verified flat. If that fails: kill switch + CRITICAL alert |
| 15:35 | End-of-day reconciliation and report |
| 16:00 | Research: 120 days of Groww history, walk-forward on all 3 families, freeze. Releasing a version stays your decision |
| 16:30 | Optional: Claude reviews and may file swing proposals for you to approve (`agent_daily_review = true`) |

Jobs record their completion in the database, so a restart mid-day neither repeats nor skips them.

The dashboard shows portfolio, positions, pending orders, swing proposals (with full rationale,
risks, max loss and a hash-bound Approve button), approvals to execute, intraday status, research
results with Release buttons, alerts, reconciliation and the audit trail. The controls are kill
switch, pause/resume, manual square-off, reconcile, approve limits, and authorise/revoke live.

## Development

```bash
pip install -e ".[dev,server]"
pytest -q                          # ~150 tests, ~40 s, no network needed
ruff check src tests

claudetrade research               # walk-forward all 3 strategy families (synthetic data)
claudetrade research --data DIR    # DIR/SYMBOL.csv: timestamp,open,high,low,close,volume (1-min, IST)
claudetrade paper-demo             # Day-T research → release → Day-T+1 validation → replayed paper session
pip install -e ".[agent]"
claudetrade agent "Review today's opportunities and propose at most one swing trade"
```

Without `--data`, the commands use **synthetic random-walk bars**. On that data no strategy
passes the eligibility gate, so the demo stands down for the day ("no trade is a valid
outcome"). Synthetic results show the pipeline runs. They are not evidence of a market edge.

## Architecture

```text
market_data ──► backtest / strategies ──► lifecycle (freeze → operator release → T+1 validation)
                                                   │
Claude ──► tools.ToolGateway (typed, schema-checked, permissioned)
                │                     │
                ▼                     ▼
          swing.SwingDesk ──►  execution.ExecutionService ──► risk.RiskEngine (mandatory gate)
          (proposal → hash-bound        │                         reads ledger / portfolio /
           operator approval)           ▼                         market data / health itself
                         broker.PaperBroker | broker.GrowwBroker (live)
                                        │
                                        │
                     ledger.CapitalLedger (shared, event-sourced) · portfolio.Portfolio
monitoring.Monitor / IntradayEngine (exits, daily-loss, square-off) · audit.AuditLog (hash-chained)
store.Store (SQLite, append-only) ◄── persistence.Persistence     scheduler.Scheduler ◄── feeds.GrowwFeed
server (FastAPI operator API + static dashboard) ── runtime.build_runtime(settings)
```

| Module | PRD | What it does |
|---|---|---|
| `market_data.py` | FR-01 | Quote freshness per mode, crossed/inconsistent prices, session status, bar validation (missing candles, bad OHLC) |
| `strategies/` | FR-02 | ORB, VWAP trend-pullback, mean reversion. Each declares entry/exit/stop/sizing/regime/liquidity assumptions |
| `backtest.py` | FR-02, FR-04 | Next-bar fills, stop-first on ambiguous bars, gap-through stops, forced square-off, cost model, walk-forward, stress (2×/4× slippage, 1.5× costs), top-5%-trade removal, regime breakdown |
| `lifecycle.py` | FR-05, FR-12 | Immutable hashed `StrategyVersion` (params, code hash, dataset id, cost and risk-config hashes). Operator `release` per session. T+1 outcome `APPROVED / DEFERRED / REJECTED / EXPIRED` |
| `risk.py` | FR-08 | 30+ checks. Fails closed. Exits are allowed through the kill switch, loss limit and stale data. `size_by_risk` = floor(budget / risk per share), capped by cash |
| `ledger.py` | FR-07 | One capital pool. Reservations keyed by order id, so the same funds can't back two orders |
| `execution.py` | FR-09 | Runs the risk check itself. Atomic reservation. A timeout means "unknown": it reconciles by client id and never blindly resubmits. Fills applied once. Position reconciliation blocks new entries on any mismatch |
| `swing.py` | FR-06 | Schema-validated proposals. Approvals bound to version + SHA-256, single use, with expiry. A price move or edit voids the approval |
| `monitoring.py` | FR-10, §14 | Stop/target exits, daily-loss pause, square-off with broker-verified flat check. On failure: kill switch + CRITICAL alert |
| `tools.py` | §8 | The 14 Claude tools. JSON-schema validated, permissioned per environment, audited |
| `agent.py` | FR-03 | Claude orchestration loop (`claude-opus-5-5`, adaptive thinking, server-side refusal fallback enabled) |
| `app.py` | FR-11 | Composition root and `dashboard()` snapshot (portfolio, intraday, swing, research, operations) |
| `broker/groww.py` | §E, FR-09 | Groww adapter on the official `growwapi` SDK. Our order id goes out as `order_reference_id`. After a timeout it looks the order up by that reference, and "not found" only counts after a 30 s grace period. Errors are split into definitive vs ambiguous. An order goes terminal only once Groww's trade list covers the filled quantity. Client-side rate limits per Groww's categories |
| `feeds.py` | FR-01 | Groww quotes, with freshness taken from the exchange's last-trade time. Intraday 1-min candles, and history fetched in 30-day chunks with a CSV cache |
| `store.py`, `persistence.py` | §10, §12 | SQLite with append-only triggers. Audit, ledger and fills are written *before* they take effect. State is restored on restart with hash checks. The kill switch survives a restart; live authorisation never does |
| `scheduler.py`, `runtime.py`, `settings.py` | §9, §11 | Daily lifecycle jobs, assembly from a TOML file. The file can't approve anything |
| `desk.py` | §5, FR-12 | Paper and live books in one service, with the per-strategy OFF/PAPER/LIVE switch, its gates, and the live master switch |
| `server.py`, `static/index.html` | §4, FR-11 | Token-authenticated operator API (fails closed) and the dashboard |

## Safety invariants (each covered by tests)

- **No order bypasses the Risk Engine.** `ExecutionService.execute` is the only path to a broker, and it runs the risk check itself.
- **No live order without explicit authorisation.** You must pass a non-paper broker explicitly. The risk config must be operator-approved. `LiveAuthorisation` is set only at runtime, never from config or environment, and is bound to the risk-config hash, so changing limits revokes it.
- **No swing order without a valid approval** for the exact proposal version and hash: unexpired, unused, and no material change since.
- **Claude can't escalate.** There are no tools to approve, release, authorise live, resume entries, reset the kill switch or change limits. Claude can't open intraday positions or cancel exit orders. Its advisory flags can only defer or reject a session.
- **No double-committed capital.** Duplicate request ids are rejected (by the risk engine, ledger and broker).
- **Never falsely reports flat.** It is flat only when both the local book and the broker show no intraday (MIS) positions.
- **Tamper-evident audit.** Every risk decision, order, fill, approval and tool call is recorded, and the log is hash-chained. The database refuses UPDATE and DELETE, and a tampered chain stops the system from starting.
- **Restarts are safe.** Duplicate protection, reservations, approvals and the kill switch survive a restart. An order caught mid-submission is reconciled by reference, never resubmitted. Live authorisation always has to be granted again.

## Live pilot checklist (PRD Phase 4)

The live adapter was written against the `growwapi` 1.5.0 SDK source and Groww's published
docs, and tested against a faithful fake. It has **not** touched a real account yet.

**Operator decisions (2026-10-22), already in `config/claudetrade.example.toml`:** ₹100 risk per
trade, ₹300 daily loss limit, ₹1,000 per order (and per position, ₹3,000 total exposure), and
72 hours of paper trading before live.

The 72 hours are **enforced**. `POST /api/live/authorise` is refused until the paper deployment's
own database (mounted read-only, audit chain verified) shows at least 72 hours since paper
trading started, with the last 3 sessions clean: flat by 15:10, reconciled, and no failed jobs.
It also needs a working alert channel. `GET /api/live/gate` shows where you stand.

Before authorising live, follow [`docs/RUNBOOK.md`](docs/RUNBOOK.md):

1. §1: set up Telegram alerts and press **Send test alert**.
2. §5a: the one-share live test (start with `claudetrade groww-check`, which is read-only).
3. §5b: compare the estimated fees with a real contract note.
4. §3: read the square-off failure procedure. A failed square-off sends a CRITICAL alert that
   repeats every 2 minutes until you acknowledge it, and the kill switch stays on until positions
   reconcile.

At ₹1,000 per order, fixed charges are about 1.5% of an intraday round trip and about 3.7% of
a delivery round trip, so expect most strategies to fail the eligibility gate at this size.

Remaining gaps: PostgreSQL (SQLite is used, with the same append-only design), alert delivery
beyond the dashboard (`Monitor.notifier` is the hook for email/SMS/Telegram), and contract-note
reconciliation of actual charges.

A stop-loss does not guarantee the exit price. Gaps, poor liquidity and execution failures can cause losses larger than planned.
