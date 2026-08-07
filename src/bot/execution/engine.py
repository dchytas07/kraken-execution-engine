"""Trade lifecycle: entry -> protection -> TP ladder -> breakeven -> close.

Three rules govern every method here.

1. **State advances only on observed fills.** Every transition is driven by a
   `FillObserver` reading, never by an `OrderResult` from a submission. Brief 27
   names the alternative as a historical bug.

2. **Intent is persisted before the exchange is called.** An order row exists in
   the database with status INTENT before submission, keyed by a deterministic
   client order id, so a crash between "sent" and "recorded" is recoverable
   (brief 30).

3. **Protection tracks the ACTUAL position.** Stop and TP sizes come from the
   exchange's position, never from the intended size — one entry filling means
   half the intended position, and the stop must cover exactly that (brief 39).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ..alerting import notify_trade_event, send_alert
from ..config import GOLD_ENTRY_DEVIATION_PERCENT, Config
from ..db.store import Store
from ..exchange.base import ExchangeClient, OrderRequest, Position
from ..killswitch import KillSwitch
from ..logging_setup import get_logger
from ..models import (
    CanonicalAsset,
    Direction,
    EventType,
    InstrumentSpec,
    OrderPurpose,
    OrderStatus,
    TradeState,
)
from ..sizing import (
    SizingError,
    calculate_size,
    effective_leverage,
    check_sufficient_margin,
    is_entry_stale,
    round_price_to_tick,
    split_entry_allocations,
    split_take_profits,
)
from .observer import FillObserver, ObservedOrder

log = get_logger(__name__)


class ExecutionRefused(Exception):
    """Execution declined for a stated, recorded reason. Never silent."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def client_order_id(trade_uid: str, purpose: OrderPurpose, sequence: int) -> str:
    """Deterministic client order id.

    Deterministic on purpose: after a crash, recovery can reconstruct the exact
    id a previous run would have used and ask the exchange whether that order
    exists, instead of blindly resubmitting (brief 30).
    """
    return f"{trade_uid}-{purpose.value}-{sequence}"


@dataclass(frozen=True, slots=True)
class EntryPlan:
    """What we intend to do. Distinct from what the exchange does (brief 39)."""

    symbol: str
    direction: Direction
    leverage: Decimal
    entries: tuple[tuple[Decimal, Decimal], ...]  # (price, size)
    total_intended_size: Decimal
    total_intended_margin: Decimal


