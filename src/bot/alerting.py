"""Best-effort real-time alerts for dangerous trade states.

Separate, send-only Telegram Bot API client — NOT Telethon, and NOT the
user-account listener that reads the signal channel (telegram/listener.py).
A bot-token client is the right tool for a one-way notification; Telethon
authenticates as a full user account and is overkill (and riskier) for
sending a single message nobody replies to.

A failure here must never propagate. Alerting is observability sitting on
top of a state transition that has already happened — the transition itself
is the safety mechanism; this just makes sure a human notices without
watching logs in real time. Same philosophy as _enter_safe_error's own
"record AND shout", just carried one step further.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .logging_setup import get_logger

log = get_logger(__name__)

ALERT_TIMEOUT_SECONDS = 10


def send_alert(bot_token: str | None, chat_id: str | None, text: str) -> bool:
    """Send one message via the Telegram Bot API. Never raises.

    Returns True on a confirmed send, False on anything else (missing
    config, network failure, non-2xx response). Callers should treat the
    return value as informational only — a failed alert must never block or
    reverse the state transition it's reporting on.
    """
    if not bot_token or not chat_id:
        log.warning(
            "alert not sent: ALERT_TELEGRAM_BOT_TOKEN/ALERT_TELEGRAM_CHAT_ID "
            "not configured",
            extra={"action": "alert_skipped"},
        )
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=ALERT_TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                log.warning(
                    f"alert send failed: HTTP {resp.status}",
                    extra={"action": "alert_failed"},
                )
                return False
        return True
    except Exception as exc:  # noqa: BLE001 — alerting must never crash the bot
        log.warning(f"alert send failed: {exc}", extra={"action": "alert_failed"})
        return False
