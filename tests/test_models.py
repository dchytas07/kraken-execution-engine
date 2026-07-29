"""Trade state machine and domain type guarantees."""

from decimal import Decimal

import pytest

from bot.models import (
    CanonicalAsset,
    Direction,
    IllegalTransition,
    InstrumentSpec,
    LEGAL_TRANSITIONS,
    MessageType,
    OrderStatus,
    TradeState,
    assert_transition,
    can_transition,
)


# -- the closed-only-from-closing rule (brief 27) --------------------------


def test_closed_is_reachable_only_from_closing():
    """A trade must never be marked CLOSED because the bot *sent* a close.

    This is the historical bug from the previous implementation, encoded as a
    structural property of the state machine rather than a code review note.
    """
    sources = [s for s, targets in LEGAL_TRANSITIONS.items() if TradeState.CLOSED in targets]
    assert sources == [TradeState.CLOSING]


@pytest.mark.parametrize(
    "state",
    [s for s in TradeState if s not in (TradeState.CLOSING, TradeState.CLOSED)],
)
def test_no_state_jumps_straight_to_closed(state):
    assert not can_transition(state, TradeState.CLOSED)


def test_closing_can_fail_into_safe_error_rather_than_closing_falsely():
    """If the close cannot be confirmed, the trade must not become CLOSED."""
    assert can_transition(TradeState.CLOSING, TradeState.SAFE_ERROR)
    assert can_transition(TradeState.CLOSING, TradeState.REQUIRES_REVIEW)


# -- breakeven ordering (brief 15) -----------------------------------------


def test_breakeven_requires_tp2_filled():
    """Only a confirmed TP2 fill authorises the breakeven move."""
    for state in TradeState:
        if state is TradeState.TP2_FILLED:
            continue
        assert not can_transition(state, TradeState.BREAKEVEN_ACTIVE), state


def test_tp2_leads_to_breakeven():
    assert can_transition(TradeState.TP2_FILLED, TradeState.BREAKEVEN_ACTIVE)


def test_tp_ladder_is_ordered():
    assert can_transition(TradeState.PROTECTED, TradeState.TP1_FILLED)
    assert can_transition(TradeState.TP1_FILLED, TradeState.TP2_FILLED)
    assert not can_transition(TradeState.PROTECTED, TradeState.TP2_FILLED)
    assert not can_transition(TradeState.TP1_FILLED, TradeState.TP3_FILLED)


# -- terminal states -------------------------------------------------------


@pytest.mark.parametrize(
    "state", [TradeState.CLOSED, TradeState.CANCELLED, TradeState.FAILED]
)
def test_terminal_states_have_no_exits(state):
    assert state.is_terminal
    assert LEGAL_TRANSITIONS[state] == frozenset()


def test_illegal_transition_raises():
    with pytest.raises(IllegalTransition):
        assert_transition(TradeState.RECEIVED, TradeState.CLOSED)


def test_reopening_a_closed_trade_is_impossible():
    for target in TradeState:
        assert not can_transition(TradeState.CLOSED, target)


def test_every_state_has_a_transition_entry():
    """A missing entry would KeyError at runtime inside a transition."""
    for state in TradeState:
        assert state in LEGAL_TRANSITIONS


def test_open_states_can_always_escape_to_review_or_close():
    """No live trade can get stuck with no way out to operator review."""
    for state in TradeState:
        if state.is_terminal or state in (
            TradeState.CLOSING,
            TradeState.REQUIRES_REVIEW,
        ):
            continue
        assert TradeState.REQUIRES_REVIEW in LEGAL_TRANSITIONS[state], state


def test_requires_review_can_be_resolved_onward():
    """REQUIRES_REVIEW is recoverable by operator action, not a dead end."""
    assert LEGAL_TRANSITIONS[TradeState.REQUIRES_REVIEW]
    assert can_transition(TradeState.REQUIRES_REVIEW, TradeState.CLOSING)
    assert can_transition(TradeState.REQUIRES_REVIEW, TradeState.CANCELLED)


# -- spot boundary (brief 8) -----------------------------------------------


def test_spot_signal_is_not_an_executable_candidate():
    """Structural, not a text check — SPOT_SIGNAL can never reach execution."""
    assert not MessageType.SPOT_SIGNAL.is_executable_candidate


@pytest.mark.parametrize(
    "mt", [MessageType.COMMENTARY, MessageType.UNKNOWN, MessageType.SAFETY_REVIEW]
)
def test_non_instructions_are_not_executable(mt):
    assert not mt.is_executable_candidate


def test_new_signal_is_executable():
    assert MessageType.NEW_SIGNAL.is_executable_candidate


def test_exit_like_types_flagged():
    assert MessageType.CLOSE_ALL.is_exit_like
    assert MessageType.PARTIAL_CLOSE.is_exit_like
    assert not MessageType.NEW_SIGNAL.is_exit_like


# -- direction -------------------------------------------------------------


def test_direction_sides():
    assert Direction.LONG.opening_side == "buy"
    assert Direction.LONG.closing_side == "sell"
    assert Direction.SHORT.opening_side == "sell"
    assert Direction.SHORT.closing_side == "buy"
    assert Direction.LONG.opposite is Direction.SHORT


# -- instrument spec -------------------------------------------------------


def spec(**kw):
    base = dict(
        symbol="PF_XBTUSD",
        asset=CanonicalAsset.BTC,
        base="BTC",
        quote="USD",
        contract_size=Decimal("1"),
        size_increment=Decimal("0.0001"),
        tick_size=Decimal("1"),
        max_leverage=Decimal("100"),
        tradeable=True,
    )
    return InstrumentSpec(**{**base, **kw})


def test_valid_spec_constructs():
    assert spec().symbol == "PF_XBTUSD"


@pytest.mark.parametrize(
    "field", ["size_increment", "tick_size", "contract_size", "max_leverage"]
)
def test_non_positive_spec_fields_rejected(field):
    with pytest.raises(ValueError, match=field):
        spec(**{field: Decimal("0")})


def test_substitute_instrument_is_flagged():
    s = spec(
        symbol="PF_XAUTUSD",
        asset=CanonicalAsset.XAU,
        is_substitute=True,
        substitute_note="Tether Gold, not spot XAU",
    )
    assert s.is_substitute


# -- order status ----------------------------------------------------------


def test_resting_and_final_statuses():
    assert OrderStatus.OPEN.is_resting
    assert OrderStatus.PARTIALLY_FILLED.is_resting
    assert not OrderStatus.FILLED.is_resting
    assert OrderStatus.FILLED.is_final
    assert not OrderStatus.INTENT.is_final
