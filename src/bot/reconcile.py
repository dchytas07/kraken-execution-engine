"""Reconciliation: local intent vs exchange reality.

Division of authority (brief 28):

    the exchange  is authoritative for FILLS and POSITIONS
    the database  is authoritative for INTENT and ASSOCIATION

Reconciliation bridges the two. It never blindly overwrites: each discrepancy
has an explicit, conservative repair, and anything it cannot resolve safely is
escalated rather than guessed at (brief 40).

## Consistent snapshot

Stage 6's `PollingFillObserver` reads orders and positions in separate calls,
so a fill landing between them yields a stale label (docs/KRAKEN_NOTES.md 6.3).
`ExchangeSnapshot` captures positions, orders, fills and leverage in one pass,
and `SnapshotObserver` serves the Stage 6 `FillObserver` contract from that
frozen capture — so every read within one reconciliation cycle sees the same
instant. The engine is unchanged; it depends on the interface, not the source.

## WebSocket fast path

`futures ws fills` streams private fills. It is used ONLY to wake the reconciler
early — it never mutates state directly. A bug or a dropped stream therefore
cannot corrupt anything: the worst case is that the periodic loop notices a
moment later. Latency optimisation, never a source of truth.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from .config import Config
from .db.store import Store
from .exchange.base import ExchangeClient, ExchangeError, Fill, Order, Position
from .execution.engine import ExecutionEngine
from .execution.observer import ObservedOrder
from .logging_setup import get_logger
from .models import (
    EventType,
    InstrumentSpec,
    OrderPurpose,
    OrderStatus,
    TradeState,
)

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Consistent snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExchangeSnapshot:
    """One coherent capture of exchange state."""

    taken_at: str
    positions: dict[str, Position]
    orders: tuple[Order, ...]
    fills: tuple[Fill, ...]
    leverage: dict[str, Decimal]

    @classmethod
    def capture(
        cls, client: ExchangeClient, symbols: list[str]
    ) -> "ExchangeSnapshot":
        positions = {p.symbol: p for p in client.get_open_positions()}
        orders = tuple(client.get_open_orders())
        fills = tuple(client.get_fills())
        leverage: dict[str, Decimal] = {}
        for symbol in symbols:
            try:
                value = client.get_leverage_preference(symbol)
            except ExchangeError:
                value = None
            if value is not None:
                leverage[symbol] = value
        return cls(
            taken_at=datetime.now(UTC).isoformat(),
            positions=positions,
            orders=orders,
            fills=fills,
            leverage=leverage,
        )

    def order_by_client_id(self, client_order_id: str) -> Order | None:
        return next(
            (o for o in self.orders if o.client_order_id == client_order_id), None
        )

    def fills_for(self, client_order_id: str) -> list[Fill]:
        return [f for f in self.fills if f.client_order_id == client_order_id]


class SnapshotObserver:
    """FillObserver served from a frozen snapshot.

    Implements the same three methods Stage 6 built against, so the engine
    works unchanged — but every read inside one cycle is consistent, which is
    what removes the entry-observation race.
    """

    def __init__(self, snapshot: ExchangeSnapshot) -> None:
        self.snapshot = snapshot

    def observe_order(
        self, client_order_id: str, *, expected_size: Decimal | None = None
    ) -> ObservedOrder:
        fills = self.snapshot.fills_for(client_order_id)
        resting = self.snapshot.order_by_client_id(client_order_id)

        filled = sum((f.size for f in fills), Decimal(0))
        avg = (
            sum((f.price * f.size for f in fills), Decimal(0)) / filled
            if filled > 0
            else None
        )
        target = expected_size if expected_size is not None else (
            resting.size if resting else None
        )

        if resting is not None:
            status = OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.OPEN
        elif filled > 0:
            status = (
                OrderStatus.FILLED
                if target is not None and filled >= target
                else OrderStatus.PARTIALLY_FILLED
            )
        else:
            status = OrderStatus.UNKNOWN

        return ObservedOrder(
            client_order_id=client_order_id,
            exchange_order_id=(
                resting.exchange_order_id
                if resting
                else next((f.order_id for f in fills if f.order_id), None)
            ),
            status=status,
            filled_size=filled,
            avg_fill_price=avg,
            resting=resting is not None,
            fills=tuple(fills),
        )

    def observe_position(self, symbol: str) -> Position | None:
        position = self.snapshot.positions.get(symbol)
        return position if position and not position.is_flat else None

    def await_flat(self, symbol: str, *, timeout_s=None, poll_s=None) -> bool:
        """A snapshot cannot wait — it answers about one instant.

        Returns whether the symbol was flat AT CAPTURE. Callers needing to
        block should use the polling observer; reconciliation only ever asks
        about the snapshot it holds.
        """
        return self.observe_position(symbol) is None

    def weighted_average_entry(
        self, client_order_ids: list[str]
    ) -> tuple[Decimal, Decimal]:
        wanted = set(client_order_ids)
        fills = [f for f in self.snapshot.fills if f.client_order_id in wanted]
        total = sum((f.size for f in fills), Decimal(0))
        if total == 0:
            return Decimal(0), Decimal(0)
        return total, sum((f.price * f.size for f in fills), Decimal(0)) / total


# ---------------------------------------------------------------------------
# Discrepancies
# ---------------------------------------------------------------------------


class DiscrepancyKind(StrEnum):
    UNPROTECTED_POSITION = "UNPROTECTED_POSITION"
    STOP_SIZE_MISMATCH = "STOP_SIZE_MISMATCH"
    ORPHAN_POSITION = "ORPHAN_POSITION"
    POSITION_GONE = "POSITION_GONE"
    ORDER_MISSING_ON_EXCHANGE = "ORDER_MISSING_ON_EXCHANGE"
    UNTRACKED_ORDER = "UNTRACKED_ORDER"
    FILL_NOT_RECORDED = "FILL_NOT_RECORDED"
    STALE_ENTRY_ORDER = "STALE_ENTRY_ORDER"
    LEVERAGE_DRIFT = "LEVERAGE_DRIFT"
    TP_EXCEEDS_POSITION = "TP_EXCEEDS_POSITION"


@dataclass(frozen=True, slots=True)
class Discrepancy:
    kind: DiscrepancyKind
    detail: str
    trade_id: int | None = None
    symbol: str | None = None
    repaired: bool = False
    repair_detail: str = ""


@dataclass(slots=True)
class ReconcileReport:
    snapshot_at: str
    discrepancies: list[Discrepancy] = field(default_factory=list)

    @property
    def repaired(self) -> list[Discrepancy]:
        return [d for d in self.discrepancies if d.repaired]

    @property
    def unrepaired(self) -> list[Discrepancy]:
        return [d for d in self.discrepancies if not d.repaired]

    @property
    def clean(self) -> bool:
        return not self.discrepancies


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------


class Reconciler:
    def __init__(
        self,
        *,
        config: Config,
        store: Store,
        client: ExchangeClient,
        engine: ExecutionEngine,
        specs: dict,
    ) -> None:
        self.config = config
        self.store = store
        self.client = client
        self.engine = engine
        self.specs = specs
        self._symbols = [s.symbol for s in specs.values()]
        self._spec_by_symbol = {s.symbol: s for s in specs.values()}

    def run_once(self) -> ReconcileReport:
        """One reconciliation pass over a single consistent snapshot."""
        snapshot = ExchangeSnapshot.capture(self.client, self._symbols)
        report = ReconcileReport(snapshot_at=snapshot.taken_at)

        observer = SnapshotObserver(snapshot)
        original_observer = self.engine.observer
        self.engine.observer = observer
        try:
            open_trades = self.store.open_trades()
            for trade in open_trades:
                self._reconcile_trade(trade, snapshot, report)
            self._check_orphan_positions(open_trades, snapshot, report)
            self._check_leverage_drift(snapshot, report)
        finally:
            self.engine.observer = original_observer

        self._log_report(report)
        return report

    # -- per-trade -------------------------------------------------------

    def _reconcile_trade(self, trade, snapshot, report) -> None:
        trade_id = trade["id"]
        symbol = trade["symbol"]
        state = TradeState(trade["state"])
        position = snapshot.positions.get(symbol)
        if position is not None and position.is_flat:
            position = None

        # 1. Fills the local DB has not caught up with.
        self._sync_order_states(trade_id, snapshot, report)

        # 2. Exchange says flat but we think the trade is live.
        #    CLOSING is deliberately INCLUDED: a trade left mid-close by a crash
        #    must be finalised, and in-process a flat position means the close
        #    already succeeded. _repair_position_gone guards the transition.
        if position is None and state.is_open:
            report.discrepancies.append(
                self._repair_position_gone(trade_id, symbol, state)
            )
            return

        if position is None:
            return

        # 3. Advance the entry lifecycle label to match reality.
        #    After a restart a trade can be filled and protected while still
        #    labelled WAITING_FOR_ENTRY. The stop is correct either way (it is
        #    sized from the position), but a stale label misleads an operator
        #    and gates later TP transitions, so it is corrected here.
        #    sync_entry_fills is idempotent and reads the installed snapshot
        #    observer, so this stays within the consistent capture.
        if state in (TradeState.WAITING_FOR_ENTRY, TradeState.PARTIALLY_ENTERED):
            self.engine.sync_entry_fills(trade_id)
            trade = self.store.get_trade(trade_id)
            state = TradeState(trade["state"])

        # 4. Local quantity vs actual.
        local_qty = Decimal(trade["actual_quantity"])
        if local_qty != position.size:
            self.store.update_trade_actuals(trade_id, actual_quantity=position.size)
            report.discrepancies.append(
                Discrepancy(
                    DiscrepancyKind.FILL_NOT_RECORDED,
                    f"local {local_qty} != exchange {position.size}",
                    trade_id, symbol, repaired=True,
                    repair_detail="local quantity updated from the exchange",
                )
            )

        # 5. Protection. An open position with no stop is the single most
        #    dangerous state this system can be in.
        stops = [
            o for o in snapshot.orders
            if o.order_type == "stop" and o.symbol == symbol
        ]
        if not stops:
            report.discrepancies.append(
                self._repair_unprotected(trade, position)
            )
        elif len(stops) == 1 and stops[0].size != position.size:
            ok = self.engine.resize_stop_to_position(
                trade_id=trade_id, position=position
            )
            report.discrepancies.append(
                Discrepancy(
                    DiscrepancyKind.STOP_SIZE_MISMATCH,
                    f"stop {stops[0].size} != position {position.size}",
                    trade_id, symbol, repaired=ok,
                    repair_detail="stop resized" if ok else "resize FAILED",
                )
            )

        # 6. TP total must never exceed the position (brief 13).
        tps = [
            o for o in snapshot.orders
            if o.order_type == "take-profit" and o.symbol == symbol
        ]
        tp_total = sum((o.size for o in tps), Decimal(0))
        if tp_total > position.size:
            self.engine._enter_safe_error(
                trade_id,
                f"TP orders total {tp_total} exceed the position {position.size} — "
                "a fill could open a reverse position",
                symbol=symbol,
            )
            report.discrepancies.append(
                Discrepancy(
                    DiscrepancyKind.TP_EXCEEDS_POSITION,
                    f"{tp_total} > {position.size}",
                    trade_id, symbol,
                )
            )

        # 7. Stale entry orders (brief 12).
        expired = self.engine.expire_stale_entries(
            trade_id, datetime.now(UTC).isoformat()
        )
        if expired:
            report.discrepancies.append(
                Discrepancy(
                    DiscrepancyKind.STALE_ENTRY_ORDER,
                    f"{expired} entry order(s) past their {self.config.entry_order_expiry_hours}h lifetime",
                    trade_id, symbol, repaired=True,
                    repair_detail="cancelled",
                )
            )

    def _sync_order_states(self, trade_id, snapshot, report) -> None:
        """Bring local order rows in line with the snapshot."""
        observer = SnapshotObserver(snapshot)
        for row in self.store.orders_for_trade(trade_id):
            if OrderStatus(row["status"]).is_final:
                continue
            observed = observer.observe_order(
                row["client_order_id"], expected_size=Decimal(row["quantity"])
            )
            if observed.status.value != row["status"] or (
                observed.filled_size != Decimal(row["filled_quantity"])
            ):
                self.store.update_order(
                    row["id"],
                    status=observed.status,
                    exchange_order_id=observed.exchange_order_id,
                    filled_quantity=observed.filled_size,
                    avg_fill_price=observed.avg_fill_price,
                )
                if observed.has_any_fill:
                    report.discrepancies.append(
                        Discrepancy(
                            DiscrepancyKind.FILL_NOT_RECORDED,
                            f"{row['purpose']} order {row['client_order_id']} "
                            f"filled {observed.filled_size}, was {row['status']}",
                            trade_id, row["symbol"], repaired=True,
                            repair_detail="order state updated from fills",
                        )
                    )
                elif observed.is_ambiguous and row["exchange_order_id"]:
                    # Not resting, never filled: cancelled, rejected, or never
                    # placed. Indistinguishable — record, never guess.
                    report.discrepancies.append(
                        Discrepancy(
                            DiscrepancyKind.ORDER_MISSING_ON_EXCHANGE,
                            f"{row['purpose']} order {row['client_order_id']} is "
                            "neither resting nor filled",
                            trade_id, row["symbol"],
                        )
                    )

    def _repair_unprotected(self, trade, position: Position) -> Discrepancy:
        """An open position with no protective stop.

        Repaired by placing the stop from stored INTENT — the database is
        authoritative for intent, which is exactly what is needed here. If the
        intent is missing we cannot invent a level, so it escalates.
        """
        trade_id = trade["id"]
        symbol = trade["symbol"]
        intent_sl = trade["intent_stop_loss"]
        spec: InstrumentSpec | None = self._spec_by_symbol.get(symbol)

        if intent_sl is None or spec is None:
            self.engine._enter_safe_error(
                trade_id,
                f"{symbol} holds {position.size} with NO protective stop and no "
                "recoverable stop-loss intent — cannot place protection safely",
                symbol=symbol,
            )
            return Discrepancy(
                DiscrepancyKind.UNPROTECTED_POSITION,
                "no stop and no intent to rebuild one from",
                trade_id, symbol,
            )

        from .models import Direction

        try:
            self.engine.place_stop(
                trade_id=trade_id,
                trade_uid=trade["trade_uid"],
                spec=spec,
                direction=Direction(trade["direction"]),
                trigger_price=Decimal(intent_sl),
                position=position,
            )
        except Exception as exc:
            self.engine._enter_safe_error(
                trade_id,
                f"{symbol} is UNPROTECTED and the recovery stop failed: {exc}",
                symbol=symbol,
            )
            return Discrepancy(
                DiscrepancyKind.UNPROTECTED_POSITION,
                f"stop placement failed: {exc}", trade_id, symbol,
            )

        log.warning(
            f"{symbol} was unprotected; protective stop restored from intent "
            f"at {intent_sl}",
            extra={"trade_id": trade_id, "symbol": symbol,
                   "action": "protection_restored"},
        )
        return Discrepancy(
            DiscrepancyKind.UNPROTECTED_POSITION,
            f"{position.size} held with no stop",
            trade_id, symbol, repaired=True,
            repair_detail=f"stop restored from intent at {intent_sl}",
        )

    def _repair_position_gone(self, trade_id, symbol, state) -> Discrepancy:
        """Exchange is flat but the trade is still open locally.

        The exchange is authoritative for positions, so this is real — the
        position closed without us driving it (stop hit, TP4, liquidation, or a
        manual close). Cancel leftovers and close the trade through the same
        confirmation path a bot-driven close uses, so CLOSED still means
        'flat AND no orders remain'.
        """
        cancelled = 0
        for row in self.store.orders_for_trade(trade_id):
            if OrderStatus(row["status"]).is_final or not row["exchange_order_id"]:
                continue
            if self.client.cancel_order(row["exchange_order_id"]):
                self.store.update_order(row["id"], status=OrderStatus.CANCELLED)
                cancelled += 1

        if state is not TradeState.CLOSING:
            self.store.transition_trade(
                trade_id, TradeState.CLOSING, "exchange reports flat"
            )
        self.store.append_event(
            EventType.POSITION_CONFIRMED_FLAT,
            f"{symbol} flat on the exchange; closed outside the bot",
            trade_id=trade_id,
        )
        self.store.transition_trade(trade_id, TradeState.CLOSED)
        self.store.append_event(
            EventType.TRADE_CLOSED,
            f"closed by reconciliation; {cancelled} leftover order(s) cancelled",
            trade_id=trade_id,
        )
        log.warning(
            f"trade {trade_id} closed by reconciliation: {symbol} flat on the "
            f"exchange, {cancelled} leftover order(s) cancelled",
            extra={"trade_id": trade_id, "symbol": symbol,
                   "action": "reconcile_closed"},
        )
        return Discrepancy(
            DiscrepancyKind.POSITION_GONE,
            f"{symbol} flat on the exchange while the trade was {state.value}",
            trade_id, symbol, repaired=True,
            repair_detail=f"trade closed, {cancelled} order(s) cancelled",
        )

    # -- account-level ---------------------------------------------------

    def _check_orphan_positions(self, open_trades, snapshot, report) -> None:
        """A position the bot has no trade for.

        Never auto-closed. Closing a position we cannot explain could unwind
        something an operator opened by hand; brief 40 says reconcile, do not
        guess. Logged at ERROR so it cannot pass unnoticed.
        """
        known = {t["symbol"] for t in open_trades}
        for symbol, position in snapshot.positions.items():
            if position.is_flat or symbol in known:
                continue
            log.error(
                f"ORPHAN POSITION: {symbol} holds {position.size} on the exchange "
                "but no local trade matches it — not closing automatically",
                extra={"symbol": symbol, "action": "orphan_position"},
            )
            self.store.append_event(
                EventType.RECONCILE_MISMATCH,
                f"orphan position {symbol} {position.size}",
                severity="ERROR",
            )
            report.discrepancies.append(
                Discrepancy(
                    DiscrepancyKind.ORPHAN_POSITION,
                    f"{symbol} {position.size} with no local trade",
                    symbol=symbol,
                )
            )

    def _check_leverage_drift(self, snapshot, report) -> None:
        """Study B: a set-leverage that succeeded for a trade which never
        happened silently governs the NEXT position on that symbol.

        Safe to re-assert because Study B(a) showed set-leverage does not
        re-margin an open position.
        """
        for symbol in self._symbols:
            actual = snapshot.leverage.get(symbol)
            if actual is None or actual == self.config.max_leverage:
                continue
            try:
                # Through the engine, never direct: leverage mutation is
                # funnelled so the cap lives in one place (docs/PLAN.md 10.1).
                self.engine.reassert_leverage(symbol)
                repaired, detail = True, f"re-asserted {self.config.max_leverage}x"
            except ExchangeError as exc:
                repaired, detail = False, f"re-assert failed: {exc}"
            log.warning(
                f"leverage drift on {symbol}: account has {actual}x, intent is "
                f"{self.config.max_leverage}x — {detail}",
                extra={"symbol": symbol, "action": "leverage_drift"},
            )
            report.discrepancies.append(
                Discrepancy(
                    DiscrepancyKind.LEVERAGE_DRIFT,
                    f"{symbol} at {actual}x, expected {self.config.max_leverage}x",
                    symbol=symbol, repaired=repaired, repair_detail=detail,
                )
            )

    def _log_report(self, report: ReconcileReport) -> None:
        if report.clean:
            log.debug("reconciliation clean", extra={"action": "reconcile_clean"})
            return
        for d in report.discrepancies:
            emit = log.info if d.repaired else log.error
            emit(
                f"reconcile [{d.kind.value}] {d.detail}"
                + (f" -> {d.repair_detail}" if d.repair_detail else ""),
                extra={"action": "reconcile_discrepancy", "trade_id": d.trade_id,
                       "symbol": d.symbol, "kind": d.kind.value},
            )
            self.store.append_event(
                EventType.RECONCILE_REPAIRED if d.repaired
                else EventType.RECONCILE_MISMATCH,
                f"{d.kind.value}: {d.detail}",
                trade_id=d.trade_id,
                severity="INFO" if d.repaired else "ERROR",
            )


# ---------------------------------------------------------------------------
# WebSocket fast path
# ---------------------------------------------------------------------------


class FillStream:
    """Private fill stream, used ONLY to wake the reconciler early.

    Deliberately does not mutate state. A dropped or malformed stream therefore
    degrades latency, never correctness — the periodic loop remains the
    authoritative safety net (brief 28).

    Not verifiable without live credentials: `futures ws` is auth-only and has
    no paper equivalent. The parsing is covered by tests against injected
    lines, and the stream itself is listed as a live-verification item.
    """

    def __init__(
        self,
        *,
        binary: str = "kraken",
        on_event=None,
        spawn=subprocess.Popen,
    ) -> None:
        self.binary = binary
        self.on_event = on_event or (lambda payload: None)
        self._spawn = spawn
        self._proc = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.events_seen = 0

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._proc = self._spawn(
                    [self.binary, "futures", "ws", "fills", "-o", "json"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                for line in self._proc.stdout:
                    if self._stop.is_set():
                        break
                    self.handle_line(line)
                    backoff = 1.0
            except Exception as exc:
                log.warning(
                    f"fill stream error: {exc}; reconciliation loop still covers "
                    "this — reconnecting",
                    extra={"action": "fill_stream_error"},
                )
            if self._stop.is_set():
                break
            time.sleep(min(backoff, 30.0))
            backoff *= 2

    def handle_line(self, line: str) -> bool:
        """Parse one stream line. Returns True if it was a usable event.

        Malformed lines are dropped rather than raised: the stream is an
        optimisation, and a parse failure must not take down the loop.
        """
        line = line.strip()
        if not line:
            return False
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            log.debug("unparseable fill stream line", extra={"action": "ws_parse"})
            return False
        if not isinstance(payload, dict):
            return False
        if "error" in payload:
            return False
        self.events_seen += 1
        self.on_event(payload)
        return True


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------


class ReconciliationLoop:
    """Periodic reconciliation, optionally woken early by the fill stream."""

    def __init__(
        self,
        *,
        reconciler: Reconciler,
        interval_seconds: int,
        stream: FillStream | None = None,
    ) -> None:
        self.reconciler = reconciler
        self.interval_seconds = interval_seconds
        self.stream = stream
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.cycles = 0

        if stream is not None:
            stream.on_event = self._on_fill

    def _on_fill(self, payload: dict) -> None:
        """Fast path: a fill arrived, reconcile now instead of waiting."""
        self._wake.set()

    def run_forever(self) -> None:
        if self.stream is not None:
            self.stream.start()
        try:
            while not self._stop.is_set():
                self.run_cycle()
                # Wake early on a fill, otherwise honour the interval.
                self._wake.wait(timeout=self.interval_seconds)
                self._wake.clear()
        finally:
            if self.stream is not None:
                self.stream.stop()

    def run_cycle(self) -> ReconcileReport:
        self.cycles += 1
        try:
            return self.reconciler.run_once()
        except Exception:
            # The loop must survive anything: it is the safety net.
            log.error(
                "reconciliation cycle failed; will retry next interval",
                exc_info=True,
                extra={"action": "reconcile_cycle_failed"},
            )
            return ReconcileReport(snapshot_at=datetime.now(UTC).isoformat())

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
