"""Dead-man's-switch heartbeat: ping an external monitor while the loop is healthy.

The point is the ABSENCE of pings. A frozen, hung or wedged bot cannot report
that it is broken, so an external monitor (healthchecks.io style) alerts when
the pings stop. That only works if the ping means "the loop is genuinely doing
its job", not "the process is alive" — so the runner pings only after a
protection sweep AND a reconciliation have finished successfully recently.

Best-effort by construction: a failed ping never raises and never affects the
bot. The URL is a credential in practice (healthchecks UUIDs are unguessable
capability URLs), so it is never logged.
"""

from __future__ import annotations

import urllib.request

from .logging_setup import get_logger

log = get_logger(__name__)

# Short on purpose: the ping runs on the trading thread, so its worst case is
# a delay to the next protection sweep.
HEARTBEAT_TIMEOUT_SECONDS = 3


def send_heartbeat(url: str | None) -> bool:
    """GET the monitor URL once. Never raises; True only on a 2xx.

    No URL means the feature is off, silently.
    """
    if not url:
        return False
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=HEARTBEAT_TIMEOUT_SECONDS) as resp:
            ok = 200 <= resp.status < 300
        if not ok:
            log.warning(f"heartbeat ping failed: HTTP {resp.status}",
                        extra={"action": "heartbeat_failed"})
        return ok
    except Exception as exc:  # noqa: BLE001 — must never reach the bot
        # Type name only: str(exc) can embed the URL, which is a secret.
        log.warning(f"heartbeat ping failed: {type(exc).__name__}",
                    extra={"action": "heartbeat_failed"})
        return False
