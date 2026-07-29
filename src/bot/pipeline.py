"""Message -> trade pipeline.

The orchestration seam: Telegram (Stage 9) hands raw messages here, and this
drives parse -> persist -> admit -> create -> plan -> open. It exists so the
admission gate has a caller on the real path; without it, admission was a
correct, well-tested component that nothing invoked.
"""

from __future__ import annotations

from dataclasses import dataclass

from .admission import AdmissionResult, SignalAdmission
from .config import Config
from .db.store import DuplicateMessage, Store, SymbolAlreadyActive
from .execution.engine import ExecutionEngine, ExecutionRefused
from .logging_setup import get_logger
from .models import (
    CanonicalAsset,
    Direction,
    MessageType,
    EventType,
    ExecutionDecision,
    InstrumentSpec,
    RejectionReason,
    TradeState,
)
from .execution.manager import TradeManager
from .parser.parse import fingerprint_for, parse_message
from .sizing import SizingError

log = get_logger(__name__)


def build_admission(
    *,
    config: Config,
    store: Store,
    kill_switch,
    client,
    specs: dict[CanonicalAsset, InstrumentSpec],
) -> SignalAdmission:
    """Construct the admission gate with BOTH state sources wired.

    The single production construction point. `position_for` must be supplied:
    local state alone cannot see an orphan position, and Kraken nets per symbol,
    so opening into one would silently merge the two.
    """
    return SignalAdmission(
        config=config,
        store=store,
        kill_switch=kill_switch,
        symbol_for={asset: spec.symbol for asset, spec in specs.items()},
        position_for=client.get_position,
    )


@dataclass(frozen=True, slots=True)
class IngestResult:
    accepted: bool
    message_row_id: int | None
    trade_id: int | None = None
    detail: str = ""


