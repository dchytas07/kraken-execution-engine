"""SQLite persistence layer.

Decimals are stored as TEXT and returned as Decimal. Money never round-trips
through a float — SQLite's REAL is a binary double, and 0.0046 BTC becoming
0.004600000000000001 in a size field is exactly the class of bug that produces
a rejected or mis-sized order.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..models import (
    EventType,
    ExecutionDecision,
    OrderPurpose,
    OrderStatus,
    RejectionReason,
    TradeState,
    assert_transition,
)

SCHEMA_VERSION = 1
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dec(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _txt(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


class DuplicateMessage(Exception):
    """This (channel_id, message_id) has already been ingested."""


class SymbolAlreadyActive(Exception):
    """The symbol already holds as many active trades as are allowed."""


class OppositeDirectionActive(SymbolAlreadyActive):
    """An active trade on this symbol goes the other way. Kraken nets positions
    per symbol, so an opposite trade would silently shrink or flip the existing
    one instead of opening beside it."""


class Store:
    """Transactional repository. All writes go through `transaction()`."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            db_path, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA synchronous = FULL")
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    # -- schema -----------------------------------------------------------

    def _migrate(self) -> None:
        self._conn.executescript(_SCHEMA_PATH.read_text())
        row = self._conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        if row["v"] is None:
            self._conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, _now()),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Explicit transaction. Rolls back on any exception."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -- messages ----------------------------------------------------------

    def record_message(
        self,
        *,
        channel_id: int,
        message_id: int,
        raw_text: str,
        message_timestamp: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        """Persist an inbound message before any processing.

        Raises DuplicateMessage if already seen — idempotency layer 1 (brief 5).
        """
        try:
            with self.transaction() as conn:
                cur = conn.execute(
                    """INSERT INTO messages
                       (channel_id, message_id, received_at, message_timestamp,
                        raw_text, reply_to_message_id, execution_decision)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        channel_id,
                        message_id,
                        _now(),
                        message_timestamp,
                        raw_text,
                        reply_to_message_id,
                        ExecutionDecision.PENDING.value,
                    ),
                )
                return int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" in str(exc):
                raise DuplicateMessage(
                    f"message {channel_id}/{message_id} already recorded"
                ) from exc
            raise

    def has_message(self, channel_id: int, message_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM messages WHERE channel_id = ? AND message_id = ?",
            (channel_id, message_id),
        ).fetchone()
        return row is not None

    def last_message_id(self, channel_id: int | None) -> int | None:
        """Highest message id already stored, for reconnect backfill.

        None means nothing has been seen yet, so the listener replays whatever
        the backfill window holds rather than assuming a starting point.
        """
        if channel_id is None:
            row = self._conn.execute(
                "SELECT MAX(message_id) AS m FROM messages"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT MAX(message_id) AS m FROM messages WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
        return row["m"] if row and row["m"] is not None else None

    def get_message(self, channel_id: int, message_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM messages WHERE channel_id = ? AND message_id = ?",
            (channel_id, message_id),
        ).fetchone()

    def update_message_parse(
        self,
        row_id: int,
        *,
        parsed_type: str,
        parsed_asset: str | None = None,
        parsed_direction: str | None = None,
        parsed_values: dict[str, Any] | None = None,
        confidence: float = 0.0,
        warnings: list[Any] | None = None,
        errors: list[str] | None = None,
        signal_fingerprint: str | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """UPDATE messages SET
                     parsed_type = ?, parsed_asset = ?, parsed_direction = ?,
                     parsed_values_json = ?, parse_confidence = ?,
                     parse_warnings_json = ?, parse_errors_json = ?,
                     signal_fingerprint = ?
                   WHERE id = ?""",
                (
                    parsed_type,
                    parsed_asset,
                    parsed_direction,
                    json.dumps(parsed_values, default=str) if parsed_values else None,
                    confidence,
                    json.dumps(warnings, default=str) if warnings else None,
                    json.dumps(errors) if errors else None,
                    signal_fingerprint,
                    row_id,
                ),
            )

    def record_decision(
        self,
        row_id: int,
        decision: ExecutionDecision,
        reason: RejectionReason | None = None,
        detail: str | None = None,
    ) -> None:
        """Every message ends with a decision and, if not executed, a reason."""
        with self.transaction() as conn:
            conn.execute(
                """UPDATE messages SET execution_decision = ?, rejection_reason = ?,
                          rejection_detail = ?, processed_at = ? WHERE id = ?""",
                (
                    decision.value,
                    reason.value if reason else None,
                    detail,
                    _now(),
                    row_id,
                ),
            )

    # -- message versions --------------------------------------------------

    @staticmethod
    def text_hash(text: str) -> str:
        import hashlib

        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]

    def record_message_version(
        self,
        *,
        message_row_id: int,
        channel_id: int,
        message_id: int,
        text: str,
        message_class: str,
        action: str,
        reason: str,
        media: str | None = None,
        edit_date: str | None = None,
        reply_to_message_id: int | None = None,
        trade_id: int | None = None,
    ) -> tuple[int, int]:
        """Append the next version of a message. Returns (row id, version).

        Version 1 is the original; each text change appends the next number.
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM message_versions "
                "WHERE channel_id = ? AND message_id = ?",
                (channel_id, message_id),
            ).fetchone()
            version = row["v"] + 1
            cur = conn.execute(
                """INSERT INTO message_versions
                   (message_row_id, channel_id, message_id, version, recorded_at,
                    edit_date, text, text_hash, media, reply_to_message_id,
                    message_class, action, reason, trade_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (message_row_id, channel_id, message_id, version, _now(),
                 edit_date, text, self.text_hash(text), media,
                 reply_to_message_id, message_class, action, reason, trade_id),
            )
            return cur.lastrowid, version

    def set_version_outcome(
        self, version_row_id: int, *, action: str, reason: str,
        trade_id: int | None = None,
    ) -> None:
        """Replace the PLANNED action with what actually happened."""
        with self.transaction() as conn:
            conn.execute(
                """UPDATE message_versions SET action = ?, reason = ?,
                          trade_id = COALESCE(?, trade_id) WHERE id = ?""",
                (action, reason, trade_id, version_row_id),
            )

    def latest_message_version(self, channel_id: int, message_id: int):
        return self._conn.execute(
            """SELECT * FROM message_versions
               WHERE channel_id = ? AND message_id = ?
               ORDER BY version DESC LIMIT 1""",
            (channel_id, message_id),
        ).fetchone()

    def message_versions(self, channel_id: int, message_id: int) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            """SELECT * FROM message_versions
               WHERE channel_id = ? AND message_id = ? ORDER BY version""",
            (channel_id, message_id),
        ).fetchall())

    def trade_for_origin_message(
        self, channel_id: int, message_id: int
    ) -> sqlite3.Row | None:
        """The trade a message opened, if any (newest wins)."""
        return self._conn.execute(
            """SELECT * FROM trades
               WHERE origin_channel_id = ? AND origin_message_id = ?
               ORDER BY id DESC LIMIT 1""",
            (channel_id, message_id),
        ).fetchone()

    def update_intent_stop(self, trade_id: int, stop: Decimal) -> None:
        """Latest stop INSTRUCTION. Reconciliation rebuilds a missing stop from
        intent, so a tightened stop must be reflected here or a repair would
        quietly restore the wider original."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE trades SET intent_stop_loss = ?, updated_at = ? WHERE id = ?",
                (str(stop), _now(), trade_id),
            )

    def find_by_fingerprint(self, fingerprint: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM messages WHERE signal_fingerprint = ? ORDER BY id",
                (fingerprint,),
            ).fetchall()
        )

    # -- trades ------------------------------------------------------------

    def create_trade(
        self,
        *,
        origin_message_row_id: int,
        origin_channel_id: int,
        origin_message_id: int,
        asset: str,
        symbol: str,
        direction: str,
        intent_entry_prices: list[Decimal] | None = None,
        intent_take_profits: list[Decimal] | None = None,
        intent_stop_loss: Decimal | None = None,
        intent_margin_usd: Decimal | None = None,
        intent_leverage: Decimal | None = None,
        intent_quantity: Decimal | None = None,
        is_substitute_instrument: bool = False,
        substitute_note: str | None = None,
        max_active_per_symbol: int = 1,
    ) -> tuple[int, str]:
        """Create a trade in RECEIVED state. Returns (row_id, trade_uid).

        Raises SymbolAlreadyActive when the symbol already holds
        `max_active_per_symbol` non-terminal trades, and OppositeDirectionActive
        when any of them goes the other way. Both checks and the insert share
        one IMMEDIATE transaction, so they hold against a concurrent writer too.
        """
        trade_uid = f"T-{uuid.uuid4().hex[:12]}"
        now = _now()
        try:
            with self.transaction() as conn:
                active = conn.execute(
                    """SELECT id, direction FROM trades WHERE symbol = ?
                       AND state NOT IN ('CLOSED','CANCELLED','FAILED')""",
                    (symbol,),
                ).fetchall()
                if any(r["direction"] != direction for r in active):
                    raise OppositeDirectionActive(
                        f"{symbol} already has an active trade going the other "
                        f"way (new {direction})"
                    )
                if len(active) >= max_active_per_symbol:
                    raise SymbolAlreadyActive(
                        f"{symbol} already has {len(active)} active trade(s); "
                        f"the limit is {max_active_per_symbol}"
                    )
                cur = conn.execute(
                    """INSERT INTO trades
                       (trade_uid, origin_message_row_id, origin_channel_id,
                        origin_message_id, asset, symbol, direction,
                        is_substitute_instrument, substitute_note,
                        intent_entry_prices_json, intent_take_profits_json,
                        intent_stop_loss, intent_margin_usd, intent_leverage,
                        intent_quantity, state, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        trade_uid,
                        origin_message_row_id,
                        origin_channel_id,
                        origin_message_id,
                        asset,
                        symbol,
                        direction,
                        1 if is_substitute_instrument else 0,
                        substitute_note,
                        json.dumps([str(p) for p in (intent_entry_prices or [])]),
                        json.dumps([str(p) for p in (intent_take_profits or [])]),
                        _txt(intent_stop_loss),
                        _txt(intent_margin_usd),
                        _txt(intent_leverage),
                        _txt(intent_quantity),
                        TradeState.RECEIVED.value,
                        now,
                        now,
                    ),
                )
                return int(cur.lastrowid), trade_uid
        except sqlite3.IntegrityError:
            raise

    def get_trade(self, trade_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()

    def active_trades_for_symbol(self, symbol: str) -> list[sqlite3.Row]:
        """Every non-terminal trade on `symbol`, oldest first. More than one
        means they SHARE the netted exchange position."""
        return list(self._conn.execute(
            """SELECT * FROM trades WHERE symbol = ?
               AND state NOT IN ('CLOSED','CANCELLED','FAILED') ORDER BY id""",
            (symbol,),
        ).fetchall())

    def total_open_margin(self) -> Decimal:
        """Margin committed across every non-terminal trade.

        Counted from the trade's own intent_margin_usd, and including trades
        still waiting for entry: their resting orders reserve margin as soon as
        they fill, so a limit that ignored them could be breached by entries
        that have not filled yet.
        """
        total = Decimal(0)
        for row in self._conn.execute(
            """SELECT intent_margin_usd FROM trades
               WHERE state NOT IN ('CLOSED','CANCELLED','FAILED')"""
        ).fetchall():
            if row["intent_margin_usd"] is None:
                raise ValueError("a live trade carries no recorded margin")
            total += Decimal(row["intent_margin_usd"])
        return total

    def open_trades(self) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """SELECT * FROM trades
                   WHERE state NOT IN ('CLOSED','CANCELLED','FAILED')
                   ORDER BY id"""
            ).fetchall()
        )

    def trades_in_states(self, states: Iterable[TradeState]) -> list[sqlite3.Row]:
        """Trades currently in any of `states`, with a `state_since` column.

        `state_since` is the timestamp of the most recent TRADE_STATE_CHANGED
        event, i.e. when the trade entered the state it is in now. It is NOT
        `updated_at`, which update_trade_actuals also bumps and would make a
        long-quarantined trade look freshly changed.

        Falls back to `updated_at` for rows that predate transition auditing,
        so callers get an approximate age rather than nothing. Those callers
        cannot tell the difference, so anything user-facing should describe
        the age as approximate.
        """
        values = [s.value for s in states]
        if not values:
            return []
        placeholders = ",".join("?" * len(values))
        return list(
            self._conn.execute(
                f"""SELECT t.*,
                           COALESCE(
                               (SELECT MAX(e.occurred_at) FROM events e
                                 WHERE e.trade_id = t.id
                                   AND e.event_type = 'TRADE_STATE_CHANGED'),
                               t.updated_at
                           ) AS state_since
                      FROM trades t
                     WHERE t.state IN ({placeholders})
                     ORDER BY t.id""",
                values,
            ).fetchall()
        )

    def realized_pnl_since(self, since_iso: str) -> tuple[Decimal, int, int]:
        """(total realised P&L, trades closed, trades with NO recorded P&L).

        Counts only trades that actually reached CLOSED — a CANCELLED trade
        never had a position, so including it would report a P&L for
        something that never traded.

        The third value matters: SUM() over a NULL realized_pnl contributes
        nothing, so a stopped-out trade whose P&L was never computed used to
        be reported as $0.00 — indistinguishable from a genuine breakeven.
        That happened for real (trade 3, a -$5.86 stop-out, summarised as
        "+$0.00 across 1 closed trade"). Callers must surface the unknown
        count rather than presenting the total as complete.
        """
        row = self._conn.execute(
            """SELECT COALESCE(SUM(CAST(realized_pnl AS REAL)), 0) AS total,
                      COUNT(*) AS n,
                      SUM(CASE WHEN realized_pnl IS NULL THEN 1 ELSE 0 END) AS unknown
                 FROM trades
                WHERE state = 'CLOSED' AND closed_at IS NOT NULL
                  AND closed_at >= ?""",
            (since_iso,),
        ).fetchone()
        return (Decimal(str(row["total"] or 0)), int(row["n"] or 0),
                int(row["unknown"] or 0))

    def last_event_at(self, event_type: EventType) -> str | None:
        """When an event of this type last occurred, or None if never.

        Used for cadences that must survive a restart — `time.monotonic()`
        resets to zero on every process, so a bot restarted more often than
        its own interval would never fire.
        """
        row = self._conn.execute(
            "SELECT MAX(occurred_at) AS at FROM events WHERE event_type = ?",
            (event_type.value,),
        ).fetchone()
        return row["at"] if row else None

    def transition_trade(
        self, trade_id: int, target: TradeState, detail: str | None = None
    ) -> None:
        """Move a trade to a new state, refusing illegal transitions.

        The check and the write happen in one transaction, so a concurrent
        transition cannot slip between them.
        """
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT state FROM trades WHERE id = ?", (trade_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no trade with id {trade_id}")
            current = TradeState(row["state"])
            assert_transition(current, target)
            at = _now()
            conn.execute(
                """UPDATE trades SET state = ?, state_detail = ?, updated_at = ?,
                          closed_at = CASE WHEN ? = 'CLOSED' THEN ? ELSE closed_at END
                   WHERE id = ?""",
                (target.value, detail, at, target.value, at, trade_id),
            )
            # Audit the transition in the same transaction, so the trail can
            # never disagree with the state it describes. This is also the
            # ONLY reliable record of when a trade entered its current state:
            # `updated_at` is bumped by update_trade_actuals too, so it means
            # "last touched", not "state last changed". Inlined rather than
            # calling append_event because transaction() uses BEGIN IMMEDIATE
            # and is not reentrant.
            conn.execute(
                """INSERT INTO events
                   (occurred_at, event_type, trade_id, severity, summary, detail_json)
                   VALUES (?,?,?,?,?,?)""",
                (
                    at,
                    EventType.TRADE_STATE_CHANGED.value,
                    trade_id,
                    "INFO",
                    f"{current.value} -> {target.value}",
                    json.dumps(
                        {"from": current.value, "to": target.value, "detail": detail}
                    ),
                ),
            )

    def update_trade_actuals(
        self,
        trade_id: int,
        *,
        actual_quantity: Decimal | None = None,
        actual_avg_entry_price: Decimal | None = None,
        actual_effective_leverage: Decimal | None = None,
        realized_pnl: Decimal | None = None,
    ) -> None:
        """Write exchange-sourced reality. Never called with signal-derived
        values — that is what the intent_* columns are for (brief 39)."""
        sets, params = [], []
        for col, val in (
            ("actual_quantity", actual_quantity),
            ("actual_avg_entry_price", actual_avg_entry_price),
            ("actual_effective_leverage", actual_effective_leverage),
            ("realized_pnl", realized_pnl),
        ):
            if val is not None:
                sets.append(f"{col} = ?")
                params.append(str(val))
        if not sets:
            return
        sets.append("updated_at = ?")
        params.extend([_now(), trade_id])
        with self.transaction() as conn:
            conn.execute(f"UPDATE trades SET {', '.join(sets)} WHERE id = ?", params)

    def set_intent_leverage(self, trade_id: int, leverage: Decimal) -> None:
        """Record the leverage the trade is opened at (the signal's request
        after the cap and the stop-vs-liquidation guard). Recovery and
        reconciliation treat it as the symbol's expected leverage."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE trades SET intent_leverage = ?, updated_at = ? WHERE id = ?",
                (str(leverage), _now(), trade_id),
            )

    def set_active_stop(
        self,
        trade_id: int,
        *,
        order_uid: str | None,
        trigger_price: Decimal | None,
        breakeven: bool = False,
        breakeven_price: Decimal | None = None,
    ) -> None:
        """Record which order is the authoritative protective stop (brief 16)."""
        with self.transaction() as conn:
            conn.execute(
                """UPDATE trades SET active_stop_order_uid = ?, stop_trigger_price = ?,
                          breakeven_active = ?, breakeven_price = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    order_uid,
                    _txt(trigger_price),
                    1 if breakeven else 0,
                    _txt(breakeven_price),
                    _now(),
                    trade_id,
                ),
            )

    # -- orders ------------------------------------------------------------

    def record_order_intent(
        self,
        *,
        trade_id: int,
        client_order_id: str,
        purpose: OrderPurpose,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None = None,
        trigger_price: Decimal | None = None,
        trigger_signal: str | None = None,
        reduce_only: bool = False,
        sequence: int = 0,
        expires_at: str | None = None,
    ) -> tuple[int, str]:
        """Persist intent BEFORE submitting (brief 30).

        If the process dies between this row and the exchange call, recovery
        finds an INTENT order and reconciles against the exchange using
        client_order_id rather than blindly resubmitting.
        """
        order_uid = f"O-{uuid.uuid4().hex[:12]}"
        now = _now()
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO orders
                   (order_uid, client_order_id, trade_id, purpose, sequence, symbol,
                    side, order_type, reduce_only, price, trigger_price,
                    trigger_signal, quantity, status, expires_at, created_at,
                    updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    order_uid,
                    client_order_id,
                    trade_id,
                    purpose.value,
                    sequence,
                    symbol,
                    side,
                    order_type,
                    1 if reduce_only else 0,
                    _txt(price),
                    _txt(trigger_price),
                    trigger_signal,
                    str(quantity),
                    OrderStatus.INTENT.value,
                    expires_at,
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid), order_uid

    def update_order(
        self,
        order_id: int,
        *,
        status: OrderStatus | None = None,
        exchange_order_id: str | None = None,
        filled_quantity: Decimal | None = None,
        avg_fill_price: Decimal | None = None,
        status_detail: str | None = None,
        submitted: bool = False,
    ) -> None:
        sets, params = [], []
        if status is not None:
            sets.append("status = ?")
            params.append(status.value)
        if exchange_order_id is not None:
            sets.append("exchange_order_id = ?")
            params.append(exchange_order_id)
        if filled_quantity is not None:
            sets.append("filled_quantity = ?")
            params.append(str(filled_quantity))
        if avg_fill_price is not None:
            sets.append("avg_fill_price = ?")
            params.append(str(avg_fill_price))
        if status_detail is not None:
            sets.append("status_detail = ?")
            params.append(status_detail)
        if submitted:
            sets.append("submitted_at = ?")
            params.append(_now())
        if not sets:
            return
        sets.append("updated_at = ?")
        params.extend([_now(), order_id])
        with self.transaction() as conn:
            conn.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id = ?", params)

    def get_order(self, order_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()

    def get_order_by_client_id(self, client_order_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()

    def orders_for_trade(
        self, trade_id: int, purpose: OrderPurpose | None = None
    ) -> list[sqlite3.Row]:
        if purpose is None:
            rows = self._conn.execute(
                "SELECT * FROM orders WHERE trade_id = ? ORDER BY sequence, id",
                (trade_id,),
            )
        else:
            rows = self._conn.execute(
                """SELECT * FROM orders WHERE trade_id = ? AND purpose = ?
                   ORDER BY sequence, id""",
                (trade_id, purpose.value),
            )
        return list(rows.fetchall())

    def live_orders(self) -> list[sqlite3.Row]:
        """Orders that may exist on the exchange — including INTENT, which is
        precisely the crash window recovery must investigate."""
        return list(
            self._conn.execute(
                """SELECT * FROM orders
                   WHERE status IN ('INTENT','SUBMITTED','OPEN','PARTIALLY_FILLED')
                   ORDER BY id"""
            ).fetchall()
        )

    # -- events ------------------------------------------------------------

    def append_event(
        self,
        event_type: EventType,
        summary: str,
        *,
        trade_id: int | None = None,
        order_id: int | None = None,
        message_row_id: int | None = None,
        severity: str = "INFO",
        detail: dict[str, Any] | None = None,
        occurred_at: str | None = None,
    ) -> int:
        """Append one audit row.

        `occurred_at` defaults to now. Callers that work from an injected
        clock MUST pass it: an event used as a cadence marker and stamped
        with a different clock than the one comparing against it will
        misfire (that is a real bug this had — see
        alerting.send_unresolved_digest).
        """
        with self.transaction() as conn:
            cur = conn.execute(
                """INSERT INTO events
                   (occurred_at, event_type, trade_id, order_id, message_row_id,
                    severity, summary, detail_json)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    occurred_at or _now(),
                    event_type.value,
                    trade_id,
                    order_id,
                    message_row_id,
                    severity,
                    summary,
                    json.dumps(detail, default=str) if detail else None,
                ),
            )
            return int(cur.lastrowid)

    def events_for_trade(self, trade_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM events WHERE trade_id = ? ORDER BY id", (trade_id,)
            ).fetchall()
        )
