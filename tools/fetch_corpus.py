"""Pull the signal channel's real history into corpus/channel_history.jsonl.

    .venv/bin/python tools/fetch_corpus.py [--limit N]

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


async def fetch(limit: int | None) -> list[dict]:
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
    try:
        if not await client.is_user_authorized():
            raise SystemExit("session copy is not authorized")
        rows = []
        async for m in client.iter_messages(config.telegram_channel, limit=limit):
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
        return sorted(rows, key=lambda r: r["id"])
    finally:
        await client.disconnect()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    rows = asyncio.run(fetch(args.limit))
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    print(f"wrote {len(rows)} messages to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
