"""Read-only status report: `python -m bot.main --status`.

Safe to run at any time, including while the live service is running. Two
deliberate choices make that true, and both matter:

1. **The database is opened read-only** (`mode=ro`), with plain queries
   rather than `Store`. `Store.__init__` runs `_migrate()`, which executes
   the schema script and can INSERT a schema_version row — a write against
   a database the live service is using.

2. **The paper account is read from its state FILE, never via
   `kraken futures paper ...`.** Those subcommands take the paper-state
   lock, and a CLI call killed mid-flight leaves that lock orphaned, which
   wedges the running bot's protection sweep until someone clears it. That
   happened during a manual status check. Reading the JSON directly cannot
   take the lock, cannot orphan it, and cannot block the service.

Mark prices come from `kraken futures ticker`, which is a public endpoint
and does NOT touch paper state, so it is lock-free too.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .alerting import _format_age
from .logging_setup import get_logger

log = get_logger(__name__)

# Test/operator override; None means "resolve like kraken-cli does".
PAPER_STATE: Path | None = None


def paper_state_path(env: dict[str, str] | None = None,
                     platform: str | None = None) -> Path:
    """Where kraken-cli keeps the futures paper state on this OS.

    Mirrors the Rust `dirs::config_dir()` kraken-cli uses:
      Linux    $XDG_CONFIG_HOME (if absolute) else ~/.config
      macOS    ~/Library/Application Support
      Windows  %APPDATA%
    then `kraken/paper/futures_state.json`. Pure path arithmetic: nothing is
    opened, created or locked here.
    """
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    home = Path(env.get("HOME") or Path.home())
    if platform == "darwin":
        base = home / "Library" / "Application Support"
    elif platform.startswith("win"):
        base = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
    else:
        xdg = env.get("XDG_CONFIG_HOME", "")
        base = Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".config"
    return base / "kraken" / "paper" / "futures_state.json"

UNRESOLVED = ("REQUIRES_REVIEW", "SAFE_ERROR")
_TERMINAL = ("CLOSED", "CANCELLED", "FAILED")


def _read_db(db_path: Path) -> dict:
    """Open read-only and pull everything in one pass."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" * len(_TERMINAL))
        trades = conn.execute(
            f"""SELECT * FROM trades WHERE state NOT IN ({placeholders})
                ORDER BY id""",
            _TERMINAL,
        ).fetchall()
        orders = conn.execute(
            f"""SELECT o.* FROM orders o
                  JOIN trades t ON t.id = o.trade_id
                 WHERE o.status IN ('INTENT','SUBMITTED','OPEN','PARTIALLY_FILLED')
                   AND t.state NOT IN ({placeholders})
                 ORDER BY o.trade_id, o.sequence""",
            _TERMINAL,
        ).fetchall()
        # Leftover non-final rows on TERMINAL trades: not divergence (the
        # position is flat and the trade is done), but worth surfacing as a
        # data-hygiene note rather than silently dropping.
        stale = conn.execute(
            f"""SELECT o.client_order_id, o.status, o.trade_id, t.state
                  FROM orders o JOIN trades t ON t.id = o.trade_id
                 WHERE o.status IN ('INTENT','SUBMITTED','OPEN','PARTIALLY_FILLED')
                   AND t.state IN ({placeholders})
                 ORDER BY o.trade_id""",
            _TERMINAL,
        ).fetchall()
        unresolved = conn.execute(
            f"""SELECT t.*,
                       COALESCE((SELECT MAX(e.occurred_at) FROM events e
                                  WHERE e.trade_id = t.id
                                    AND e.event_type = 'TRADE_STATE_CHANGED'),
                                t.updated_at) AS state_since
                  FROM trades t
                 WHERE t.state IN ({",".join("?" * len(UNRESOLVED))})
                 ORDER BY t.id""",
            UNRESOLVED,
        ).fetchall()
        midnight = datetime.now(UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        today = conn.execute(
            """SELECT id, symbol, direction, realized_pnl, closed_at
                 FROM trades
                WHERE state = 'CLOSED' AND closed_at >= ?
                ORDER BY closed_at""",
            (midnight,),
        ).fetchall()
        return {"trades": trades, "orders": orders, "stale": stale,
                "unresolved": unresolved, "today": today}
    finally:
        conn.close()


def _read_paper_state() -> tuple[dict | None, str]:
    """Positions and resting orders straight from the paper state file.

    Returns (state, note). A parse failure is reported, never raised: the
    file can be read mid-write, and a status command must not crash for it.
    """
    path = PAPER_STATE or paper_state_path()
    if not path.exists():
        return None, f"paper state file not found at {path}"
    for attempt in (1, 2):
        try:
            return json.loads(path.read_text()), ""
        except (json.JSONDecodeError, OSError) as exc:
            if attempt == 2:
                return None, f"could not read paper state ({exc})"
    return None, "unreachable"


def _marks(symbols: list[str], binary: str) -> dict[str, Decimal]:
    """Mark prices from the PUBLIC ticker endpoint — no paper lock involved."""
    out: dict[str, Decimal] = {}
    for sym in symbols:
        try:
            raw = subprocess.run(
                [binary, "futures", "ticker", sym, "-o", "json"],
                capture_output=True, text=True, timeout=20, check=False,
            ).stdout
            mark = json.loads(raw).get("ticker", {}).get("markPrice")
            if mark is not None:
                out[sym] = Decimal(str(mark))
        except Exception:  # noqa: BLE001 — a missing mark is cosmetic
            continue
    return out


def _dec(value) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal(0)


def build_status(config, *, now: datetime | None = None) -> str:
    """Render the whole report. Pure read; never mutates anything."""
    now = now or datetime.now(UTC)
    db = _read_db(config.db_path)
    state, note = _read_paper_state()

    ex_positions = {p["symbol"]: p for p in (state or {}).get("positions", [])}
    ex_orders = {o["client_order_id"]: o
                 for o in (state or {}).get("open_orders", [])
                 if o.get("client_order_id")}

    trades = db["trades"]
    symbols = sorted({t["symbol"] for t in trades} | set(ex_positions))
    marks = _marks(symbols, config.kraken_cli_path)

    L: list[str] = []
    L.append(f"KRAKEN SIGNAL BOT — STATUS        {now:%Y-%m-%d %H:%M UTC}")
    L.append(f"mode: {config.trading_mode.value.upper()}    "
             f"tradeable: {', '.join(sorted(a.value for a in config.tradeable_assets))}")
    L.append("")

    # -- open trades -------------------------------------------------------
    L.append(f"OPEN TRADES ({len(trades)})")
    if not trades:
        L.append("  none")
    for t in trades:
        orders = [o for o in db["orders"] if o["trade_id"] == t["id"]]
        stops = [o for o in orders if o["purpose"] == "SL"]
        tps = [o for o in orders if o["purpose"] == "TP"]
        entries = [o for o in orders if o["purpose"] == "ENTRY"]
        size = _dec(t["actual_quantity"])
        stop_cover = sum(_dec(o["quantity"]) for o in stops)
        protected = bool(stops) and stop_cover >= size and size > 0
        flag = "[PROTECTED]" if protected else (
            "[** NO STOP **]" if size > 0 else "[no position yet]")
        L.append(
            f"  #{t['id']}  {t['asset']:<4} {t['symbol']:<12} {t['direction']:<5} "
            f"{size} @ {t['actual_avg_entry_price'] or '—'}   "
            f"{t['state']}   {flag}"
        )
        bits = [f"stop {stop_cover}" if stops else "stop NONE"]
        if tps:
            tp_total = sum(_dec(o["quantity"]) for o in tps)
            pct = f" ({tp_total / size * 100:.0f}% of position)" if size else ""
            bits.append(f"TP ladder {tp_total}{pct}")
        if entries:
            bits.append(f"{len(entries)} entry order(s) resting")
        L.append(f"        {'  ·  '.join(bits)}")
        mark = marks.get(t["symbol"])
        entry = _dec(t["actual_avg_entry_price"])
        if mark and entry and size:
            move = (mark - entry) * (-1 if t["direction"] == "SHORT" else 1)
            L.append(f"        mark {mark:.2f}  ·  unrealised "
                     f"{'+' if move >= 0 else '-'}${abs(move * size):.2f}")
        if t["is_substitute_instrument"]:
            L.append("        SUBSTITUTE INSTRUMENT — not the quoted asset")
    L.append("")

    # -- cross-check -------------------------------------------------------
    L.append("EXCHANGE CROSS-CHECK")
    if state is None:
        L.append(f"  UNAVAILABLE — {note}")
    else:
        problems: list[str] = []
        # Per SYMBOL: trades sharing a symbol share the netted exchange
        # position, so only their sum can be compared with it.
        by_symbol: dict[str, list] = {}
        for t in trades:
            by_symbol.setdefault(t["symbol"], []).append(t)
        for symbol, group in by_symbol.items():
            pos = ex_positions.get(symbol)
            local = sum((_dec(t["actual_quantity"]) for t in group), Decimal(0))
            ids = ", ".join(f"#{t['id']}" for t in group)
            if pos is None:
                if local > 0:
                    problems.append(
                        f"{ids} {symbol}: local holds {local}, exchange has NO "
                        "position")
                continue
            if _dec(pos["size"]) != local:
                problems.append(
                    f"{ids} {symbol}: size local {local} vs exchange {pos['size']}")
            if any(str(pos["side"]).lower() != t["direction"].lower() for t in group):
                problems.append(
                    f"{ids} {symbol}: side local "
                    f"{'/'.join(sorted({t['direction'] for t in group}))} vs "
                    f"exchange {pos['side']}")
        for o in db["orders"]:
            eo = ex_orders.get(o["client_order_id"])
            if eo is None:
                problems.append(
                    f"order {o['client_order_id']} is live locally but NOT on "
                    "the exchange")
            elif _dec(eo["size"]) != _dec(o["quantity"]):
                problems.append(
                    f"order {o['client_order_id']}: size local {o['quantity']} "
                    f"vs exchange {eo['size']}")
        local_ids = {o["client_order_id"] for o in db["orders"]}
        for cid in ex_orders:
            if cid not in local_ids:
                problems.append(f"order {cid} rests on the exchange with no "
                                "local record")
        tracked = {t["symbol"] for t in trades}
        orphans = [s for s in ex_positions if s not in tracked]

        if problems:
            L.append(f"  MISMATCH — {len(problems)} problem(s):")
            L.extend(f"    ! {p}" for p in problems)
        else:
            L.append(f"  OK — {len(trades)} trade(s) and {len(db['orders'])} "
                     "order(s) match the paper account")
        for s in orphans:
            p = ex_positions[s]
            L.append(f"  ORPHAN — {s} {p['side']} {p['size']} on the exchange "
                     "with no local trade")
        if db["stale"]:
            L.append(f"  note: {len(db['stale'])} order row(s) left non-final on "
                     "already-closed trades (history, not divergence)")
    L.append("")

    # -- needs review ------------------------------------------------------
    L.append(f"NEEDS REVIEW ({len(db['unresolved'])})")
    if not db["unresolved"]:
        L.append("  none")
    for r in db["unresolved"]:
        L.append(f"  #{r['id']}  {r['symbol']:<12} {r['state']:<16} "
                 f"stuck ~{_format_age(r['state_since'], now)}")
        if r["state_detail"]:
            L.append(f"        {r['state_detail']}")
    L.append("")

    # -- today -------------------------------------------------------------
    closed = db["today"]
    total = sum(_dec(r["realized_pnl"]) for r in closed if r["realized_pnl"])
    unknown = sum(1 for r in closed if not r["realized_pnl"])
    L.append("TODAY (UTC)")
    L.append(f"  realised P&L  {'+' if total >= 0 else '-'}${abs(total):.2f}"
             f"    trades closed  {len(closed)}")
    if unknown:
        L.append(f"  ({unknown} closed trade(s) have no recorded P&L)")
    for r in closed:
        pnl = r["realized_pnl"]
        shown = (f"{'+' if _dec(pnl) >= 0 else '-'}${abs(_dec(pnl)):.2f}"
                 if pnl else "—")
        L.append(f"    #{r['id']} {r['symbol']} {r['direction']}  {shown}")
    return "\n".join(L)
