"""Restart recovery (brief 29, 30).

Structured as a crash-point matrix: for each dangerous moment the brief names,
the test builds the exact database + exchange state a crash there would leave,
runs recovery over it, and asserts the outcome.

A "crash" is modelled by constructing a NEW Store and ExecutionEngine over the
same database file — which is what a restart actually is. Nothing is carried
over in memory, so anything recovery gets right, it got right from durable
state plus the exchange.
"""

import json
import logging
from decimal import Decimal
from pathlib import Path

import pytest

from bot.config import load_config
from bot.db.store import Store
from bot.exchange.base import Position
from bot.exchange.specs import parse_instrument
from bot.execution.engine import ExecutionEngine, ExecutionRefused
from bot.execution.observer import PollingFillObserver
from bot.killswitch import KillSwitch
from bot.models import (
    CanonicalAsset,
    Direction,
    OrderPurpose,
    OrderStatus,
    TradeState,
)
from bot.reconcile import Reconciler
from bot.recovery import IntentOutcome, RecoveryManager
from tests.test_execution import FakeExchange, replace_order

D = Decimal
RAW = {i["symbol"]: i for i in json.loads(
    (Path(__file__).parent / "fixtures" / "futures_instruments.json").read_text()
)["instruments"]}
BTC = parse_instrument(RAW["PF_XBTUSD"], CanonicalAsset.BTC)
ETH = parse_instrument(RAW["PF_ETHUSD"], CanonicalAsset.ETH)
SPECS = {CanonicalAsset.BTC: BTC, CanonicalAsset.ETH: ETH}
ENTRIES = (D(64000), D(63500))


class Session:
    """One bot process. `restart()` returns a fresh one over the same DB."""

    def __init__(self, tmp_path, exchange):
        self.tmp_path = tmp_path
        self.exchange = exchange
        self.config = load_config({
            "TRADING_MODE": "paper", "TRADE_MARGIN_USD": "30",
            "MAX_LEVERAGE": "10", "DEFAULT_LEVERAGE": "10",
            "MAX_ENTRY_DEVIATION_PERCENT": "1.5",
            "KILL_SWITCH_PATH": str(tmp_path / "KILL"),
        }, dotenv_path=None)
        self.store = Store(tmp_path / "recovery.db")
        self.kill_switch = KillSwitch(path=tmp_path / "KILL")
        self.engine = ExecutionEngine(
            config=self.config, store=self.store, client=exchange,
            observer=PollingFillObserver(exchange, sleep=lambda s: None,
                                         default_timeout_s=0.05, default_poll_s=0.0),
            kill_switch=self.kill_switch,
        )
        self.reconciler = Reconciler(
            config=self.config, store=self.store, client=exchange,
            engine=self.engine, specs=SPECS,
        )
        self.recovery = RecoveryManager(
            config=self.config, store=self.store, client=exchange,
            engine=self.engine, reconciler=self.reconciler, specs=SPECS,
        )

    def crash(self):
        """Process dies. Nothing in memory survives."""
        self.store.close()
        return Session(self.tmp_path, self.exchange)


@pytest.fixture
def session(tmp_path):
    s = Session(tmp_path, FakeExchange(mark=D(63800)))
    yield s
    try:
        s.store.close()
    except Exception:
        pass


def new_trade(session):
    msg = session.store.record_message(channel_id=-1, message_id=1, raw_text="BTC")
    trade_id, uid = session.store.create_trade(
        origin_message_row_id=msg, origin_channel_id=-1, origin_message_id=1,
        asset="BTC", symbol="PF_XBTUSD", direction="LONG",
        intent_stop_loss=D(62500),
    )
    session.store.transition_trade(trade_id, TradeState.PARSED)
    session.store.transition_trade(trade_id, TradeState.VALIDATED)
    return trade_id, uid


def open_entries(session, trade_id, uid):
    session.engine.mark_recovery_complete()
    plan = session.engine.plan_entries(
        spec=BTC, direction=Direction.LONG, entry_prices=ENTRIES,
        signal_leverage=D(10),
    )
    coids = session.engine.open_trade(
        trade_id=trade_id, trade_uid=uid, spec=BTC, plan=plan
    )
    return plan, coids


def outcomes(result):
    return {r.outcome for r in result.intents_resolved}


