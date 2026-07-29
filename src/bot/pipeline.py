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
    EventType,
    ExecutionDecision,
    InstrumentSpec,
    RejectionReason,
    TradeState,
)
from .parser.parse import fingerprint_for, parse_message
from .sizing import SizingError

log = get_logger(__name__)


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
    ) -> None:
        self.config = config
        self.store = store
        self.admission = admission
        self.engine = engine
        self.specs = specs

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

        return self._open_from_signal(parsed, row_id)

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
