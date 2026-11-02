#!/bin/bash
# Double-click me (Mac). Stops ClaudeTrade. Your practice history is kept.
cd "$(dirname "$0")" || exit 1
docker compose down && echo "ClaudeTrade stopped. Your history is saved." || echo "Docker isn't running, so ClaudeTrade is already stopped."
sleep 2
