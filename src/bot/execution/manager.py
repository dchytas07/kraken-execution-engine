"""Forward lifecycle driver.

Reconciliation is a safety net. This is the thing it is a net *for*.

Before this existed the pipeline opened trades and stopped: the TP ladder,
breakeven and closes had no production caller, and a position was protected
only when the 60-second reconciliation loop noticed it was unprotected. That
meant **up to a full cycle of real exposure after every fill**.

## The same-cycle protection guarantee

`step()` observes fills and, in the *same call*, places the protective stop
before returning. There is no path where a fill is observed and the stop waits
for a later cycle:

    sync_entry_fills()      -> observe via the FillObserver
    if position and no stop -> place_stop() immediately
    ...only then continue to TPs, breakeven, closes

`SignalPipeline` calls `step()` directly after `open_trade`, so an entry that
fills immediately is protected within the same request. Subsequent fills are
picked up by the manage loop, which runs far faster than reconciliation
(`MANAGE_INTERVAL_SECONDS`, default 5s) and is also woken by the fill stream.

Reconciliation still checks protection — but as a net catching what this
missed, not as the mechanism that provides it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..config import Config
from ..db.store import Store
from ..exchange.base import ExchangeClient, Position
from ..logging_setup import get_logger
from ..models import Direction, EventType, OrderPurpose, OrderStatus, TradeState
from .engine import ExecutionEngine, ExecutionRefused

log = get_logger(__name__)

# A position must reach PROTECTED within this many seconds of its fill, by ANY
# code path. Not an aspiration — the soak measures actual fill->protect latency
# and fails the run if this is exceeded.
#
# Chosen from measurement, not by feel (tools/measure_tick.py):
#   raw CLI read                     median 0.01s, max 0.06s
#   full manage tick, 3 trades       median 17.3s, p95 26.7s, max 50.6s
#
# A full step costs O(open orders) CLI calls, so it degrades exactly as a book
# grows — worst case ~53s including the loop interval, barely better than the
# 60s reconciler. So protection cannot ride on the full step. protection_sweep()
# is O(1) in orders (two calls total) and runs on its own fast cadence, which is
# what makes 15s achievable with margin.
PROTECTION_SLA_SECONDS = 15


@dataclass(slots=True)
class StepResult:
    trade_id: int
    actions: list[str] = field(default_factory=list)
    protected_this_step: bool = False

    def note(self, action: str) -> None:
        self.actions.append(action)


class TradeManager:
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
        self._spec_by_symbol = {s.symbol: s for s in specs.values()}

    # ------------------------------------------------------------------

    def protection_sweep(self) -> list[str]:
        """Protect every unprotected position. Cheap, and safe to run often.

        Exactly TWO exchange reads regardless of how many trades or orders
        exist, because it asks only "what positions are open?" and "where are
        the stops?". The full step is O(open orders) and measured at 17-50s
        with three trades; this is the mechanism that actually meets
        PROTECTION_SLA_SECONDS.

        Deliberately does nothing else: no TPs, no breakeven, no closes. It is
        the one thing that must never queue behind slower work.
        """
        protected: list[str] = []
        try:
            positions = [p for p in self.client.get_open_positions() if not p.is_flat]
            if not positions:
                return protected
            orders = self.client.get_open_orders()
        except Exception:
            log.error("protection sweep could not read the exchange",
                      exc_info=True, extra={"action": "sweep_failed"})
            return protected

        stopped = {o.symbol for o in orders if o.order_type == "stop"}
        for position in positions:
            if position.symbol in stopped:
                continue
            trade = self.store.get_active_trade_for_symbol(position.symbol)
            if trade is None:
                continue  # orphan: never adopted, reported by reconciliation
            if TradeState(trade["state"]).is_terminal:
                continue
            spec = self._spec_by_symbol.get(position.symbol)
            if spec is None:
                continue
            if self.ensure_protected(
                trade["id"], spec, Direction(trade["direction"]), position
            ):
                protected.append(position.symbol)
        return protected

    def step_all(self) -> list[StepResult]:
        """Advance every open trade one step."""
        results = []
        for trade in self.store.open_trades():
            try:
                results.append(self.step(trade["id"]))
            except Exception:
                log.error(
                    f"trade {trade['id']} step failed; reconciliation remains the "
                    "safety net",
                    exc_info=True,
                    extra={"trade_id": trade["id"], "action": "step_failed"},
                )
        return results

    def step(self, trade_id: int) -> StepResult:
        """One forward step for one trade.

        Ordering is the safety property: protection is established before any
        other action, in this same call.
        """
        result = StepResult(trade_id=trade_id)
        trade = self.store.get_trade(trade_id)
        state = TradeState(trade["state"])
        if state.is_terminal:
            return result

        symbol = trade["symbol"]
        spec = self._spec_by_symbol.get(symbol)
        if spec is None:
            return result
        direction = Direction(trade["direction"])

        quarantined = state is TradeState.REQUIRES_REVIEW
        if quarantined:
            # Quarantine suspends STRATEGY, never PROTECTION. A trade whose
            # state we cannot resolve still holds real exposure, so it keeps
            # its stop; it just gets no new TPs, breakeven or auto-close.
            position = self.engine.observer.observe_position(symbol)
            if position and not position.is_flat:
                if self.ensure_protected(trade_id, spec, direction, position):
                    result.protected_this_step = True
                    result.note("quarantined trade protected")
            return result

        # 1. Observe fills. Returns the ACTUAL position.
        position = self.engine.sync_entry_fills(trade_id)
        if position is None or position.is_flat:
            return result

        # 2. PROTECT IMMEDIATELY — same call, before anything else.
        if self.ensure_protected(trade_id, spec, direction, position):
            result.protected_this_step = True
            result.note("stop placed")

        # 3. TP ladder, once protected.
        if self.ensure_take_profits(trade_id, spec, direction, position):
            result.note("tp ladder placed")

        # 4. Observe TP fills; advances TP1..TP4 states on CONFIRMED fills.
        highest = self.engine.sync_take_profits(trade_id)
        if highest:
            result.note(f"tp{highest} filled")

        # 5. Breakeven — authorised only by a confirmed TP2 fill (brief 15).
        state = TradeState(self.store.get_trade(trade_id)["state"])
        if state is TradeState.TP2_FILLED:
            fresh = self.engine.observer.observe_position(symbol)
            if fresh and not fresh.is_flat:
                if self.engine.move_stop_to_breakeven(
                    trade_id=trade_id, spec=spec, direction=direction,
                    position=fresh,
                ):
                    result.note("breakeven active")

        # 6. TP4 filled — close whatever remains.
        state = TradeState(self.store.get_trade(trade_id)["state"])
        if state is TradeState.TP4_FILLED:
            if self.engine.close_trade(
                trade_id=trade_id, trade_uid=trade["trade_uid"], spec=spec,
                direction=direction,
            ):
                result.note("closed after TP4")

        return result

    # ------------------------------------------------------------------

    def ensure_protected(
        self, trade_id: int, spec, direction: Direction, position: Position
    ) -> bool:
        """Place the protective stop if the position has none.

        Called in the same breath as observing the fill. Returns True when a
        stop was placed by this call.
        """
        if self.active_stop_exists(trade_id, spec.symbol):
            return False

        trade = self.store.get_trade(trade_id)
        intent_sl = trade["intent_stop_loss"]
        if intent_sl is None:
            self.engine._enter_safe_error(
                trade_id,
                f"{spec.symbol} holds {position.size} but the trade carries no "
                "stop-loss level to protect it with",
                symbol=spec.symbol,
            )
            return False

        try:
            self.engine.place_stop(
                trade_id=trade_id, trade_uid=trade["trade_uid"], spec=spec,
                direction=direction, trigger_price=Decimal(intent_sl),
                position=position,
            )
        except ExecutionRefused as exc:
            self.engine._enter_safe_error(
                trade_id, f"could not protect {spec.symbol}: {exc}",
                symbol=spec.symbol,
            )
            return False

        log.info(
            f"{spec.symbol} protected at {intent_sl} for {position.size} "
            "(same cycle as the fill)",
            extra={"trade_id": trade_id, "symbol": spec.symbol,
                   "action": "protected"},
        )
        return True

    def active_stop_exists(self, trade_id: int, symbol: str) -> bool:
        """True when a live protective stop covers this symbol."""
        return any(
            o.order_type == "stop" and o.symbol == symbol
            for o in self.client.get_open_orders()
        )

    def ensure_take_profits(
        self, trade_id: int, spec, direction: Direction, position: Position
    ) -> bool:
        """Place the TP ladder once, from stored intent."""
        existing = [
            r for r in self.store.orders_for_trade(trade_id, OrderPurpose.TAKE_PROFIT)
            if not OrderStatus(r["status"]).is_final
        ]
        if existing:
            return False

        trade = self.store.get_trade(trade_id)
        import json as _json

        levels = [Decimal(p) for p in _json.loads(trade["intent_take_profits_json"] or "[]")]
        if not levels:
            return False

        try:
            self.engine.place_take_profits(
                trade_id=trade_id, trade_uid=trade["trade_uid"], spec=spec,
                direction=direction, take_profits=tuple(levels), position=position,
            )
        except ExecutionRefused as exc:
            log.error(
                f"TP ladder not placed for {spec.symbol}: {exc}",
                extra={"trade_id": trade_id, "action": "tp_failed"},
            )
            return False

        self.store.append_event(
            EventType.ORDER_ACCEPTED,
            f"take-profit ladder placed ({len(levels)} levels)",
            trade_id=trade_id,
        )
        return True

    # ------------------------------------------------------------------
    # Instruction handling (close / partial close)
    # ------------------------------------------------------------------

    def close_trades(
        self, trade_ids: list[int], *, percentage: Decimal | None = None
    ) -> dict[int, bool]:
        """Close (or partially close) specific trades. Returns per-trade
        confirmation — True ONLY when the exchange confirmed flat."""
        outcomes: dict[int, bool] = {}
        for trade_id in trade_ids:
            trade = self.store.get_trade(trade_id)
            if trade is None:
                continue
            spec = self._spec_by_symbol.get(trade["symbol"])
            if spec is None:
                outcomes[trade_id] = False
                continue
            try:
                outcomes[trade_id] = self.engine.close_trade(
                    trade_id=trade_id, trade_uid=trade["trade_uid"], spec=spec,
                    direction=Direction(trade["direction"]), percentage=percentage,
                )
            except ExecutionRefused as exc:
                log.error(
                    f"close refused for trade {trade_id}: {exc}",
                    extra={"trade_id": trade_id, "action": "close_refused"},
                )
                outcomes[trade_id] = False
        return outcomes

    def cancel_unentered_trade(self, trade_id: int) -> tuple[bool, str]:
        """Cancel a trade that never entered. (done, why).

        Only for a trade with NO exposure: if any entry has filled, or the
        exchange shows a position, this refuses and the caller escalates to a
        human, because ending a trade that holds a position is a close, not a
        cancel. State is refreshed from the exchange first, so a fill that
        landed since the last observation is seen rather than cancelled
        around.
        """
        trade = self.store.get_trade(trade_id)
        if trade is None:
            return False, "no such trade"
        state = TradeState(trade["state"])
        if state is not TradeState.WAITING_FOR_ENTRY:
            return False, f"trade is {state.value}, not waiting for entry"

        position = self.engine.sync_entry_fills(trade_id)
        if position is not None and not position.is_flat:
            return False, f"an entry already filled ({position.size} held)"

        self.engine.cancel_remaining_entries(trade_id)

        rows = self.store.orders_for_trade(trade_id, OrderPurpose.ENTRY)
        if any(Decimal(r["filled_quantity"] or 0) > 0 for r in rows):
            return False, "an entry order filled while cancelling"
        if any(not OrderStatus(r["status"]).is_final for r in rows):
            return False, "an entry order could not be cancelled"

        self.store.transition_trade(
            trade_id, TradeState.CANCELLED, "cancelled by the channel"
        )
        self.store.append_event(
            EventType.ORDER_CANCELLED,
            "trade cancelled before entry on the channel's instruction",
            trade_id=trade_id,
        )
        log.warning(
            f"trade {trade_id} {trade['symbol']} cancelled before entry",
            extra={"trade_id": trade_id, "symbol": trade["symbol"],
                   "action": "trade_cancelled_by_channel"},
        )
        return True, "entries cancelled, no exposure"

    def resolve_close_targets(
        self,
        *,
        symbol: str | None = None,
        direction: Direction | None = None,
        reply_to_message_id: int | None = None,
        close_all: bool = False,
    ) -> list[int] | None:
        """Resolve which trades a close instruction refers to (brief 20, 21).

        Returns None when the target cannot be determined — the caller must then
        refuse rather than guess. Resolution order is deterministic: reply
        relationship, then explicit symbol, then explicit direction, then an
        explicit 'all'.
        """
        # DENY-list, deliberately. This previously enumerated which states
        # were "closeable" (is_open or WAITING_FOR_ENTRY) and silently omitted
        # REQUIRES_REVIEW — so a quarantined trade holding a real position was
        # invisible to "close all", which then reported "nothing to close".
        #
        # An allow-list has to be kept in sync with the state machine by hand
        # forever. A trade is closeable unless it is TERMINAL: quarantine stops
        # the bot acting on strategy, it must never stop an operator acting on
        # exposure. store.open_trades() already excludes the terminal states.
        live = [t for t in self.store.open_trades()
                if not TradeState(t["state"]).is_terminal]
        if not live:
            return []

        if reply_to_message_id is not None:
            matched = [t for t in live if t["origin_message_id"] == reply_to_message_id]
            if matched:
                return [t["id"] for t in matched]
            # A reply we cannot tie to a trade is ambiguous, not "all".
            return None

        if symbol is not None:
            return [t["id"] for t in live if t["symbol"] == symbol]

        if direction is not None:
            return [t["id"] for t in live if t["direction"] == direction.value]

        if close_all:
            return [t["id"] for t in live]

        return None
