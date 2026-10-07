"""Telegram listener.

Reads the signal channel as a **user account**, not a Bot API account, because
the channel is one the user reads normally (brief 5). Telethon, verified
working on Python 3.14.6 including session handling, AES-IGE and asyncio.

Two properties matter more than throughput:

**Idempotency.** Every message is identified by `(channel_id, message_id)` and
handed to the pipeline, which enforces that pair as a UNIQUE index. Text is
never used for identity — brief 5 forbids it, and reposts differ in whitespace.

**Backfill on reconnect.** Telethon delivers live events, but a message that
arrives while the process is down is simply never delivered. On start and after
every reconnect the listener fetches exactly the messages newer than the highest
id already stored for this channel (at most CATCH_UP_CAP of them; more than
that alerts), so a restart does not silently skip signals. Replayed messages
are deduplicated by the same identity check, so replay is free of side effects.

The listener owns no trading logic. It converts Telegram events into pipeline
calls and nothing else.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ..alerting import send_alert
from ..logging_setup import get_logger
from ..pipeline import SignalPipeline

log = get_logger(__name__)

# Most messages replayed after an outage. Above it the OLDEST missed messages
# are not replayed and the operator is alerted: the bot must never claim it
# caught up when it did not.
CATCH_UP_CAP = 500
# With nothing stored yet there is no high-water mark to resume from, so the
# first start reads only this much recent history.
INITIAL_WINDOW = 50
# Already-stored messages re-read on start to catch edits made while down
# (an edit is never delivered live to a disconnected client).
EDIT_RECHECK_WINDOW = 50

# Telethon stamps a channel's messages with its "marked" id: -100 followed by
# the bare id, i.e. -(10**12 + id). That is the key rows are stored under.
_CHANNEL_MARK = 10**12


def channel_key(channel) -> int | None:
    """The chat id stored rows carry for this configured channel.

    TELEGRAM_CHANNEL may be the bare id (1000000001) or the marked one
    (-1001000000001); messages always arrive with the marked one. Looking
    the high-water mark up under the bare id finds nothing, which silently
    turned every catch-up into a full re-read. None for an @username: that
    can only be resolved through Telegram (see TelegramListener._channel_key).
    """
    if isinstance(channel, bool):
        return None
    try:
        n = int(channel)
    except (TypeError, ValueError):
        return None
    return n if n < 0 else -(_CHANNEL_MARK + n)


def media_kind(message) -> str | None:
    """What kind of attachment a Telethon message carries, if any."""
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "poll", None):
        return "poll"
    if getattr(message, "sticker", None):
        return "sticker"
    if getattr(message, "video", None) or getattr(message, "gif", None):
        return "video"
    if getattr(message, "voice", None) or getattr(message, "audio", None):
        return "audio"
    if getattr(message, "document", None):
        return "document"
    if getattr(message, "web_preview", None):
        return "webpage"
    return "other" if getattr(message, "media", None) else None


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """A Telegram message reduced to what the pipeline needs."""

    channel_id: int
    message_id: int
    text: str
    timestamp: float = 0.0
    reply_to_message_id: int | None = None
    # Text-less messages are NOT dropped: a picture may carry a signal, and
    # "signal-like but unparseable" must reach a human, not vanish here.
    media: str | None = None
    edit_date: str | None = None
    is_edit: bool = False


class TelegramListener:
    """Bridges Telethon events to the signal pipeline.

    `client_factory` is injectable so the whole class is testable without
    Telegram: tests supply a fake client and drive events directly.
    """

    def __init__(
        self,
        *,
        pipeline: SignalPipeline,
        api_id: int,
        api_hash: str,
        channel: str,
        session_path: Path,
        client_factory=None,
        catch_up_cap: int = CATCH_UP_CAP,
        initial_window: int = INITIAL_WINDOW,
    ) -> None:
        self.pipeline = pipeline
        self.api_id = api_id
        self.api_hash = api_hash
        self.channel = channel
        self.session_path = session_path
        self.catch_up_cap = catch_up_cap
        self.initial_window = initial_window
        self._client_factory = client_factory
        self._client = None
        self.messages_handled = 0
        self.messages_skipped = 0

    # -- lifecycle -------------------------------------------------------

    def _build_client(self):
        if self._client_factory is not None:
            return self._client_factory()
        from telethon import TelegramClient  # imported lazily so tests need no network

        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.session_path.parent.chmod(0o700)
        except OSError:
            pass
        return TelegramClient(str(self.session_path), self.api_id, self.api_hash)

    async def start(self) -> None:
        from telethon import events

        self._client = self._build_client()
        await self._client.start()
        log.info(
            f"telegram connected; listening to {self.channel}",
            extra={"action": "telegram_connected"},
        )

        @self._client.on(events.NewMessage(chats=self.channel))
        async def _on_new_message(event):  # pragma: no cover - thin adapter
            await self.handle_event(event)

        # Half of this channel's messages are edited (result replies get their
        # profit figure updated, signal posts get '/ HOLD' appended).
        @self._client.on(events.MessageEdited(chats=self.channel))
        async def _on_edited_message(event):  # pragma: no cover - thin adapter
            await self.handle_event(event, edited=True)

        # A message that arrived while we were down is never delivered live.
        await self.backfill()

    async def run_forever(self) -> None:
        await self.start()
        await self._client.run_until_disconnected()

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.disconnect()

    # -- message handling ------------------------------------------------

    @staticmethod
    def to_inbound(event, *, edited: bool = False) -> InboundMessage | None:
        """Normalise a Telethon event. Returns None for anything unusable."""
        # Telethon overloads `.message`: an EVENT exposes `.message` as the
        # Message object, while a MESSAGE exposes `.message` as its text. So
        # unwrapping unconditionally yields the text and loses `.id`. Unwrap
        # only when the attribute is not a string — that distinguishes the two
        # and lets this accept either, which backfill relies on.
        inner = getattr(event, "message", None)
        message = inner if inner is not None and not isinstance(inner, str) else event
        text = getattr(message, "message", None) or getattr(message, "text", None)
        text = str(text) if text and str(text).strip() else ""
        media = media_kind(message)
        if not text and media is None:
            return None            # a service message: nothing to classify

        chat_id = getattr(event, "chat_id", None) or getattr(message, "chat_id", None)
        message_id = getattr(message, "id", None)
        if chat_id is None or message_id is None:
            return None

        date = getattr(message, "date", None)
        timestamp = date.timestamp() if hasattr(date, "timestamp") else 0.0

        edit_date = getattr(message, "edit_date", None)
        return InboundMessage(
            channel_id=int(chat_id),
            message_id=int(message_id),
            text=text,
            timestamp=timestamp,
            reply_to_message_id=getattr(message, "reply_to_msg_id", None),
            media=media,
            edit_date=edit_date.isoformat() if hasattr(edit_date, "isoformat") else None,
            is_edit=edited,
        )

    async def handle_event(self, event, *, edited: bool = False) -> bool:
        """Handle one Telegram event. Returns True if it reached the pipeline.

        Never raises: an exception here would kill the Telethon event loop and
        stop the bot receiving anything. Failures are logged and the listener
        keeps running — reconciliation remains the safety net for anything a
        dropped message would have driven.
        """
        inbound = self.to_inbound(event, edited=edited)
        if inbound is None:
            self.messages_skipped += 1
            return False
        return self.dispatch(inbound)

    def dispatch(self, inbound: InboundMessage) -> bool:
        try:
            if inbound.is_edit:
                result = self.pipeline.handle_edit(
                    channel_id=inbound.channel_id,
                    message_id=inbound.message_id,
                    text=inbound.text,
                    edit_date=inbound.edit_date,
                    timestamp=inbound.timestamp,
                    reply_to_message_id=inbound.reply_to_message_id,
                    media=inbound.media,
                )
            else:
                result = self.pipeline.handle_message(
                    channel_id=inbound.channel_id,
                    message_id=inbound.message_id,
                    text=inbound.text,
                    timestamp=inbound.timestamp,
                    reply_to_message_id=inbound.reply_to_message_id,
                    media=inbound.media,
                    edit_date=inbound.edit_date,
                )
        except Exception:
            log.error(
                "pipeline raised while handling a Telegram message",
                exc_info=True,
                extra={
                    "channel_id": inbound.channel_id,
                    "message_id": inbound.message_id,
                    "action": "dispatch_failed",
                },
            )
            return False

        self.messages_handled += 1
        log.info(
            f"message {inbound.channel_id}/{inbound.message_id} handled: "
            f"accepted={result.accepted}",
            extra={
                "channel_id": inbound.channel_id,
                "message_id": inbound.message_id,
                "decision": "accepted" if result.accepted else "refused",
                "action": "message_handled",
            },
        )
        return True

    # -- backfill --------------------------------------------------------

    async def backfill(self) -> int:
        """Replay messages missed while disconnected; returns how many.

        Resumes from the highest Telegram id stored for this channel and asks
        Telegram for exactly the messages after it (`min_id`), at most
        `catch_up_cap`. If more than that were missed, the newest
        `catch_up_cap` are replayed and the operator is ALERTED that older
        ones were not: a gap is reported, never silently skipped.

        Separately, the last EDIT_RECHECK_WINDOW stored messages are re-read
        so an edit made while we were down is still classified.
        """
        if self._client is None:
            return 0

        key = await self._channel_key()
        last_seen = (self.pipeline.store.last_message_id(key)
                     if key is not None else None)
        if key is None:
            log.warning(
                f"cannot resolve the stored key for channel {self.channel!r}; "
                f"catch-up falls back to the latest {self.initial_window} messages",
                extra={"action": "backfill_no_channel_key"},
            )
        try:
            if last_seen is None:
                messages = list(await self._client.get_messages(
                    self.channel, limit=self.initial_window))
            else:
                # One over the cap tells "exactly at the cap" from "beyond it".
                messages = list(await self._client.get_messages(
                    self.channel, limit=self.catch_up_cap + 1, min_id=last_seen))
        except Exception:
            log.error(
                "backfill failed; live messages will still be processed",
                exc_info=True,
                extra={"action": "backfill_failed"},
            )
            return 0

        if last_seen is not None and len(messages) > self.catch_up_cap:
            messages = messages[:self.catch_up_cap]      # newest first: keep newest
            self._alert_catch_up_overflow(last_seen, messages)

        # get_messages returns newest first; replay oldest first so ordering
        # matches how the channel actually posted them.
        replayed = 0
        for message in reversed(messages):
            inbound = self.to_inbound(message)
            if inbound is None:
                continue
            if last_seen is not None and inbound.message_id <= last_seen:
                continue                                  # never re-dispatch stored ids
            if self.dispatch(inbound):
                replayed += 1

        if last_seen is not None:
            await self._recheck_edits(last_seen)

        if replayed:
            log.warning(
                f"backfilled {replayed} message(s) that arrived while disconnected"
                + (f" (after stored id {last_seen})" if last_seen is not None else
                   " (no stored high-water mark: initial window)"),
                extra={"action": "backfill", "count": replayed},
            )
        return replayed

    async def _recheck_edits(self, last_seen: int) -> int:
        """Re-read recent STORED messages and dispatch any edited since."""
        try:
            recent = await self._client.get_messages(
                self.channel, limit=EDIT_RECHECK_WINDOW, max_id=last_seen + 1)
        except Exception:
            log.error("edit recheck after reconnect failed", exc_info=True,
                      extra={"action": "backfill_edit_recheck_failed"})
            return 0
        rechecked = 0
        for message in reversed(list(recent)):
            inbound = self.to_inbound(message)
            if inbound is None or not inbound.edit_date:
                continue
            if self._changed_since_stored(inbound):
                edited = self.to_inbound(message, edited=True)
                if edited is not None and self.dispatch(edited):
                    rechecked += 1
        return rechecked

    def _alert_catch_up_overflow(self, last_seen: int, kept) -> None:
        oldest_kept = min((getattr(m, "id", 0) for m in kept), default=0)
        text = (
            f"⚠️ Telegram catch-up cap hit: more than {self.catch_up_cap} "
            f"messages arrived while the bot was down.\n"
            f"Replayed the newest {self.catch_up_cap} (ids {oldest_kept}+). "
            f"Messages after stored id {last_seen} and before {oldest_kept} "
            f"were NOT processed — check the channel by hand."
        )
        log.error(text, extra={"action": "backfill_cap_hit",
                               "last_seen": last_seen, "oldest_kept": oldest_kept})
        config = self.pipeline.config
        send_alert(config.alert_telegram_bot_token,
                   config.alert_telegram_chat_id, text)

    def _changed_since_stored(self, inbound: InboundMessage) -> bool:
        store = self.pipeline.store
        last = store.latest_message_version(inbound.channel_id, inbound.message_id)
        return last is not None and last["text_hash"] != store.text_hash(inbound.text)

    async def _channel_key(self) -> int | None:
        key = channel_key(self.channel)
        if key is not None:
            return key
        # An @username: ask Telethon for the peer id (served from its cache).
        try:
            return int(await self._client.get_peer_id(self.channel))
        except Exception:
            return None
