#!/bin/bash
# Double-click me (Mac) to get the newest version, then ClaudeTrade restarts.
cd "$(dirname "$0")" || exit 1
git pull --ff-only || { echo "Could not update. Ask for help."; read -r -p "Press Enter to close."; exit 1; }
exec ./"Start ClaudeTrade.command"
