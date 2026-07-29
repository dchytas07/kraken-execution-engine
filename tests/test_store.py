"""Persistence guarantees: idempotency, one-trade-per-symbol, audit integrity."""

import sqlite3
from decimal import Decimal

import pytest

from bot.db.store import DuplicateMessage, Store, SymbolAlreadyActive
from bot.models import (
    EventType,
    ExecutionDecision,
    IllegalTransition,
    OrderPurpose,
    OrderStatus,
    RejectionReason,
    TradeState,
)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def make_message(store, message_id=1, channel_id=-100123):
    return store.record_message(
        channel_id=channel_id, message_id=message_id, raw_text="BTC LONG"
    )


def make_trade(store, symbol="PF_XBTUSD", message_id=1):
    row_id = make_message(store, message_id=message_id)
    return store.create_trade(
        origin_message_row_id=row_id,
        origin_channel_id=-100123,
        origin_message_id=message_id,
        asset="BTC",
        symbol=symbol,
        direction="LONG",
    )


# -- message idempotency (brief 5, 23) -------------------------------------


def test_duplicate_message_id_rejected(store):
    make_message(store, message_id=42)
    with pytest.raises(DuplicateMessage):
        make_message(store, message_id=42)


def test_same_message_id_different_channel_is_allowed(store):
    """Identity is (channel_id, message_id), not message_id alone."""
    make_message(store, message_id=42, channel_id=-1)
    make_message(store, message_id=42, channel_id=-2)


def test_identical_text_in_different_messages_is_not_a_duplicate(store):
    """Text alone must never be the identity key (brief 5)."""
    make_message(store, message_id=1)
    make_message(store, message_id=2)
    assert store.has_message(-100123, 1)
    assert store.has_message(-100123, 2)


def test_has_message_reports_processed_state(store):
    assert not store.has_message(-100123, 7)
    make_message(store, message_id=7)
    assert store.has_message(-100123, 7)


def test_message_starts_pending_and_records_a_decision(store):
    row_id = make_message(store)
    assert store.get_message(-100123, 1)["execution_decision"] == "PENDING"

    store.record_decision(
        row_id, ExecutionDecision.REJECTED, RejectionReason.SPOT_SIGNAL, "spot"
    )
    row = store.get_message(-100123, 1)
    assert row["execution_decision"] == "REJECTED"
    assert row["rejection_reason"] == "SPOT_SIGNAL"
    assert row["rejection_detail"] == "spot"
    assert row["processed_at"] is not None


# -- one active trade per symbol (docs/PLAN.md 4.1) ------------------------


def test_second_active_trade_on_same_symbol_refused(store):
    make_trade(store, "PF_XBTUSD", message_id=1)
    with pytest.raises(SymbolAlreadyActive):
        make_trade(store, "PF_XBTUSD", message_id=2)


def test_different_symbols_may_be_active_together(store):
    """Concurrency across symbols stays unlimited."""
    make_trade(store, "PF_XBTUSD", message_id=1)
    make_trade(store, "PF_ETHUSD", message_id=2)
    make_trade(store, "PF_SOLUSD", message_id=3)
    assert len(store.open_trades()) == 3


def test_symbol_frees_up_after_trade_closes(store):
    trade_id, _ = make_trade(store, "PF_XBTUSD", message_id=1)
    for state in (
        TradeState.PARSED,
        TradeState.VALIDATED,
        TradeState.WAITING_FOR_ENTRY,
        TradeState.CLOSING,
        TradeState.CLOSED,
    ):
        store.transition_trade(trade_id, state)
    make_trade(store, "PF_XBTUSD", message_id=2)


# -- state machine enforcement at the DB boundary --------------------------


def test_illegal_transition_refused_by_store(store):
    trade_id, _ = make_trade(store)
    with pytest.raises(IllegalTransition):
        store.transition_trade(trade_id, TradeState.CLOSED)


def test_legal_transition_persists(store):
    trade_id, _ = make_trade(store)
    store.transition_trade(trade_id, TradeState.PARSED, "ok")
    row = store.get_trade(trade_id)
    assert row["state"] == "PARSED"
    assert row["state_detail"] == "ok"


