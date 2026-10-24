#!/usr/bin/env bash
# One-time local setup on macOS / Linux (Windows: use deploy/setup-local.ps1).
#   - checks Docker
#   - writes .env (asks for your Groww TOTP token + secret; the secret is typed hidden and never echoed)
#   - creates config/claudetrade.toml from the example
#   - runs the pre-flight checks and starts paper trading (unless --no-start)
set -euo pipefail
cd "$(dirname "$0")/.."

START=1
[[ "${1:-}" == "--no-start" ]] && START=0

say() { printf '\n== %s\n' "$*"; }

if [[ "${SKIP_DOCKER_CHECK:-0}" != 1 ]]; then
  say "Checking Docker"
  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker is not installed. Install Docker Desktop: https://docs.docker.com/get-docker/ then re-run this script."
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "Docker is installed but not running. Start Docker Desktop, wait until it says 'running', then re-run."
    exit 1
  fi
  docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is missing (comes with Docker Desktop)."; exit 1; }
  echo "Docker OK"
fi

if [[ -f .env ]]; then
  say ".env already exists — keeping it (delete it to start over)"
else
  say "Groww Trade API (needs an active Trading API subscription on Groww)"
  cat <<'TXT'
In your browser:
  1. Open https://groww.in/trade-api/api-keys and sign in.
  2. Next to "Generate API Key", open the dropdown and choose "Generate TOTP token".
  3. Name it (e.g. claudetrade) and continue.
  4. Copy the TOTP token and the TOTP secret shown. Do not share them with anyone, including in chat.
TXT
  read -r -p "Paste the Groww TOTP token: " GROWW_KEY
  read -r -s -p "Paste the Groww TOTP secret (input hidden): " GROWW_SECRET; echo
  GROWW_SECRET="$(printf '%s' "$GROWW_SECRET" | tr -d ' ' | tr '[:lower:]' '[:upper:]')"
  if [[ -z "$GROWW_KEY" || -z "$GROWW_SECRET" ]]; then echo "Both values are required."; exit 1; fi
  if ! [[ "$GROWW_SECRET" =~ ^[A-Z2-7]+=*$ ]]; then
    echo "That secret doesn't look like a TOTP secret (letters A-Z and digits 2-7). Check you copied the secret, not the token."
    exit 1
  fi

  say "Phone alerts via Telegram (optional now, required before live; see docs/RUNBOOK.md §1)"
  read -r -p "Telegram bot token (Enter to skip): " TG_TOKEN
  TG_CHAT=""
  [[ -n "$TG_TOKEN" ]] && read -r -p "Telegram chat id: " TG_CHAT

  OP_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))' 2>/dev/null || openssl rand -base64 33 | tr -d '/+=\n')"
  umask 077
  cat > .env <<ENV
# Written by deploy/setup-local.sh — keep private (chmod 600). Never commit.
CLAUDETRADE_OPERATOR_TOKEN=${OP_TOKEN}
GROWW_API_KEY=${GROWW_KEY}
GROWW_TOTP_SECRET=${GROWW_SECRET}
GROWW_API_SECRET=
GROWW_ACCESS_TOKEN=
CLAUDETRADE_TELEGRAM_BOT_TOKEN=${TG_TOKEN}
CLAUDETRADE_TELEGRAM_CHAT_ID=${TG_CHAT}
CLAUDETRADE_ALERT_WEBHOOK_URL=
CLAUDETRADE_ALERT_MIN_SEVERITY=HIGH
ANTHROPIC_API_KEY=
ENV
  chmod 600 .env
  unset GROWW_SECRET GROWW_KEY
  echo "Wrote .env (permissions 600)."
fi

if [[ ! -f config/claudetrade.toml ]]; then
  cp config/claudetrade.example.toml config/claudetrade.toml
  say "Created config/claudetrade.toml from the example (limits ₹100 / ₹300 / ₹1,000; paper only)"
  echo "Before your first trading day, add this year's NSE holidays under [session] holidays."
fi

if [[ $START == 1 ]]; then
  ./deploy/start.sh
  token="$(grep '^CLAUDETRADE_OPERATOR_TOKEN=' .env | cut -d= -f2-)"
  cat <<MSG

Dashboard: http://127.0.0.1:8000
Operator token (sign-in): ${token}

Keep this computer awake and online on trading days 09:00–16:30 IST (on a Mac: System Settings >
Battery > prevent sleeping when the display is off, or run: caffeinate -dimsu). If it sleeps, that
session simply doesn't trade, and a missed square-off check marks the day unclean for the 72-hour gate.
MSG
fi
