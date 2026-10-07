#!/bin/bash
# Launcher for a supervisor that wants a simple entry point (the macOS
# LaunchAgent; the Linux systemd unit runs python directly). Keeps the plist
# simple and guarantees the working directory, venv and PATH are right
# regardless of how the supervisor invokes us. Portable across macOS and Linux:
# nothing here names an OS-specific path.
#
# Sourceable for tests: `source tools/run_bot.sh` defines the functions and
# starts nothing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${BOT_PYTHON:-$ROOT/.venv/bin/python}"

# kraken-cli lives in ~/.cargo/bin, which is NOT on a supervisor's default PATH.
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

# Where kraken-cli keeps its paper-state lock ON THIS OS. Asked of the bot's own
# resolution (bot.status.paper_state_path, the same one --status uses) rather
# than hardcoded here, so there is exactly one place that knows the layout.
paper_lock_path() {
    PYTHONPATH="$ROOT/src" "$PY" -c \
        'from bot.status import paper_lock_path; print(paper_lock_path())'
}

# Clear a demonstrably-orphaned paper-state lock.
#
# Stopping the bot (launchctl unload, systemctl stop, or a crash) while
# kraken-cli holds the lock leaves the file behind. Recovery then cannot read
# exchange state and refuses to trade -- correct, but it means the service is
# wedged until someone clears it by hand, which happened on two consecutive
# restarts.
#
# Only removed when the recorded holder PID is definitively NOT running. If
# the holder is alive this leaves it alone and lets recovery refuse, because
# clearing a live lock could corrupt paper state mid-write.
clear_orphaned_lock() {
    local lock holder
    lock="$(paper_lock_path)" || {
        echo "run_bot.sh: could not resolve the paper lock path; leaving it" >&2
        return 0
    }
    [ -e "$lock" ] || return 0
    holder="$(cut -d: -f1 "$lock" 2>/dev/null || true)"
    if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
        echo "run_bot.sh: clearing orphaned paper lock (holder $holder is dead)" >&2
        rm -f "$lock"
    else
        echo "run_bot.sh: paper lock held by live PID ${holder:-?}; leaving it" >&2
    fi
}

main() {
    cd "$ROOT"

    # Refuse to start if someone has flipped this to live. This launcher is for
    # paper observation only; going live should be a deliberate, foreground act.
    local mode
    mode="$(grep -E '^TRADING_MODE=' .env | head -1 | cut -d= -f2 | tr -d '[:space:]')"
    if [ "$mode" != "paper" ]; then
        echo "run_bot.sh: refusing to start under a supervisor with TRADING_MODE=$mode." >&2
        echo "This service is for paper mode only. Start live runs manually." >&2
        exit 78   # EX_CONFIG — see KeepAlive note in the plist
    fi

    clear_orphaned_lock
    exec "$PY" -m bot.main --run
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
