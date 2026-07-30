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
from datetime import UTC, datetime

from .logging_setup import get_logger
from .models import EventType, TradeState

log = get_logger(__name__)

ALERT_TIMEOUT_SECONDS = 10

# States a trade cannot leave without an operator looking at it.
UNRESOLVED_STATES = (TradeState.REQUIRES_REVIEW, TradeState.SAFE_ERROR)

DIGEST_INTERVAL_SECONDS = 24 * 60 * 60


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
            raw = resp.read()

        # HTTP 200 is not the same as "Telegram sent it". The API reports
        # per-request success in an `ok` field, so returning True on the
        # status code alone would be the same submitted-equals-confirmed
        # mistake brief 27 forbids on the order path. Parse and check.
        payload = json.loads(raw)
        if not payload.get("ok"):
            log.warning(
                f"alert send failed: Telegram returned ok=false "
                f"({payload.get('description') or payload})",
                extra={"action": "alert_failed"},
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001 — alerting must never crash the bot
        log.warning(f"alert send failed: {exc}", extra={"action": "alert_failed"})
        return False


def _format_age(since: str | None, now: datetime) -> str:
    """Human-readable time since an ISO timestamp. Never raises."""
    if not since:
        return "unknown"
    try:
        started = datetime.fromisoformat(since)
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        seconds = int((now - started).total_seconds())
    except (ValueError, TypeError):
        return "unknown"
    if seconds < 0:
        return "just now"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def send_unresolved_digest(
    store,
    bot_token: str | None,
    chat_id: str | None,
    *,
    now: datetime | None = None,
    interval_seconds: int = DIGEST_INTERVAL_SECONDS,
) -> bool:
    """Once every 24h, summarise every trade still awaiting an operator.

    The per-transition alert fires once, at the moment a trade enters
    REQUIRES_REVIEW or SAFE_ERROR. Nothing then reminds anyone it is still
    sitting there — this closes that gap.

    Deliberately silent when nothing is unresolved: a monitoring channel that
    pings daily to say "all fine" trains you to ignore it, which is exactly
    when the one real alert gets missed.

    The cadence marker is an ALERT_DIGEST event row, not an in-process timer,
    because `time.monotonic()` resets on restart — a bot restarted daily
    would otherwise never reach 24h. An evaluation is recorded even when it
    sends nothing, so a trade that becomes unresolved shortly after a quiet
    check waits for the next window instead of being double-reported
    alongside its own immediate transition alert.

    Returns True only if a digest message was actually sent. Never raises.
    """
    now = now or datetime.now(UTC)
    try:
        last = store.last_event_at(EventType.ALERT_DIGEST)
        if last:
            try:
                last_at = datetime.fromisoformat(last)
                if last_at.tzinfo is None:
                    last_at = last_at.replace(tzinfo=UTC)
                if (now - last_at).total_seconds() < interval_seconds:
                    return False
            except (ValueError, TypeError):
                # An unparseable marker must not wedge the digest forever;
                # treat it as due and let this run rewrite it.
                pass

        rows = store.trades_in_states(UNRESOLVED_STATES)
        if not rows:
            store.append_event(
                EventType.ALERT_DIGEST, "digest skipped: nothing unresolved"
            )
            return False

        lines = [f"{len(rows)} trade(s) awaiting review:"]
        for row in rows:
            age = _format_age(row["state_since"], now)
            reason = row["state_detail"] or "no reason recorded"
            lines.append(
                f"\n• trade {row['id']} ({row['symbol']}) — {row['state']}"
                f"\n  stuck ~{age} | {reason}"
            )
        text = "\n".join(lines)

        if not send_alert(bot_token, chat_id, text):
            # Deliberately does NOT record the cadence marker. Recording here
            # would let one transient network blip cost a full day of
            # visibility; leaving the window open retries on the next check.
            # send_alert has already logged why it failed.
            return False

        store.append_event(
            EventType.ALERT_DIGEST,
            f"digest sent: {len(rows)} unresolved",
            detail={"unresolved": len(rows)},
        )
        return True
    except Exception as exc:  # noqa: BLE001 — alerting must never crash the bot
        log.warning(
            f"unresolved digest failed: {exc}", extra={"action": "digest_failed"}
        )
        return False
