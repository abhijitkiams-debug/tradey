# Deploying ClaudeTrade and starting paper trading

About 30 minutes, once. You need: a small always-on Linux server, a Groww Trade API key, and
(strongly recommended) a Telegram bot for alerts. Paper trading places **no real orders**; it
only reads prices from Groww.

## 1. A server

Any Ubuntu 22.04+ VM with 1 vCPU / 1 GB RAM is enough. Pick an India region (for example,
Mumbai) for lower latency to Groww. It must run every trading day from 09:00 to 16:30 IST, so
use a server, not a laptop that sleeps.

```bash
curl -fsSL https://get.docker.com | sudo sh        # Docker + compose plugin
sudo usermod -aG docker $USER && newgrp docker
git clone https://github.com/abhijitkiams-debug/tradey && cd tradey
```

## 2. Groww API access (TOTP: no daily approval)

1. Groww app/web → **Trade API** (Groww Cloud API keys page) → create an API key of type **TOTP**.
2. Save the API key and the TOTP secret (the text behind the QR code).
3. These go in `.env` as `GROWW_API_KEY` and `GROWW_TOTP_SECRET`. ClaudeTrade logs in by itself
   every morning at 08:45 IST, and again if a session expires.

(The API key + secret flow also works, but you must approve it on Groww Cloud **every day**.)

## 3. Alerts to your phone

Follow `docs/RUNBOOK.md` §1 (Telegram bot, about 5 minutes). The start script sends a test alert.

## 4. Configure

```bash
cp .env.example .env && chmod 600 .env
python3 -c "import secrets;print(secrets.token_urlsafe(32))"   # paste as CLAUDETRADE_OPERATOR_TOKEN
nano .env                                                      # Groww + Telegram values

cp config/claudetrade.example.toml config/claudetrade.toml
nano config/claudetrade.toml
```

In the config, check:
- `universe`: the stocks to trade (keep them well under your ₹1,000 order cap).
- `[session] holidays`: copy the NSE holiday list for the year. Otherwise the scheduler treats
  holidays as trading days.
- `auto_paper`: already lists all three strategy families, so paper trading starts without clicks.
- `live_enabled = false`: leave it off until the 72-hour paper record is complete.

## 5. Start

```bash
./deploy/start.sh
```

This builds the image, runs `claudetrade doctor` (config, token, alert test, Groww login, live
quotes for every symbol, history), and starts the desk only if those checks pass.

## 6. Open the dashboard

The service listens only on the server's localhost. From your laptop:

```bash
ssh -N -L 8000:127.0.0.1:8000 you@your-server
```

Then browse to http://127.0.0.1:8000 and sign in with the operator token.

## What happens next

| When | What you'll see |
|---|---|
| First minutes | Startup research: ~120 days of 1-minute history per symbol from Groww, then walk-forward on all three families. Versions appear under **Strategies**, and the `auto_paper` families switch to **PAPER** (marked "paper trial" if they haven't passed the eligibility checks) |
| Next trading day 09:20 | Each PAPER strategy is validated for the day and starts trading the paper book (if you start mid-session, this happens immediately) |
| 15:00–15:10 | Paper square-off, verified flat |
| 15:35 | End-of-day report; the session counts toward each strategy's 72-hour record if clean |
| 16:00 daily | Fresh research. Running strategies are kept; new versions are listed for comparison |
| After 72 h and 3 clean sessions | The LIVE button for that strategy shows what's still missing. Follow `docs/RUNBOOK.md` §5 (one-share test, fees) before enabling the live book |

Day to day: `docker compose logs -f desk`, plus the dashboard. Everything is in the
`desk-data` volume (`paper.db`, `live.db`, history cache). It survives restarts and upgrades.
