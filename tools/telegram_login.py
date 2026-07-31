"""One-time interactive Telethon login, plus a channel finder.

Run this from a REAL terminal (it prompts for your phone number and the code
Telegram sends you). It creates the session file the bot then reuses, so the
background service never needs to prompt for anything.

    python tools/telegram_login.py

Nothing here trades, reads Kraken, or touches the bot database. It only
creates the session and lists the channels your account can see, so you can
copy the correct identifier into TELEGRAM_CHANNEL.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bot.config import load_config  # noqa: E402


async def main() -> int:
    from telethon import TelegramClient

    config = load_config()
    if not (config.telegram_api_id and config.telegram_api_hash):
        print("TELEGRAM_API_ID / TELEGRAM_API_HASH are not set in .env",
              file=sys.stderr)
        return 2

    session = config.telegram_session_path
    session.parent.mkdir(parents=True, exist_ok=True)
    try:
        session.parent.chmod(0o700)
    except OSError:
        pass

    existed = session.exists()
    print(f"session file: {session}  ({'exists' if existed else 'will be created'})")

    client = TelegramClient(str(session), config.telegram_api_id,
                            config.telegram_api_hash)
    # Prompts for phone + login code on first run; silent afterwards.
    await client.start()

    me = await client.get_me()
    print(f"\nlogged in as: {me.first_name or ''} (@{me.username or 'no username'})")
    print(f"session saved: {session}\n")

    print("Channels and groups this account can see")
    print("Copy the USERNAME (preferred) or ID into TELEGRAM_CHANNEL in .env.")
    print("A title with spaces is NOT a valid identifier.\n")
    print(f"  {'title':<40} {'username':<26} id")
    print(f"  {'-' * 40} {'-' * 26} {'-' * 16}")

    configured = (config.telegram_channel or "").strip().lower()
    matches = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if not getattr(entity, "broadcast", False) and not getattr(
            entity, "megagroup", False
        ):
            continue
        title = (dialog.name or "")[:39]
        username = getattr(entity, "username", None)
        marker = ""
        if configured and configured == (dialog.name or "").strip().lower():
            marker = "   <-- matches your TELEGRAM_CHANNEL title"
            matches.append((dialog.name, username, entity.id))
        print(f"  {title:<40} {('@' + username) if username else '—':<26} "
              f"{entity.id}{marker}")

    if configured:
        print()
        if matches:
            name, username, cid = matches[0]
            better = f"@{username}" if username else str(cid)
            print(f"TELEGRAM_CHANNEL is currently the TITLE {name!r}, which "
                  f"Telethon cannot resolve.")
            print(f"Change it in .env to:  TELEGRAM_CHANNEL={better}")
        else:
            print(f"No channel matched the configured title "
                  f"{config.telegram_channel!r} — pick one from the list above.")

    await client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