# ==========================================================================
# The gate: no orders before recovery (brief 29)
# ==========================================================================


def test_engine_refuses_to_open_before_recovery(session):
    """A fresh process must reconcile before placing anything."""
    trade_id, uid = new_trade(session)
    plan = session.engine.plan_entries(
        spec=BTC, direction=Direction.LONG, entry_prices=ENTRIES,
        signal_leverage=D(10),
    )
    with pytest.raises(ExecutionRefused, match="RECOVERY_INCOMPLETE"):
        session.engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC,
                                  plan=plan)
    assert session.exchange.orders == {}


def test_recovery_opens_the_gate(session):
    new_trade(session)
    result = session.recovery.recover()
    assert result.safe_to_trade
    session.engine.require_recovered()  # must not raise


def test_gate_stays_shut_when_recovery_is_blocked(session):
    trade_id, uid = new_trade(session)
    # A position with no recoverable stop intent cannot be protected.
    with session.store.transaction() as conn:
        conn.execute("UPDATE trades SET intent_stop_loss = NULL WHERE id = ?",
                     (trade_id,))
    session.store.transition_trade(trade_id, TradeState.WAITING_FOR_ENTRY)
    session.store.transition_trade(trade_id, TradeState.PARTIALLY_ENTERED)
    session.exchange.position = Position(symbol="PF_XBTUSD", side="long",
                                         size=D("0.0046"), entry_price=D(64000))

    result = session.recovery.recover()
    assert not result.safe_to_trade
    assert "unprotected" in result.blocking_reason
    with pytest.raises(ExecutionRefused, match="RECOVERY_INCOMPLETE"):
        session.engine.require_recovered()


def test_unreadable_exchange_blocks_recovery(session):
    from bot.exchange.base import ExchangeError

    def explode():
        raise ExchangeError("network down", retryable=True)
    session.exchange.get_open_positions = explode

    result = session.recovery.recover()
    assert not result.completed
    assert "could not read exchange state" in result.blocking_reason


# ==========================================================================
# Crash point: after placing an entry, before recording the exchange id
# ==========================================================================


def test_crash_after_entry_placed_adopts_the_live_order(session):
    """The dangerous sequence from brief 30 — must NOT resubmit."""
    trade_id, uid = new_trade(session)
    plan, coids = open_entries(session, trade_id, uid)

    # Simulate dying before the id was recorded.
    for row in session.store.orders_for_trade(trade_id, OrderPurpose.ENTRY):
        with session.store.transaction() as conn:
            conn.execute(
                "UPDATE orders SET status = 'INTENT', exchange_order_id = NULL "
                "WHERE id = ?", (row["id"],))

    restarted = session.crash()
    orders_before = len(restarted.exchange.orders)
    result = restarted.recovery.recover()

    assert IntentOutcome.ADOPTED_RESTING in outcomes(result)
    assert len(restarted.exchange.orders) == orders_before, "recovery duplicated an order"
    rows = restarted.store.orders_for_trade(trade_id, OrderPurpose.ENTRY)
    assert all(r["exchange_order_id"] is not None for r in rows)
    assert all(r["status"] == OrderStatus.OPEN.value for r in rows)
    restarted.store.close()


def test_crash_after_order_never_reached_the_exchange(session):
    """An INTENT row with nothing on the exchange and a flat symbol."""
    trade_id, uid = new_trade(session)
    session.store.record_order_intent(
        trade_id=trade_id, client_order_id=f"{uid}-ENTRY-1",
        purpose=OrderPurpose.ENTRY, symbol="PF_XBTUSD", side="buy",
        order_type="limit", quantity=D("0.0023"), price=D(64000), sequence=1,
    )

    restarted = session.crash()
    result = restarted.recovery.recover()

    assert IntentOutcome.NEVER_PLACED in outcomes(result)
    assert restarted.exchange.orders == {}, "recovery must not place anything"
    restarted.store.close()


# ==========================================================================
# Crash point: after an entry fills
# ==========================================================================


