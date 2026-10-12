# ClaudeTrade

AI-assisted trading for NSE cash equities with ₹10,000 capital. Claude does the research,
risk rules are enforced in code, and every broker order is checked and reconciled.
Implements PRD v1.0 Phases 1–3 (foundation, paper trading, swing approvals).
**Only paper mode can execute; live trading is not wired up.**

> Research intelligently. Validate independently. Execute deterministically. Reconcile continuously.

## Quick start

```bash
cd claudetrade
pip install -e ".[dev]"            # numpy, pandas, pytest
pytest -q                          # 96 tests, ~10 s

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
                                  broker.PaperBroker  (Groww adapter: pending verification)
                                        │
                     ledger.CapitalLedger (shared, event-sourced) · portfolio.Portfolio
monitoring.Monitor / IntradayEngine (exits, daily-loss, square-off) · audit.AuditLog (hash-chained)
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

## Safety invariants (each covered by tests)

- **No order bypasses the Risk Engine.** `ExecutionService.execute` is the only path to a broker, and it runs the risk check itself.
- **No live order without explicit authorisation.** You must pass a non-paper broker explicitly. The risk config must be operator-approved. `LiveAuthorisation` is set only at runtime, never from config or environment, and is bound to the risk-config hash, so changing limits revokes it.
- **No swing order without a valid approval** for the exact proposal version and hash: unexpired, unused, and no material change since.
- **Claude can't escalate.** There are no tools to approve, release, authorise live, resume entries, reset the kill switch or change limits. Claude can't open intraday positions or cancel exit orders. Its advisory flags can only defer or reject a session.
- **No double-committed capital.** Duplicate request ids are rejected (by the risk engine, ledger and broker).
- **Never falsely reports flat.** It is flat only when both the local book and the broker show no intraday (MIS) positions.
- **Tamper-evident audit.** Every risk decision, order, fill, approval and tool call is recorded, and the log is hash-chained.

## Not done yet (needs decisions or access)

1. **Groww adapter.** `broker/groww.py` deliberately raises an error. Before building it, verify API access, order types, product codes, whether client references can be looked up after a timeout, and rate limits and fees (PRD §9, §18 Q3). Then run `tests/test_execution.py` against it.
2. **Market-data provider** for historical and live data (§18 Q2). `cli.load_csv_dir` accepts 1-min CSVs for now.
3. **Persistence.** State lives in memory, plus an optional JSONL audit file. PostgreSQL (append-only tables per §10) and a durable scheduler are the next step.
4. **Dashboard UI and FastAPI service.** `ClaudeTrade.dashboard()` returns the full FR-11 data set, ready to serve.
5. **Operator decisions (§18).** The `RiskConfig` defaults (₹100 per-trade risk, ₹300 daily loss, ₹4,000 max order, 3 positions) and the `EligibilityCriteria` thresholds are paper placeholders. Live use needs approved values, square-off failure procedure, cutoff times (`SessionConfig`) and a minimum paper-trading period.
6. **Cost model.** The `costs.py` rates are estimates and must be checked against Groww's current fee schedule.

A stop-loss does not guarantee the exit price. Gaps, poor liquidity and execution failures can cause losses larger than planned.
