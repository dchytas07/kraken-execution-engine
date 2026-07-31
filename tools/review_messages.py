"""Review what the parser makes of real channel messages.

    python tools/review_messages.py                  # fetch last 1000, write report
    python tools/review_messages.py --limit 200
    python tools/review_messages.py --from-db        # only messages the bot processed
    python tools/review_messages.py --all            # include the boring ones too

Two safety properties, both deliberate:

1. **Never touches the live bot.** Messages are parsed with `parse_message`,
   a pure function. Nothing goes through SignalPipeline, so no admission
   runs, no trade is created and no order is placed. The bot's database is
   opened read-only.

2. **Never shares the Telethon session.** The running bot holds
   `secrets/telegram.session` (SQLite); a second process on the same file can
   lock or corrupt it. This copies the session to a temp file and connects
   with the copy, so the live listener is unaffected.

Output is markdown, ordered so the messages worth your attention come first,
and GROUPED by shape — a channel repeats the same handful of templates, so
reviewing 1000 messages is really reviewing ~20 patterns.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bot.config import load_config  # noqa: E402
from bot.models import MessageType  # noqa: E402
from bot.parser.parse import parse_message  # noqa: E402

LOW_CONFIDENCE = 0.5

# Collapse everything that varies between two instances of the same template.
# Without the ticker rule, "#ETH/USDT Entry 1" and "#BTC/USDT Entry 1" look
# like different patterns and 1000 messages explode into ~470 groups instead
# of the ~20 templates the channel actually uses.
_TICKER = re.compile(
    r"[#$]?\b[A-Z]{2,6}(?:/(?:USDT|USDC|USD))?\b(?=\s|$|[^A-Za-z])"
)
_KNOWN = {
    "BTC", "ETH", "SOL", "GOLD", "XAU", "XAUT", "BNB", "ADA", "XRP", "DOGE",
    "SPX", "SP500", "DXY", "LTC", "LINK", "AVAX", "DOT", "MATIC", "TRX",
    "SUI", "PEPE", "SHIB", "TON", "NEAR", "APT", "ARB", "OP", "INJ", "TIA",
}
_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿️←-⇿⬀-⯿]"
)
_MD = re.compile(r"[*_`]+")
_WS = re.compile(r"\s+")


def _mask_ticker(m: re.Match) -> str:
    raw = m.group(0).lstrip("#$").split("/")[0].upper()
    return "<ASSET>" if raw in _KNOWN else m.group(0)


def signature(text: str) -> str:
    """A shape-key for grouping near-identical messages.

    Asset names, numbers, emoji and markdown are all collapsed, because a
    signal channel says the same handful of things about a dozen different
    coins and the operator wants to review the SHAPE once, not once per coin.
    """
    s = text or ""
    s = _EMOJI.sub(" ", s)
    s = _MD.sub("", s)
    s = _TICKER.sub(_mask_ticker, s)
    s = _NUM.sub("#", s)
    s = _WS.sub(" ", s).strip().lower()
    # Templates diverge in their tail (extra commentary lines); the head is
    # what identifies them.
    return s[:90]


def priority(msg_type: str, confidence: float, decision: str | None) -> tuple[int, str]:
    """Lower sorts first. Mirrors what the operator asked to see first."""
    if decision and "REQUIRES_REVIEW" in decision:
        return 0, "REQUIRES_REVIEW"
    if msg_type == MessageType.UNKNOWN.value:
        return 1, "UNKNOWN"
    if confidence is not None and confidence < LOW_CONFIDENCE:
        return 2, f"low confidence (<{LOW_CONFIDENCE})"
    return 3, "routine"


def from_db(db_path: Path) -> list[dict]:
    """Messages the bot actually processed, with its real decisions."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT message_id, raw_text, parsed_type, parsed_asset,
                  parse_confidence, execution_decision, rejection_reason,
                  rejection_detail
             FROM messages ORDER BY message_id"""
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        decision = r["execution_decision"] or ""
        if r["rejection_reason"]:
            decision = f"{decision} [{r['rejection_reason']}]".strip()
        out.append({
            "id": r["message_id"],
            "text": r["raw_text"] or "",
            "type": r["parsed_type"] or "—",
            "asset": r["parsed_asset"] or "—",
            "confidence": r["parse_confidence"],
            "decision": decision or "—",
            "detail": r["rejection_detail"] or "",
            "source": "db",
        })
    return out


async def from_telegram(limit: int) -> list[dict]:
    """Fetch history and parse it. Read-only; uses a COPY of the session."""
    from telethon import TelegramClient

    config = load_config()
    if not config.telegram_channel:
        raise SystemExit("TELEGRAM_CHANNEL is not set")

    live = config.telegram_session_path
    if not live.exists():
        raise SystemExit(f"no Telethon session at {live}; run tools/telegram_login.py")

    tmpdir = Path(tempfile.mkdtemp(prefix="review-session-"))
    copy = tmpdir / "review.session"
    shutil.copy2(live, copy)

    client = TelegramClient(str(copy), config.telegram_api_id, config.telegram_api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise SystemExit("session copy is not authorized")

    out = []
    async for m in client.iter_messages(config.telegram_channel, limit=limit):
        text = m.text or ""
        if not text.strip():
            continue
        parsed = parse_message(channel_id=-1, message_id=m.id, text=text)
        out.append({
            "id": m.id,
            "date": m.date,
            "text": text,
            "type": parsed.message_type.value,
            "asset": parsed.asset.value if parsed.asset else "—",
            "confidence": parsed.confidence,
            # Parse-level only: real admission also checks dedupe, occupancy,
            # exchange position and tradeability, so this is NOT the bot's
            # final decision — it is what the parser alone implies.
            "decision": ("executable-type"
                         if parsed.message_type.is_executable_candidate
                         else "not executable"),
            "detail": "; ".join(w.code for w in parsed.parse_warnings),
            "source": "telegram",
        })
    await client.disconnect()
    shutil.rmtree(tmpdir, ignore_errors=True)
    return out



def render(records: list[dict], show_all: bool, limit_note: str,
           top: int = 25) -> str:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        groups[signature(r["text"])].append(r)

    ranked = []
    for sig, items in groups.items():
        head = items[0]
        rank, label = priority(head["type"], head["confidence"], head["decision"])
        ranked.append((rank, -len(items), label, sig, items))
    ranked.sort(key=lambda t: (t[0], t[1]))

    total = len(records)
    lines = [
        "# Message review",
        "",
        f"{total} message(s), {len(groups)} distinct pattern(s). {limit_note}",
        "",
        "Grouped by shape and ordered so the ones worth your attention come "
        "first: REQUIRES_REVIEW, then UNKNOWN, then low confidence, then "
        "routine. Numbers are collapsed to `#` when grouping, so one entry "
        "covers every instance of that template.",
        "",
        "For telegram-sourced rows the decision column is **parse-level "
        "only** — real admission also applies dedupe, symbol occupancy, "
        "exchange-position and tradeability checks.",
        "",
        "## Summary",
        "",
        "| bucket | patterns | messages |",
        "|---|---|---|",
    ]
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for rank, _neg, label, _sig, items in ranked:
        counts[label][0] += 1
        counts[label][1] += len(items)
    for label, (npat, nmsg) in counts.items():
        lines.append(f"| {label} | {npat} | {nmsg} |")
    lines.append("")

    shown = 0
    skipped_tail = 0
    for rank, _neg, label, _sig, items in ranked:
        if rank == 3 and not show_all:
            continue
        if shown >= top:
            skipped_tail += len(items)
            continue
        shown += 1
        head = items[0]
        ids = ", ".join(str(i["id"]) for i in items[:6])
        more = f" (+{len(items) - 6} more)" if len(items) > 6 else ""
        lines += [
            f"## {shown}. [{label}] ×{len(items)}",
            "",
            f"- **parsed**: `{head['type']}`  ·  **asset**: `{head['asset']}`  "
            f"·  **confidence**: `{head['confidence']}`",
            f"- **decision**: `{head['decision']}`"
            + (f"  ·  _{head['detail']}_" if head["detail"] else ""),
            f"- **message ids**: {ids}{more}",
            "",
            "```",
            *(head["text"].strip().splitlines()[:14] or ["<empty>"]),
            "```",
            "",
            "**What does this actually mean?** →  _(your answer here)_",
            "",
            "---",
            "",
        ]

    if skipped_tail:
        lines.append(
            f"_Stopped after the top {top} patterns "
            f"({skipped_tail} further message(s) in longer-tail patterns, "
            "mostly one-off prose commentary). Use `--top N` to see more._"
        )
        lines.append("")
    if not show_all:
        routine = sum(len(i[4]) for i in ranked if i[0] == 3)
        if routine:
            lines.append(
                f"_{routine} routine message(s) hidden. Re-run with `--all` "
                "to include them._"
            )
    return "\n".join(lines)



def disposition(parsed, config) -> tuple[str, str]:
    """What the bot would do, judged from the PARSE alone.

    Deliberately not called "the decision": real admission also applies
    message idempotency, signal dedupe, symbol occupancy, unrecognised
    exchange positions and entry staleness, none of which are knowable from
    text. This is the subset that IS knowable, so a wrong verdict here is a
    genuine parser bug rather than a runtime condition.
    """
    if not parsed.message_type.is_executable_candidate:
        return "IGNORED", f"{parsed.message_type.value} is not an instruction type"
    if parsed.asset is None:
        if parsed.message_type.is_exit_like:
            return "REFUSED", "close instruction with no resolvable target"
        return "REFUSED", "no asset could be resolved"
    if not config.is_tradeable(parsed.asset):
        return "REFUSED", f"{parsed.asset.value} is not tradeable in this build"
    return "WOULD EXECUTE", f"executable {parsed.message_type.value} on {parsed.asset.value}"


def render_flat(records: list[dict], note: str) -> str:
    """Every message, one at a time, oldest first. No grouping."""
    config = load_config()
    rows = sorted(records, key=lambda r: r["id"])

    entries, tally = [], defaultdict(int)
    for n, r in enumerate(rows, start=1):
        # Re-parse from the cached text: the cache predates the direction
        # field, and re-parsing is free and keeps this honest about what the
        # CURRENT parser does rather than what it did at fetch time.
        parsed = parse_message(channel_id=-1, message_id=r["id"], text=r["text"])
        verdict, why = disposition(parsed, config)
        tally[verdict] += 1

        bits = [f"`{parsed.message_type.value}`", f"asset `{parsed.asset.value if parsed.asset else '-'}`"]
        if parsed.direction:
            bits.append(f"direction `{parsed.direction.value}`")
        bits.append(f"confidence `{parsed.confidence}`")

        body = (r["text"] or "").strip() or "<empty>"
        entries.append("\n".join([
            f"### {n} · msg {r['id']}",
            "",
            "```",
            body,
            "```",
            "",
            "- **parsed**: " + "  ·  ".join(bits),
            f"- **decision**: **{verdict}** — {why}",
            "- `[ ] correct`   `[ ] WRONG →` _(what it should be)_",
            "",
            "---",
            "",
        ]))

    head = [
        "# Message review — every message, one by one",
        "",
        f"{len(rows)} messages, oldest first (ids {rows[0]['id']}–{rows[-1]['id']}). {note}",
        "",
        "**decision** is judged from the parse alone: message type, asset "
        "resolution and tradeability. Real admission additionally applies "
        "idempotency, dedupe, symbol occupancy, unrecognised-position and "
        "staleness checks, which text cannot predict — so a wrong verdict "
        "here is a parser bug, not a runtime condition.",
        "",
        "| verdict | count |",
        "|---|---|",
    ]
    for k in ("WOULD EXECUTE", "REFUSED", "IGNORED"):
        head.append(f"| {k} | {tally.get(k, 0)} |")
    head += ["", "---", ""]
    return "\n".join(head) + "\n".join(entries)


def main() -> int:
    ap = argparse.ArgumentParser(prog="review_messages")
    ap.add_argument("--limit", type=int, default=1000,
                    help="how many channel messages to fetch (default 1000)")
    ap.add_argument("--from-db", action="store_true",
                    help="use only messages the bot has processed locally")
    ap.add_argument("--all", action="store_true",
                    help="include routine messages too")
    ap.add_argument("--out", default="docs/MESSAGE_REVIEW.md")
    ap.add_argument("--from-cache", action="store_true",
                    help="re-render from the last fetch, no Telegram call")
    ap.add_argument("--cache", default=".review_cache.json")
    ap.add_argument("--top", type=int, default=25,
                    help="how many patterns to detail (default 25)")
    ap.add_argument("--flat", action="store_true",
                    help="list every message individually, no grouping")
    args = ap.parse_args()

    cache = Path(args.cache)
    if args.from_db:
        config = load_config()
        records = from_db(config.db_path)
        note = f"Source: local database ({config.db_path})."
    elif args.from_cache:
        if not cache.exists():
            raise SystemExit(f"no cache at {cache}; run without --from-cache first")
        records = json.loads(cache.read_text())
        note = f"Source: cached Telegram fetch ({cache})."
    else:
        import asyncio
        records = asyncio.run(from_telegram(args.limit))
        # Cached so grouping can be re-tuned without re-hitting Telegram, and
        # without a second process ever opening the live bot's session again.
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(
            [{k: v for k, v in r.items() if k != "date"} for r in records],
            indent=1,
        ))
        note = f"Source: Telegram history, last {args.limit} requested."

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.flat:
        out.write_text(render_flat(records, note))
    else:
        out.write_text(render(records, args.all, note, top=args.top))
    print(f"wrote {out}  ({len(records)} messages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