def test_closed_at_is_stamped_only_on_close(store):
    trade_id, _ = make_trade(store)
    store.transition_trade(trade_id, TradeState.PARSED)
    assert store.get_trade(trade_id)["closed_at"] is None
    for state in (
        TradeState.VALIDATED,
        TradeState.WAITING_FOR_ENTRY,
        TradeState.CLOSING,
        TradeState.CLOSED,
    ):
        store.transition_trade(trade_id, state)
    assert store.get_trade(trade_id)["closed_at"] is not None


# -- intent vs actual separation (brief 39) --------------------------------


def test_intent_and_actual_are_separate_columns(store):
    row_id = make_message(store)
    trade_id, _ = store.create_trade(
        origin_message_row_id=row_id,
        origin_channel_id=-100123,
        origin_message_id=1,
        asset="BTC",
        symbol="PF_XBTUSD",
        direction="LONG",
        intent_quantity=Decimal("0.0046"),
        intent_margin_usd=Decimal("30"),
    )
    # Only one entry filled — actual is half of intent.
    store.update_trade_actuals(trade_id, actual_quantity=Decimal("0.0023"))

    row = store.get_trade(trade_id)
    assert row["intent_quantity"] == "0.0046"
    assert row["actual_quantity"] == "0.0023"


def test_actual_quantity_defaults_to_zero_not_intent(store):
    """A created trade holds no position until the exchange says otherwise."""
    trade_id, _ = make_trade(store)
    assert store.get_trade(trade_id)["actual_quantity"] == "0"


def test_decimals_survive_roundtrip_without_float_error(store):
    """Money must never pass through a binary float."""
    trade_id, _ = make_trade(store)
    store.update_trade_actuals(trade_id, actual_quantity=Decimal("0.0046"))
    assert Decimal(store.get_trade(trade_id)["actual_quantity"]) == Decimal("0.0046")


# -- order idempotency (brief 30) ------------------------------------------


