"""Message -> trade pipeline.

The orchestration seam: Telegram (Stage 9) hands raw messages here, and this
drives parse -> persist -> admit -> create -> plan -> open. It exists so the
admission gate has a caller on the real path; without it, admission was a
correct, well-tested component that nothing invoked.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from .admission import AdmissionResult, SignalAdmission
from .alerting import notify_trade_event, send_review_alert
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
    MessageAction,
    MessageClass,
    MessageType,
    RejectionReason,
    TradeState,
)
from .execution.manager import TradeManager
from .parser.message_class import Classification, classify_message
from .parser.parse import fingerprint_for, parse_message
from .sizing import SizingError

log = get_logger(__name__)


def _log_observation(parsed, raw_text: str, cls: Classification | None = None) -> None:
    """Emit a readable per-message block: raw text, parse, then decision.

    Operator-facing only — nothing here affects control flow. Exists because
    "message 123 handled: accepted=False" tells you a signal was refused but
    not what it parsed as or why, which is precisely what you need when
    watching a live channel to judge whether the parser is right.
    """
    # Blank lines are dropped: this channel separates every field with one,
    # so keeping them would burn the display budget and truncate the stop
    # loss and leverage — the two fields you most need to eyeball.
    lines = [ln for ln in (raw_text or "").strip().splitlines() if ln.strip()]
    log.info("─" * 60, extra={"action": "observe_start"})
    log.info("  RAW         :", extra={"action": "observe_raw"})
    for line in lines[:14]:
        log.info(f"    | {line}", extra={"action": "observe_raw"})
    if len(lines) > 14:
        log.info(f"    | … {len(lines) - 14} more line(s)",
                 extra={"action": "observe_raw"})

    log.info(
        f"  PARSED      : {parsed.message_type.value} "
        f"(confidence {parsed.confidence})",
        extra={"action": "observe_parse"},
    )
    bits = [
        ("asset", parsed.asset.value if parsed.asset else None),
        ("direction", parsed.direction.value if parsed.direction else None),
        ("entry", ", ".join(str(p) for p in parsed.entry_range) or None),
        ("take profits", ", ".join(str(p) for p in parsed.take_profits) or None),
        ("stop loss", str(parsed.stop_loss) if parsed.stop_loss else None),
        ("leverage", f"{parsed.leverage}x" if parsed.leverage else None),
        ("percentage", str(parsed.percentage) if parsed.percentage else None),
    ]
    for label, value in bits:
        if value:
            log.info(f"    {label:<13}: {value}", extra={"action": "observe_parse"})
    if cls is not None:
        log.info(
            f"  CLASS       : {cls.message_class.value} -> {cls.action.value} "
            f"({cls.reason})",
            extra={"action": "observe_class"},
        )
    for w in parsed.parse_warnings:
        log.info(f"    note         : [{w.code}] {w.detail}",
                 extra={"action": "observe_parse"})
    for e in parsed.parse_errors:
        log.warning(f"    error        : {e}", extra={"action": "observe_parse"})


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
    # What the message was and what the bot did about it (persisted per
    # version in message_versions).
    message_class: str | None = None
    action: str | None = None
    version: int | None = None


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

    # ------------------------------------------------------------------
    # Intake: new messages and edits
    # ------------------------------------------------------------------

    def handle_message(
        self,
        *,
        channel_id: int,
        message_id: int,
        text: str,
        timestamp: float = 0.0,
        reply_to_message_id: int | None = None,
        media: str | None = None,
        edit_date: str | None = None,
    ) -> IngestResult:
        """Full path for one NEW inbound message.

        Persist-first: the raw message is stored before any parsing, so a crash
        mid-processing still leaves an auditable record of what arrived. Every
        message ends with a class, an action and a reason in `message_versions`.
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

        parsed = self._parse_and_store(
            row_id, channel_id, message_id, text, timestamp, reply_to_message_id
        )
        cls = classify_message(parsed, media=media)
        version_id, version = self.store.record_message_version(
            message_row_id=row_id, channel_id=channel_id, message_id=message_id,
            text=text, media=media, edit_date=edit_date,
            reply_to_message_id=reply_to_message_id,
            message_class=cls.message_class.value, action=cls.action.value,
            reason=cls.reason,
        )
        _log_observation(parsed, text, cls)
        return self._act(parsed, cls, row_id, version_id, version, media=media,
                         timestamp=timestamp)

    def handle_edit(
        self,
        *,
        channel_id: int,
        message_id: int,
        text: str,
        edit_date: str | None = None,
        timestamp: float = 0.0,
        reply_to_message_id: int | None = None,
        media: str | None = None,
    ) -> IngestResult:
        """An EDITED message. Never re-executes what an earlier version did.

        This channel edits constantly (half of all messages carry an edit date):
        result replies get their profit figure updated, and signal posts get
        '/ HOLD' appended to a target hours later. So an edit is classified like
        any message, but with a stricter rule on what it may DO:

          * a signal that already opened a trade is never re-opened or amended.
            Unchanged levels are logged; changed levels, or a newly added HOLD,
            alert a human;
          * a signal that never opened (incomplete when posted, then completed
            by an edit) may open now, through the full admission funnel, unless
            the original post is older than the entry-order lifetime;
          * an UPDATE / CLOSE / CANCEL that arrives as an edit is never executed
            automatically; it alerts.
        """
        row = self.store.get_message(channel_id, message_id)
        if row is None:
            # Never saw the original (posted while we were down, outside the
            # backfill window). Treat as new; the version row records the edit.
            return self.handle_message(
                channel_id=channel_id, message_id=message_id, text=text,
                timestamp=timestamp, reply_to_message_id=reply_to_message_id,
                media=media, edit_date=edit_date,
            )

        last = self.store.latest_message_version(channel_id, message_id)
        if last is not None and last["text_hash"] == self.store.text_hash(text):
            return IngestResult(False, row["id"], detail="edit changed no text",
                                message_class=last["message_class"],
                                action=MessageAction.LOG_ONLY.value)

        parsed = parse_message(
            channel_id=channel_id, message_id=message_id, text=text,
            timestamp=timestamp, reply_to_message_id=reply_to_message_id,
        )
        cls = classify_message(parsed, media=media)
        prior_trade = self.store.trade_for_origin_message(channel_id, message_id)
        planned, reason = cls.action, cls.reason
        override: Classification | None = None

        if prior_trade is not None:
            override = self._judge_edit_of_live_signal(
                prior_trade, row, last, parsed, cls, text
            )
        elif cls.action in (MessageAction.UPDATE_STOP, MessageAction.CLOSE_TRADE,
                            MessageAction.CANCEL_TRADE):
            override = replace(
                cls, action=MessageAction.REVIEW_ALERT,
                reason=f"{cls.action.value} arrived as an EDIT; instructions "
                       "are only executed from the original message",
            )
        elif cls.action is MessageAction.OPEN_TRADE and timestamp and (
            time.time() - timestamp
            > self.config.entry_order_expiry_hours * 3600
        ):
            override = replace(
                cls, action=MessageAction.REFUSED,
                reason="edit completed a signal posted more than "
                       f"{self.config.entry_order_expiry_hours}h ago",
            )
        if override is not None:
            cls = override
            planned, reason = cls.action, cls.reason

        version_id, version = self.store.record_message_version(
            message_row_id=row["id"], channel_id=channel_id,
            message_id=message_id, text=text, media=media, edit_date=edit_date,
            reply_to_message_id=reply_to_message_id,
            message_class=cls.message_class.value, action=planned.value,
            reason=reason,
            trade_id=prior_trade["id"] if prior_trade is not None else None,
        )
        self.store.append_event(
            EventType.MESSAGE_EDITED,
            f"message {message_id} edited (v{version}): "
            f"{cls.message_class.value} -> {planned.value}",
            message_row_id=row["id"],
        )
        _log_observation(parsed, text, cls)

        if planned is MessageAction.OPEN_TRADE:
            # An edit that completes a signal enters the same funnel as a new
            # one. The message row already exists, so decisions land on it.
            self.store.update_message_parse(
                row["id"], parsed_type=parsed.message_type.value,
                parsed_asset=parsed.asset.value if parsed.asset else None,
                parsed_direction=parsed.direction.value if parsed.direction else None,
                confidence=parsed.confidence,
                signal_fingerprint=fingerprint_for(parsed),
            )
        return self._act(parsed, cls, row["id"], version_id, version, media=media,
                         timestamp=timestamp, is_edit=True,
                         trade_id=prior_trade["id"] if prior_trade is not None else None)

    def _parse_and_store(self, row_id, channel_id, message_id, text, timestamp,
                         reply_to_message_id):
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
        return parsed

    def _judge_edit_of_live_signal(self, trade, row, last, parsed, cls, text):
        """What an edit of a signal that opened a trade means. Never executes."""
        terminal = TradeState(trade["state"]).is_terminal
        old = parse_message(
            channel_id=row["channel_id"], message_id=row["message_id"],
            text=last["text"] if last is not None else row["raw_text"],
        )
        if cls.message_class in (MessageClass.RESULT_REPORT, MessageClass.CHATTER):
            return replace(cls, action=MessageAction.LOG_ONLY,
                           reason="edit annotated the signal post; levels not touched")
        same_levels = fingerprint_for(parsed) == fingerprint_for(old) and \
            parsed.leverage == old.leverage
        hold_added = _has_hold_marker(parsed.raw_text) and not _has_hold_marker(old.raw_text)
        if same_levels and not hold_added:
            return replace(cls, message_class=MessageClass.NEW_SIGNAL,
                           action=MessageAction.LOG_ONLY,
                           reason="edit left the signal levels unchanged")
        if terminal:
            return replace(cls, action=MessageAction.LOG_ONLY,
                           reason="edit of a signal whose trade is already finished")
        if same_levels and hold_added:
            return replace(
                cls, message_class=MessageClass.SIGNAL_UPDATE,
                action=MessageAction.REVIEW_ALERT,
                reason=f"trade #{trade['id']}: the signal now marks its last "
                       "target HOLD; the bot keeps its TP4 order until told otherwise",
            )
        return replace(
            cls, message_class=MessageClass.SIGNAL_UPDATE,
            action=MessageAction.REVIEW_ALERT,
            reason=f"trade #{trade['id']}: signal edited after the trade opened "
                   f"({_diff_levels(old, parsed)}); the bot did NOT amend it",
        )

    # ------------------------------------------------------------------
    # Acting on a classified message
    # ------------------------------------------------------------------

    def _act(self, parsed, cls: Classification, row_id: int, version_id: int,
             version: int, *, media, timestamp, is_edit: bool = False,
             trade_id: int | None = None) -> IngestResult:
        """Execute (or refuse, or escalate) one classified message.

        Every outcome funnels through `_finalise`, which is the ONLY place that
        records what was done and sends the review alert. Anything raised on
        the way fails CLOSED to a review alert: intake must never leave a
        message unrecorded, and a trade-affecting error must reach a human.
        """
        action = cls.action
        try:
            if action is MessageAction.OPEN_TRADE:
                verdict: AdmissionResult = self.admission.admit(parsed, row_id)
                if verdict.refused:
                    log.info(f"  DECISION    : REFUSED — {verdict.detail}",
                             extra={"action": "observe_decision",
                                    "decision": "refused"})
                    result = IngestResult(False, row_id, detail=verdict.detail)
                else:
                    log.info("  DECISION    : ADMITTED — routing as NEW_SIGNAL",
                             extra={"action": "observe_decision",
                                    "decision": "admitted"})
                    result = self._open_from_signal(parsed, row_id)
            elif action is MessageAction.UPDATE_STOP:
                result = self._apply_stop_update(parsed, cls, row_id)
            elif action is MessageAction.CLOSE_TRADE:
                routed = replace(parsed, message_type=cls.close_scope,
                                 asset=cls.asset, direction=cls.direction)
                verdict = self.admission.admit(routed, row_id)
                result = (IngestResult(False, row_id, detail=verdict.detail)
                          if verdict.refused else self._handle_exit(routed, row_id))
            elif action is MessageAction.CANCEL_TRADE:
                result = self._handle_cancel(parsed, cls, row_id)
            elif action is MessageAction.REFUSED:
                self.store.record_decision(
                    row_id, ExecutionDecision.REJECTED,
                    RejectionReason.UNSUPPORTED_ASSET, cls.reason)
                result = IngestResult(False, row_id, detail=cls.reason)
            elif action is MessageAction.REVIEW_ALERT:
                self.store.record_decision(
                    row_id, ExecutionDecision.REQUIRES_REVIEW,
                    RejectionReason.NEEDS_HUMAN, cls.reason)
                result = IngestResult(False, row_id, trade_id=trade_id,
                                      detail=cls.reason)
            elif parsed.message_type is MessageType.SPOT_SIGNAL:
                # Spot talk is logged, but still through admission so the hard
                # spot boundary (brief 8) records its own enumerated reason.
                verdict = self.admission.admit(parsed, row_id)
                result = IngestResult(False, row_id, detail=verdict.detail)
            else:
                self.store.record_decision(
                    row_id, ExecutionDecision.IGNORED, None, cls.reason)
                result = IngestResult(False, row_id, trade_id=trade_id,
                                      detail=cls.reason)
        except Exception as exc:  # noqa: BLE001 — fail closed to a human
            log.error(
                f"handling message {parsed.message_id} ({action.value}) failed",
                exc_info=True,
                extra={"message_id": parsed.message_id, "action": "act_failed"},
            )
            self.store.record_decision(
                row_id, ExecutionDecision.REQUIRES_REVIEW,
                RejectionReason.EXCHANGE_ERROR,
                f"{action.value} failed with {type(exc).__name__}: {exc}")
            result = IngestResult(False, row_id, detail=f"error: {exc}")
        return self._finalise(parsed, cls, result, row_id, version_id, version,
                              media=media)

    def _finalise(self, parsed, cls: Classification, result: IngestResult,
                  row_id: int, version_id: int, version: int, *,
                  media) -> IngestResult:
        """Record what actually happened, and alert a human if it needs one."""
        msg = self.store.get_message(parsed.channel_id, parsed.message_id)
        decision = (msg["execution_decision"] if msg is not None else None)
        detail = (msg["rejection_detail"] if msg is not None else None) or \
            result.detail or cls.reason
        planned = cls.action
        if decision == ExecutionDecision.REQUIRES_REVIEW.value:
            final = MessageAction.REVIEW_ALERT
        elif decision in (ExecutionDecision.REJECTED.value,
                          ExecutionDecision.DUPLICATE.value):
            final = MessageAction.REFUSED
        elif decision == ExecutionDecision.IGNORED.value:
            final = (MessageAction.REFUSED if planned is MessageAction.REFUSED
                     else MessageAction.LOG_ONLY)
        elif decision == ExecutionDecision.EXECUTED.value and planned in (
            MessageAction.OPEN_TRADE, MessageAction.UPDATE_STOP,
            MessageAction.CLOSE_TRADE, MessageAction.CANCEL_TRADE,
        ):
            final = planned
        else:
            final = planned
        self.store.set_version_outcome(
            version_id, action=final.value, reason=detail,
            trade_id=result.trade_id,
        )
        if final is MessageAction.REVIEW_ALERT:
            send_review_alert(
                self.config,
                channel_id=parsed.channel_id, message_id=parsed.message_id,
                version=version, message_class=cls.message_class.value,
                reason=detail, text=parsed.raw_text, media=media,
                trade_id=result.trade_id,
            )
        return replace(result, message_class=cls.message_class.value,
                       action=final.value, version=version)

    # -- linking an update/cancel to its trade ---------------------------

    def _resolve_target(self, parsed, asset: CanonicalAsset | None):
        """(trades, why). trades is None when the target is ambiguous.

        A reply is the strong link: this channel answers its own signal post
        (all 25 stop updates and all 19 cancels in its history are replies).
        The reply chain is followed a few hops because a reply to a reply is
        common. A reply that leads to no trade of ours means 'not our signal',
        which is [] (log), never a fallback to the symbol. Without a reply the
        symbol is the only other link, and only when it picks exactly one trade.
        """
        cur = parsed.referenced_message_id
        hops = 0
        while cur is not None and hops < 4:
            trade = self.store.trade_for_origin_message(parsed.channel_id, cur)
            if trade is not None:
                if TradeState(trade["state"]).is_terminal:
                    return [], f"trade #{trade['id']} is already finished"
                return [trade], f"linked by reply to trade #{trade['id']}"
            msg = self.store.get_message(parsed.channel_id, cur)
            cur = msg["reply_to_message_id"] if msg is not None else None
            hops += 1
        if parsed.referenced_message_id is not None:
            return [], "the reply does not lead to one of our trades"
        if asset is not None and asset in self.specs:
            symbol = self.specs[asset].symbol
            trades = [t for t in self.store.open_trades() if t["symbol"] == symbol]
            return trades, f"matched by symbol {symbol}"
        return None, "no reply and no asset: cannot tell which trade this is for"

    def _review(self, row_id: int, detail: str, trade_id: int | None = None,
                reason: RejectionReason = RejectionReason.NEEDS_HUMAN) -> IngestResult:
        self.store.record_decision(
            row_id, ExecutionDecision.REQUIRES_REVIEW, reason, detail)
        return IngestResult(False, row_id, trade_id=trade_id, detail=detail)

    def _apply_stop_update(self, parsed, cls: Classification, row_id: int) -> IngestResult:
        routed = replace(parsed, message_type=MessageType.SL_UPDATE,
                         stop_loss=cls.new_stop, asset=cls.asset)
        verdict = self.admission.admit(routed, row_id)
        if verdict.refused:
            return IngestResult(False, row_id, detail=verdict.detail)

        trades, why = self._resolve_target(parsed, cls.asset)
        if trades is None:
            return self._review(row_id, why)
        if not trades:
            self.store.record_decision(
                row_id, ExecutionDecision.IGNORED, None,
                f"stop update not applied: {why}")
            return IngestResult(False, row_id, detail=why)
        if len(trades) > 1:
            return self._review(
                row_id, f"stop update matches {len(trades)} open trades "
                        f"({', '.join('#' + str(t['id']) for t in trades)}); "
                        "refusing to guess which")
        trade = trades[0]
        asset = CanonicalAsset(trade["asset"])
        if cls.asset is not None and cls.asset is not asset:
            return self._review(
                row_id, f"update names {cls.asset.value} but it is linked to "
                        f"{asset.value} trade #{trade['id']}", trade["id"])
        spec = self.specs.get(asset)
        if spec is None:
            return self._review(row_id, f"no instrument spec for {asset.value}",
                                trade["id"])

        applied, detail = self.engine.apply_stop_update(
            trade_id=trade["id"], spec=spec,
            direction=Direction(trade["direction"]), new_stop=cls.new_stop,
        )
        if not applied:
            return self._review(row_id, f"stop update refused: {detail}",
                                trade["id"], RejectionReason.STOP_UPDATE_REFUSED)
        self.store.record_decision(row_id, ExecutionDecision.EXECUTED, None, detail)
        notify_trade_event(self.config, "stop_updated", symbol=trade["symbol"],
                           side=trade["direction"], price=cls.new_stop,
                           detail=f"{why}; {detail}")
        return IngestResult(True, row_id, trade["id"], detail)

    def _handle_cancel(self, parsed, cls: Classification, row_id: int) -> IngestResult:
        asset = cls.asset or parsed.asset
        if parsed.referenced_message_id is None and asset is None:
            return self._review(row_id, "cancel notice with no reply and no asset")
        scope = (MessageType.CLOSE_REPLY if parsed.referenced_message_id is not None
                 else MessageType.CLOSE_SYMBOL)
        verdict = self.admission.admit(
            replace(parsed, message_type=scope, asset=asset), row_id)
        if verdict.refused:
            return IngestResult(False, row_id, detail=verdict.detail)

        trades, why = self._resolve_target(parsed, asset)
        if trades is None:
            return self._review(row_id, why)
        if not trades:
            self.store.record_decision(
                row_id, ExecutionDecision.IGNORED, None,
                f"cancel not applied: {why}")
            return IngestResult(False, row_id, detail=why)
        if len(trades) > 1:
            return self._review(
                row_id, f"cancel matches {len(trades)} open trades; "
                        "refusing to guess which")
        trade = trades[0]
        if self.manager is None:
            return self._review(row_id, "no trade manager wired", trade["id"])
        done, detail = self.manager.cancel_unentered_trade(trade["id"])
        if not done:
            # Anything holding exposure is a CLOSE, which is a human decision
            # when the channel only said 'cancelled'.
            return self._review(
                row_id, f"channel cancelled trade #{trade['id']} but it cannot "
                        f"be cancelled automatically: {detail}", trade["id"])
        self.store.record_decision(row_id, ExecutionDecision.EXECUTED, None, detail)
        notify_trade_event(self.config, "cancelled", symbol=trade["symbol"],
                           side=trade["direction"],
                           detail="the channel cancelled this signal before it entered")
        return IngestResult(True, row_id, trade["id"], detail)

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

        # "Close BTC short" names a direction as well as a symbol. The symbol
        # picks the trade; the direction must AGREE with it, or the message is
        # about a position we do not hold and nothing may be closed.
        if (targets and parsed.message_type is MessageType.CLOSE_SYMBOL
                and parsed.direction is not None):
            matching = [t for t in targets
                        if self.store.get_trade(t)["direction"] == parsed.direction.value]
            if not matching:
                self.store.record_decision(
                    row_id, ExecutionDecision.REQUIRES_REVIEW,
                    RejectionReason.AMBIGUOUS_CLOSE_TARGET,
                    f"close names a {parsed.direction.value} but the open trade "
                    "on that symbol goes the other way; refusing to close")
                return IngestResult(False, row_id, detail="direction mismatch")
            targets = matching

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
                stop_loss=parsed.stop_loss,
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


import re as _re

_HOLD_MARK = _re.compile(r"/\s*(?:hold|hodl)\b", _re.I)


def _has_hold_marker(text: str) -> bool:
    return bool(_HOLD_MARK.search(text or ""))


def _diff_levels(old, new) -> str:
    """Which signal fields an edit changed, in words."""
    parts = []
    if old.entry_range != new.entry_range:
        parts.append(f"entry {[str(x) for x in old.entry_range]} -> "
                     f"{[str(x) for x in new.entry_range]}")
    if old.take_profits != new.take_profits:
        parts.append(f"targets {[str(x) for x in old.take_profits]} -> "
                     f"{[str(x) for x in new.take_profits]}")
    if old.stop_loss != new.stop_loss:
        parts.append(f"stop {old.stop_loss} -> {new.stop_loss}")
    if old.leverage != new.leverage:
        parts.append(f"leverage {old.leverage} -> {new.leverage}")
    if old.direction != new.direction:
        parts.append(f"direction {old.direction} -> {new.direction}")
    return "; ".join(parts) or "text changed"


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
        "STOP_BEYOND_LIQUIDATION": RejectionReason.STOP_BEYOND_LIQUIDATION,
    }.get(code, RejectionReason.SIZING_FAILED)
