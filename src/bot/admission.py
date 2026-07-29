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
    ) -> None:
        self.config = config
        self.store = store
        self.kill_switch = kill_switch
        self.symbol_for = symbol_for or {}

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

        # 2. Only instruction types proceed.
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

        # 3. Parser could not resolve required fields.
        if parsed.parse_errors:
            return self._refuse(
                message_row_id,
                RejectionReason.MISSING_FIELDS,
                "; ".join(parsed.parse_errors),
            )

        # 4. Asset must resolve, and unambiguously.
        if parsed.asset is None:
            return self._refuse(
                message_row_id,
                RejectionReason.UNSUPPORTED_ASSET,
                "no whitelisted asset could be resolved (absent, unknown, or "
                "two different assets named)",
            )

        # 5. Whitelist. GOLD gets its own reason so the pin is visible in the
        #    audit trail rather than looking like a generic rejection.
        if not self.config.is_tradeable(parsed.asset):
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
        #    Layer 1 (channel_id, message_id) is enforced by a UNIQUE index at
        #    ingest. This catches a reposted signal that was reformatted.
        fingerprint = fingerprint_for(parsed)
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

        # 7. One active trade per symbol — Kraken nets positions, so two
        #    concurrent trades on one symbol cannot be protected independently
        #    (docs/PLAN.md 4.1).
        symbol = self.symbol_for.get(parsed.asset)
        if symbol and self.store.get_active_trade_for_symbol(symbol) is not None:
            return self._refuse(
                message_row_id,
                RejectionReason.SYMBOL_ALREADY_ACTIVE,
                f"{symbol} already has an active trade; a second position "
                "cannot be protected independently",
                decision=ExecutionDecision.REQUIRES_REVIEW,
            )

        # 8. Kill switch, checked last so earlier refusals record their real
        #    reason rather than being masked by it.
        if not self.kill_switch.allows_new_positions():
            return self._refuse(
                message_row_id,
                RejectionReason.KILL_SWITCH_ACTIVE,
                "kill switch engaged; no new positions",
            )

        self.store.append_event(
            EventType.SIGNAL_VALIDATED,
            f"admitted {parsed.asset.value} {parsed.message_type.value}",
            message_row_id=message_row_id,
        )
        return AdmissionResult(True, ExecutionDecision.PENDING)
