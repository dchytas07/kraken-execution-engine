"""Admission control: the gate between a parsed message and a live trade.

This module exists because a sweep for unwired safety mechanisms found that
five of them — the spot boundary, the whitelist, duplicate detection, the
one-trade-per-symbol rule and decision recording — were implemented and
unit-tested but had **zero production call sites**. They had no caller because
the layer that should call them did not exist: Stage 3 built the parser, Stage
6 built the engine, and nothing joined them.

Every rejection is recorded with an enumerated reason (brief 7, 21, 40), so a
message never simply disappears. Checks run in fail-closed order, cheapest and
most absolute first.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .db.store import Store
from .killswitch import KillSwitch
from .logging_setup import get_logger
from .models import (
    CanonicalAsset,
    EventType,
    ExecutionDecision,
    MessageType,
    ParsedMessage,
    RejectionReason,
    TradeState,
)
from .parser.parse import fingerprint_for

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    admitted: bool
    decision: ExecutionDecision
    reason: RejectionReason | None = None
    detail: str = ""

    @property
    def refused(self) -> bool:
        return not self.admitted


class SignalAdmission:
    """Decides whether a parsed message may become a trade."""

    def __init__(
        self,
        *,
        config: Config,
        store: Store,
        kill_switch: KillSwitch,
        symbol_for: dict[CanonicalAsset, str] | None = None,
        position_for=None,
        exposure_for=None,
    ) -> None:
        self.config = config
        self.store = store
        self.kill_switch = kill_switch
        self.symbol_for = symbol_for or {}
        # Callable[[str], Position | None] — the LIVE exchange position for a
        # symbol. Local state alone is not sufficient: Kraken nets per symbol,
        # so a position the bot does not know about would silently merge with a
        # new trade. See admit() step 7b.
        self.position_for = position_for
        # Callable[[str], Decimal] — what the LIVE trades on a symbol say they
        # hold in total. With several trades sharing a netted position, "a
        # position exists" no longer means "an orphan"; it is acceptable only
        # when the trades account for exactly that much.
        self.exposure_for = exposure_for

    def _refuse(
        self,
        message_row_id: int,
        reason: RejectionReason,
        detail: str,
        *,
        decision: ExecutionDecision = ExecutionDecision.REJECTED,
    ) -> AdmissionResult:
        self.store.record_decision(message_row_id, decision, reason, detail)
        self.store.append_event(
            EventType.SIGNAL_REJECTED,
            f"{reason.value}: {detail}",
            message_row_id=message_row_id,
        )
        log.info(
            f"signal refused [{reason.value}] {detail}",
            extra={
                "action": "admission_refused",
                "reason": reason.value,
                "decision": decision.value,
                "message_id": message_row_id,
            },
        )
        return AdmissionResult(False, decision, reason, detail)

    def check_exchange_position(
        self, symbol: str, message_row_id: int
    ) -> AdmissionResult | None:
        """Refuse if the exchange holds a position we cannot account for.

        Returns None when it is safe to proceed. Named rather than inline so
        the wiring guard can see it — an unwired version of this check is what
        allowed opening into an orphan position.

        A position is accounted for only if the live trades on the symbol hold,
        between them, exactly that size. No trades and a position is an orphan;
        trades that explain less (or more) than the exchange holds are a
        divergence. Either way opening would net a new trade into exposure the
        bot cannot attribute, so it is refused. If the accounting cannot be
        done at all it is refused too (brief 40).
        """
        if self.position_for is None:
            return None
        try:
            position = self.position_for(symbol)
        except Exception as exc:
            # Cannot verify -> cannot trade (brief 40).
            return self._refuse(
                message_row_id,
                RejectionReason.POSITION_CHECK_FAILED,
                f"could not read the live {symbol} position: {exc}",
            )
        if position is None or position.is_flat:
            return None

        active = self.store.active_trades_for_symbol(symbol)
        tracked = None
        if active and self.exposure_for is not None:
            try:
                tracked = self.exposure_for(symbol)
            except Exception as exc:
                return self._refuse(
                    message_row_id,
                    RejectionReason.POSITION_CHECK_FAILED,
                    f"could not work out what the live {symbol} trades hold: {exc}",
                )
        if active and tracked is not None and position.size == tracked:
            return None

        log.error(
            f"refusing {symbol}: the exchange holds {position.size} but "
            + (f"the {len(active)} live trade(s) account for {tracked}"
               if tracked is not None else "no local trade accounts for it"),
            extra={"symbol": symbol, "action": "unrecognised_position"},
        )
        return self._refuse(
            message_row_id,
            RejectionReason.UNRECOGNISED_POSITION,
            f"{symbol} holds {position.size} on the exchange but "
            + (f"the live trades on it account for {tracked}"
               if tracked is not None else "no matching local trade")
            + "; opening would net into exposure the bot cannot attribute",
            decision=ExecutionDecision.REQUIRES_REVIEW,
        )

    def check_total_margin(self, message_row_id: int) -> AdmissionResult | None:
        """MAX_TOTAL_OPEN_MARGIN: refuse a trade that would exceed it.

        Counts every live trade's margin, including trades still waiting for
        entry (their orders will commit margin the moment they fill). Fails
        closed three ways: not configured, not computable, or exceeded.
        """
        limit = self.config.max_total_open_margin
        if limit is None:
            return self._refuse(
                message_row_id,
                RejectionReason.TOTAL_MARGIN_LIMIT,
                "MAX_TOTAL_OPEN_MARGIN is not configured, so no new trade may "
                "open (fail closed)",
                decision=ExecutionDecision.REQUIRES_REVIEW,
            )
        try:
            committed = self.store.total_open_margin()
        except Exception as exc:
            return self._refuse(
                message_row_id,
                RejectionReason.TOTAL_MARGIN_LIMIT,
                f"could not work out the margin already committed: {exc}",
                decision=ExecutionDecision.REQUIRES_REVIEW,
            )
        new = self.config.trade_margin_usd
        if committed + new > limit:
            return self._refuse(
                message_row_id,
                RejectionReason.TOTAL_MARGIN_LIMIT,
                f"${committed} is already committed and this trade needs ${new}; "
                f"MAX_TOTAL_OPEN_MARGIN is ${limit}",
                decision=ExecutionDecision.REQUIRES_REVIEW,
            )
        return None

    def admit(
        self, parsed: ParsedMessage, message_row_id: int
    ) -> AdmissionResult:
        """Run every admission check.

        Returns admitted=False with a reason, or admitted=True meaning a trade
        may be created.

        ┌─ ADMISSION FUNNEL ──────────────────────────────────────────────┐
        │ EVERY inbound instruction MUST pass through here before it can  │
        │ create or modify a trade — including instruction types not yet  │
        │ handled: SL_UPDATE, ENTRY_UPDATE, ADD_POSITION, PARTIAL_CLOSE   │
        │ and the CLOSE_* family.                                         │
        │                                                                 │
        │ Do NOT add a second path that reaches the engine directly. The  │
        │ spot boundary (brief 8), whitelist (brief 4), duplicate         │
        │ detection (brief 23), one-trade-per-symbol and decision         │
        │ recording (brief 7) all live here. A bypass re-creates the bug  │
        │ where five safety mechanisms existed with no caller.            │
        │                                                                 │
        │ New instruction types extend THIS method. Order construction    │
        │ then happens in ExecutionEngine — never in between.             │
        └─────────────────────────────────────────────────────────────────┘
        """

        # Which checks apply depends on what the instruction DOES.
        #
        # This scoping was originally absent, and three checks fired on
        # instructions they had no business blocking:
        #   * the kill switch refused CLOSES — directly contrary to brief 25,
        #     which requires it to stop opening while exits keep working
        #   * symbol occupancy refused a close on an occupied symbol, when an
        #     occupied symbol is the whole precondition for closing
        #   * asset resolution refused CLOSE_ALL, which by nature names none
        opens_exposure = parsed.message_type in (
            MessageType.NEW_SIGNAL, MessageType.ADD_POSITION
        )
        needs_asset = opens_exposure or parsed.message_type is MessageType.CLOSE_SYMBOL

        # 1. Spot boundary FIRST (brief 8). Structural and unconditional: a
        #    spot message must never reach the leveraged path, whatever else it
        #    carries.
        if parsed.message_type is MessageType.SPOT_SIGNAL:
            return self._refuse(
                message_row_id,
                RejectionReason.SPOT_SIGNAL,
                "spot/accumulation signal; never enters the leveraged pipeline",
                decision=ExecutionDecision.IGNORED,
            )

        # 2. A named-but-unsupported asset reports that specifically (brief 4:
        #    log it, record it, make the reason visible). Checked before the
        #    type check, because such a message classifies as UNKNOWN and would
        #    otherwise be refused with the uninformative NOT_EXECUTABLE_TYPE.
        from .parser.aliases import is_known_other_asset
        from .parser.normalize import basic_normalize

        if parsed.asset is None and is_known_other_asset(
            basic_normalize(parsed.raw_text)
        ):
            return self._refuse(
                message_row_id,
                RejectionReason.UNSUPPORTED_ASSET,
                "names a real asset that is not whitelisted",
            )

        # 3. An incomplete signal reports the missing fields, for the same
        #    reason: it classifies as UNKNOWN, so the type check would mask it.
        if parsed.parse_errors:
            return self._refuse(
                message_row_id,
                RejectionReason.MISSING_FIELDS,
                "; ".join(parsed.parse_errors),
            )

        # 4. Only instruction types proceed.
        if not parsed.message_type.is_executable_candidate:
            return self._refuse(
                message_row_id,
                RejectionReason.NOT_EXECUTABLE_TYPE,
                f"message type {parsed.message_type.value} is not executable",
                decision=(
                    ExecutionDecision.REQUIRES_REVIEW
                    if parsed.message_type is MessageType.SAFETY_REVIEW
                    else ExecutionDecision.IGNORED
                ),
            )

        # 5. Asset must resolve, and unambiguously — for instructions that
        #    name an instrument. CLOSE_ALL / CLOSE_DIRECTION / CLOSE_REPLY are
        #    scoped by other means.
        if needs_asset and parsed.asset is None:
            return self._refuse(
                message_row_id,
                RejectionReason.UNSUPPORTED_ASSET,
                "no whitelisted asset could be resolved (absent, unknown, or "
                "two different assets named)",
            )

        # 5. Whitelist. GOLD gets its own reason so the pin is visible in the
        #    audit trail rather than looking like a generic rejection.
        if parsed.asset is not None and not self.config.is_tradeable(parsed.asset):
            if parsed.asset is CanonicalAsset.XAU:
                return self._refuse(
                    message_row_id,
                    RejectionReason.GOLD_PINNED_OFF,
                    "GOLD/XAU is disabled: Kraken lists no spot-gold perpetual "
                    "and the XAUT substitution failed its tracking study "
                    "(docs/KRAKEN_NOTES.md 1B)",
                )
            return self._refuse(
                message_row_id,
                RejectionReason.UNSUPPORTED_INSTRUMENT,
                f"{parsed.asset.value} is not in the tradeable set",
            )

        # 6. Duplicate detection (brief 23), layer 2: content fingerprint.
        #    Only a new signal can be a duplicate SIGNAL; a repeated close is
        #    handled by message-id idempotency.
        #    Layer 1 (channel_id, message_id) is enforced by a UNIQUE index at
        #    ingest. This catches a reposted signal that was reformatted.
        fingerprint = fingerprint_for(parsed) if opens_exposure else None
        if fingerprint:
            prior = [
                row
                for row in self.store.find_by_fingerprint(fingerprint)
                if row["id"] != message_row_id
                and row["execution_decision"] == ExecutionDecision.EXECUTED.value
            ]
            if prior:
                return self._refuse(
                    message_row_id,
                    RejectionReason.DUPLICATE_SIGNAL,
                    f"same parsed levels as already-executed message "
                    f"{prior[0]['channel_id']}/{prior[0]['message_id']}",
                    decision=ExecutionDecision.DUPLICATE,
                )

        # 7a. Trades per symbol, from LOCAL state. Kraken nets positions per
        #     symbol, so extra trades SHARE the exchange position: up to
        #     MAX_TRADES_PER_SYMBOL, same direction only. An opposite trade
        #     would shrink or flip the existing one instead of sitting beside
        #     it, so it opens nothing and a human is told.
        symbol = self.symbol_for.get(parsed.asset) if parsed.asset else None
        if opens_exposure and symbol:
            active = self.store.active_trades_for_symbol(symbol)
            opposite = [
                t for t in active
                if parsed.direction is not None
                and t["direction"] != parsed.direction.value
            ]
            if opposite:
                return self._refuse(
                    message_row_id,
                    RejectionReason.OPPOSITE_DIRECTION_ACTIVE,
                    f"{symbol} has an active {opposite[0]['direction']} "
                    f"(trade #{opposite[0]['id']}); this signal goes "
                    f"{parsed.direction.value if parsed.direction else '?'}. "
                    "Kraken nets positions per symbol, so it would shrink or "
                    "flip that trade rather than open beside it",
                    decision=ExecutionDecision.REQUIRES_REVIEW,
                )
            # A quarantined trade holds exposure nobody has resolved. A new trade
            # would net into it, so the symbol takes no new exposure until a
            # human has dealt with that one.
            unresolved = [
                t for t in active
                if TradeState(t["state"]) in (TradeState.REQUIRES_REVIEW,
                                              TradeState.SAFE_ERROR)
            ]
            if unresolved:
                return self._refuse(
                    message_row_id,
                    RejectionReason.SYMBOL_ALREADY_ACTIVE,
                    f"{symbol} has trade #{unresolved[0]['id']} in "
                    f"{unresolved[0]['state']}; no new exposure is opened on a "
                    "symbol with an unresolved trade",
                    decision=ExecutionDecision.REQUIRES_REVIEW,
                )
            if len(active) >= self.config.max_trades_per_symbol:
                return self._refuse(
                    message_row_id,
                    RejectionReason.SYMBOL_ALREADY_ACTIVE,
                    f"{symbol} already has {len(active)} active trade(s); the "
                    f"limit is {self.config.max_trades_per_symbol}",
                    decision=ExecutionDecision.REQUIRES_REVIEW,
                )

        # 7a'. Total committed margin, across every symbol. FAILS CLOSED: an
        #      unconfigured limit, or one that cannot be evaluated, refuses.
        if opens_exposure:
            refusal = self.check_total_margin(message_row_id)
            if refusal is not None:
                return refusal

        # 7b. ...and from the EXCHANGE. Local state is not enough.
        #
        #     An orphan position — one the reconciler detects and deliberately
        #     refuses to auto-close — has no local trade, so 7a would pass it.
        #     Opening into it would net the two together, and every subsequent
        #     decision would be wrong in both directions: the stop and TP ladder
        #     would be sized against the COMBINED position (so the bot would
        #     manage, and eventually close, a position it did not open), while
        #     breakeven would be computed from OUR fills only (so the level
        #     would be wrong for the merged position).
        #
        #     Refuse instead. An operator's manual position is not ours to
        #     absorb.
        if opens_exposure and symbol:
            refusal = self.check_exchange_position(symbol, message_row_id)
            if refusal is not None:
                return refusal

        # 8. Kill switch — blocks OPENING only. Brief 25 is explicit: stop
        #    opening new positions, continue managing existing protective stops
        #    and exits. Refusing a close here would trap an operator in a
        #    position they were trying to get out of.
        if opens_exposure and not self.kill_switch.allows_new_positions():
            return self._refuse(
                message_row_id,
                RejectionReason.KILL_SWITCH_ACTIVE,
                "kill switch engaged; no new positions",
            )

        self.store.append_event(
            EventType.SIGNAL_VALIDATED,
            f"admitted {parsed.asset.value if parsed.asset else '—'} "
            f"{parsed.message_type.value}",
            message_row_id=message_row_id,
        )
        return AdmissionResult(True, ExecutionDecision.PENDING)
