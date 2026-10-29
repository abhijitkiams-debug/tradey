#!/usr/bin/env bash
# One-time local setup on macOS / Linux (Windows: use deploy/setup-local.ps1).
#   - checks Docker
#   - writes .env (asks for your Groww TOTP token + secret; the secret is typed hidden and never echoed)
#   - creates config/claudetrade.toml from the example
#   - runs the pre-flight checks and starts paper trading (unless --no-start)
set -euo pipefail
cd "$(dirname "$0")/.."

START=1
GROWW_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --no-start) START=0 ;;
    --groww) GROWW_ONLY=1 ;;   # re-enter only the Groww token/secret in an existing .env
  esac
done

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

# Read a long secret safely. On macOS, from the clipboard (no terminal length limit, never shown);
# otherwise with canonical mode off, because terminals silently cut pasted lines at ~1024 chars.
read_secret() {  # $1 = prompt, $2 = hidden (1/0); result in REPLY_VALUE
  REPLY_VALUE=""
  if [[ -t 0 ]] && command -v pbpaste >/dev/null 2>&1; then
    # -s: nothing typed or pasted here is ever shown (people paste out of habit).
    local typed=""
    IFS= read -r -s -p "$1 — copy it in the browser, then just press Enter here (do NOT paste): " typed || true
    echo
    REPLY_VALUE="$(pbpaste)"
    if [[ -z "$REPLY_VALUE" && -n "$typed" ]]; then REPLY_VALUE="$typed"; fi
    typed=""
    printf '' | pbcopy 2>/dev/null || true   # don't leave the secret on the clipboard
    echo "  read ${#REPLY_VALUE} characters (not shown)"
  elif [[ -t 0 ]]; then
    local old; old="$(stty -g)"
    stty -icanon -echo   # never echo credentials
    IFS= read -r -p "$1 (input hidden): " REPLY_VALUE || true
    stty "$old"; echo
  else
    IFS= read -r REPLY_VALUE || true
  fi
  REPLY_VALUE="$(printf '%s' "$REPLY_VALUE" | tr -d '\r\n' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
}

ask_groww() {
  say "Groww Trade API (needs an active Trading API subscription on Groww)"
  cat <<'TXT'
In your browser:
  1. Open https://groww.in/trade-api/api-keys and sign in.
  2. Next to "Generate API Key", open the dropdown and choose "Generate TOTP token".
  3. Name it (e.g. claudetrade) and continue.
  4. You'll see a TOTP token (long) and a TOTP secret. Never share them, and never paste terminal
     output that shows them: anyone with both can use your Groww API.
TXT
  read_secret "Groww TOTP token" 0; GROWW_KEY="$REPLY_VALUE"
  read_secret "Groww TOTP secret" 1; GROWW_SECRET="$(printf '%s' "$REPLY_VALUE" | tr -d ' ' | tr '[:lower:]' '[:upper:]')"
  if [[ -z "$GROWW_KEY" || -z "$GROWW_SECRET" ]]; then echo "Both values are required."; exit 1; fi
  if [[ ${#GROWW_KEY} -eq 1023 || ${#GROWW_KEY} -eq 1024 ]]; then
    echo "The token is exactly ${#GROWW_KEY} characters: it was almost certainly cut off. Try again."; exit 1
  fi
  if [[ "$GROWW_KEY" =~ ^[A-Z2-7]+=*$ && ${#GROWW_KEY} -lt 80 ]]; then
    echo "That token looks like a TOTP *secret*. Paste the long TOTP token first, then the secret."; exit 1
  fi
  if ! [[ "$GROWW_SECRET" =~ ^[A-Z2-7]+=*$ ]]; then
    echo "That secret doesn't look like a TOTP secret (letters A-Z and digits 2-7). Check you copied the secret, not the token."
    exit 1
  fi
  echo "  token: ${#GROWW_KEY} characters; secret: ${#GROWW_SECRET} characters (values not shown)"
}

if [[ $GROWW_ONLY == 1 ]]; then
  [[ -f .env ]] || { echo "No .env yet: run ./deploy/setup-local.sh without --groww"; exit 1; }
  ask_groww
  tmp="$(mktemp .env.XXXXXX)"
  grep -vE '^(GROWW_API_KEY|GROWW_TOTP_SECRET)=' .env > "$tmp" || true
  printf 'GROWW_API_KEY=%s\nGROWW_TOTP_SECRET=%s\n' "$GROWW_KEY" "$GROWW_SECRET" >> "$tmp"
  chmod 600 "$tmp" && mv "$tmp" .env
  unset GROWW_KEY GROWW_SECRET REPLY_VALUE
  echo "Updated the Groww values in .env."
elif [[ -f .env ]]; then
  say ".env already exists — keeping it (re-enter Groww values with: ./deploy/setup-local.sh --groww)"
else
  ask_groww

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
  unset GROWW_SECRET GROWW_KEY REPLY_VALUE
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