def test_client_order_id_is_unique(store):
    trade_id, _ = make_trade(store)
    store.record_order_intent(
        trade_id=trade_id,
        client_order_id="dup-1",
        purpose=OrderPurpose.ENTRY,
        symbol="PF_XBTUSD",
        side="buy",
        order_type="limit",
        quantity=Decimal("0.0023"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.record_order_intent(
            trade_id=trade_id,
            client_order_id="dup-1",
            purpose=OrderPurpose.ENTRY,
            symbol="PF_XBTUSD",
            side="buy",
            order_type="limit",
            quantity=Decimal("0.0023"),
        )


def test_order_intent_is_persisted_before_submission(store):
    """The crash window: intent exists on disk before the exchange is called."""
    trade_id, _ = make_trade(store)
    order_id, _ = store.record_order_intent(
        trade_id=trade_id,
        client_order_id="e1",
        purpose=OrderPurpose.ENTRY,
        symbol="PF_XBTUSD",
        side="buy",
        order_type="limit",
        quantity=Decimal("0.0023"),
    )
    row = store.get_order(order_id)
    assert row["status"] == OrderStatus.INTENT.value
    assert row["exchange_order_id"] is None
    assert row["submitted_at"] is None


def test_intent_orders_appear_in_live_orders_for_recovery(store):
    """Recovery must investigate INTENT rows — they may exist on the exchange."""
    trade_id, _ = make_trade(store)
    store.record_order_intent(
        trade_id=trade_id,
        client_order_id="e1",
        purpose=OrderPurpose.ENTRY,
        symbol="PF_XBTUSD",
        side="buy",
        order_type="limit",
        quantity=Decimal("0.0023"),
    )
    assert [o["status"] for o in store.live_orders()] == ["INTENT"]


def test_order_lookup_by_client_id(store):
    trade_id, _ = make_trade(store)
    store.record_order_intent(
        trade_id=trade_id,
        client_order_id="find-me",
        purpose=OrderPurpose.STOP_LOSS,
        symbol="PF_XBTUSD",
        side="sell",
        order_type="stop",
        quantity=Decimal("0.0046"),
        trigger_price=Decimal("62000"),
        reduce_only=True,
    )
    row = store.get_order_by_client_id("find-me")
    assert row["purpose"] == "SL"
    assert row["reduce_only"] == 1
    assert row["trigger_price"] == "62000"


def test_filled_order_leaves_live_set(store):
    trade_id, _ = make_trade(store)
    order_id, _ = store.record_order_intent(
        trade_id=trade_id,
        client_order_id="e1",
        purpose=OrderPurpose.ENTRY,
        symbol="PF_XBTUSD",
        side="buy",
        order_type="limit",
        quantity=Decimal("0.0023"),
    )
    store.update_order(
        order_id,
        status=OrderStatus.FILLED,
        exchange_order_id="FP-1",
        filled_quantity=Decimal("0.0023"),
        submitted=True,
    )
    assert store.live_orders() == []
    assert store.get_order(order_id)["submitted_at"] is not None


def test_orders_filter_by_purpose(store):
    trade_id, _ = make_trade(store)
    for i, purpose in enumerate(
        [OrderPurpose.ENTRY, OrderPurpose.ENTRY, OrderPurpose.TAKE_PROFIT]
    ):
        store.record_order_intent(
            trade_id=trade_id,
            client_order_id=f"o{i}",
            purpose=purpose,
            symbol="PF_XBTUSD",
            side="buy",
            order_type="limit",
            quantity=Decimal("0.001"),
            sequence=i,
        )
    assert len(store.orders_for_trade(trade_id, OrderPurpose.ENTRY)) == 2
    assert len(store.orders_for_trade(trade_id)) == 3


# -- authoritative stop tracking (brief 16) --------------------------------


def test_active_stop_is_tracked_explicitly(store):
    trade_id, _ = make_trade(store)
    store.set_active_stop(
        trade_id, order_uid="O-abc", trigger_price=Decimal("62000")
    )
    row = store.get_trade(trade_id)
    assert row["active_stop_order_uid"] == "O-abc"
    assert row["breakeven_active"] == 0

    store.set_active_stop(
        trade_id,
        order_uid="O-abc",
        trigger_price=Decimal("64598"),
        breakeven=True,
        breakeven_price=Decimal("64598"),
    )
    row = store.get_trade(trade_id)
    assert row["breakeven_active"] == 1
    assert row["stop_trigger_price"] == "64598"


# -- audit log integrity ---------------------------------------------------


def test_events_are_append_only_no_update(store):
    trade_id, _ = make_trade(store)
    event_id = store.append_event(EventType.TRADE_CREATED, "created", trade_id=trade_id)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store.transaction() as conn:
            conn.execute("UPDATE events SET summary = 'tampered' WHERE id = ?", (event_id,))


def test_events_are_append_only_no_delete(store):
    trade_id, _ = make_trade(store)
    store.append_event(EventType.TRADE_CREATED, "created", trade_id=trade_id)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with store.transaction() as conn:
            conn.execute("DELETE FROM events")


def test_events_ordered_for_trade(store):
    trade_id, _ = make_trade(store)
    for et in (
        EventType.TRADE_CREATED,
        EventType.ORDER_SUBMITTED,
        EventType.ORDER_FILLED,
    ):
        store.append_event(et, et.value, trade_id=trade_id)
    assert [e["event_type"] for e in store.events_for_trade(trade_id)] == [
        "TRADE_CREATED",
        "ORDER_SUBMITTED",
        "ORDER_FILLED",
    ]


# -- transactions ----------------------------------------------------------


def test_transaction_rolls_back_on_error(store):
    trade_id, _ = make_trade(store)
    with pytest.raises(RuntimeError):
        with store.transaction() as conn:
            conn.execute("UPDATE trades SET asset = 'ZZZ' WHERE id = ?", (trade_id,))
            raise RuntimeError("boom")
    assert store.get_trade(trade_id)["asset"] == "BTC"


def test_store_reopens_existing_database(tmp_path):
    """Restart must find prior state intact."""
    path = tmp_path / "persist.db"
    s1 = Store(path)
    make_trade(s1)
    s1.close()

    s2 = Store(path)
    assert len(s2.open_trades()) == 1
    s2.close()
