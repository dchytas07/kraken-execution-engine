"""Pull the signal channel's real history into corpus/channel_history.jsonl.

    .venv/bin/python tools/fetch_corpus.py [--limit N]
    .venv/bin/python tools/fetch_corpus.py --list-dialogs "Some Name" ["Other"]
    .venv/bin/python tools/fetch_corpus.py --channel "Exact Title" | --channel -100123...

With no --channel it fetches the bot's configured channel into
corpus/channel_history.jsonl, as it always has. --channel picks another dialog
the account is ALREADY in (never joins anything) by exact title or numeric id
and writes corpus/<slug>/history.jsonl. A title that matches more than one
dialog is refused with the candidates listed. --list-dialogs only prints the
dialogs whose title contains any of the given strings.

Read-only, and never shares the live Telethon session: the running bot holds
secrets/telegram.session (SQLite), so this connects with a COPY, exactly as
tools/review_messages.py does. Nothing here touches the bot or the exchange.

One JSON object per line, oldest first, for EVERY message including ones with
no text (images, polls, stickers) because "signal-like but unparseable" is a
class the bot has to handle. Telegram returns only the latest text of an edited
message, so `edit_date` marks which messages were edited but the earlier
versions are not recoverable from the API; the live listener records each
version it sees.

The raw dump is bulk third-party content and is gitignored (corpus/). Tests
use a curated, de-duplicated sample (tests/fixtures/channel_corpus.json).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bot.config import load_config  # noqa: E402
from bot.telegram.listener import media_kind  # noqa: E402

OUT = ROOT / "corpus" / "channel_history.jsonl"


def slug(title: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "-" for c in title)
    return "-".join(part for part in out.split("-") if part) or "channel"


def _kind(entity) -> str:
    if getattr(entity, "broadcast", False):
        return "channel"
    if getattr(entity, "megagroup", False):
        return "supergroup"
    if getattr(entity, "first_name", None) is not None or getattr(entity, "bot", False):
        return "user/bot"
    return "group"


async def _members(client, entity) -> int | None:
    count = getattr(entity, "participants_count", None)
    if count is not None:
        return count
    try:  # read-only metadata request; does not join
        from telethon.tl.functions.channels import GetFullChannelRequest
        full = await client(GetFullChannelRequest(entity))
        return full.full_chat.participants_count
    except Exception:  # noqa: BLE001 — not a channel, or not allowed
        return None


async def _matching_dialogs(client, needles: list[str], exact: bool) -> list:
    found = []
    async for d in client.iter_dialogs():
        title = d.title or ""
        if exact:
            if any(title.casefold() == n.casefold() for n in needles):
                found.append(d)
        elif any(n.casefold() in title.casefold() for n in needles):
            found.append(d)
    return found


async def _connect():
    from telethon import TelegramClient

    config = load_config()
    live = config.telegram_session_path
    if not live.exists():
        raise SystemExit(f"no Telethon session at {live}")
    tmp = Path(tempfile.mkdtemp(prefix="corpus-session-"))
    copy = tmp / "corpus.session"
    shutil.copy2(live, copy)
    client = TelegramClient(str(copy), config.telegram_api_id, config.telegram_api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit("session copy is not authorized")
    return client, tmp, config


async def list_dialogs(needles: list[str]) -> None:
    client, tmp, _ = await _connect()
    try:
        for d in await _matching_dialogs(client, needles, exact=False):
            members = await _members(client, d.entity)
            print(f"{d.title!r}  id={d.id}  type={_kind(d.entity)}  "
                  f"members={members if members is not None else 'n/a'}")
    finally:
        await client.disconnect()
        shutil.rmtree(tmp, ignore_errors=True)


async def fetch(limit: int | None, channel: str | None = None) -> tuple[list[dict], str | None]:
    client, tmp, config = await _connect()
    try:
        title = None
        target = config.telegram_channel
        if channel is not None:
            if channel.lstrip("-").isdigit():
                matches = [d async for d in client.iter_dialogs() if d.id == int(channel)]
            else:
                matches = await _matching_dialogs(client, [channel], exact=True)
            if len(matches) != 1:
                for d in matches:
                    print(f"  candidate: {d.title!r} id={d.id}", file=sys.stderr)
                raise SystemExit(f"{channel!r} matches {len(matches)} dialogs; "
                                 "pass the exact id")
            target, title = matches[0].entity, matches[0].title
        rows = []
        async for m in client.iter_messages(target, limit=limit):
            if len(rows) % 250 == 0:
                print(f"  fetched {len(rows)}...", flush=True)
            rows.append({
                "id": m.id,
                "date": m.date.isoformat() if m.date else None,
                "edit_date": m.edit_date.isoformat() if m.edit_date else None,
                "text": m.message or "",
                "reply_to": getattr(m, "reply_to_msg_id", None),
                "media": media_kind(m),
                "forwarded": bool(getattr(m, "fwd_from", None)),
                "grouped_id": getattr(m, "grouped_id", None),
            })
        return sorted(rows, key=lambda r: r["id"]), title
    finally:
        await client.disconnect()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--channel", default=None,
                    help="exact dialog title or numeric id; default: the bot's channel")
    ap.add_argument("--list-dialogs", nargs="+", metavar="TEXT",
                    help="print dialogs whose title contains TEXT, then exit")
    args = ap.parse_args()
    if args.list_dialogs:
        asyncio.run(list_dialogs(args.list_dialogs))
        return
    rows, title = asyncio.run(fetch(args.limit, args.channel))
    out = OUT if args.channel is None else ROOT / "corpus" / slug(title) / "history.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    print(f"wrote {len(rows)} messages to {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