def test_crash_after_entry_fills_recovers_the_fill(session):
    trade_id, uid = new_trade(session)
    plan, coids = open_entries(session, trade_id, uid)
    session.exchange.fill(coids[0], plan.entries[0][1], D(64000))
    # Died before sync_entry_fills ran; the trade is still WAITING_FOR_ENTRY.

    restarted = session.crash()
    result = restarted.recovery.recover()

    assert IntentOutcome.ADOPTED_FILLED in outcomes(result)
    trade = restarted.store.get_trade(trade_id)
    assert Decimal(trade["actual_quantity"]) == plan.entries[0][1]
    restarted.store.close()


def test_crash_after_fill_leaves_the_position_protected(session):
    """The fill happened, no stop was ever placed — recovery must protect it."""
    trade_id, uid = new_trade(session)
    plan, coids = open_entries(session, trade_id, uid)
    session.exchange.fill(coids[0], plan.entries[0][1], D(64000))

    restarted = session.crash()
    restarted.recovery.recover()

    stops = [o for o in restarted.exchange.get_open_orders() if o.order_type == "stop"]
    assert len(stops) == 1, "position left unprotected after restart"
    assert stops[0].size == restarted.exchange.get_position("PF_XBTUSD").size
    restarted.store.close()


# ==========================================================================
# Crash point: during stop replacement
# ==========================================================================


def test_crash_after_stop_cancelled_before_replacement(session):
    """The worst window: no stop exists and the position is live."""
    trade_id, uid = new_trade(session)
    plan, coids = open_entries(session, trade_id, uid)
    session.exchange.fill(coids[0], plan.entries[0][1], D(64000))
    session.exchange.fill(coids[1], plan.entries[1][1], D(63500))
    session.engine.sync_entry_fills(trade_id)
    session.engine.place_stop(trade_id=trade_id, trade_uid=uid, spec=BTC,
                              direction=Direction.LONG, trigger_price=D(62500),
                              position=session.exchange.get_position("PF_XBTUSD"))
    # Stop cancelled, process dies before the replacement is placed.
    for oid, order in list(session.exchange.orders.items()):
        if order.order_type == "stop":
            session.exchange.orders[oid] = replace_order(
                order, status=OrderStatus.CANCELLED)

    restarted = session.crash()
    result = restarted.recovery.recover()

    stops = [o for o in restarted.exchange.get_open_orders() if o.order_type == "stop"]
    assert len(stops) == 1, "position left unprotected after restart"
    assert result.unprotected_repaired == 1
    assert result.safe_to_trade
    restarted.store.close()


def test_replacement_stop_does_not_collide_with_the_old_id(session):
    """Sequenced ids survive the restart (the SL-1 collision bug)."""
    trade_id, uid = new_trade(session)
    plan, coids = open_entries(session, trade_id, uid)
    session.exchange.fill(coids[0], plan.entries[0][1], D(64000))
    session.engine.sync_entry_fills(trade_id)
    session.engine.place_stop(trade_id=trade_id, trade_uid=uid, spec=BTC,
                              direction=Direction.LONG, trigger_price=D(62500),
                              position=session.exchange.get_position("PF_XBTUSD"))
    for oid, order in list(session.exchange.orders.items()):
        if order.order_type == "stop":
            session.exchange.orders[oid] = replace_order(
                order, status=OrderStatus.CANCELLED)

    restarted = session.crash()
    restarted.recovery.recover()

    ids = [r["client_order_id"]
           for r in restarted.store.orders_for_trade(trade_id, OrderPurpose.STOP_LOSS)]
    assert len(ids) == len(set(ids)), f"duplicate stop ids after restart: {ids}"
    restarted.store.close()


# ==========================================================================
# Crash point: after a close was submitted
# ==========================================================================


def test_crash_after_close_submitted_and_position_actually_flat(session):
    trade_id, uid = new_trade(session)
    plan, coids = open_entries(session, trade_id, uid)
    session.exchange.fill(coids[0], plan.entries[0][1], D(64000))
    session.engine.sync_entry_fills(trade_id)
    session.engine.place_stop(trade_id=trade_id, trade_uid=uid, spec=BTC,
                              direction=Direction.LONG, trigger_price=D(62500),
                              position=session.exchange.get_position("PF_XBTUSD"))
    session.store.transition_trade(trade_id, TradeState.CLOSING)
    session.exchange.position = None  # the close did land

    restarted = session.crash()
    restarted.recovery.recover()

    assert restarted.store.get_trade(trade_id)["state"] == TradeState.CLOSED.value
    assert restarted.exchange.get_open_orders() == []
    restarted.store.close()


