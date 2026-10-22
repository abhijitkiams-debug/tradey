# ClaudeTrade operations runbook

Who does what when something goes wrong, plus the two pre-live checks. Where the system acts
automatically, this document says so; where a human must act, it gives the steps.

**Decision log**

| Date | Decision | By |
|---|---|---|
| 2026-10-22 | Risk per trade ₹100 · daily loss limit ₹300 · max ₹1,000 per order (and per position) · max exposure ₹3,000 | Owner |
| 2026-10-22 | Paper-trade at least 72 hours and the last 3 paper sessions clean before live (enforced in code) | Owner |
| 2026-10-22 | Square-off failure procedure below (§3): alerts to the owner's phone, repeating until acknowledged | Delegated to Claude, owner to confirm |

---

## 1. Who gets alerts, and how

**Responder: the account owner (you).** The Groww account is yours, so only you can exit
positions in it. There is no backup person. If you add one later, they need their own operator
token and a way to reach you; they can't trade your account.

**Channel: Telegram push notification to your phone**, plus an optional webhook.

| Severity | Examples | Delivery |
|---|---|---|
| CRITICAL | Square-off failed, kill switch activated | Phone push, **re-sent every 2 minutes until you press Acknowledge** in the dashboard. It keeps going after market close and after a restart |
| HIGH | Broker disconnected, reconciliation mismatch, daily loss limit hit, swing stop hit, job failure | Phone push, sent once |
| INFO | Target reached, back to flat after a failure | Dashboard only (change `CLAUDETRADE_ALERT_MIN_SEVERITY` to `INFO` to receive these too) |

Setup (once, about 5 minutes):

1. In Telegram, message **@BotFather**, send `/newbot` and copy the bot token.
2. Send any message to your new bot. Then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": ...}`.
3. Put both in `.env.paper` and `.env.live`:
   `CLAUDETRADE_TELEGRAM_BOT_TOKEN=...`, `CLAUDETRADE_TELEGRAM_CHAT_ID=...`
4. In Telegram, unmute the bot chat and allow its notifications through Do Not Disturb.
5. Dashboard → **Send test alert**. Live authorisation is refused until an alert channel is
   configured; the test proves it actually reaches you.

Write down here, during setup, how you'll reach Groww if the app and website both fail:

