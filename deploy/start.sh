#!/usr/bin/env bash
# Build, run pre-flight checks, then start the desk (paper trading; live stays off until you enable it).
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -f .env ]] || { echo "Missing .env: cp .env.example .env and fill it in (see deploy/README.md)"; exit 1; }
[[ -f config/claudetrade.toml ]] || { echo "Missing config: cp config/claudetrade.example.toml config/claudetrade.toml"; exit 1; }
grep -q '^CLAUDETRADE_OPERATOR_TOKEN=change-me' .env && { echo "Set CLAUDETRADE_OPERATOR_TOKEN in .env first"; exit 1; }

docker compose build
echo "== Pre-flight checks =="
docker compose run --rm --entrypoint claudetrade desk doctor --config /config/claudetrade.toml --send-test-alert
echo "== Starting =="
docker compose up -d
sleep 5
docker compose ps
cat <<MSG

ClaudeTrade is running (paper trading).

Dashboard: http://127.0.0.1:8000  (sign in with CLAUDETRADE_OPERATOR_TOKEN from .env)
On a remote server, reach it through an SSH tunnel from your laptop:
    ssh -N -L 8000:127.0.0.1:8000 <user>@<this-server>

Logs:    docker compose logs -f desk
Stop:    docker compose down          (state is kept in the desk-data volume)
Update:  git pull && ./deploy/start.sh
MSG