def test_crash_after_close_submitted_but_position_remains(session):
    """The close did NOT land. The trade must stay open and protected."""
    trade_id, uid = new_trade(session)
    plan, coids = open_entries(session, trade_id, uid)
    session.exchange.fill(coids[0], plan.entries[0][1], D(64000))
    session.engine.sync_entry_fills(trade_id)
    session.engine.place_stop(trade_id=trade_id, trade_uid=uid, spec=BTC,
                              direction=Direction.LONG, trigger_price=D(62500),
                              position=session.exchange.get_position("PF_XBTUSD"))
    session.store.transition_trade(trade_id, TradeState.CLOSING)

    restarted = session.crash()
    restarted.recovery.recover()

    assert restarted.store.get_trade(trade_id)["state"] != TradeState.CLOSED.value
    stops = [o for o in restarted.exchange.get_open_orders() if o.order_type == "stop"]
    assert len(stops) == 1, "still-open position lost its protection"
    restarted.store.close()


# ==========================================================================
# Ambiguity: never guessed
# ==========================================================================


def test_missing_order_with_a_live_position_is_ambiguous_not_assumed(session):
    """Fills history is bounded, so absence is not proof it never filled."""
    trade_id, uid = new_trade(session)
    session.store.record_order_intent(
        trade_id=trade_id, client_order_id=f"{uid}-ENTRY-1",
        purpose=OrderPurpose.ENTRY, symbol="PF_XBTUSD", side="buy",
        order_type="limit", quantity=D("0.0023"), price=D(64000), sequence=1,
    )
    session.store.transition_trade(trade_id, TradeState.WAITING_FOR_ENTRY)
    # A position exists that something must account for.
    session.exchange.position = Position(symbol="PF_XBTUSD", side="long",
                                         size=D("0.0023"), entry_price=D(64000))

    restarted = session.crash()
    result = restarted.recovery.recover()

    assert IntentOutcome.AMBIGUOUS in outcomes(result)
    assert trade_id in result.trades_needing_review
    assert restarted.store.get_trade(trade_id)["state"] == \
        TradeState.REQUIRES_REVIEW.value
    restarted.store.close()


def test_ambiguous_intent_is_logged_at_error(session, caplog):
    trade_id, uid = new_trade(session)
    session.store.record_order_intent(
        trade_id=trade_id, client_order_id=f"{uid}-ENTRY-1",
        purpose=OrderPurpose.ENTRY, symbol="PF_XBTUSD", side="buy",
        order_type="limit", quantity=D("0.0023"), price=D(64000), sequence=1,
    )
    session.store.transition_trade(trade_id, TradeState.WAITING_FOR_ENTRY)
    session.exchange.position = Position(symbol="PF_XBTUSD", side="long",
                                         size=D("0.0023"), entry_price=D(64000))

    restarted = session.crash()
    with caplog.at_level(logging.ERROR):
        restarted.recovery.recover()
    assert any("AMBIGUOUS ORDER" in r.message for r in caplog.records)
    restarted.store.close()


# ==========================================================================
# Leverage: crash between set-leverage and the order (Study B(b))
# ==========================================================================


def test_recovery_reasserts_leaked_leverage(session):
    new_trade(session)
    session.exchange.leverage["PF_XBTUSD"] = D(3)  # leaked by the crash

    restarted = session.crash()
    restarted.recovery.recover()

    assert restarted.exchange.leverage["PF_XBTUSD"] == D(10)
    restarted.store.close()


# ==========================================================================
# Orphans and idempotency
# ==========================================================================


def test_orphan_position_is_reported_and_not_adopted(session):
    session.exchange.position = Position(symbol="PF_XBTUSD", side="long",
                                         size=D("0.0050"), entry_price=D(64000))
    restarted = session.crash()
    result = restarted.recovery.recover()

    assert "PF_XBTUSD" in result.orphan_positions
    assert restarted.store.open_trades() == [], "recovery invented a trade"
    assert restarted.exchange.get_position("PF_XBTUSD").size == D("0.0050")
    restarted.store.close()


