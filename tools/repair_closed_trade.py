"""Repair a CLOSED trade whose books were left incomplete.

    .venv/bin/python tools/repair_closed_trade.py --trade 8            # dry run
    .venv/bin/python tools/repair_closed_trade.py --trade 8 --apply

Written for trade 8 (2026-10-04): an ETH short LIQUIDATED before its stop could
trigger. The close was recorded, but realised P&L stayed NULL (the liquidation
fill belongs to none of our orders) and the stop row stayed UNKNOWN (the order
vanished with the position). The engine and reconciler now handle both; this
fixes the one trade that predates that.

Safe by construction:
  * DRY RUN BY DEFAULT. Nothing is written without --apply, and the dry run opens
    the database read-only.
  * Reads the paper account from its state FILE, never `kraken ... paper`, so it
    takes no paper-state lock (see bot/status.py).
  * Repairs only what is demonstrably unset: a CLOSED trade with realised P&L NULL,
    and order rows still UNKNOWN whose exchange order is no longer resting. Every
    change is appended to the audit log with its before/after values.
  * P&L uses the same arithmetic as the engine (bot.pnl): gross of fees.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bot.config import load_config  # noqa: E402
from bot.exchange.kraken import fill_from_raw  # noqa: E402
from bot.pnl import exit_pnl, liquidation_exit  # noqa: E402
from bot.status import paper_state_path  # noqa: E402


def plan(db: Path, state_file: Path, trade_id: int) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        trade = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        if trade is None:
            raise SystemExit(f"no trade {trade_id}")
        if trade["state"] != "CLOSED":
            raise SystemExit(f"trade {trade_id} is {trade['state']}, not CLOSED: refusing")
        rows = conn.execute(
            "SELECT * FROM orders WHERE trade_id = ? ORDER BY id", (trade_id,)
        ).fetchall()
    finally:
        conn.close()

    state = json.loads(state_file.read_text())
    resting = {o.get("id") or o.get("order_id") for o in state.get("open_orders", [])}
    fills = [fill_from_raw(f) for f in state.get("fills", [])]

    changes: dict = {"trade": trade_id, "orders": [], "realized_pnl": None, "notes": []}
    for r in rows:
        if r["status"] == "UNKNOWN" and r["exchange_order_id"] not in resting:
            changes["orders"].append({
                "id": r["id"], "client_order_id": r["client_order_id"],
                "from": "UNKNOWN", "to": "CANCELLED",
                "detail": "already gone from the exchange when the position closed",
            })

    if trade["realized_pnl"] is None:
        entries = sum((Decimal(r["filled_quantity"] or 0) for r in rows
                       if r["purpose"] == "ENTRY"), Decimal(0))
        exits = sum((Decimal(r["filled_quantity"] or 0) for r in rows
                     if r["purpose"] != "ENTRY"), Decimal(0))
        own = [(Decimal(r["filled_quantity"]), Decimal(r["avg_fill_price"]))
               for r in rows if r["purpose"] != "ENTRY"
               and Decimal(r["filled_quantity"] or 0) > 0 and r["avg_fill_price"]]
        liq = liquidation_exit(fills, symbol=trade["symbol"],
                               since=trade["created_at"], quantity=entries - exits)
        if liq is not None:
            own.append(liq)
            changes["notes"].append(
                f"liquidation fill: {liq[0]} @ {liq[1]} on {trade['symbol']}")
        entry = trade["actual_avg_entry_price"]
        pnl = exit_pnl(trade["direction"], Decimal(entry), own) if entry else None
        if pnl is None:
            changes["notes"].append("realised P&L could not be derived: left NULL")
        else:
            changes["realized_pnl"] = str(pnl)
    return changes


def apply(db: Path, changes: dict) -> None:
    # Imported here so a dry run never constructs a Store. migrate=False: this
    # may run against the database of a LIVE service, and must change only the
    # rows it reports, never the schema the running code was built against.
    from bot.db.store import Store
    from bot.models import EventType, OrderStatus

    store = Store(db, migrate=False)
    try:
        for o in changes["orders"]:
            store.update_order(o["id"], status=OrderStatus.CANCELLED,
                               status_detail=o["detail"])
        if changes["realized_pnl"] is not None:
            store.update_trade_actuals(
                changes["trade"], realized_pnl=Decimal(changes["realized_pnl"]))
        store.append_event(
            EventType.RECONCILE_REPAIRED,
            f"books repaired by tools/repair_closed_trade.py: "
            f"{len(changes['orders'])} order row(s), realised P&L "
            f"{changes['realized_pnl'] or 'unchanged'}",
            trade_id=changes["trade"], detail=changes,
        )
    finally:
        store.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trade", type=int, required=True)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--state", type=Path, default=None)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db = args.db or load_config().db_path
    state = args.state or paper_state_path()
    changes = plan(db, state, args.trade)

    print(f"trade {changes['trade']}  ({db})")
    for o in changes["orders"]:
        print(f"  order {o['client_order_id']}: {o['from']} -> {o['to']}  ({o['detail']})")
    print(f"  realised P&L: {changes['realized_pnl'] or '(unchanged)'}")
    for n in changes["notes"]:
        print(f"  note: {n}")
    if not changes["orders"] and changes["realized_pnl"] is None:
        print("  nothing to repair")
        return
    if not args.apply:
        print("\nDRY RUN: nothing written. Re-run with --apply to write it.")
        return
    apply(db, changes)
    print("\napplied.")


if __name__ == "__main__":
    main()
