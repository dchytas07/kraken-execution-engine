"""Core domain types.

Everything the rest of the system exchanges is defined here as an enum or a
frozen dataclass. Strings are not used as domain values anywhere outside this
module, so an invalid state or purpose fails at construction rather than at the
exchange boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, StrEnum


class TradingMode(StrEnum):
    """How far an execution request is allowed to travel.

    DRY_RUN never invokes the exchange CLI at all — it stops at the adapter
    boundary. PAPER routes to `kraken futures paper`. LIVE is real money and
    requires a second independent acknowledgement (see config).
    """

    DRY_RUN = "dry_run"
    PAPER = "paper"
    LIVE = "live"

    @property
    def is_live(self) -> bool:
        return self is TradingMode.LIVE

    @property
    def touches_exchange(self) -> bool:
        """False for DRY_RUN — no subprocess is spawned."""
        return self is not TradingMode.DRY_RUN


class CanonicalAsset(StrEnum):
    """Internal asset identity. The parser normalises every alias to one of
    these; the exchange adapter is solely responsible for mapping them to a
    Kraken instrument. No Kraken symbol ever appears outside the adapter."""

    BTC = "BTC"
    ETH = "ETH"
    SOL = "SOL"
    BNB = "BNB"
    XRP = "XRP"
    DOGE = "DOGE"
    XAU = "XAU"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def opposite(self) -> "Direction":
        return Direction.SHORT if self is Direction.LONG else Direction.LONG

    @property
    def closing_side(self) -> str:
        """The order side that reduces a position of this direction."""
        return "sell" if self is Direction.LONG else "buy"

    @property
    def opening_side(self) -> str:
        return "buy" if self is Direction.LONG else "sell"


class MessageType(StrEnum):
    """Classification of an inbound Telegram message.

    SAFETY_REVIEW is the catch-all required by brief 22: anything that looks
    exit-related but does not match a known safe pattern lands here rather than
    being silently discarded as COMMENTARY.
    """

    NEW_SIGNAL = "NEW_SIGNAL"
    SPOT_SIGNAL = "SPOT_SIGNAL"
    SL_UPDATE = "SL_UPDATE"
    ENTRY_UPDATE = "ENTRY_UPDATE"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    CLOSE_ALL = "CLOSE_ALL"
    CLOSE_DIRECTION = "CLOSE_DIRECTION"
    CLOSE_SYMBOL = "CLOSE_SYMBOL"
    CLOSE_REPLY = "CLOSE_REPLY"
    ADD_POSITION = "ADD_POSITION"
    COMMENTARY = "COMMENTARY"
    SAFETY_REVIEW = "SAFETY_REVIEW"
    UNKNOWN = "UNKNOWN"

    @property
    def is_executable_candidate(self) -> bool:
        """Types that may reach the execution engine after validation.

        SPOT_SIGNAL is deliberately excluded — brief 8 makes this a structural
        boundary, not a text check. COMMENTARY, UNKNOWN and SAFETY_REVIEW are
        recorded and never executed.
        """
        return self in _EXECUTABLE_CANDIDATES

    @property
    def is_exit_like(self) -> bool:
        return self in _EXIT_LIKE


_EXECUTABLE_CANDIDATES = frozenset(
    {
        MessageType.NEW_SIGNAL,
        MessageType.SL_UPDATE,
        MessageType.ENTRY_UPDATE,
        MessageType.PARTIAL_CLOSE,
        MessageType.CLOSE_ALL,
        MessageType.CLOSE_DIRECTION,
        MessageType.CLOSE_SYMBOL,
        MessageType.CLOSE_REPLY,
        MessageType.ADD_POSITION,
    }
)

_EXIT_LIKE = frozenset(
    {
        MessageType.PARTIAL_CLOSE,
        MessageType.CLOSE_ALL,
        MessageType.CLOSE_DIRECTION,
        MessageType.CLOSE_SYMBOL,
        MessageType.CLOSE_REPLY,
    }
)


class TradeState(StrEnum):
    """Explicit trade lifecycle (brief 27).

    Deliberately NOT a set of booleans. Every transition is checked against
    LEGAL_TRANSITIONS, so an illegal move raises rather than silently corrupting
    state. Note CLOSED is reachable only via CLOSING, which in turn requires the
    exchange to have confirmed the position is actually flat.
    """

    RECEIVED = "RECEIVED"
    PARSED = "PARSED"
    VALIDATED = "VALIDATED"
    WAITING_FOR_ENTRY = "WAITING_FOR_ENTRY"
    PARTIALLY_ENTERED = "PARTIALLY_ENTERED"
    FULLY_ENTERED = "FULLY_ENTERED"
    PROTECTED = "PROTECTED"
    TP1_FILLED = "TP1_FILLED"
    TP2_FILLED = "TP2_FILLED"
    BREAKEVEN_ACTIVE = "BREAKEVEN_ACTIVE"
    TP3_FILLED = "TP3_FILLED"
    TP4_FILLED = "TP4_FILLED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"
    SAFE_ERROR = "SAFE_ERROR"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATES

    @property
    def is_open(self) -> bool:
        """True when the trade may hold an actual exchange position."""
        return self in _OPEN_STATES


_TERMINAL_STATES = frozenset(
    {TradeState.CLOSED, TradeState.CANCELLED, TradeState.FAILED}
)

_OPEN_STATES = frozenset(
    {
        TradeState.PARTIALLY_ENTERED,
        TradeState.FULLY_ENTERED,
        TradeState.PROTECTED,
        TradeState.TP1_FILLED,
        TradeState.TP2_FILLED,
        TradeState.BREAKEVEN_ACTIVE,
        TradeState.TP3_FILLED,
        TradeState.TP4_FILLED,
        TradeState.CLOSING,
        TradeState.SAFE_ERROR,
    }
)

# Terminal-ish states any live trade can be forced into. SAFE_ERROR and
# REQUIRES_REVIEW are recoverable by operator action; the rest are final.
_ESCAPES = frozenset(
    {
        TradeState.CLOSING,
        TradeState.CANCELLED,
        TradeState.FAILED,
        TradeState.REQUIRES_REVIEW,
        TradeState.SAFE_ERROR,
    }
)

LEGAL_TRANSITIONS: dict[TradeState, frozenset[TradeState]] = {
    TradeState.RECEIVED: frozenset({TradeState.PARSED}) | _ESCAPES,
    TradeState.PARSED: frozenset({TradeState.VALIDATED}) | _ESCAPES,
    TradeState.VALIDATED: frozenset({TradeState.WAITING_FOR_ENTRY}) | _ESCAPES,
    TradeState.WAITING_FOR_ENTRY: frozenset(
        {TradeState.PARTIALLY_ENTERED, TradeState.FULLY_ENTERED}
    )
    | _ESCAPES,
    TradeState.PARTIALLY_ENTERED: frozenset(
        {TradeState.FULLY_ENTERED, TradeState.PROTECTED}
    )
    | _ESCAPES,
    TradeState.FULLY_ENTERED: frozenset({TradeState.PROTECTED}) | _ESCAPES,
    # A protected trade can take further entry fills (second entry landing after
    # protection was established) without leaving PROTECTED.
    TradeState.PROTECTED: frozenset({TradeState.PROTECTED, TradeState.TP1_FILLED})
    | _ESCAPES,
    TradeState.TP1_FILLED: frozenset({TradeState.TP2_FILLED}) | _ESCAPES,
    # TP2 filling is what authorises the breakeven move — and only that.
    TradeState.TP2_FILLED: frozenset({TradeState.BREAKEVEN_ACTIVE}) | _ESCAPES,
    TradeState.BREAKEVEN_ACTIVE: frozenset({TradeState.TP3_FILLED}) | _ESCAPES,
    TradeState.TP3_FILLED: frozenset({TradeState.TP4_FILLED}) | _ESCAPES,
    TradeState.TP4_FILLED: frozenset({TradeState.CLOSING}) | _ESCAPES,
    # CLOSED is reachable ONLY from CLOSING. The execution engine may enter
    # CLOSED only after the exchange confirms the position is flat AND all
    # associated orders are gone (brief 27).
    TradeState.CLOSING: frozenset(
        {TradeState.CLOSED, TradeState.SAFE_ERROR, TradeState.REQUIRES_REVIEW}
    ),
    # Recoverable states — an operator or reconciliation can route them onward.
    TradeState.SAFE_ERROR: frozenset(
        {TradeState.PROTECTED, TradeState.CLOSING, TradeState.REQUIRES_REVIEW,
         TradeState.CANCELLED, TradeState.FAILED}
    ),
    TradeState.REQUIRES_REVIEW: frozenset(
        {TradeState.CLOSING, TradeState.CANCELLED, TradeState.FAILED,
         TradeState.SAFE_ERROR}
    ),
    TradeState.CLOSED: frozenset(),
    TradeState.CANCELLED: frozenset(),
    TradeState.FAILED: frozenset(),
}


class IllegalTransition(Exception):
    """Raised when code attempts a state move the lifecycle does not permit."""

    def __init__(self, current: TradeState, target: TradeState) -> None:
        super().__init__(
            f"illegal trade state transition {current.value} -> {target.value}"
        )
        self.current = current
        self.target = target


def can_transition(current: TradeState, target: TradeState) -> bool:
    return target in LEGAL_TRANSITIONS[current]


def assert_transition(current: TradeState, target: TradeState) -> None:
    """Fail loudly rather than allow an impossible lifecycle move."""
    if not can_transition(current, target):
        raise IllegalTransition(current, target)


class OrderPurpose(StrEnum):
    """Why the bot placed an order. Drives reconciliation and cleanup."""

    ENTRY = "ENTRY"
    TAKE_PROFIT = "TP"
    STOP_LOSS = "SL"
    CLOSE = "CLOSE"
    DCA = "DCA"


class OrderStatus(StrEnum):
    INTENT = "INTENT"  # persisted before submission — crash-recovery anchor
    SUBMITTED = "SUBMITTED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_resting(self) -> bool:
        return self in (OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)

    @property
    def is_final(self) -> bool:
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )


class MessageClass(StrEnum):
    """What an inbound message IS, independent of what the bot does about it.

    Coarser than MessageType on purpose: MessageType is the parser's
    fine-grained read, this is the operational taxonomy every stored message
    carries (and every edit of it).
    """

    NEW_SIGNAL = "NEW_SIGNAL"
    SIGNAL_UPDATE = "SIGNAL_UPDATE"              # SL / TP / entry change
    CANCEL_OR_CLOSE = "CANCEL_OR_CLOSE"
    RESULT_REPORT = "RESULT_REPORT"              # informational: TP hit, stop hit
    CHATTER = "CHATTER"
    UNPARSEABLE_SIGNAL_LIKE = "UNPARSEABLE_SIGNAL_LIKE"  # images, odd formats


class MessageAction(StrEnum):
    """What the bot did (or, from the classifier, intends to try)."""

    OPEN_TRADE = "OPEN_TRADE"
    UPDATE_STOP = "UPDATE_STOP"
    CLOSE_TRADE = "CLOSE_TRADE"
    CANCEL_TRADE = "CANCEL_TRADE"
    LOG_ONLY = "LOG_ONLY"
    REVIEW_ALERT = "REVIEW_ALERT"    # no trade; a human is told
    REFUSED = "REFUSED"              # understood, deliberately not acted on


class ExecutionDecision(StrEnum):
    """What the system decided to do with a message. Every stored message gets
    one — silence is never an outcome."""

    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"
    IGNORED = "IGNORED"
    DUPLICATE = "DUPLICATE"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"
    PENDING = "PENDING"


class RejectionReason(StrEnum):
    """Explicit, enumerated reasons. Brief 7/21/40 require that a non-execution
    always carries a machine-readable cause."""

    UNSUPPORTED_ASSET = "UNSUPPORTED_ASSET"
    UNSUPPORTED_INSTRUMENT = "UNSUPPORTED_INSTRUMENT"
    GOLD_PINNED_OFF = "GOLD_PINNED_OFF"
    SPOT_SIGNAL = "SPOT_SIGNAL"
    MISSING_FIELDS = "MISSING_FIELDS"
    AMBIGUOUS = "AMBIGUOUS"
    AMBIGUOUS_CLOSE_TARGET = "AMBIGUOUS_CLOSE_TARGET"
    DUPLICATE_MESSAGE = "DUPLICATE_MESSAGE"
    DUPLICATE_SIGNAL = "DUPLICATE_SIGNAL"
    SYMBOL_ALREADY_ACTIVE = "SYMBOL_ALREADY_ACTIVE"
    # An active trade on this symbol goes the other way. Kraken nets per symbol,
    # so an opposite order would shrink or flip it, not sit beside it.
    OPPOSITE_DIRECTION_ACTIVE = "OPPOSITE_DIRECTION_ACTIVE"
    # MAX_TOTAL_OPEN_MARGIN would be exceeded, or is not configured (fail closed).
    TOTAL_MARGIN_LIMIT = "TOTAL_MARGIN_LIMIT"
    UNRECOGNISED_POSITION = "UNRECOGNISED_POSITION"
    POSITION_CHECK_FAILED = "POSITION_CHECK_FAILED"
    STALE_ENTRY = "STALE_ENTRY"
    # The signal's Telegram post time is older than MAX_SIGNAL_AGE_MINUTES, or
    # unknown/unusable (fail closed). Alerted, never traded.
    SIGNAL_TOO_OLD = "SIGNAL_TOO_OLD"
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    SIZING_FAILED = "SIZING_FAILED"
    SIZE_BELOW_MINIMUM = "SIZE_BELOW_MINIMUM"
    SIZING_DEVIATION = "SIZING_DEVIATION"
    LEVERAGE_INVALID = "LEVERAGE_INVALID"
    # The stop would trigger only after the estimated liquidation even at
    # MIN_LEVERAGE (trade 8, 2026-10-04).
    STOP_BEYOND_LIQUIDATION = "STOP_BEYOND_LIQUIDATION"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"
    INSTRUMENT_SPEC_UNAVAILABLE = "INSTRUMENT_SPEC_UNAVAILABLE"
    EXCHANGE_ERROR = "EXCHANGE_ERROR"
    NOT_EXECUTABLE_TYPE = "NOT_EXECUTABLE_TYPE"
    # Understood as an instruction, but not safe or not supported to act on
    # automatically: a human is alerted (conditional close, partial close,
    # TP4 hold, entry adjust, image, ambiguous target, edit of a live signal).
    NEEDS_HUMAN = "NEEDS_HUMAN"
    STOP_UPDATE_REFUSED = "STOP_UPDATE_REFUSED"


class EventType(StrEnum):
    """Append-only audit trail vocabulary (brief 31)."""

    MESSAGE_RECEIVED = "MESSAGE_RECEIVED"
    MESSAGE_PARSED = "MESSAGE_PARSED"
    MESSAGE_EDITED = "MESSAGE_EDITED"
    STOP_UPDATED = "STOP_UPDATED"
    SIGNAL_VALIDATED = "SIGNAL_VALIDATED"
    SIGNAL_REJECTED = "SIGNAL_REJECTED"
    TRADE_CREATED = "TRADE_CREATED"
    TRADE_STATE_CHANGED = "TRADE_STATE_CHANGED"
    ORDER_INTENT_RECORDED = "ORDER_INTENT_RECORDED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_ACCEPTED = "ORDER_ACCEPTED"
    ORDER_PARTIALLY_FILLED = "ORDER_PARTIALLY_FILLED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_REJECTED = "ORDER_REJECTED"
    POSITION_UPDATED = "POSITION_UPDATED"
    SL_REPLACEMENT_INITIATED = "SL_REPLACEMENT_INITIATED"
    SL_REPLACEMENT_VERIFIED = "SL_REPLACEMENT_VERIFIED"
    SL_REPLACEMENT_FAILED = "SL_REPLACEMENT_FAILED"
    BREAKEVEN_ACTIVATED = "BREAKEVEN_ACTIVATED"
    CLOSE_REQUESTED = "CLOSE_REQUESTED"
    CLOSE_SUBMITTED = "CLOSE_SUBMITTED"
    POSITION_CONFIRMED_FLAT = "POSITION_CONFIRMED_FLAT"
    TRADE_CLOSED = "TRADE_CLOSED"
    RECONCILE_MISMATCH = "RECONCILE_MISMATCH"
    RECONCILE_REPAIRED = "RECONCILE_REPAIRED"
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
    KILL_SWITCH_RELEASED = "KILL_SWITCH_RELEASED"
    SAFETY_CHECK_PASSED = "SAFETY_CHECK_PASSED"
    SAFETY_CHECK_FAILED = "SAFETY_CHECK_FAILED"
    STARTUP = "STARTUP"
    SHUTDOWN = "SHUTDOWN"
    # Recorded on every digest evaluation, sent or skipped. Doubles as the
    # persisted 24h cadence marker, so the digest survives restarts.
    ALERT_DIGEST = "ALERT_DIGEST"
    # Cadence marker for the once-a-day trading summary.
    DAILY_SUMMARY = "DAILY_SUMMARY"


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Exchange-sourced instrument facts. Never hardcoded — always read from
    `kraken futures instruments` (brief 3, 45)."""

    symbol: str
    asset: CanonicalAsset
    base: str
    quote: str
    contract_size: Decimal
    size_increment: Decimal
    tick_size: Decimal
    max_leverage: Decimal
    tradeable: bool
    # True when this instrument is a stand-in for the canonical asset rather
    # than the asset itself (XAUT for XAU). Surfaces in logs and trade records.
    is_substitute: bool = False
    substitute_note: str = ""

    def __post_init__(self) -> None:
        if self.size_increment <= 0:
            raise ValueError(f"{self.symbol}: size_increment must be > 0")
        if self.tick_size <= 0:
            raise ValueError(f"{self.symbol}: tick_size must be > 0")
        if self.contract_size <= 0:
            raise ValueError(f"{self.symbol}: contract_size must be > 0")
        if self.max_leverage <= 0:
            raise ValueError(f"{self.symbol}: max_leverage must be > 0")


@dataclass(frozen=True, slots=True)
class ParseWarning:
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    """Structured form of an inbound Telegram message (brief 6).

    Populated by the parser in Stage 3. Fields are Optional because a message
    legitimately may not carry them; the validator — not the parser — decides
    whether the absence is disqualifying.
    """

    channel_id: int
    message_id: int
    timestamp: float
    raw_text: str
    message_type: MessageType
    asset: CanonicalAsset | None = None
    direction: Direction | None = None
    entry_range: tuple[Decimal, ...] = ()
    take_profits: tuple[Decimal, ...] = ()
    stop_loss: Decimal | None = None
    leverage: Decimal | None = None
    percentage: Decimal | None = None
    referenced_message_id: int | None = None
    confidence: float = 0.0
    parse_warnings: tuple[ParseWarning, ...] = ()
    parse_errors: tuple[str, ...] = field(default=())

    @property
    def identity(self) -> tuple[int, int]:
        """Idempotency key. Brief 5 forbids using message text for this."""
        return (self.channel_id, self.message_id)
