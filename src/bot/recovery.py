"""Restart recovery.

After a crash, local state is by definition the least trustworthy thing
available — it is exactly as current as the last write that completed. So
recovery reconstructs from the exchange and treats the database as a record of
*intent*, never of fact.

## The crash window

`record_order_intent` writes a row with status INTENT before the exchange is
called, keyed by a deterministic client order id. That ordering makes the
dangerous sequence recoverable:

    bot sends order -> exchange accepts -> bot dies before recording the id
    bot restarts -> finds an INTENT row -> asks the exchange about that id

Without the intent row the restarted bot would see nothing and resubmit,
duplicating a live order (brief 30).

## Resolving an intent

A client order id is looked up in the snapshot's open orders and fills:

  resting            -> adopt the exchange id, the order is live
  in fills           -> it filled; record the fill
  in neither, flat   -> it never reached the exchange; safe to abandon
  in neither, position exists -> AMBIGUOUS

The last case is the one that must not be guessed. Kraken's fills history is
bounded, so "not in recent fills" is not proof an order never filled — and a
position exists that something must account for. Those go to REQUIRES_REVIEW.

## The gate

`ExecutionEngine` refuses to open any trade until recovery has completed
successfully. Recovering *after* placing orders would defeat the point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from .alerting import send_alert
from .config import Config
from .db.store import Store
from .exchange.base import ExchangeClient, ExchangeError
from .execution.engine import ExecutionEngine
from .logging_setup import get_logger
from .models import EventType, OrderPurpose, OrderStatus, TradeState
from .reconcile import ExchangeSnapshot, Reconciler
from .sizing import SizingError, check_sufficient_margin

log = get_logger(__name__)


class IntentOutcome(StrEnum):
    ADOPTED_RESTING = "ADOPTED_RESTING"
    ADOPTED_FILLED = "ADOPTED_FILLED"
    NEVER_PLACED = "NEVER_PLACED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class ResolvedIntent:
    client_order_id: str
    purpose: str
    outcome: IntentOutcome
    detail: str = ""


@dataclass(slots=True)
class RecoveryResult:
    completed: bool = False
    trades_examined: int = 0
    intents_resolved: list[ResolvedIntent] = field(default_factory=list)
    orphan_positions: list[str] = field(default_factory=list)
    unprotected_repaired: int = 0
    trades_needing_review: list[int] = field(default_factory=list)
    blocking_reason: str = ""

    @property
    def ambiguous(self) -> list[ResolvedIntent]:
        return [i for i in self.intents_resolved if i.outcome is IntentOutcome.AMBIGUOUS]

    @property
    def safe_to_trade(self) -> bool:
        return self.completed and not self.blocking_reason


class RecoveryManager:
    def __init__(
        self,
        *,
        config: Config,
        store: Store,
        client: ExchangeClient,
        engine: ExecutionEngine,
        reconciler: Reconciler,
        specs: dict,
    ) -> None:
        self.config = config
        self.store = store
        self.client = client
        self.engine = engine
        self.reconciler = reconciler
        self.specs = specs
        self._symbols = [s.symbol for s in specs.values()]

    def recover(self) -> RecoveryResult:
        """Reconstruct state and decide whether trading may resume."""
        result = RecoveryResult()
        self.store.append_event(EventType.STARTUP, "recovery started")

        # 1. One consistent capture. Everything below reads from it, so
        #    recovery cannot be confused by state moving mid-pass.
        try:
            snapshot = ExchangeSnapshot.capture(self.client, self._symbols)
        except ExchangeError as exc:
            result.blocking_reason = f"could not read exchange state: {exc}"
            log.critical(
                f"RECOVERY BLOCKED: {result.blocking_reason} — refusing to trade",
                extra={"action": "recovery_blocked"},
            )
            return result

        open_trades = self.store.open_trades()
        result.trades_examined = len(open_trades)

        # 2. Resolve the crash window for every unacknowledged order.
        for trade in open_trades:
            for resolved in self._resolve_trade_intents(trade, snapshot):
                result.intents_resolved.append(resolved)
                if resolved.outcome is IntentOutcome.AMBIGUOUS:
                    if trade["id"] not in result.trades_needing_review:
                        result.trades_needing_review.append(trade["id"])

        # 3. Re-assert leverage. Study B(b): a crash between set-leverage and
        #    the order leaves a preference that would govern the next position.
        #
        #    NOT a no-op on an open position. Live measurement (KRAKEN_NOTES
        #    1C, 2026-07-30) found set-leverage on a symbol with an open
        #    position leaves the position's size/price alone but immediately
        #    moves the account's initialMargin — a real mutation, not a safe
        #    formality. Right after a crash is exactly when margin might
        #    already be thin, so this is margin-gated: refuse the mutation
        #    if headroom is already below one trade's margin, and if the
        #    mutation itself (whose cost cannot be known beforehand) leaves
        #    headroom thin afterward, block the gate rather than stay quiet
        #    about it. Before/after margin is logged on every reassertion
        #    regardless of outcome.
        for symbol in self._symbols:
            actual = snapshot.leverage.get(symbol)
            if actual is not None and actual != self.config.max_leverage:
                before = self.client.get_account_state()
                try:
                    check_sufficient_margin(
                        self.config.trade_margin_usd, before.available_margin
                    )
                except SizingError as exc:
                    result.blocking_reason = (
                        f"refused to re-assert leverage on {symbol}: available "
                        f"margin is already thin (${before.available_margin}): {exc}"
                    )
                    log.critical(
                        f"RECOVERY BLOCKED: refusing to re-assert leverage on "
                        f"{symbol} — available margin (${before.available_margin}) "
                        "is already below one trade's margin, and set-leverage "
                        "measurably reduces it on a live position (KRAKEN_NOTES "
                        "1C); not risking a margin call during recovery",
                        extra={
                            "symbol": symbol,
                            "action": "recovery_leverage_refused",
                            "available_margin": str(before.available_margin),
                        },
                    )
                    continue

                try:
                    self.engine.reassert_leverage(symbol)
                    after = self.client.get_account_state()
                    log.warning(
                        f"recovery re-asserted leverage on {symbol}: {actual}x -> "
                        f"{self.config.max_leverage}x — available margin "
                        f"${before.available_margin} -> ${after.available_margin}",
                        extra={
                            "symbol": symbol,
                            "action": "recovery_leverage",
                            "available_margin_before": str(before.available_margin),
                            "available_margin_after": str(after.available_margin),
                        },
                    )
                    if after.available_margin < self.config.trade_margin_usd:
                        result.blocking_reason = (
                            f"leverage re-assertion on {symbol} left available "
                            f"margin (${after.available_margin}) below one "
                            f"trade's margin (${self.config.trade_margin_usd})"
                        )
                        log.critical(
                            f"RECOVERY BLOCKED: re-asserting leverage on {symbol} "
                            f"pushed available margin down to "
                            f"${after.available_margin} — refusing to permit "
                            "trading until an operator reviews the account",
                            extra={
                                "symbol": symbol,
                                "action": "recovery_leverage_thin",
                                "available_margin_after": str(after.available_margin),
                            },
                        )
                except ExchangeError as exc:
                    result.blocking_reason = (
                        f"could not re-assert leverage on {symbol}: {exc}"
                    )

        # 4. Trades whose intents could not be resolved go to review BEFORE
        #    reconciliation touches them, so it does not act on a trade whose
        #    true position is unknown. A real-time alert fires on the actual
        #    transition (not on a repeat pass over an already-quarantined
        #    trade) — see alerting.py. Best-effort: never allowed to affect
        #    the transition itself.
        reason = "unresolvable order intent after restart"
        for trade_id in result.trades_needing_review:
            row = self.store.get_trade(trade_id)
            state = TradeState(row["state"])
            if not state.is_terminal and state is not TradeState.REQUIRES_REVIEW:
                self.store.transition_trade(trade_id, TradeState.REQUIRES_REVIEW, reason)
                try:
                    send_alert(
                        self.config.alert_telegram_bot_token,
                        self.config.alert_telegram_chat_id,
                        f"REQUIRES_REVIEW trade {trade_id} ({row['symbol']}): {reason}",
                    )
                except Exception as exc:  # noqa: BLE001 — must never crash recovery
                    log.warning(
                        f"alert send raised unexpectedly: {exc}",
                        extra={"trade_id": trade_id, "action": "alert_failed"},
                    )
            log.error(
                f"trade {trade_id} needs review: an order intent could not be "
                "resolved against the exchange after restart",
                extra={"trade_id": trade_id, "action": "recovery_review"},
            )

        # 5. Full reconciliation — protection, stale entries, closed positions,
        #    orphans. Reuses Stage 7 rather than duplicating its rules.
        report = self.reconciler.run_once()
        for d in report.discrepancies:
            if d.kind.value == "ORPHAN_POSITION" and d.symbol:
                result.orphan_positions.append(d.symbol)
            if d.kind.value == "UNPROTECTED_POSITION" and d.repaired:
                result.unprotected_repaired += 1

        # 6. An unprotected position that could not be repaired blocks trading.
        unrepaired_protection = [
            d for d in report.discrepancies
            if d.kind.value == "UNPROTECTED_POSITION" and not d.repaired
        ]
        if unrepaired_protection:
            result.blocking_reason = (
                f"{len(unrepaired_protection)} position(s) remain unprotected"
            )

        result.completed = True
        self.store.append_event(
            EventType.STARTUP,
            "recovery complete",
            detail={
                "trades": result.trades_examined,
                "intents": len(result.intents_resolved),
                "ambiguous": len(result.ambiguous),
                "orphans": result.orphan_positions,
                "blocked": result.blocking_reason or None,
            },
        )

        if result.safe_to_trade:
            self.engine.mark_recovery_complete()
            log.info(
                f"recovery complete: {result.trades_examined} trade(s), "
                f"{len(result.intents_resolved)} intent(s) resolved, "
                f"{len(result.orphan_positions)} orphan position(s)",
                extra={"action": "recovery_complete"},
            )
        else:
            log.critical(
                f"RECOVERY INCOMPLETE: {result.blocking_reason} — the engine will "
                "not open new trades",
                extra={"action": "recovery_blocked"},
            )
        return result

    # ------------------------------------------------------------------

    def _resolve_trade_intents(self, trade, snapshot) -> list[ResolvedIntent]:
        """Resolve every unacknowledged order for one trade."""
        resolved: list[ResolvedIntent] = []
        symbol = trade["symbol"]
        position = snapshot.positions.get(symbol)
        has_position = position is not None and not position.is_flat

        for row in self.store.orders_for_trade(trade["id"]):
            status = OrderStatus(row["status"])
            if status.is_final:
                continue
            # Rows that already carry an exchange id and a resting status are
            # handled by ordinary reconciliation; only the crash window needs
            # this treatment.
            if status not in (OrderStatus.INTENT, OrderStatus.SUBMITTED, OrderStatus.UNKNOWN):
                continue

            coid = row["client_order_id"]
            resting = snapshot.order_by_client_id(coid)
            fills = snapshot.fills_for(coid)

            if resting is not None:
                self.store.update_order(
                    row["id"],
                    status=OrderStatus.OPEN,
                    exchange_order_id=resting.exchange_order_id,
                )
                resolved.append(ResolvedIntent(
                    coid, row["purpose"], IntentOutcome.ADOPTED_RESTING,
                    f"live on the exchange as {resting.exchange_order_id}",
                ))
                self._log_intent(trade, coid, "adopted a resting order")
                continue

            if fills:
                filled = sum((f.size for f in fills), Decimal(0))
                avg = sum((f.price * f.size for f in fills), Decimal(0)) / filled
                self.store.update_order(
                    row["id"],
                    status=(
                        OrderStatus.FILLED
                        if filled >= Decimal(row["quantity"])
                        else OrderStatus.PARTIALLY_FILLED
                    ),
                    exchange_order_id=next((f.order_id for f in fills if f.order_id), None),
                    filled_quantity=filled,
                    avg_fill_price=avg,
                )
                resolved.append(ResolvedIntent(
                    coid, row["purpose"], IntentOutcome.ADOPTED_FILLED,
                    f"filled {filled} @ {avg}",
                ))
                self._log_intent(trade, coid, f"recovered a fill of {filled}")
                continue

            # Neither resting nor filled.
            if not has_position:
                # Nothing on the exchange for this symbol at all, so the order
                # cannot have taken effect. Safe to abandon.
                self.store.update_order(
                    row["id"],
                    status=OrderStatus.CANCELLED,
                    status_detail="not on the exchange after restart; symbol flat",
                )
                resolved.append(ResolvedIntent(
                    coid, row["purpose"], IntentOutcome.NEVER_PLACED,
                    "absent from the exchange and the symbol is flat",
                ))
                continue

            # A position exists that something must account for, and fills
            # history is bounded, so absence is not proof. Do not guess.
            self.store.update_order(
                row["id"],
                status=OrderStatus.UNKNOWN,
                status_detail="unresolvable after restart; position exists",
            )
            resolved.append(ResolvedIntent(
                coid, row["purpose"], IntentOutcome.AMBIGUOUS,
                f"absent from open orders and recent fills, but {symbol} holds "
                f"{position.size} — cannot determine whether it executed",
            ))
            log.error(
                f"AMBIGUOUS ORDER after restart: {coid} is absent from the "
                f"exchange but {symbol} holds {position.size}",
                extra={"trade_id": trade["id"], "symbol": symbol,
                       "action": "ambiguous_intent"},
            )
        return resolved

    def _log_intent(self, trade, coid: str, what: str) -> None:
        self.store.append_event(
            EventType.RECONCILE_REPAIRED,
            f"recovery {what}: {coid}",
            trade_id=trade["id"],
        )