class ExecutionEngine:
    def __init__(
        self,
        *,
        config: Config,
        store: Store,
        client: ExchangeClient,
        observer: FillObserver,
        kill_switch: KillSwitch,
    ) -> None:
        self.config = config
        self.store = store
        self.client = client
        self.observer = observer
        self.kill_switch = kill_switch
        # brief 29: after a restart the bot must reconcile against the exchange
        # BEFORE placing anything. Recovering after placing orders defeats the
        # point, so this starts False and only RecoveryManager clears it.
        self._recovery_complete = False

    def mark_recovery_complete(self) -> None:
        """Called by RecoveryManager once state has been reconciled."""
        self._recovery_complete = True

    def require_recovered(self) -> None:
        """Gate on the trading path. Fails closed on a fresh process."""
        if not self._recovery_complete:
            raise ExecutionRefused(
                "RECOVERY_INCOMPLETE",
                "the engine has not reconciled against the exchange since "
                "starting; refusing to open a trade (brief 29)",
            )

    # ------------------------------------------------------------------
    # Danger signalling
    # ------------------------------------------------------------------

    def _enter_safe_error(self, trade_id: int, summary: str, **ctx) -> None:
        """Record AND shout.

        Writing an event row with severity=ERROR puts it in the database, which
        nobody is watching in real time. Brief 32 requires dangerous
        inconsistencies to be loud, so every SAFE_ERROR also emits a CRITICAL
        log line — that is the channel an operator actually sees.

        A real-time Telegram alert is sent on the actual transition (not on a
        repeat call while already in SAFE_ERROR) — see alerting.py. Best-effort:
        a failed send is logged and swallowed, never allowed to affect the
        transition it's reporting on.
        """
        row = self.store.get_trade(trade_id)
        symbol = row["symbol"]
        message = f"SAFE_ERROR trade {trade_id} ({symbol}): {summary}"
        log.critical(
            message,
            extra={"trade_id": trade_id, "action": "safe_error", **ctx},
        )
        self.store.append_event(
            EventType.SAFETY_CHECK_FAILED, summary, trade_id=trade_id, severity="ERROR"
        )
        state = TradeState(row["state"])
        if state is not TradeState.SAFE_ERROR and not state.is_terminal:
            self.store.transition_trade(trade_id, TradeState.SAFE_ERROR, summary)
            try:
                send_alert(
                    self.config.alert_telegram_bot_token,
                    self.config.alert_telegram_chat_id,
                    message,
                )
            except Exception as exc:  # noqa: BLE001 — alerting must never crash the bot
                log.warning(
                    f"alert send raised unexpectedly: {exc}",
                    extra={"trade_id": trade_id, "action": "alert_failed"},
                )

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def plan_entries(
        self,
        *,
        spec: InstrumentSpec,
        direction: Direction,
        entry_prices: tuple[Decimal, ...],
        signal_leverage: Decimal | None,
    ) -> EntryPlan:
        """Split margin 50/50 across the entry prices and size each one.

        Margin is split, not contracts: the two entries sit at different prices,
        so equal margin yields different sizes (brief 11).

        ┌─ ORDER-CONSTRUCTION FUNNEL ─────────────────────────────────────┐
        │ EVERY order that reaches the exchange MUST be built inside this  │
        │ class. That includes surfaces not yet written: add/DCA (brief    │
        │ 19), entry updates (brief 20), partial closes, and anything a    │
        │ later stage introduces.                                          │
        │                                                                  │
        │ Do NOT construct an OrderRequest anywhere else. The leverage cap │
        │ below, the sizing validation, the stale-entry guard and the kill │
        │ switch all live on this path; a second construction site bypasses│
        │ them silently. That is exactly how the 10x cap came to have zero │
        │ call sites while passing five unit tests.                        │
        │                                                                  │
        │ Enforced by tests/test_wiring.py::                               │
        │   test_order_requests_are_only_built_inside_the_engine           │
        └──────────────────────────────────────────────────────────────────┘

        Takes the SIGNAL's requested leverage and caps it here. The parameter is
        deliberately named `signal_leverage` rather than `leverage`: the previous
        signature accepted an already-final value, and no caller applied the cap,
        so a 50x signal would have reached the exchange as 50x. The cap now lives
        on the only path that can build a plan, and cannot be bypassed by a
        caller passing a bigger number.
        """
        if not entry_prices:
            raise ExecutionRefused("NO_ENTRY_PRICES")

        # brief 9: min(signal, internal cap, exchange max)
        leverage = effective_leverage(
            signal_leverage,
            max_leverage=self.config.max_leverage,
            default_leverage=self.config.default_leverage,
            exchange_max_leverage=spec.max_leverage,
        )
        if signal_leverage is not None and leverage < signal_leverage:
            log.warning(
                f"leverage capped {signal_leverage}x -> {leverage}x for "
                f"{spec.symbol}",
                extra={"symbol": spec.symbol, "action": "leverage_capped"},
            )

        allocations = split_entry_allocations(
            self.config.trade_margin_usd, len(entry_prices)
        )

        entries: list[tuple[Decimal, Decimal]] = []
        for price, margin in zip(entry_prices, allocations):
            snapped = round_price_to_tick(
                price, spec.tick_size, round_up=direction is Direction.SHORT
            )
            sized = calculate_size(
                margin_usd=margin,
                leverage=leverage,
                price=snapped,
                spec=spec,
                max_deviation_percent=self.config.max_sizing_deviation_percent,
            )
            entries.append((snapped, sized.size))

        total_size = sum((s for _, s in entries), Decimal(0))
        return EntryPlan(
            symbol=spec.symbol,
            direction=direction,
            leverage=leverage,
            entries=tuple(entries),
            total_intended_size=total_size,
            total_intended_margin=self.config.trade_margin_usd,
        )

    def entry_deviation_limit(self, spec: InstrumentSpec) -> Decimal:
        """The stale-entry allowance for this instrument.

        Substitute instruments get their own, wider allowance. Gold trades
        against PF_XAUTUSD, whose basis to spot gold was measured at -1.85%
        live — larger than the default 1.5% budget on its own, so every gold
        signal would be refused as "stale" for a reason that has nothing to
        do with the signal's age. See config.GOLD_ENTRY_DEVIATION_PERCENT.
        """
        if spec.is_substitute and spec.asset is CanonicalAsset.XAU:
            return GOLD_ENTRY_DEVIATION_PERCENT
        return self.config.max_entry_deviation_percent

    def check_entry_freshness(
        self, spec: InstrumentSpec, entry_prices: tuple[Decimal, ...]
    ) -> Decimal:
        """Stale-signal guard (brief 12). Raises if price has run too far."""
        mark = self.client.get_mark_price(spec.symbol)
        limit = self.entry_deviation_limit(spec)
        stale, deviation = is_entry_stale(mark, entry_prices, limit)
        if stale:
            raise ExecutionRefused(
                "STALE_ENTRY",
                f"{spec.symbol}: mark {mark} is {deviation:.2f}% beyond the entry "
                f"range {entry_prices} (limit {limit}%)",
            )
        return mark

    def reassert_leverage(self, symbol: str) -> Decimal:
        """Re-apply the configured leverage cap to a symbol.

        The ONLY sanctioned leverage mutation outside open_trade. Reconciliation
        calls this rather than touching the exchange directly, so the cap is
        applied in one place — enforced by
        tests/test_wiring.py::test_leverage_is_only_set_through_the_engine.

        NOT free of consequence with a position open. Live measurement
        (KRAKEN_NOTES 1C, 2026-07-30) found set-leverage on a symbol with an
        open position leaves the position's size/price alone but measurably
        moves the account's initialMargin/availableMargin. Both callers
        (recovery.py, reconcile.py) check available-margin headroom before
        calling this and log the before/after numbers — this method does not
        re-check, so call it only through those gated paths.
        """
        return self.client.set_leverage_preference(symbol, self.config.max_leverage)

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    def open_trade(
        self,
        *,
        trade_id: int,
        trade_uid: str,
        spec: InstrumentSpec,
        plan: EntryPlan,
    ) -> list[str]:
        """Submit both entry orders. Returns their client order ids.

        Does NOT advance the trade past WAITING_FOR_ENTRY — nothing has filled
        yet, and only a confirmed fill may advance state.
        """
        self.require_recovered()

        if not self.kill_switch.allows_new_positions():
            raise ExecutionRefused(
                "KILL_SWITCH_ACTIVE", "kill switch engaged; no new positions"
            )

        # Brief 12 enforced on the execution path itself. Previously this lived
        # only in a public method callers were trusted to invoke first, which
        # made it an unenforced guard -- a stale signal would have executed.
        self.check_entry_freshness(spec, tuple(p for p, _ in plan.entries))

        account = self.client.get_account_state()
        check_sufficient_margin(plan.total_intended_margin, account.available_margin)

        # Leverage is per-symbol account state and leaks across trades, so it is
        # set and verified immediately before opening (KRAKEN_NOTES 1C).
        if plan.leverage > self.config.max_leverage:
            raise ExecutionRefused(
                "LEVERAGE_EXCEEDS_CAP",
                f"plan carries {plan.leverage}x but the hard cap is "
                f"{self.config.max_leverage}x — refusing to set it",
            )
        self.client.set_leverage_preference(spec.symbol, plan.leverage)

        self.store.transition_trade(trade_id, TradeState.WAITING_FOR_ENTRY)

        # brief 12: resting entries get a bounded lifetime. Without this the
        # expiry sweep has nothing to act on.
        expires_at = (
            datetime.now(UTC) + timedelta(hours=self.config.entry_order_expiry_hours)
        ).isoformat()

        submitted: list[str] = []
        for index, (price, size) in enumerate(plan.entries, start=1):
            coid = client_order_id(trade_uid, OrderPurpose.ENTRY, index)
            side = plan.direction.opening_side

            # Intent first, exchange second. The gap between these two lines is
            # the crash window, and this ordering is what makes it recoverable.
            order_id, _ = self.store.record_order_intent(
                trade_id=trade_id,
                client_order_id=coid,
                purpose=OrderPurpose.ENTRY,
                symbol=spec.symbol,
                side=side,
                order_type="limit",
                quantity=size,
                price=price,
                sequence=index,
                expires_at=expires_at,
            )
            self.store.append_event(
                EventType.ORDER_INTENT_RECORDED,
                f"entry {index} intent: {side} {size} @ {price}",
                trade_id=trade_id,
                order_id=order_id,
            )

            result = self.client.place_order(
                OrderRequest(
                    symbol=spec.symbol,
                    side=side,
                    size=size,
                    order_type="limit",
                    purpose=OrderPurpose.ENTRY,
                    client_order_id=coid,
                    price=price,
                )
            )

            if not result.accepted:
                # Do not retry. The order may have reached the exchange; the
                # client order id lets recovery find out (brief 40).
                self.store.update_order(
                    order_id, status=OrderStatus.UNKNOWN, status_detail=result.detail
                )
                self.store.append_event(
                    EventType.ORDER_REJECTED,
                    f"entry {index} not accepted: {result.detail}",
                    trade_id=trade_id,
                    order_id=order_id,
                    severity="ERROR",
                )
                continue

            self.store.update_order(
                order_id,
                status=OrderStatus.SUBMITTED,
                exchange_order_id=result.exchange_order_id,
                submitted=True,
            )
            self.store.append_event(
                EventType.ORDER_SUBMITTED,
                f"entry {index} submitted",
                trade_id=trade_id,
                order_id=order_id,
            )
            submitted.append(coid)

        notify_trade_event(
            self.config, "opened",
            symbol=spec.symbol, side=plan.direction.value,
            size=plan.total_intended_size,
            detail=f"{len(submitted)} entry order(s) resting, "
                   f"{plan.leverage}x, ${plan.total_intended_margin} margin",
        )
        return submitted

    def sync_entry_fills(self, trade_id: int) -> Position | None:
        """Observe entry orders and advance state on CONFIRMED fills only.

        The stop is resized to the observed position on EVERY call, gated only
        on a size mismatch and never on the label. That ordering matters: the
        per-order reads and the position read are not atomic, so an entry that
        fills between them yields PARTIALLY_ENTERED alongside a full-size
        position (observed in paper 2026-07-29). If resizing were gated on
        reaching FULLY_ENTERED, that window would leave the extra size
        genuinely unprotected — an exposure gap, not a cosmetic lag.

        With resizing driven by the position rather than the label, the stale
        label is harmless: it errs conservative, the stop always covers the
        actual holding, and the next call promotes the label once the orders
        read as done.

        Making the reads atomic requires a single exchange snapshot, which is
        Stage 7's job (reconciliation owns consistent state capture).
        """
        trade = self.store.get_trade(trade_id)
        symbol = trade["symbol"]
        entry_orders = self.store.orders_for_trade(trade_id, OrderPurpose.ENTRY)

        total_filled = Decimal(0)
        all_done = True
        for row in entry_orders:
            observed = self.observer.observe_order(
                row["client_order_id"], expected_size=Decimal(row["quantity"])
            )
            self._persist_observation(row["id"], observed)
            total_filled += observed.filled_size
            if observed.resting:
                all_done = False

        position = self.observer.observe_position(symbol)
        actual_size = position.size if position else Decimal(0)

        if actual_size > 0:
            # Protection first, bookkeeping second. Driven by the observed
            # position, never by the state label — see the docstring.
            self.resize_stop_to_position(trade_id=trade_id, position=position)
            # The ladder must track the position too — a later entry filling
            # used to leave it covering only the earlier entry's share.
            self.resize_take_profits_to_position(
                trade_id=trade_id, position=position
            )

            _, avg_entry = self._weighted_entry(entry_orders)
            self.store.update_trade_actuals(
                trade_id,
                actual_quantity=actual_size,
                actual_avg_entry_price=avg_entry or None,
            )
            self.store.append_event(
                EventType.POSITION_UPDATED,
                f"position {actual_size} @ {avg_entry}",
                trade_id=trade_id,
            )

            current = TradeState(trade["state"])
            target = (
                TradeState.FULLY_ENTERED if all_done else TradeState.PARTIALLY_ENTERED
            )
            if current is TradeState.WAITING_FOR_ENTRY:
                self.store.transition_trade(trade_id, target)
            elif current is TradeState.PARTIALLY_ENTERED and all_done:
                self.store.transition_trade(trade_id, TradeState.FULLY_ENTERED)

        return position

    def _weighted_entry(self, entry_rows) -> tuple[Decimal, Decimal]:
        coids = [r["client_order_id"] for r in entry_rows]
        if hasattr(self.observer, "weighted_average_entry"):
            return self.observer.weighted_average_entry(coids)
        return Decimal(0), Decimal(0)

    def _persist_observation(self, order_row_id: int, observed: ObservedOrder) -> None:
        self.store.update_order(
            order_row_id,
            status=observed.status,
            exchange_order_id=observed.exchange_order_id,
            filled_quantity=observed.filled_size,
            avg_fill_price=observed.avg_fill_price,
        )

    # ------------------------------------------------------------------
    # Protection: stop loss
    # ------------------------------------------------------------------

    def place_stop(
        self,
        *,
        trade_id: int,
        trade_uid: str,
        spec: InstrumentSpec,
        direction: Direction,
        trigger_price: Decimal,
        position: Position,
    ) -> str:
        """Place the protective stop for the ACTUAL position size."""
        # A long's stop rounds DOWN and a short's UP, so tick snapping can never
        # pull the stop tighter than intended.
        trigger = round_price_to_tick(
            trigger_price, spec.tick_size, round_up=direction is Direction.SHORT
        )
        # Sequence, not a fixed 1. A stop may legitimately be placed more than
        # once for one trade — reconciliation restores protection when a stop is
        # cancelled out of band. Reusing SL-1 hit the UNIQUE constraint on
        # client_order_id and left the position UNPROTECTED, which is the exact
        # failure reconciliation exists to repair.
        # Still deterministic and enumerable: recovery walks SL-1, SL-2, ... and
        # asks the exchange which of them exist.
        sequence = self._next_stop_sequence(trade_id)
        coid = client_order_id(trade_uid, OrderPurpose.STOP_LOSS, sequence)

        order_id, order_uid = self.store.record_order_intent(
            trade_id=trade_id,
            client_order_id=coid,
            purpose=OrderPurpose.STOP_LOSS,
            symbol=spec.symbol,
            side=direction.closing_side,
            order_type="stop",
            quantity=position.size,
            trigger_price=trigger,
            trigger_signal="mark",
            reduce_only=True,
            sequence=sequence,
        )

        result = self.client.place_order(
            OrderRequest(
                symbol=spec.symbol,
                side=direction.closing_side,
                size=position.size,
                order_type="stop",
                purpose=OrderPurpose.STOP_LOSS,
                client_order_id=coid,
                trigger_price=trigger,
                reduce_only=True,
            )
        )
        if not result.accepted:
            self.store.update_order(
                order_id, status=OrderStatus.UNKNOWN, status_detail=result.detail
            )
            raise ExecutionRefused("STOP_NOT_PLACED", result.detail)

        self.store.update_order(
            order_id,
            status=OrderStatus.OPEN,
            exchange_order_id=result.exchange_order_id,
            submitted=True,
        )
        self.store.set_active_stop(
            trade_id, order_uid=order_uid, trigger_price=trigger
        )
        self.store.append_event(
            EventType.ORDER_ACCEPTED,
            f"protective stop active at {trigger} for {position.size}",
            trade_id=trade_id,
            order_id=order_id,
        )

        state = TradeState(self.store.get_trade(trade_id)["state"])
        if state in (TradeState.PARTIALLY_ENTERED, TradeState.FULLY_ENTERED):
            self.store.transition_trade(trade_id, TradeState.PROTECTED)
        return coid

    def resize_stop_to_position(
        self, *, trade_id: int, position: Position
    ) -> bool:
        """Keep the stop covering exactly the remaining position (brief 14).

        A stop smaller than the position means part of the position is
        unprotected, so a failed resize is a safety failure, not a no-op. It
        escalates to SAFE_ERROR with a CRITICAL log rather than returning
        quietly.
        """
        stop = self._active_stop_row(trade_id)
        if stop is None:
            # No stop yet — normal between the first fill and place_stop.
            return False

        current = Decimal(stop["quantity"])
        if current == position.size:
            return True

        ok = self.client.amend_order(stop["exchange_order_id"], size=position.size)
        if not ok:
            self._enter_safe_error(
                trade_id,
                f"stop resize FAILED: stop covers {current} but the position is "
                f"{position.size} — {position.size - current} is UNPROTECTED",
                symbol=position.symbol,
                order_id=stop["exchange_order_id"],
            )
            return False

        self.store.update_order(stop["id"], status=OrderStatus.OPEN)
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE orders SET quantity = ? WHERE id = ?",
                (str(position.size), stop["id"]),
            )
        self.store.append_event(
            EventType.ORDER_ACCEPTED,
            f"stop resized {current} -> {position.size} to match the position",
            trade_id=trade_id,
            order_id=stop["id"],
        )
        return True

    def move_stop_to_breakeven(
        self,
        *,
        trade_id: int,
        spec: InstrumentSpec,
        direction: Direction,
        position: Position,
    ) -> bool:
        """Atomically move the stop to the actual weighted-average entry.

        Uses `edit-order`, which amends in place: the order id is preserved and
        the open-order count does not change, so there is never a window with
        two stops or none. This is the fix for the historical duplicate-stop bug
        (brief 16).

        Breakeven is the ACTUAL average fill price, not the signal's stated
        entry — if only one entry filled, breakeven reflects that (brief 15).
        """
        # Brief 15 enforced HERE as well as in the state table. The transition
        # table already forbids reaching BREAKEVEN_ACTIVE from anywhere but
        # TP2_FILLED; checking it up front means we refuse before touching the
        # exchange rather than after amending a live stop.
        state = TradeState(self.store.get_trade(trade_id)["state"])
        if state is TradeState.BREAKEVEN_ACTIVE:
            return True  # idempotent: already there
        if state is not TradeState.TP2_FILLED:
            raise ExecutionRefused(
                "BREAKEVEN_NOT_AUTHORISED",
                f"trade is {state.value}; breakeven requires a CONFIRMED TP2 fill "
                "(brief 15) — not price touching TP2, not TP2 being placed",
            )

        stop = self._active_stop_row(trade_id)
        if stop is None:
            raise ExecutionRefused("NO_ACTIVE_STOP", f"trade {trade_id}")

        entry_rows = self.store.orders_for_trade(trade_id, OrderPurpose.ENTRY)
        _, avg_entry = self._weighted_entry(entry_rows)
        if avg_entry <= 0:
            raise ExecutionRefused(
                "NO_ENTRY_FILLS", "cannot compute breakeven without actual fills"
            )

        breakeven = round_price_to_tick(
            avg_entry, spec.tick_size, round_up=direction is Direction.SHORT
        )

        self.store.append_event(
            EventType.SL_REPLACEMENT_INITIATED,
            f"amending stop to breakeven {breakeven} (avg entry {avg_entry}) "
            f"size {position.size}",
            trade_id=trade_id,
            order_id=stop["id"],
        )

        ok = self.client.amend_order(
            stop["exchange_order_id"],
            size=position.size,
            trigger_price=breakeven,
        )
        if not ok:
            self.store.append_event(
                EventType.SL_REPLACEMENT_FAILED,
                "breakeven amend failed",
                trade_id=trade_id,
                order_id=stop["id"],
                severity="ERROR",
            )
            self._enter_safe_error(
                trade_id,
                "breakeven amend FAILED — the ORIGINAL stop is still active "
                "(amend is atomic, so the position remains protected), but the "
                "stop is no longer where strategy intends",
                order_id=stop["exchange_order_id"],
            )
            return False

        if not self._verify_stop(trade_id, stop["exchange_order_id"], position.size):
            self._enter_safe_error(
                trade_id,
                "stop verification FAILED after breakeven amend — cannot confirm "
                "which stop is authoritative",
                order_id=stop["exchange_order_id"],
            )
            return False

        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE orders SET quantity = ?, trigger_price = ? WHERE id = ?",
                (str(position.size), str(breakeven), stop["id"]),
            )
        self.store.set_active_stop(
            trade_id,
            order_uid=stop["order_uid"],
            trigger_price=breakeven,
            breakeven=True,
            breakeven_price=breakeven,
        )
        self.store.append_event(
            EventType.SL_REPLACEMENT_VERIFIED,
            f"breakeven stop verified at {breakeven}",
            trade_id=trade_id,
            order_id=stop["id"],
        )
        self.store.transition_trade(trade_id, TradeState.BREAKEVEN_ACTIVE)
        self.store.append_event(
            EventType.BREAKEVEN_ACTIVATED, "breakeven active", trade_id=trade_id
        )
        notify_trade_event(
            self.config, "breakeven",
            symbol=spec.symbol, side=direction.value, size=position.size,
            price=breakeven,
            detail="downside now capped at entry",
        )
        return True

    def _verify_stop(
        self, trade_id: int, exchange_order_id: str, expected_size: Decimal
    ) -> bool:
        """Confirm exactly one stop exists with the right size.

        Trigger price is deliberately NOT asserted: the paper engine omits
        `stop_price` from open orders entirely (KRAKEN_NOTES 4.2), so requiring
        it would make every paper run fail. Size, existence, uniqueness and
        reduce-only ARE verified in both engines. Stage 7 adds the trigger-price
        assertion once live behaviour is confirmed.
        """
        orders = self.client.get_open_orders()
        stops = [o for o in orders if o.order_type == "stop"]

        if len(stops) != 1:
            self.store.append_event(
                EventType.SL_REPLACEMENT_FAILED,
                f"expected exactly 1 stop, found {len(stops)} — possible duplicate "
                "or missing protective stop",
                trade_id=trade_id,
                severity="ERROR",
            )
            return False

        stop = stops[0]
        if stop.exchange_order_id != exchange_order_id:
            self.store.append_event(
                EventType.SL_REPLACEMENT_FAILED,
                f"stop id changed: expected {exchange_order_id}, found "
                f"{stop.exchange_order_id}",
                trade_id=trade_id,
                severity="ERROR",
            )
            return False
        if stop.size != expected_size:
            self.store.append_event(
                EventType.SL_REPLACEMENT_FAILED,
                f"stop size {stop.size} != position {expected_size}",
                trade_id=trade_id,
                severity="ERROR",
            )
            return False
        if not stop.reduce_only:
            self.store.append_event(
                EventType.SL_REPLACEMENT_FAILED,
                "stop is not reduce-only; it could open a reverse position",
                trade_id=trade_id,
                severity="ERROR",
            )
            return False
        return True

    def resize_take_profits_to_position(
        self, *, trade_id: int, position: Position
    ) -> bool:
        """Keep the UNFILLED TP legs summing to exactly the position.

        The stop already tracks the position (resize_stop_to_position). The
        ladder did not, so a second entry filling after the ladder was placed
        left it covering only the first entry's share — observed live on a
        real trade: stop 0.0046, ladder 0.0023, i.e. half the position with
        no take-profit cover at all.

        The two directions are NOT symmetric, and the handling differs:

          * ladder < position — under-covered. Less profit is taken than
            intended. Undesirable, not dangerous: the STOP is what bounds
            loss, and it is sized separately. Logged, never SAFE_ERROR.
          * ladder > position — over-covered. Reduce-only protects us at the
            exchange, but a ladder summing above the position is exactly what
            brief 13 forbids, so a failure to shrink it escalates.

        Only unfilled legs are resized. A filled TP has already taken its
        share, and the position observed here is what remains after it.
        """
        rows = [
            o
            for o in self.store.orders_for_trade(trade_id, OrderPurpose.TAKE_PROFIT)
            if not OrderStatus(o["status"]).is_final and o["exchange_order_id"]
        ]
        if not rows:
            return False  # no ladder yet, or every leg already filled

        current = sum(Decimal(o["quantity"]) for o in rows)
        if current == position.size:
            return True

        spec = self._spec_for_trade(trade_id)
        if spec is None:
            log.warning(
                f"cannot resize TP ladder on {position.symbol}: no instrument spec",
                extra={"trade_id": trade_id, "action": "tp_resize_no_spec"},
            )
            return False

        # Re-split the CURRENT position across the legs that remain, so the
        # parts still sum to exactly the position (brief 13).
        parts = split_take_profits(position.size, spec, levels=len(rows))
        if sum(parts) > position.size:
            self._enter_safe_error(
                trade_id,
                f"TP resize would exceed the position: {sum(parts)} > "
                f"{position.size}",
                symbol=position.symbol,
            )
            return False

        failed = []
        for row, size in zip(rows, parts):
            if Decimal(row["quantity"]) == size:
                continue
            if self.client.amend_order(row["exchange_order_id"], size=size):
                with self.store.transaction() as conn:
                    conn.execute(
                        "UPDATE orders SET quantity = ? WHERE id = ?",
                        (str(size), row["id"]),
                    )
            else:
                failed.append(row["client_order_id"])

        if failed:
            if current > position.size:
                # Shrinking failed and the ladder still exceeds the position.
                self._enter_safe_error(
                    trade_id,
                    f"TP ladder still covers {current} against a position of "
                    f"{position.size}; could not shrink {failed}",
                    symbol=position.symbol,
                )
            else:
                log.warning(
                    f"TP resize failed for {failed}; ladder covers {current} of "
                    f"{position.size} — under-covered, position still protected "
                    "by the stop",
                    extra={"trade_id": trade_id, "symbol": position.symbol,
                           "action": "tp_resize_failed"},
                )
            return False

        self.store.append_event(
            EventType.ORDER_ACCEPTED,
            f"TP ladder resized {current} -> {position.size} to match the position",
            trade_id=trade_id,
        )
        log.info(
            f"TP ladder resized {current} -> {position.size} on {position.symbol}",
            extra={"trade_id": trade_id, "symbol": position.symbol,
                   "action": "tp_resized"},
        )
        return True

    def _spec_for_trade(self, trade_id: int) -> InstrumentSpec | None:
        """Resolve the instrument spec via the ExchangeClient interface.

        Uses get_instrument_spec (declared on ExchangeClient) rather than
        load_specs (KrakenCliAdapter only) — an adapter-only call would
        silently resolve to nothing for any other conforming client, which
        is precisely the kind of quiet no-op this codebase keeps finding.
        """
        row = self.store.get_trade(trade_id)
        if row is None:
            return None
        try:
            return self.client.get_instrument_spec(CanonicalAsset(row["asset"]))
        except Exception:  # noqa: BLE001 — a missing spec is reported, not fatal
            return None

    def _active_stop_row(self, trade_id: int):
        trade = self.store.get_trade(trade_id)
        uid = trade["active_stop_order_uid"]
        if uid is None:
            return None
        return next(
            (
                o
                for o in self.store.orders_for_trade(trade_id, OrderPurpose.STOP_LOSS)
                if o["order_uid"] == uid
            ),
            None,
        )

    # ------------------------------------------------------------------
    # Take-profit ladder
    # ------------------------------------------------------------------

    def place_take_profits(
        self,
        *,
        trade_id: int,
        trade_uid: str,
        spec: InstrumentSpec,
        direction: Direction,
        take_profits: tuple[Decimal, ...],
        position: Position,
    ) -> list[str]:
        """Place the TP ladder against the ACTUAL position size.

        Sizes come from `split_take_profits`, which guarantees the parts sum to
        exactly the position — never more, which could open a reverse position
        (brief 13).
        """
        parts = split_take_profits(position.size, spec, levels=len(take_profits))
        if sum(parts) > position.size:
            raise ExecutionRefused(
                "TP_EXCEEDS_POSITION",
                f"{sum(parts)} > {position.size}",
            )

        submitted = []
        for index, (price, size) in enumerate(zip(take_profits, parts), start=1):
            trigger = round_price_to_tick(
                price, spec.tick_size, round_up=direction is Direction.LONG
            )
            coid = client_order_id(trade_uid, OrderPurpose.TAKE_PROFIT, index)

            order_id, _ = self.store.record_order_intent(
                trade_id=trade_id,
                client_order_id=coid,
                purpose=OrderPurpose.TAKE_PROFIT,
                symbol=spec.symbol,
                side=direction.closing_side,
                order_type="take-profit",
                quantity=size,
                trigger_price=trigger,
                trigger_signal="mark",
                reduce_only=True,
                sequence=index,
            )
            result = self.client.place_order(
                OrderRequest(
                    symbol=spec.symbol,
                    side=direction.closing_side,
                    size=size,
                    order_type="take-profit",
                    purpose=OrderPurpose.TAKE_PROFIT,
                    client_order_id=coid,
                    trigger_price=trigger,
                    reduce_only=True,
                )
            )
            if not result.accepted:
                self.store.update_order(
                    order_id, status=OrderStatus.UNKNOWN, status_detail=result.detail
                )
                continue
            self.store.update_order(
                order_id,
                status=OrderStatus.OPEN,
                exchange_order_id=result.exchange_order_id,
                submitted=True,
            )
            submitted.append(coid)

        return submitted

    def sync_take_profits(self, trade_id: int) -> int:
        """Observe TP orders; advance state per CONFIRMED fill.

        Returns the highest TP index confirmed filled (0 if none). TP2 filling
        is what authorises breakeven — not price touching it, not the order
        being accepted (brief 15).
        """
        rows = self.store.orders_for_trade(trade_id, OrderPurpose.TAKE_PROFIT)
        highest = 0
        for row in rows:
            observed = self.observer.observe_order(
                row["client_order_id"], expected_size=Decimal(row["quantity"])
            )
            was_filled = OrderStatus(row["status"]) is OrderStatus.FILLED
            self._persist_observation(row["id"], observed)
            if observed.is_filled:
                highest = max(highest, int(row["sequence"]))
                self.store.append_event(
                    EventType.ORDER_FILLED,
                    f"TP{row['sequence']} filled {observed.filled_size}",
                    trade_id=trade_id,
                    order_id=row["id"],
                )
                # Only on the transition into FILLED. sync_take_profits runs
                # every strategy cycle, so keying off is_filled alone would
                # re-announce the same TP indefinitely.
                if not was_filled:
                    trade = self.store.get_trade(trade_id)
                    entry = trade["actual_avg_entry_price"]
                    pnl = None
                    if entry and observed.avg_fill_price:
                        move = Decimal(str(observed.avg_fill_price)) - Decimal(str(entry))
                        if trade["direction"] == Direction.SHORT.value:
                            move = -move
                        pnl = move * Decimal(str(observed.filled_size))
                    notify_trade_event(
                        self.config, "tp_filled",
                        symbol=trade["symbol"], side=trade["direction"],
                        size=observed.filled_size, price=observed.avg_fill_price,
                        pnl=pnl, detail=f"TP{row['sequence']} of 4",
                    )

        self._advance_tp_state(trade_id, highest)
        return highest

    def _advance_tp_state(self, trade_id: int, highest_filled: int) -> None:
        """Walk the ladder one step at a time so no state is skipped."""
        ladder = {
            1: TradeState.TP1_FILLED,
            2: TradeState.TP2_FILLED,
            3: TradeState.TP3_FILLED,
            4: TradeState.TP4_FILLED,
        }
        for index in range(1, highest_filled + 1):
            current = TradeState(self.store.get_trade(trade_id)["state"])
            target = ladder[index]
            if current is target or current.is_terminal:
                continue
            from ..models import can_transition

            if can_transition(current, target):
                self.store.transition_trade(trade_id, target)

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def close_trade(
        self,
        *,
        trade_id: int,
        trade_uid: str,
        spec: InstrumentSpec,
        direction: Direction,
        percentage: Decimal | None = None,
    ) -> bool:
        """Close, then CONFIRM before marking closed.

        The sequence brief 27 mandates:

            submit close -> await exchange-confirmed flat -> cancel remaining
            orders -> verify none remain -> only then CLOSED

        A close request that is submitted but unconfirmed leaves the trade
        open. Returning False means "not closed", never "probably closed".
        """
        position = self.observer.observe_position(spec.symbol)
        if position is None or position.is_flat:
            return self._finalise_close(trade_id, spec.symbol)

        # A partial close is NOT a close: it must not enter CLOSING, because a
        # trade that keeps a live position also keeps its protection and its
        # place in the TP ladder. Only a full close enters the closing sequence.
        state_before = TradeState(self.store.get_trade(trade_id)["state"])
        if percentage is None and state_before is not TradeState.CLOSING:
            self.store.transition_trade(trade_id, TradeState.CLOSING)

        self.store.append_event(
            EventType.CLOSE_REQUESTED,
            f"close requested for {position.size}"
            + (f" ({percentage}%)" if percentage else ""),
            trade_id=trade_id,
        )

        size = position.size
        if percentage is not None:
            size = (position.size * percentage / 100).quantize(spec.size_increment)
            size = min(size, position.size)  # never close more than we hold
            if size <= 0:
                raise ExecutionRefused(
                    "PARTIAL_CLOSE_TOO_SMALL",
                    f"{percentage}% of {position.size} rounds below the increment",
                )

        coid = client_order_id(
            trade_uid, OrderPurpose.CLOSE, self._next_close_sequence(trade_id)
        )
        order_id, _ = self.store.record_order_intent(
            trade_id=trade_id,
            client_order_id=coid,
            purpose=OrderPurpose.CLOSE,
            symbol=spec.symbol,
            side=direction.closing_side,
            order_type="market",
            quantity=size,
            reduce_only=True,
        )
        result = self.client.place_order(
            OrderRequest(
                symbol=spec.symbol,
                side=direction.closing_side,
                size=size,
                order_type="market",
                purpose=OrderPurpose.CLOSE,
                client_order_id=coid,
                reduce_only=True,
            )
        )
        self.store.append_event(
            EventType.CLOSE_SUBMITTED,
            f"close order submitted: accepted={result.accepted}",
            trade_id=trade_id,
            order_id=order_id,
        )
        if not result.accepted:
            self.store.update_order(
                order_id, status=OrderStatus.UNKNOWN, status_detail=result.detail
            )
            return False  # NOT closed

        self.store.update_order(
            order_id,
            status=OrderStatus.SUBMITTED,
            exchange_order_id=result.exchange_order_id,
            submitted=True,
        )

        if percentage is not None:
            # A partial close never closes the trade. Re-read the position and
            # shrink the stop to cover exactly what remains (brief 14).
            remaining = self.observer.observe_position(spec.symbol)
            if remaining and not remaining.is_flat:
                self.store.update_trade_actuals(
                    trade_id, actual_quantity=remaining.size
                )
                self.resize_stop_to_position(trade_id=trade_id, position=remaining)
                self.store.append_event(
                    EventType.POSITION_UPDATED,
                    f"partial close complete; {remaining.size} remains protected",
                    trade_id=trade_id,
                )
                # State is deliberately unchanged — the trade continues in
                # whatever lifecycle position it already held.
                return False
            # The "partial" took the whole position; treat it as a full close.
            if state_before is not TradeState.CLOSING:
                self.store.transition_trade(trade_id, TradeState.CLOSING)
            return self._finalise_close(trade_id, spec.symbol)

        if not self.observer.await_flat(spec.symbol):
            self.store.append_event(
                EventType.RECONCILE_MISMATCH,
                "close submitted but the position is NOT confirmed flat — trade "
                "stays open and protected",
                trade_id=trade_id,
                severity="ERROR",
            )
            return False

        return self._finalise_close(trade_id, spec.symbol)

    def _finalise_close(self, trade_id: int, symbol: str) -> bool:
        """Position is confirmed flat: cancel leftovers, verify, then CLOSED.

        Cancelling the remaining orders is not optional — a resting entry order
        left behind after a close would silently reopen the position (brief 11).
        """
        self.store.append_event(
            EventType.POSITION_CONFIRMED_FLAT,
            f"{symbol} confirmed flat by the exchange",
            trade_id=trade_id,
        )

        for row in self.store.orders_for_trade(trade_id):
            if not OrderStatus(row["status"]).is_final and row["exchange_order_id"]:
                if self.client.cancel_order(row["exchange_order_id"]):
                    self.store.update_order(row["id"], status=OrderStatus.CANCELLED)
                    self.store.append_event(
                        EventType.ORDER_CANCELLED,
                        f"cancelled leftover {row['purpose']} order",
                        trade_id=trade_id,
                        order_id=row["id"],
                    )

        leftovers = [
            o
            for o in self.client.get_open_orders()
            if o.symbol == symbol and (o.client_order_id or "").startswith(
                self.store.get_trade(trade_id)["trade_uid"]
            )
        ]
        if leftovers:
            self.store.append_event(
                EventType.RECONCILE_MISMATCH,
                f"{len(leftovers)} order(s) still resting after close; refusing to "
                "mark CLOSED",
                trade_id=trade_id,
                severity="ERROR",
            )
            self._enter_safe_error(
                trade_id,
                f"{len(leftovers)} order(s) still resting after a confirmed-flat "
                "close; refusing to mark CLOSED",
                symbol=symbol,
            )
            return False

        state = TradeState(self.store.get_trade(trade_id)["state"])
        if state is not TradeState.CLOSING:
            self.store.transition_trade(trade_id, TradeState.CLOSING)
        self.store.transition_trade(trade_id, TradeState.CLOSED)
        self.store.append_event(
            EventType.TRADE_CLOSED,
            "trade closed: position flat and no orders remain",
            trade_id=trade_id,
        )
        # Realised P&L was never computed anywhere, so every close reported
        # no P&L and the daily summary always read $0.00. Derive it from the
        # confirmed CLOSE fills against the recorded average entry: both are
        # exchange-confirmed numbers, not intent.
        trade = self.store.get_trade(trade_id)
        pnl = self._realized_pnl(trade_id, trade)
        if pnl is not None:
            self.store.update_trade_actuals(trade_id, realized_pnl=pnl)
            trade = self.store.get_trade(trade_id)

        notify_trade_event(
            self.config, "closed",
            symbol=symbol, side=trade["direction"],
            size=trade["actual_quantity"], pnl=trade["realized_pnl"],
            detail="position flat, no orders remain",
        )
        return True

    def _realized_pnl(self, trade_id: int, trade) -> Decimal | None:
        """(exit - entry) x size, signed by direction. None when unknowable.

        Returns None rather than 0 when the inputs are missing: a confident
        zero reads as breakeven, which is a different claim from "we could
        not work it out".
        """
        entry = trade["actual_avg_entry_price"]
        if not entry:
            return None
        total_size = Decimal(0)
        weighted = Decimal(0)
        # Every way a position can LEAVE, not just an explicit close. A
        # stop-out or a TP fill exits the position without any CLOSE order,
        # and those are precisely the closes worth accounting for -- looking
        # only at CLOSE returned None for a real stopped-out trade.
        exit_rows = [
            row
            for purpose in (OrderPurpose.CLOSE, OrderPurpose.STOP_LOSS,
                            OrderPurpose.TAKE_PROFIT)
            for row in self.store.orders_for_trade(trade_id, purpose)
        ]
        for row in exit_rows:
            observed = self.observer.observe_order(row["client_order_id"])
            if not observed.filled_size or not observed.avg_fill_price:
                continue
            total_size += Decimal(str(observed.filled_size))
            weighted += (Decimal(str(observed.avg_fill_price))
                         * Decimal(str(observed.filled_size)))
        if total_size <= 0:
            return None
        exit_price = weighted / total_size
        move = exit_price - Decimal(str(entry))
        if trade["direction"] == Direction.SHORT.value:
            move = -move
        return (move * total_size).quantize(Decimal("0.01"))

    def cancel_remaining_entries(self, trade_id: int) -> int:
        """Cancel resting entry orders (brief 11: never leave stale entries)."""
        cancelled = 0
        for row in self.store.orders_for_trade(trade_id, OrderPurpose.ENTRY):
            if OrderStatus(row["status"]).is_final or not row["exchange_order_id"]:
                continue
            observed = self.observer.observe_order(row["client_order_id"])
            if not observed.resting:
                continue
            if self.client.cancel_order(row["exchange_order_id"]):
                self.store.update_order(row["id"], status=OrderStatus.CANCELLED)
                self.store.append_event(
                    EventType.ORDER_CANCELLED,
                    f"cancelled resting entry {row['sequence']}",
                    trade_id=trade_id,
                    order_id=row["id"],
                )
                cancelled += 1
        return cancelled

    def expire_stale_entries(self, trade_id: int, now_iso: str) -> int:
        """Cancel entry orders past their lifetime (brief 12: max 24h)."""
        expired = 0
        for row in self.store.orders_for_trade(trade_id, OrderPurpose.ENTRY):
            if OrderStatus(row["status"]).is_final or not row["expires_at"]:
                continue
            if row["expires_at"] <= now_iso and row["exchange_order_id"]:
                if self.client.cancel_order(row["exchange_order_id"]):
                    self.store.update_order(row["id"], status=OrderStatus.EXPIRED)
                    expired += 1
        return expired

    def _next_stop_sequence(self, trade_id: int) -> int:
        return len(self.store.orders_for_trade(trade_id, OrderPurpose.STOP_LOSS)) + 1

    def _next_close_sequence(self, trade_id: int) -> int:
        return len(self.store.orders_for_trade(trade_id, OrderPurpose.CLOSE)) + 1
