#!/bin/bash
# Launcher for the LaunchAgent. Keeps the plist simple and guarantees the
# working directory, venv and PATH are right regardless of how launchd
# invokes us.
set -euo pipefail

cd "$(dirname "$0")/.."

# kraken-cli lives in ~/.cargo/bin, which is NOT on launchd's default PATH.
export PATH="$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin"

# Refuse to start if someone has flipped this to live. The LaunchAgent is for
# paper observation only; going live should be a deliberate, foreground act.
MODE="$(grep -E '^TRADING_MODE=' .env | head -1 | cut -d= -f2 | tr -d '[:space:]')"
if [ "$MODE" != "paper" ]; then
    echo "run_bot.sh: refusing to start under launchd with TRADING_MODE=$MODE." >&2
    echo "This service is for paper mode only. Start live runs manually." >&2
    exit 78   # EX_CONFIG — see KeepAlive note in the plist
fi

exec .venv/bin/python -m bot.main --run
