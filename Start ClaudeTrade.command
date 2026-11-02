#!/bin/bash
# Double-click me (Mac). Starts ClaudeTrade and opens it in your browser, already signed in.
cd "$(dirname "$0")" || exit 1
echo "Starting ClaudeTrade… (the first time takes a few minutes)"

if ! docker info >/dev/null 2>&1; then
  echo "Opening Docker…"
  open -a Docker 2>/dev/null
  for _ in $(seq 1 90); do docker info >/dev/null 2>&1 && break; sleep 2; done
fi
if ! docker info >/dev/null 2>&1; then
  osascript -e 'display alert "Docker is not running" message "Install Docker Desktop from docker.com, open it once, then double-click Start ClaudeTrade again."' >/dev/null 2>&1
  echo "Docker is not running. Install/open Docker Desktop, then try again."; read -r -p "Press Enter to close."; exit 1
fi

# First time only: make the private sign-in key and the settings file.
if [ ! -f .env ]; then
  umask 077
  printf 'CLAUDETRADE_OPERATOR_TOKEN=%s\n' "$(openssl rand -hex 24)" > .env
fi
[ -f config/claudetrade.toml ] || cp config/claudetrade.example.toml config/claudetrade.toml

docker compose up -d --build || { echo "Could not start. Take a photo of this window and ask for help."; read -r -p "Press Enter to close."; exit 1; }

echo "Waiting for ClaudeTrade to be ready…"
for _ in $(seq 1 90); do curl -fs http://127.0.0.1:8000/healthz >/dev/null 2>&1 && break; sleep 2; done
TOKEN="$(grep '^CLAUDETRADE_OPERATOR_TOKEN=' .env | cut -d= -f2-)"
open "http://127.0.0.1:8000/#token=${TOKEN}"
echo "ClaudeTrade is open in your browser. You can close this window."