def test_recovery_is_idempotent(session):
    trade_id, uid = new_trade(session)
    plan, coids = open_entries(session, trade_id, uid)
    session.exchange.fill(coids[0], plan.entries[0][1], D(64000))

    restarted = session.crash()
    restarted.recovery.recover()
    orders_after_first = len(restarted.exchange.orders)
    state_after_first = restarted.store.get_trade(trade_id)["state"]

    restarted.recovery.recover()
    assert len(restarted.exchange.orders) == orders_after_first
    assert restarted.store.get_trade(trade_id)["state"] == state_after_first
    restarted.store.close()


def test_recovery_never_places_entry_orders(session):
    """Recovery restores protection. It must never open exposure."""
    trade_id, uid = new_trade(session)
    session.store.record_order_intent(
        trade_id=trade_id, client_order_id=f"{uid}-ENTRY-1",
        purpose=OrderPurpose.ENTRY, symbol="PF_XBTUSD", side="buy",
        order_type="limit", quantity=D("0.0023"), price=D(64000), sequence=1,
    )

    restarted = session.crash()
    restarted.recovery.recover()

    entries = [o for o in restarted.exchange.get_open_orders()
               if o.order_type == "limit"]
    assert entries == [], "recovery opened new exposure"
    restarted.store.close()


def test_clean_restart_with_no_open_trades(session):
    result = session.recovery.recover()
    assert result.safe_to_trade
    assert result.trades_examined == 0
    assert result.intents_resolved == []


def test_recovery_advances_a_stale_lifecycle_label(session):
    """A filled, protected trade must not stay labelled WAITING_FOR_ENTRY.

    The stop is sized from the position so protection is correct either way,
    but a label lagging reality misleads an operator and gates later TP
    transitions.
    """
    trade_id, uid = new_trade(session)
    plan, coids = open_entries(session, trade_id, uid)
    session.exchange.fill(coids[0], plan.entries[0][1], D(64000))
    session.exchange.fill(coids[1], plan.entries[1][1], D(63500))
    # Crash before sync_entry_fills ever ran.
    assert session.store.get_trade(trade_id)["state"] == \
        TradeState.WAITING_FOR_ENTRY.value

    restarted = session.crash()
    restarted.recovery.recover()

    trade = restarted.store.get_trade(trade_id)
    assert trade["state"] == TradeState.PROTECTED.value
    assert Decimal(trade["actual_quantity"]) == plan.total_intended_size
    restarted.store.close()


# ==========================================================================
# Blast radius of an AMBIGUOUS case: quarantine, not a global halt
# ==========================================================================


def eth_trade(session, *, message_id=2):
    msg = session.store.record_message(channel_id=-1, message_id=message_id,
                                       raw_text="ETH")
    trade_id, uid = session.store.create_trade(
        origin_message_row_id=msg, origin_channel_id=-1,
        origin_message_id=message_id, asset="ETH", symbol="PF_ETHUSD",
        direction="LONG", intent_stop_loss=D(1890),
    )
    session.store.transition_trade(trade_id, TradeState.PARSED)
    session.store.transition_trade(trade_id, TradeState.VALIDATED)
    return trade_id, uid


def make_btc_ambiguous(session, trade_id, uid):
    """An intent absent from the exchange while a BTC position exists."""
    session.store.record_order_intent(
        trade_id=trade_id, client_order_id=f"{uid}-ENTRY-1",
        purpose=OrderPurpose.ENTRY, symbol="PF_XBTUSD", side="buy",
        order_type="limit", quantity=D("0.0023"), price=D(64000), sequence=1,
    )
    session.store.transition_trade(trade_id, TradeState.WAITING_FOR_ENTRY)
    session.exchange.positions["PF_XBTUSD"] = Position(
        symbol="PF_XBTUSD", side="long", size=D("0.0023"), entry_price=D(64000))


def test_ambiguous_trade_does_not_block_the_recovery_gate(session):
    """Scope check: an unresolvable BTC intent must not halt ETH.

    An ambiguous order tells us nothing about a different symbol, so blocking
    all trading would be over-broad. It quarantines the affected trade only.
    """
    btc_id, btc_uid = new_trade(session)
    make_btc_ambiguous(session, btc_id, btc_uid)
    eth_id, _ = eth_trade(session)

    restarted = session.crash()
    result = restarted.recovery.recover()

    assert IntentOutcome.AMBIGUOUS in outcomes(result)
    assert btc_id in result.trades_needing_review
    # The gate OPENS: trading is not globally halted.
    assert result.safe_to_trade
    restarted.engine.require_recovered()  # must not raise
    restarted.store.close()


