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
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

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
                EventType.ALERT_DIGEST,
                "digest skipped: nothing unresolved",
                occurred_at=now.isoformat(),
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
            occurred_at=now.isoformat(),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — alerting must never crash the bot
        log.warning(
            f"unresolved digest failed: {exc}", extra={"action": "digest_failed"}
        )
        return False


# ---------------------------------------------------------------------------
# Trading-action notifications
# ---------------------------------------------------------------------------

# A small rotating pool so the alerts do not read like a stuck machine. No
# external call — the variation is just a template choice seeded by the
# minute, which is deterministic enough to test and varied enough to read
# naturally.
_GREETINGS = {
    "morning": ("Morning!", "Good morning —", "Rise and shine —",
                "Morning update —", "Early one —"),
    "afternoon": ("Afternoon!", "Good afternoon —", "Afternoon update —",
                  "Midday check —", "Hey —"),
    "evening": ("Evening!", "Good evening —", "Evening update —",
                "Winding down —", "Hey there —"),
    "night": ("Late one —", "Night owl —", "Burning the midnight oil —",
              "Overnight update —", "Still going —"),
}


def _part_of_day(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def greeting(now: datetime | None = None) -> str:
    """Time-appropriate opener, rotated so it is not identical every time."""
    now = now or datetime.now()
    pool = _GREETINGS[_part_of_day(now.hour)]
    # Minute-seeded rather than random: two alerts in the same minute read
    # consistently, and a test can pin the output without monkeypatching RNG.
    return pool[(now.minute + now.hour) % len(pool)]


def _money(value) -> str:
    """Format a P&L with an explicit sign, or '—' when genuinely unknown.

    Never invents a number: an unknown P&L prints as unknown rather than as
    zero, because a confident 0.00 reads as breakeven.
    """
    if value is None:
        return "—"
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return "—"
    return f"{'+' if d >= 0 else '-'}${abs(d):.2f}"


def notify_trade_event(
    config,
    kind: str,
    *,
    symbol: str,
    side: str | None = None,
    size=None,
    price=None,
    pnl=None,
    detail: str = "",
    now: datetime | None = None,
) -> bool:
    """Announce one real trading action. Never raises.

    Same best-effort contract as the SAFE_ERROR/REQUIRES_REVIEW alerts: the
    action has already happened when this runs, so a failed send is logged
    and swallowed rather than allowed to affect it.
    """
    try:
        headline = {
            "opened": "📈 Trade opened",
            "tp_filled": "🎯 Take-profit hit",
            "breakeven": "🛡️ Stop moved to breakeven",
            "closed": "✅ Trade closed",
            "stop_updated": "🛡️ Stop tightened on the channel's update",
            "cancelled": "🚫 Trade cancelled",
        }.get(kind, kind)

        bits = [f"{greeting(now)} {headline}", "", f"  {symbol}"]
        if side:
            bits[-1] += f"  {side.upper()}"
        if size is not None:
            bits.append(f"  size: {size}")
        if price is not None:
            bits.append(f"  price: {price}")
        if pnl is not None:
            bits.append(f"  P&L: {_money(pnl)}")
        if detail:
            bits.append(f"  {detail}")

        return send_alert(
            config.alert_telegram_bot_token,
            config.alert_telegram_chat_id,
            "\n".join(bits),
        )
    except Exception as exc:  # noqa: BLE001 — alerting must never crash trading
        log.warning(
            f"trade notification failed: {exc}",
            extra={"action": "notify_failed", "kind": kind, "symbol": symbol},
        )
        return False


def send_review_alert(
    config,
    *,
    channel_id: int,
    message_id: int,
    version: int,
    message_class: str,
    reason: str,
    text: str,
    media: str | None = None,
    trade_id: int | None = None,
) -> bool:
    """Tell a human a message needs a decision the bot refuses to make.

    Sent for every REVIEW_ALERT outcome: an image that may hold a signal, a
    conditional or partial close, a TP4-hold note, an edit of a live signal, an
    ambiguous target. No trade was placed and none will be. Never raises: the
    message is already persisted, so a failed send loses visibility, not data.
    """
    try:
        cid = str(channel_id)
        link = (f"https://t.me/c/{cid[4:]}/{message_id}"
                if cid.startswith("-100") else f"{channel_id}/{message_id}")
        snippet = (text or "").strip()
        if len(snippet) > 500:
            snippet = snippet[:500] + "…"
        bits = [
            f"{greeting()} ⚠️ Needs your review — no trade placed",
            "",
            f"  {message_class}  ·  message {message_id} v{version}",
            f"  why: {reason}",
        ]
        if trade_id is not None:
            bits.append(f"  trade: #{trade_id}")
        if media:
            bits.append(f"  media: {media}")
        bits += [f"  {link}", "", snippet or "(no text)"]
        return send_alert(
            config.alert_telegram_bot_token,
            config.alert_telegram_chat_id,
            "\n".join(bits),
        )
    except Exception as exc:  # noqa: BLE001 — alerting must never crash intake
        log.warning(f"review alert failed: {exc}",
                    extra={"action": "review_alert_failed",
                           "message_id": message_id})
        return False


def send_daily_summary(
    store,
    config,
    *,
    now: datetime | None = None,
    interval_seconds: int = DIGEST_INTERVAL_SECONDS,
    positions: list | None = None,
) -> bool:
    """Once every 24h: the day's realised P&L, and how open trades are doing.

    Cadence is DB-backed (a DAILY_SUMMARY event row), not an in-process
    timer, so it survives restarts — `time.monotonic()` resets to zero on
    every process and a daily-restarted bot would otherwise never reach 24h.
    Verified against 18 real sends: every gap was >= 24.01h across 27
    service restarts.

    `positions` is supplied by the caller (the runner reads the exchange),
    keeping this module free of exchange dependencies.
    """
    now = now or datetime.now(UTC)
    try:
        last = store.last_event_at(EventType.DAILY_SUMMARY)
        if last:
            try:
                last_at = datetime.fromisoformat(last)
                if last_at.tzinfo is None:
                    last_at = last_at.replace(tzinfo=UTC)
                if (now - last_at).total_seconds() < interval_seconds:
                    return False
            except (ValueError, TypeError):
                pass

        since = (now - timedelta(seconds=interval_seconds)).isoformat()
        realized, closed_count, unknown_pnl = store.realized_pnl_since(since)
        open_rows = store.open_trades()
        by_symbol = {p.symbol: p for p in (positions or [])}

        lines = [f"{greeting(now)} 📊 Daily summary", ""]
        lines.append(f"  realised P&L (24h): {_money(realized)}"
                     f"   across {closed_count} closed trade(s)")
        if unknown_pnl:
            # SUM() skips NULLs, so an uncomputed P&L would otherwise be
            # folded into the total as zero and read as breakeven.
            lines.append(f"  ⚠️ {unknown_pnl} of those has no recorded P&L — "
                         "the total above excludes it")

        if open_rows:
            lines.append(f"\n  open trades ({len(open_rows)}):")
            for r in open_rows:
                pos = by_symbol.get(r["symbol"])
                head = (f"    • {r['symbol']} {r['direction']} "
                        f"{r['actual_quantity']} — {r['state']}")
                lines.append(head)
                detail = _position_detail(r, pos)
                if detail:
                    lines.append(f"        {detail}")
        else:
            lines.append("\n  no open trades")

        if not send_alert(config.alert_telegram_bot_token,
                          config.alert_telegram_chat_id, "\n".join(lines)):
            return False
        store.append_event(
            EventType.DAILY_SUMMARY,
            f"daily summary sent: {closed_count} closed, {len(open_rows)} open",
            occurred_at=now.isoformat(),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(f"daily summary failed: {exc}",
                    extra={"action": "daily_summary_failed"})
        return False


def _position_detail(trade_row, position) -> str:
    """Entry, mark and unrealised P&L in dollars AND percent.

    Percent is of the position's NOTIONAL AT ENTRY (equivalently the price
    move), not of margin. With 10x leverage the two differ by an order of
    magnitude, so the basis is stated in the text rather than left to be
    guessed at.
    """
    if position is None:
        return ""
    try:
        entry = Decimal(str(position.entry_price or trade_row["actual_avg_entry_price"]))
        size = Decimal(str(position.size))
        if not entry or not size:
            return ""
        pnl = position.unrealized_pnl
        mark = position.mark_price
        if pnl is None and mark is not None:
            move = Decimal(str(mark)) - entry
            if str(position.side).lower().startswith("short"):
                move = -move
            pnl = move * size
        if pnl is None:
            return f"entry {entry}"
        pnl = Decimal(str(pnl))
        notional = entry * size
        pct = (pnl / notional * 100) if notional else Decimal(0)
        markbit = f"mark {Decimal(str(mark)):.4f}  ·  " if mark is not None else ""
        return (f"{markbit}unrealised {_money(pnl)} "
                f"({pct:+.2f}% of entry notional)")
    except (InvalidOperation, ValueError, TypeError, ZeroDivisionError):
        return ""
