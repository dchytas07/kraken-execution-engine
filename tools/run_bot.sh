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

# Clear a demonstrably-orphaned paper-state lock.
#
# Stopping the bot (launchctl unload, or a crash) while kraken-cli holds the
# lock leaves the file behind. Recovery then cannot read exchange state and
# refuses to trade -- correct, but it means the service is wedged until
# someone clears it by hand, which happened on two consecutive restarts.
#
# Only removed when the recorded holder PID is definitively NOT running. If
# the holder is alive this leaves it alone and lets recovery refuse, because
# clearing a live lock could corrupt paper state mid-write.
LOCK="$HOME/Library/Application Support/kraken/paper/futures_state.json.lock"
if [ -e "$LOCK" ]; then
    HOLDER="$(cut -d: -f1 "$LOCK" 2>/dev/null)"
    if [ -n "$HOLDER" ] && ! kill -0 "$HOLDER" 2>/dev/null; then
        echo "run_bot.sh: clearing orphaned paper lock (holder $HOLDER is dead)" >&2
        rm -f "$LOCK"
    else
        echo "run_bot.sh: paper lock held by live PID $HOLDER; leaving it" >&2
    fi
fi

exec .venv/bin/python -m bot.main --run