def test_only_the_ambiguous_trade_is_quarantined(session):
    btc_id, btc_uid = new_trade(session)
    make_btc_ambiguous(session, btc_id, btc_uid)
    eth_id, _ = eth_trade(session)

    restarted = session.crash()
    result = restarted.recovery.recover()

    assert restarted.store.get_trade(btc_id)["state"] == \
        TradeState.REQUIRES_REVIEW.value
    assert eth_id not in result.trades_needing_review
    assert restarted.store.get_trade(eth_id)["state"] == TradeState.VALIDATED.value
    restarted.store.close()


def test_clean_symbol_can_still_open_after_an_ambiguous_one(session):
    """The load-bearing assertion: ETH trades while BTC is quarantined."""
    btc_id, btc_uid = new_trade(session)
    make_btc_ambiguous(session, btc_id, btc_uid)
    eth_id, eth_uid = eth_trade(session)

    restarted = session.crash()
    restarted.recovery.recover()

    plan = restarted.engine.plan_entries(
        spec=ETH, direction=Direction.LONG,
        entry_prices=(D(1920), D(1910)), signal_leverage=D(10),
    )
    restarted.exchange.mark = D(1915)
    coids = restarted.engine.open_trade(
        trade_id=eth_id, trade_uid=eth_uid, spec=ETH, plan=plan
    )
    assert len(coids) == 2, "a clean symbol was blocked by an unrelated quarantine"
    restarted.store.close()


def test_quarantined_symbol_refuses_new_signals(session):
    """The quarantine must actually hold: REQUIRES_REVIEW keeps the symbol
    occupied, so a fresh BTC signal cannot open into the unclear position."""
    from bot.admission import SignalAdmission
    from bot.parser.parse import parse_message

    btc_id, btc_uid = new_trade(session)
    make_btc_ambiguous(session, btc_id, btc_uid)

    restarted = session.crash()
    restarted.recovery.recover()
    assert restarted.store.get_active_trade_for_symbol("PF_XBTUSD") is not None

    admission = SignalAdmission(
        config=restarted.config, store=restarted.store,
        kill_switch=restarted.kill_switch,
        symbol_for={CanonicalAsset.BTC: "PF_XBTUSD", CanonicalAsset.ETH: "PF_ETHUSD"},
        position_for=restarted.exchange.get_position,
    )
    text = ("BTC LONG Entry: 64000 - 63500 TP1 65000 TP2 66000 TP3 67000 "
            "TP4 68000 SL 62500 10x")
    row = restarted.store.record_message(channel_id=-1, message_id=99, raw_text=text)
    parsed = parse_message(channel_id=-1, message_id=99, text=text)
    result = admission.admit(parsed, row)

    assert result.refused, "a quarantined symbol accepted a new signal"
    restarted.store.close()


def test_quarantined_trade_still_gets_its_position_protected(session):
    """Quarantine means 'do not act on strategy', not 'abandon the position'."""
    btc_id, btc_uid = new_trade(session)
    make_btc_ambiguous(session, btc_id, btc_uid)

    restarted = session.crash()
    restarted.recovery.recover()

    stops = [o for o in restarted.exchange.get_open_orders()
             if o.order_type == "stop" and o.symbol == "PF_XBTUSD"]
    assert len(stops) == 1, "a quarantined position was left unprotected"
    assert stops[0].size == restarted.exchange.get_position("PF_XBTUSD").size
    restarted.store.close()


def test_a_genuinely_global_problem_does_block_everything(session):
    """Contrast: an unreadable exchange is global, and must halt all trading."""
    from bot.exchange.base import ExchangeError
    eth_trade(session)

    def explode():
        raise ExchangeError("network down", retryable=True)
    session.exchange.get_open_positions = explode

    result = session.recovery.recover()
    assert not result.safe_to_trade
    with pytest.raises(ExecutionRefused, match="RECOVERY_INCOMPLETE"):
        session.engine.require_recovered()
