#!/bin/bash
# ExecStartPre gate for the systemd unit.
#
# Root cause this works around: on a WSL2 boot, systemd's own
# network-online.target is satisfied almost immediately (WSL has no real NIC
# coming up -- networking is proxied from the Windows host), but DNS/HTTP to
# the outside world is not actually reachable yet. kraken-cli calls
# futures.kraken.com for live mark prices even in paper mode, and that call
# hangs (not a fast failure) until our 30s subprocess timeout, which is what
# was observed crash-looping the service after a Windows restart.
#
# This is deliberately NOT a stale-lock workaround: kraken-cli's paper-state
# lock is a real kernel flock(), released automatically on process death and
# failed on fast on contention (verified: a dead-PID marker file left at the
# lock path has no effect, and a held lock returns an instant JSON error, not
# a hang). So there is nothing to clear here -- only network to wait for.
set -euo pipefail

until curl -fsS --max-time 3 -o /dev/null https://futures.kraken.com/derivatives/api/v3/instruments; do
    echo "wait-for-network: futures.kraken.com not reachable yet, retrying" >&2
    sleep 2
done

echo "wait-for-network: network ready" >&2