class SignalPipeline:
    def __init__(
        self,
        *,
        config: Config,
        store: Store,
        admission: SignalAdmission,
        engine: ExecutionEngine,
        specs: dict[CanonicalAsset, InstrumentSpec],
        manager: TradeManager | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.admission = admission
        self.engine = engine
        self.specs = specs
        self.manager = manager

    def handle_message(
        self,
        *,
        channel_id: int,
        message_id: int,
        text: str,
        timestamp: float = 0.0,
        reply_to_message_id: int | None = None,
    ) -> IngestResult:
        """Full path for one inbound message.

        Persist-first: the raw message is stored before any parsing, so a crash
        mid-processing still leaves an auditable record of what arrived.
        """
        # Idempotency layer 1 (brief 5). Checked cheaply, then enforced by a
        # UNIQUE index — the check avoids a pointless exception on the common
        # replay path, the index is what actually guarantees it.
        if self.store.has_message(channel_id, message_id):
            log.debug(
                "message already processed; skipping",
                extra={"channel_id": channel_id, "message_id": message_id,
                       "action": "duplicate_message"},
            )
            return IngestResult(False, None, detail="already processed")

        try:
            row_id = self.store.record_message(
                channel_id=channel_id,
                message_id=message_id,
                raw_text=text,
                reply_to_message_id=reply_to_message_id,
            )
        except DuplicateMessage:
            return IngestResult(False, None, detail="already processed (race)")

        self.store.append_event(
            EventType.MESSAGE_RECEIVED, "message received", message_row_id=row_id
        )

        parsed = parse_message(
            channel_id=channel_id,
            message_id=message_id,
            text=text,
            timestamp=timestamp,
            reply_to_message_id=reply_to_message_id,
        )
        self.store.update_message_parse(
            row_id,
            parsed_type=parsed.message_type.value,
            parsed_asset=parsed.asset.value if parsed.asset else None,
            parsed_direction=parsed.direction.value if parsed.direction else None,
            parsed_values={
                "entry_range": [str(p) for p in parsed.entry_range],
                "take_profits": [str(p) for p in parsed.take_profits],
                "stop_loss": str(parsed.stop_loss) if parsed.stop_loss else None,
                "leverage": str(parsed.leverage) if parsed.leverage else None,
                "percentage": str(parsed.percentage) if parsed.percentage else None,
            },
            confidence=parsed.confidence,
            warnings=[{"code": w.code, "detail": w.detail} for w in parsed.parse_warnings],
            errors=list(parsed.parse_errors),
            signal_fingerprint=fingerprint_for(parsed),
        )
        self.store.append_event(
            EventType.MESSAGE_PARSED,
            f"parsed as {parsed.message_type.value}",
            message_row_id=row_id,
        )

        verdict: AdmissionResult = self.admission.admit(parsed, row_id)
        if verdict.refused:
            return IngestResult(False, row_id, detail=verdict.detail)

        if parsed.message_type is MessageType.NEW_SIGNAL:
            return self._open_from_signal(parsed, row_id)
        if parsed.message_type.is_exit_like:
            return self._handle_exit(parsed, row_id)

        # Admitted instruction types with no handler yet (SL_UPDATE,
        # ENTRY_UPDATE, ADD_POSITION). Recorded for review rather than silently
        # dropped, and explicitly NOT executed (brief 19, 20).
        self.store.record_decision(
            row_id,
            ExecutionDecision.REQUIRES_REVIEW,
            RejectionReason.NOT_EXECUTABLE_TYPE,
            f"{parsed.message_type.value} is recognised but not yet executable; "
            "flagged for manual review",
        )
        return IngestResult(False, row_id, detail="instruction type not implemented")

    def _handle_exit(self, parsed, row_id: int) -> IngestResult:
        """Route a close-family instruction (brief 17, 18, 21).

        The target must be determinable. If it is not, nothing closes —
        a wrong close is worse than a missed one.
        """
        if self.manager is None:
            self.store.record_decision(
                row_id, ExecutionDecision.REQUIRES_REVIEW,
                RejectionReason.NOT_EXECUTABLE_TYPE,
                "no trade manager wired; close not executed",
            )
            return IngestResult(False, row_id, detail="no manager")

        symbol = None
        if parsed.asset is not None:
            spec = self.specs.get(parsed.asset)
            symbol = spec.symbol if spec else None

        targets = self.manager.resolve_close_targets(
            symbol=symbol,
            direction=parsed.direction
            if parsed.message_type is MessageType.CLOSE_DIRECTION else None,
            reply_to_message_id=parsed.referenced_message_id
            if parsed.message_type is MessageType.CLOSE_REPLY else None,
            close_all=parsed.message_type is MessageType.CLOSE_ALL,
        )

        if targets is None:
            self.store.record_decision(
                row_id, ExecutionDecision.REQUIRES_REVIEW,
                RejectionReason.AMBIGUOUS_CLOSE_TARGET,
                "close instruction could not be tied to a specific trade; "
                "refusing to guess (brief 21)",
            )
            log.error(
                "ambiguous close refused: target could not be determined",
                extra={"message_id": row_id, "action": "ambiguous_close"},
            )
            return IngestResult(False, row_id, detail="ambiguous close target")

        if not targets:
            # Guard against the failure this replaced: reporting "nothing to
            # close" while the exchange holds a position is a false all-clear.
            live = [f"{p.symbol} {p.size}"
                    for p in self.engine.client.get_open_positions()
                    if not p.is_flat]
            if live:
                log.error(
                    f"close matched no trade, but the exchange holds "
                    f"{', '.join(live)} — flagging rather than reporting "
                    "'nothing to close'",
                    extra={"action": "close_no_target_but_exposure"},
                )
                self.store.record_decision(
                    row_id, ExecutionDecision.REQUIRES_REVIEW,
                    RejectionReason.AMBIGUOUS_CLOSE_TARGET,
                    f"no trade matched, but exposure remains: {', '.join(live)}",
                )
                return IngestResult(False, row_id,
                                    detail=f"unmatched exposure: {live}")
            self.store.record_decision(
                row_id, ExecutionDecision.IGNORED, None,
                "no open trades matched the close instruction",
            )
            return IngestResult(False, row_id, detail="nothing to close")

        percentage = (
            parsed.percentage
            if parsed.message_type is MessageType.PARTIAL_CLOSE else None
        )
        outcomes = self.manager.close_trades(targets, percentage=percentage)
        confirmed = [t for t, ok in outcomes.items() if ok]

        # Exposure this instruction did NOT close. An operator reading "close
        # applied" must never infer "everything is flat" while positions remain
        # — orphans are deliberately not auto-closed, and a close can fail.
        remaining = [
            f"{p.symbol} {p.size}" for p in self.engine.client.get_open_positions()
            if not p.is_flat
        ]
        detail = (f"close applied to {len(targets)} trade(s); "
                  f"{len(confirmed)} confirmed flat")
        if remaining:
            detail += f"; STILL OPEN: {', '.join(remaining)}"
            log.error(
                f"close instruction complete but exposure remains: "
                f"{', '.join(remaining)}",
                extra={"action": "close_incomplete"},
            )
        self.store.record_decision(
            row_id, ExecutionDecision.EXECUTED, None, detail,
        )
        return IngestResult(
            True, row_id,
            trade_id=targets[0] if targets else None,
            detail=f"{len(confirmed)}/{len(targets)} confirmed flat",
        )

    def _open_from_signal(self, parsed, row_id: int) -> IngestResult:
        spec = self.specs.get(parsed.asset)
        if spec is None:
            self.store.record_decision(
                row_id,
                ExecutionDecision.REJECTED,
                RejectionReason.INSTRUMENT_SPEC_UNAVAILABLE,
                f"no loaded spec for {parsed.asset.value}",
            )
            return IngestResult(False, row_id, detail="no instrument spec")

        try:
            trade_id, trade_uid = self.store.create_trade(
                origin_message_row_id=row_id,
                origin_channel_id=parsed.channel_id,
                origin_message_id=parsed.message_id,
                asset=parsed.asset.value,
                symbol=spec.symbol,
                direction=parsed.direction.value,
                intent_entry_prices=list(parsed.entry_range),
                intent_take_profits=list(parsed.take_profits),
                intent_stop_loss=parsed.stop_loss,
                intent_margin_usd=self.config.trade_margin_usd,
                intent_leverage=parsed.leverage,
                is_substitute_instrument=spec.is_substitute,
                substitute_note=spec.substitute_note or None,
            )
        except SymbolAlreadyActive as exc:
            # Belt and braces: admission checks this, and the DB enforces it
            # against a concurrent writer.
            self.store.record_decision(
                row_id,
                ExecutionDecision.REQUIRES_REVIEW,
                RejectionReason.SYMBOL_ALREADY_ACTIVE,
                str(exc),
            )
            return IngestResult(False, row_id, detail=str(exc))

        self.store.append_event(
            EventType.TRADE_CREATED, f"trade {trade_uid} created",
            trade_id=trade_id, message_row_id=row_id,
        )
        self.store.transition_trade(trade_id, TradeState.PARSED)
        self.store.transition_trade(trade_id, TradeState.VALIDATED)

        try:
            plan = self.engine.plan_entries(
                spec=spec,
                direction=Direction(parsed.direction.value),
                entry_prices=parsed.entry_range,
                signal_leverage=parsed.leverage,
            )
            self.engine.open_trade(
                trade_id=trade_id, trade_uid=trade_uid, spec=spec, plan=plan
            )
        except (ExecutionRefused, SizingError) as exc:
            reason = _reason_for(exc)
            self.store.record_decision(
                row_id, ExecutionDecision.REJECTED, reason, str(exc)
            )
            self.store.transition_trade(
                trade_id, TradeState.CANCELLED, f"not opened: {exc}"
            )
            self.store.append_event(
                EventType.SIGNAL_REJECTED, str(exc),
                trade_id=trade_id, message_row_id=row_id, severity="ERROR",
            )
            log.warning(
                f"trade {trade_uid} not opened: {exc}",
                extra={"trade_id": trade_id, "action": "open_refused"},
            )
            return IngestResult(False, row_id, trade_id, str(exc))

        # Same-cycle protection: an entry that filled immediately must be
        # protected now, not on the next reconciliation cycle.
        if self.manager is not None:
            self.manager.step(trade_id)

        self.store.record_decision(row_id, ExecutionDecision.EXECUTED)
        return IngestResult(True, row_id, trade_id)


def _reason_for(exc: Exception) -> RejectionReason:
    code = getattr(exc, "reason", "") or ""
    return {
        "STALE_ENTRY": RejectionReason.STALE_ENTRY,
        "KILL_SWITCH_ACTIVE": RejectionReason.KILL_SWITCH_ACTIVE,
        "INSUFFICIENT_MARGIN": RejectionReason.INSUFFICIENT_MARGIN,
        "SIZE_BELOW_MINIMUM": RejectionReason.SIZE_BELOW_MINIMUM,
        "SIZING_DEVIATION": RejectionReason.SIZING_DEVIATION,
        "LEVERAGE_INVALID": RejectionReason.LEVERAGE_INVALID,
        "LEVERAGE_EXCEEDS_CAP": RejectionReason.LEVERAGE_INVALID,
    }.get(code, RejectionReason.SIZING_FAILED)
