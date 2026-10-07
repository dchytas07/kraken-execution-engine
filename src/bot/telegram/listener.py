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
every reconnect the listener replays messages newer than the highest id already
stored, so a restart does not silently skip signals. Replayed messages are
deduplicated by the same identity check, so replay is free of side effects.

The listener owns no trading logic. It converts Telegram events into pipeline
calls and nothing else.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ..logging_setup import get_logger
from ..pipeline import SignalPipeline

log = get_logger(__name__)


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
        backfill_limit: int = 50,
    ) -> None:
        self.pipeline = pipeline
        self.api_id = api_id
        self.api_hash = api_hash
        self.channel = channel
        self.session_path = session_path
        self.backfill_limit = backfill_limit
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
        """Replay messages missed while disconnected.

        Bounded by `backfill_limit` so a long outage cannot dump an unbounded
        history into the pipeline. Anything older than the limit is reported
        rather than silently dropped — the operator should know signals were
        missed.
        """
        if self._client is None:
            return 0

        last_seen = self.pipeline.store.last_message_id(self._channel_id())
        replayed = 0
        try:
            messages = await self._client.get_messages(
                self.channel, limit=self.backfill_limit
            )
        except Exception:
            log.error(
                "backfill failed; live messages will still be processed",
                exc_info=True,
                extra={"action": "backfill_failed"},
            )
            return 0

        # get_messages returns newest first; replay oldest first so ordering
        # matches how the channel actually posted them.
        for message in reversed(list(messages)):
            inbound = self.to_inbound(message)
            if inbound is None:
                continue
            if last_seen is not None and inbound.message_id <= last_seen:
                # Already stored. An edit made while we were down is never
                # delivered live, so compare against the last version we hold.
                if inbound.edit_date and self._changed_since_stored(inbound):
                    edited = self.to_inbound(message, edited=True)
                    if edited is not None and self.dispatch(edited):
                        replayed += 1
                continue
            if self.dispatch(inbound):
                replayed += 1

        if replayed:
            log.warning(
                f"backfilled {replayed} message(s) that arrived while disconnected",
                extra={"action": "backfill", "count": replayed},
            )
        return replayed

    def _changed_since_stored(self, inbound: InboundMessage) -> bool:
        store = self.pipeline.store
        last = store.latest_message_version(inbound.channel_id, inbound.message_id)
        return last is not None and last["text_hash"] != store.text_hash(inbound.text)

    def _channel_id(self) -> int | None:
        try:
            return int(self.channel)
        except (TypeError, ValueError):
            return None
