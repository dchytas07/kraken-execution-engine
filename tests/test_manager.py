"""TradeManager: the forward lifecycle, and the same-cycle protection guarantee.

Before this existed a position was protected only when the 60s reconciliation
loop noticed it was unprotected — up to a full cycle of real exposure after
every fill. These tests assert the guarantee, not just the mechanism.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from bot.config import load_config
from bot.db.store import Store
from bot.exchange.specs import parse_instrument
from bot.execution.engine import ExecutionEngine
from bot.execution.manager import TradeManager
from bot.execution.observer import PollingFillObserver
from bot.killswitch import KillSwitch
from bot.models import (
    CanonicalAsset,
    Direction,
    OrderPurpose,
    TradeState,
)
from bot.sizing import split_take_profits
from tests.test_execution import FakeExchange

D = Decimal
RAW = {i["symbol"]: i for i in json.loads(
    (Path(__file__).parent / "fixtures" / "futures_instruments.json").read_text()
)["instruments"]}
BTC = parse_instrument(RAW["PF_XBTUSD"], CanonicalAsset.BTC)
SPECS = {CanonicalAsset.BTC: BTC}
ENTRIES = (D(64000), D(63500))
STOP = D(62000)  # 3.1% below the far entry: inside liquidation at 10x
TPS = [D(65000), D(66000), D(67000), D(68000)]


@pytest.fixture
def rig(tmp_path):
    config = load_config({
        "TRADING_MODE": "paper", "TRADE_MARGIN_USD": "30",
        "MAX_LEVERAGE": "10", "DEFAULT_LEVERAGE": "10",
        "MAX_ENTRY_DEVIATION_PERCENT": "1.5",
        "KILL_SWITCH_PATH": str(tmp_path / "KILL"),
    }, dotenv_path=None)
    store = Store(tmp_path / "m.db")
    exchange = FakeExchange(mark=D(63800))
    engine = ExecutionEngine(
        config=config, store=store, client=exchange,
        observer=PollingFillObserver(exchange, sleep=lambda s: None,
                                     default_timeout_s=0.05, default_poll_s=0.0),
        kill_switch=KillSwitch(path=tmp_path / "KILL"),
    )
    engine.mark_recovery_complete()
    manager = TradeManager(config=config, store=store, client=exchange,
                           engine=engine, specs=SPECS)

    msg = store.record_message(channel_id=-1, message_id=1, raw_text="BTC")
    trade_id, uid = store.create_trade(
        origin_message_row_id=msg, origin_channel_id=-1, origin_message_id=1,
        asset="BTC", symbol="PF_XBTUSD", direction="LONG",
        intent_stop_loss=D(62500), intent_take_profits=TPS,
    )
    store.transition_trade(trade_id, TradeState.PARSED)
    store.transition_trade(trade_id, TradeState.VALIDATED)
    yield manager, engine, exchange, store, trade_id, uid
    store.close()


def opened(engine, exchange, trade_id, uid, *, both=True):
    plan = engine.plan_entries(spec=BTC, direction=Direction.LONG,
                               entry_prices=ENTRIES, signal_leverage=D(10), stop_loss=STOP)
    coids = engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=plan)
    exchange.fill(coids[0], plan.entries[0][1], D(64000))
    if both:
        exchange.fill(coids[1], plan.entries[1][1], D(63500))
    return plan, coids


def stops(exchange):
    return [o for o in exchange.get_open_orders() if o.order_type == "stop"]


# ==========================================================================
# The same-cycle protection guarantee
# ==========================================================================


def test_stop_is_placed_in_the_same_call_that_observes_the_fill(rig):
    """REGRESSION: protection must not wait for a later cycle.

    Before TradeManager, a filled position stayed unprotected until the 60s
    reconciliation loop noticed — real exposure after every fill.
    """
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    assert stops(exchange) == [], "precondition: not yet protected"

    manager.step(trade_id)  # ONE call

    assert len(stops(exchange)) == 1, "fill observed but no stop placed same-call"
    assert stops(exchange)[0].size == exchange.get_position("PF_XBTUSD").size


def test_protection_precedes_take_profits_within_the_step(rig):
    """Ordering is the safety property: no TP is placed before the stop."""
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)

    order_of_types = []
    original = exchange.place_order

    def record(request):
        order_of_types.append(request.order_type)
        return original(request)
    exchange.place_order = record

    manager.step(trade_id)
    assert order_of_types, "nothing was placed"
    assert order_of_types[0] == "stop", f"stop was not first: {order_of_types}"


def test_step_reports_that_it_protected(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    assert manager.step(trade_id).protected_this_step


def test_second_step_does_not_place_a_duplicate_stop(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    manager.step(trade_id)
    manager.step(trade_id)
    assert len(stops(exchange)) == 1


def test_no_stop_intent_escalates_rather_than_leaving_it_unprotected(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    with store.transaction() as conn:
        conn.execute("UPDATE trades SET intent_stop_loss = NULL WHERE id = ?",
                     (trade_id,))
    opened(engine, exchange, trade_id, uid)
    manager.step(trade_id)
    assert store.get_trade(trade_id)["state"] == TradeState.SAFE_ERROR.value


# ==========================================================================
# TP ladder
# ==========================================================================


def test_tp_ladder_is_placed_from_intent(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    plan, _ = opened(engine, exchange, trade_id, uid)
    manager.step(trade_id)

    rows = store.orders_for_trade(trade_id, OrderPurpose.TAKE_PROFIT)
    position = exchange.get_position("PF_XBTUSD")
    assert len(rows) == 4
    assert tuple(Decimal(r["quantity"]) for r in rows) == \
        split_take_profits(position.size, BTC, levels=4)
    assert sum(Decimal(r["quantity"]) for r in rows) == position.size


def test_tp_ladder_is_placed_once_only(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    manager.step(trade_id)
    manager.step(trade_id)
    manager.step(trade_id)
    assert len(store.orders_for_trade(trade_id, OrderPurpose.TAKE_PROFIT)) == 4


# ==========================================================================
# Breakeven, driven by confirmed TP2
# ==========================================================================


def test_breakeven_fires_only_after_a_confirmed_tp2_fill(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    manager.step(trade_id)
    tp_coids = [r["client_order_id"]
                for r in store.orders_for_trade(trade_id, OrderPurpose.TAKE_PROFIT)]
    rows = store.orders_for_trade(trade_id, OrderPurpose.TAKE_PROFIT)

    exchange.fill(tp_coids[0], Decimal(rows[0]["quantity"]), D(65000))
    manager.step(trade_id)
    assert store.get_trade(trade_id)["state"] == TradeState.TP1_FILLED.value
    assert store.get_trade(trade_id)["breakeven_active"] == 0

    exchange.fill(tp_coids[1], Decimal(rows[1]["quantity"]), D(66000))
    manager.step(trade_id)
    trade = store.get_trade(trade_id)
    assert trade["state"] == TradeState.BREAKEVEN_ACTIVE.value
    assert trade["breakeven_active"] == 1
    assert len(stops(exchange)) == 1, "duplicate or missing stop after breakeven"


def test_breakeven_uses_the_actual_average_entry(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    manager.step(trade_id)
    rows = store.orders_for_trade(trade_id, OrderPurpose.TAKE_PROFIT)
    for r in rows[:2]:
        exchange.fill(r["client_order_id"], Decimal(r["quantity"]),
                      D(r["trigger_price"]))
    manager.step(trade_id)
    be = Decimal(store.get_trade(trade_id)["breakeven_price"])
    assert D(63500) <= be <= D(64000)


# ==========================================================================
# Close-target resolution (brief 20, 21)
# ==========================================================================


def test_close_by_symbol_resolves(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    manager.step(trade_id)
    assert manager.resolve_close_targets(symbol="PF_XBTUSD") == [trade_id]


def test_close_all_resolves_every_live_trade(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    manager.step(trade_id)
    assert manager.resolve_close_targets(close_all=True) == [trade_id]


def test_close_by_direction_resolves(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    manager.step(trade_id)
    assert manager.resolve_close_targets(direction=Direction.LONG) == [trade_id]
    assert manager.resolve_close_targets(direction=Direction.SHORT) == []


def test_close_by_reply_resolves_to_the_originating_trade(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    manager.step(trade_id)
    assert manager.resolve_close_targets(reply_to_message_id=1) == [trade_id]


def test_reply_to_an_unknown_message_is_ambiguous_not_close_all(rig):
    """REGRESSION for brief 21: a reply we cannot tie to a trade must NOT be
    treated as 'close everything'."""
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    manager.step(trade_id)
    assert manager.resolve_close_targets(reply_to_message_id=99999) is None


def test_no_scope_at_all_is_ambiguous(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    manager.step(trade_id)
    assert manager.resolve_close_targets() is None


def test_close_confirms_flat_before_reporting_success(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    manager.step(trade_id)

    original = exchange.place_order

    def fill_closes(request):
        result = original(request)
        if request.purpose is OrderPurpose.CLOSE:
            exchange.fill(request.client_order_id, request.size, D(65000))
        return result
    exchange.place_order = fill_closes

    outcomes = manager.close_trades([trade_id])
    assert outcomes[trade_id] is True
    assert store.get_trade(trade_id)["state"] == TradeState.CLOSED.value


def test_close_that_does_not_go_flat_is_not_reported_as_closed(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    manager.step(trade_id)
    outcomes = manager.close_trades([trade_id])  # nothing fills
    assert outcomes[trade_id] is False
    assert store.get_trade(trade_id)["state"] != TradeState.CLOSED.value


# ==========================================================================
# Resilience
# ==========================================================================


def test_step_all_survives_one_failing_trade(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)

    def explode(tid):
        raise RuntimeError("boom")
    manager.step = explode
    assert manager.step_all() == []  # logged, not raised


def test_terminal_and_quarantined_trades_are_skipped(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    store.transition_trade(trade_id, TradeState.REQUIRES_REVIEW)
    assert manager.step(trade_id).actions == []


# ==========================================================================
# Close targeting must be a DENY-list (soak finding)
# ==========================================================================


def test_quarantined_trade_is_still_closeable(rig):
    """REGRESSION from the Stage 10 soak.

    resolve_close_targets enumerated which states were closeable and omitted
    REQUIRES_REVIEW, so "close all" reported 'nothing to close' while a
    quarantined trade held a real position. Quarantine stops the bot acting on
    STRATEGY; it must never stop an operator acting on EXPOSURE.
    """
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    manager.step(trade_id)
    store.transition_trade(trade_id, TradeState.REQUIRES_REVIEW)

    assert manager.resolve_close_targets(close_all=True) == [trade_id]
    assert manager.resolve_close_targets(symbol="PF_XBTUSD") == [trade_id]


def test_safe_error_trade_is_still_closeable(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    manager.step(trade_id)
    store.transition_trade(trade_id, TradeState.SAFE_ERROR)
    assert manager.resolve_close_targets(close_all=True) == [trade_id]


@pytest.mark.parametrize("state", [s for s in TradeState if not s.is_terminal])
def test_every_non_terminal_state_is_closeable(rig, state):
    """The property, not a list: closeable == not terminal.

    Parametrised over the live state machine, so a state added later is
    covered automatically — which is the whole point of a deny-list.
    """
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    manager.step(trade_id)
    current = TradeState(store.get_trade(trade_id)["state"])
    if current is not state:
        # Force the state; the transition table is exercised elsewhere.
        with store.transaction() as conn:
            conn.execute("UPDATE trades SET state = ? WHERE id = ?",
                         (state.value, trade_id))
    assert manager.resolve_close_targets(close_all=True) == [trade_id], \
        f"{state.value} is non-terminal but was not closeable"


@pytest.mark.parametrize("state", [s for s in TradeState if s.is_terminal])
def test_terminal_states_are_not_closeable(rig, state):
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    manager.step(trade_id)
    with store.transaction() as conn:
        conn.execute("UPDATE trades SET state = ? WHERE id = ?",
                     (state.value, trade_id))
    assert manager.resolve_close_targets(close_all=True) == []


def test_quarantined_trade_keeps_its_protection(rig):
    """Quarantine suspends strategy, not protection."""
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    store.transition_trade(trade_id, TradeState.REQUIRES_REVIEW)

    manager.step(trade_id)
    assert len(stops(exchange)) == 1
    assert stops(exchange)[0].size == exchange.get_position("PF_XBTUSD").size


def test_quarantined_trade_gets_no_new_strategy_orders(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    store.transition_trade(trade_id, TradeState.REQUIRES_REVIEW)
    manager.step(trade_id)
    assert store.orders_for_trade(trade_id, OrderPurpose.TAKE_PROFIT) == []


# ==========================================================================
# Protection SLA and the O(1) sweep (soak run 4 finding)
# ==========================================================================


def test_protection_sweep_costs_two_reads_regardless_of_book_size(rig):
    """The property that makes the SLA achievable.

    A full step costs O(open orders) and was measured at 17s median / 50s max
    with three trades. The sweep must not scale that way.
    """
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    manager.step(trade_id)  # creates a stop + 4 TPs

    calls = {"n": 0}
    for name in ("get_open_positions", "get_open_orders"):
        original = getattr(exchange, name)

        def counted(*a, _o=original, **k):
            calls["n"] += 1
            return _o(*a, **k)
        setattr(exchange, name, counted)

    manager.protection_sweep()
    assert calls["n"] <= 2, f"sweep made {calls['n']} exchange reads, expected <=2"


def test_sweep_protects_an_unprotected_position(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    engine.sync_entry_fills(trade_id)
    assert stops(exchange) == []

    assert manager.protection_sweep() == ["PF_XBTUSD"]
    assert len(stops(exchange)) == 1
    assert stops(exchange)[0].size == exchange.get_position("PF_XBTUSD").size


def test_sweep_is_idempotent(rig):
    manager, engine, exchange, store, trade_id, uid = rig
    opened(engine, exchange, trade_id, uid)
    engine.sync_entry_fills(trade_id)
    manager.protection_sweep()
    assert manager.protection_sweep() == []
    assert len(stops(exchange)) == 1


def test_sweep_never_adopts_an_orphan(rig):
    """No local trade means no protection — adopting it is exactly what
    reconciliation refuses to do."""
    manager, engine, exchange, store, trade_id, uid = rig
    store.transition_trade(trade_id, TradeState.CANCELLED)
    exchange.positions["PF_XBTUSD"] = __import__(
        "bot.exchange.base", fromlist=["Position"]
    ).Position(symbol="PF_XBTUSD", side="long", size=D("0.005"),
               entry_price=D(64000))
    assert manager.protection_sweep() == []
    assert stops(exchange) == []


def test_sweep_survives_an_unreadable_exchange(rig):
    manager, engine, exchange, store, trade_id, uid = rig

    def explode():
        raise RuntimeError("exchange unreachable")
    exchange.get_open_positions = explode
    assert manager.protection_sweep() == []  # logged, not raised


def test_sla_constant_is_documented_and_sane():
    from bot.execution.manager import PROTECTION_SLA_SECONDS
    assert 0 < PROTECTION_SLA_SECONDS <= 30