- Groww support / dealing desk: `________________` (from the Groww app's Help section)

## 2. Daily routine

| When | You |
|---|---|
| Before 09:05 | Only if you use the API key + secret login: approve today's access on Groww Cloud (that flow needs daily approval; the TOTP flow does not) |
| 09:30 | Glance at the dashboard: all status chips green, the Intraday panel shows today's validation result |
| 15:15 | Intraday panel shows **verified flat** |
| After 15:40 | Read the end-of-day report; acknowledge anything critical |

The system does the rest: pre-open reconciliation 09:05, validation 09:20, trading, square-off
15:00–15:10, end-of-day report 15:35, research 16:00.

## 3. Square-off failure (the critical case)

**What "failed" means:** at 15:10 IST (`square_off_deadline`) the broker still reports an
intraday (MIS) position, or the broker can't be reached to confirm there is none.

### What the system does automatically

| Time | Action |
|---|---|
| 15:00 | Cancels intraday entry orders; sends market exit orders for every intraday position |
| 15:00–15:10 | Re-checks each tick and re-submits exits for anything still open |
| 15:10 if not flat | **Kill switch ON** (no new entries anywhere), **CRITICAL alert** to your phone listing the open positions and these steps; it repeats every 2 minutes until acknowledged |
| 15:10–15:30 | Keeps retrying exits every tick while the market is open (exits are allowed through the kill switch) |
| Becomes flat | Sends an INFO message saying it is "now verified FLAT". The kill switch stays on, and the day is still recorded as a failed square-off |
| 15:35 | End-of-day reconciliation; the report records `square_off_failed: true` |
| Next day 09:05 | Pre-open reconciliation; no new entries until you reset the kill switch |

### What you do (target: within 5 minutes of the first alert)

1. **Open the Groww app → your intraday positions.** For each open position, **Exit at
   market**. If the app is down, use groww.in in a browser. If both are down, call the
   number in §1.
2. **Cancel pending orders** for those symbols in the orders list, so nothing re-opens a position.
3. **Confirm** the positions screen shows no intraday quantity.
4. **Acknowledge** the alert in the dashboard (the red panel at the top). This stops the repeats;
   it does *not* reset the kill switch.
5. Dashboard → **Reconcile now**. It must show "Book matches broker".
6. Only then, and once you understand the cause, **Reset kill switch** and give the reason.
   The reset is refused while any reconciliation mismatch remains.

Backstop: Groww runs its own automatic square-off of MIS positions near the close (confirm the
exact time during the live test, §5). It typically carries a per-order charge. Don't rely on it,
but it caps how long a stuck position can stay open intraday.

Consequence for the live gate: a failed square-off day is never "clean", so during paper trading
it restarts the count of 3 clean sessions.

## 4. Other incidents

| Alert / symptom | System does | You do |
|---|---|---|
| Broker disconnected / auth failed | Blocks new entries; exits keep trying | Check Groww status and your token (daily approval?); restart the service if credentials changed; Reconcile |
| Market data stale / feed down | Blocks entries for the affected symbols | Usually transient. If it lasts more than 10 minutes, pause entries and check the Groww API status |
| Reconciliation mismatch | Blocks all new entries | Compare the dashboard with the Groww app. If you traded manually in a managed symbol, list it under `[external_holdings]` in the config and restart. Then Reconcile |
| Order status UNKNOWN (timeout) | Looks the order up by reference; blocks entries until resolved | Normally clears within 30 s. If it persists, check the Groww orders list for that reference id |
| Daily loss limit (₹300) hit | Pauses all new entries for the day; open positions keep their stops | Nothing urgent. Resume next morning (Resume entries) after reviewing |
| Swing stop hit | Sells the swing position at market (the stop was part of your approval) | Review; nothing to approve |
| Swing target reached | Alert only | Decide whether to exit (taking profit is your call) |
| Job failed | Pauses entries, logs the error | Read the scheduler error on the dashboard; fix; Resume entries |
| Server restarted | Restores state; kill switch and pending alerts survive; reconciles with the broker | Live trading must be authorised again (by design) |

## 5. Pre-live checks (your items 2 and 3)

Do these in **LIVE mode with one share** of a liquid stock under ₹1,000, after the live gate
passes. Record the results here.

### 5a. One-share live test

| # | Step | Expect | Result |
|---|---|---|---|
| 1 | Run `claudetrade groww-check` (read-only; looks up a made-up reference) | "not found" (404). If it errors differently, timeouts stay UNKNOWN longer; report it | |
| 2 | Intraday: buy 1 share MIS, then sell it the same session | Dashboard shows the position, then flat; **Reconcile** shows no mismatch after each step | |
| 3 | Delivery: buy 1 share CNC via an approved swing proposal | Reconcile clean on day T **and** day T+1 (holdings move from today's position to holdings) | |
| 4 | Sell that CNC share | Reconcile clean | |
| 5 | Check Groww's MIS auto square-off time (Groww help or the order/charges page) | Later than 15:10. If not, move `square_off_start` / `square_off_deadline` earlier | |
| 6 | Trade list | Fills show up within one tick and aren't duplicated after a restart | |

### 5b. Fees vs contract note

1. Download the contract notes for the trades in 5a from Groww.
2. Compare each charge with the dashboard's **Charges paid** (from `costs.py`):
   brokerage, STT, exchange transaction charge, SEBI fee, stamp duty, GST, DP charge (CNC sell).
3. If any line differs by more than ₹0.50, update `CostModel` in `costs.py` and re-run
   research. Strategy results depend on these numbers.

**Note on order size:** at ₹1,000 per order, fixed charges dominate. Estimated round trips are
about ₹12 (≈1.5%) intraday and about ₹30 (≈3.7%) for delivery. A strategy has to beat
that on every trade, so expect the eligibility gate to reject most strategies at this size. That
is the system working as intended. If you later want strategies to clear costs more easily, the
lever is the order cap, not the risk budget.
