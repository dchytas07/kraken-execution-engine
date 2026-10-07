"""Reconciliation tests.

Per the lesson from the wiring sweep, repairs are exercised through
`Reconciler.run_once()` — the loop that is meant to trigger them — not by
calling the repair helpers directly.
"""

import json
import logging
from decimal import Decimal
from pathlib import Path

import pytest

from bot.config import load_config
from bot.db.store import Store
from bot.exchange.base import Order, Position
from bot.exchange.specs import parse_instrument
from bot.execution.engine import ExecutionEngine
from bot.execution.observer import PollingFillObserver
from bot.killswitch import KillSwitch
from bot.models import (
    CanonicalAsset,
    Direction,
    OrderPurpose,
    OrderStatus,
    TradeState,
)
from bot.reconcile import (
    DiscrepancyKind,
    ExchangeSnapshot,
    FillStream,
    Reconciler,
    ReconciliationLoop,
    SnapshotObserver,
)
from tests.test_execution import FakeExchange, replace_order

D = Decimal
RAW = {i["symbol"]: i for i in json.loads(
    (Path(__file__).parent / "fixtures" / "futures_instruments.json").read_text()
)["instruments"]}
BTC = parse_instrument(RAW["PF_XBTUSD"], CanonicalAsset.BTC)
SPECS = {CanonicalAsset.BTC: BTC}
ENTRIES = (D(64000), D(63500))
STOP = D(62000)  # 3.1% below the far entry: inside liquidation at 10x


@pytest.fixture
def rig(tmp_path):
    config = load_config({
        "TRADING_MODE": "paper", "TRADE_MARGIN_USD": "30",
        "MAX_LEVERAGE": "10", "DEFAULT_LEVERAGE": "10",
        "MAX_ENTRY_DEVIATION_PERCENT": "1.5",
        "KILL_SWITCH_PATH": str(tmp_path / "KILL"),
    }, dotenv_path=None)
    store = Store(tmp_path / "r.db")
    exchange = FakeExchange(mark=D(63800))
    engine = ExecutionEngine(
        config=config, store=store, client=exchange,
        observer=PollingFillObserver(exchange, sleep=lambda s: None,
                                     default_timeout_s=0.05, default_poll_s=0.0),
        kill_switch=KillSwitch(path=tmp_path / "KILL"),
    )
    reconciler = Reconciler(config=config, store=store, client=exchange,
                            engine=engine, specs=SPECS)
    engine.mark_recovery_complete()  # fixtures bypass startup recovery
    msg = store.record_message(channel_id=-1, message_id=1, raw_text="BTC LONG")
    trade_id, uid = store.create_trade(
        origin_message_row_id=msg, origin_channel_id=-1, origin_message_id=1,
        asset="BTC", symbol="PF_XBTUSD", direction="LONG",
        intent_stop_loss=D(62500),
    )
    store.transition_trade(trade_id, TradeState.PARSED)
    store.transition_trade(trade_id, TradeState.VALIDATED)
    return engine, exchange, store, reconciler, trade_id, uid


def open_and_fill(engine, exchange, trade_id, uid, *, both=True):
    plan = engine.plan_entries(spec=BTC, direction=Direction.LONG,
                               entry_prices=ENTRIES, signal_leverage=D(10), stop_loss=STOP)
    coids = engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=plan)
    exchange.fill(coids[0], plan.entries[0][1], D(64000))
    if both:
        exchange.fill(coids[1], plan.entries[1][1], D(63500))
    engine.sync_entry_fills(trade_id)
    return plan, coids


def protect(engine, exchange, trade_id, uid):
    engine.place_stop(trade_id=trade_id, trade_uid=uid, spec=BTC,
                      direction=Direction.LONG, trigger_price=D(62500),
                      position=exchange.get_position("PF_XBTUSD"))


def kinds(report):
    return {d.kind for d in report.discrepancies}


# ==========================================================================
# Consistent snapshot — removes the Stage 6 race
# ==========================================================================


def test_snapshot_reads_are_internally_consistent(rig):
    """Every read within a cycle sees the same instant (KRAKEN_NOTES 6.3)."""
    engine, exchange, store, rec, trade_id, uid = rig
    plan, coids = open_and_fill(engine, exchange, trade_id, uid, both=False)

    snapshot = ExchangeSnapshot.capture(exchange, ["PF_XBTUSD"])
    observer = SnapshotObserver(snapshot)

    # Mutate the exchange AFTER capture; the snapshot must not shift.
    exchange.fill(coids[1], plan.entries[1][1], D(63500))

    assert observer.observe_position("PF_XBTUSD").size == plan.entries[0][1]
    assert observer.observe_order(coids[1]).filled_size == D(0)


def test_snapshot_observer_satisfies_the_stage6_contract(rig):
    """The engine is unchanged because it depends on the interface."""
    engine, exchange, store, rec, trade_id, uid = rig
    plan, coids = open_and_fill(engine, exchange, trade_id, uid)
    observer = SnapshotObserver(ExchangeSnapshot.capture(exchange, ["PF_XBTUSD"]))

    for method in ("observe_order", "observe_position", "await_flat"):
        assert hasattr(observer, method)
    observed = observer.observe_order(coids[0], expected_size=plan.entries[0][1])
    assert observed.is_filled
    assert observed.avg_fill_price == D(64000)


def test_snapshot_await_flat_answers_about_capture_instant(rig):
    engine, exchange, store, rec, trade_id, uid = rig
    open_and_fill(engine, exchange, trade_id, uid)
    assert not SnapshotObserver(
        ExchangeSnapshot.capture(exchange, ["PF_XBTUSD"])).await_flat("PF_XBTUSD")
    exchange.position = None
    assert SnapshotObserver(
        ExchangeSnapshot.capture(exchange, ["PF_XBTUSD"])).await_flat("PF_XBTUSD")


# ==========================================================================
# Protection gaps
# ==========================================================================


def test_unprotected_position_is_detected_and_stop_restored(rig):
    """The most dangerous state: a live position with no stop."""
    engine, exchange, store, rec, trade_id, uid = rig
    open_and_fill(engine, exchange, trade_id, uid)
    protect(engine, exchange, trade_id, uid)

    # Stop vanishes (cancelled out of band, or never really landed).
    for oid, order in list(exchange.orders.items()):
        if order.order_type == "stop":
            exchange.orders[oid] = replace_order(order, status=OrderStatus.CANCELLED)

    report = rec.run_once()
    assert DiscrepancyKind.UNPROTECTED_POSITION in kinds(report)

    stops = [o for o in exchange.get_open_orders() if o.order_type == "stop"]
    assert len(stops) == 1, "protection was not restored"
    assert stops[0].size == exchange.get_position("PF_XBTUSD").size


def test_unprotected_without_recoverable_intent_escalates(rig, tmp_path, caplog):
    """No stop and no intent to rebuild from -> SAFE_ERROR, never a guess."""
    engine, exchange, store, rec, trade_id, uid = rig
    with store.transaction() as conn:
        conn.execute("UPDATE trades SET intent_stop_loss = NULL WHERE id = ?",
                     (trade_id,))
    open_and_fill(engine, exchange, trade_id, uid)

    with caplog.at_level(logging.CRITICAL):
        report = rec.run_once()

    assert DiscrepancyKind.UNPROTECTED_POSITION in kinds(report)
    assert store.get_trade(trade_id)["state"] == TradeState.SAFE_ERROR.value
    assert any(r.levelno >= logging.CRITICAL for r in caplog.records)


def test_stop_size_mismatch_is_repaired(rig):
    engine, exchange, store, rec, trade_id, uid = rig
    plan, coids = open_and_fill(engine, exchange, trade_id, uid, both=False)
    protect(engine, exchange, trade_id, uid)
    # Second entry fills without a sync — stop now covers too little.
    exchange.fill(coids[1], plan.entries[1][1], D(63500))

    report = rec.run_once()
    assert DiscrepancyKind.STOP_SIZE_MISMATCH in kinds(report)
    stop = next(o for o in exchange.get_open_orders() if o.order_type == "stop")
    assert stop.size == exchange.get_position("PF_XBTUSD").size


def test_tp_total_exceeding_position_escalates(rig, caplog):
    """Brief 13: a TP fill must never open a reverse position."""
    engine, exchange, store, rec, trade_id, uid = rig
    open_and_fill(engine, exchange, trade_id, uid)
    protect(engine, exchange, trade_id, uid)
    position = exchange.get_position("PF_XBTUSD")
    engine.place_take_profits(
        trade_id=trade_id, trade_uid=uid, spec=BTC, direction=Direction.LONG,
        take_profits=(D(65000), D(66000), D(67000), D(68000)), position=position,
    )
    # Position shrinks (partial close out of band); TPs now oversized.
    exchange.position = Position(symbol="PF_XBTUSD", side="long",
                                 size=D("0.0010"), entry_price=D(64000))

    with caplog.at_level(logging.CRITICAL):
        report = rec.run_once()

    assert DiscrepancyKind.TP_EXCEEDS_POSITION in kinds(report)
    assert store.get_trade(trade_id)["state"] == TradeState.SAFE_ERROR.value


# ==========================================================================
# Position lifecycle divergence
# ==========================================================================


def test_position_closed_outside_the_bot_closes_the_trade(rig):
    """Stop hit / manual close: the exchange is authoritative for positions."""
    engine, exchange, store, rec, trade_id, uid = rig
    open_and_fill(engine, exchange, trade_id, uid)
    protect(engine, exchange, trade_id, uid)

    exchange.position = None  # e.g. the stop fired

    report = rec.run_once()
    assert DiscrepancyKind.POSITION_GONE in kinds(report)
    assert store.get_trade(trade_id)["state"] == TradeState.CLOSED.value


def test_closing_a_gone_position_cancels_leftover_orders(rig):
    engine, exchange, store, rec, trade_id, uid = rig
    open_and_fill(engine, exchange, trade_id, uid, both=False)
    protect(engine, exchange, trade_id, uid)
    exchange.position = None

    rec.run_once()
    assert exchange.get_open_orders() == [], "leftovers could reopen the position"


def test_orphan_position_is_reported_but_never_auto_closed(rig, caplog):
    """Closing a position we cannot explain could unwind an operator's trade."""
    engine, exchange, store, rec, trade_id, uid = rig
    # Retire the rig's trade so nothing local can claim this position.
    store.transition_trade(trade_id, TradeState.CANCELLED)
    exchange.position = Position(symbol="PF_XBTUSD", side="long",
                                 size=D("0.0050"), entry_price=D(64000))

    with caplog.at_level(logging.ERROR):
        report = rec.run_once()

    assert DiscrepancyKind.ORPHAN_POSITION in kinds(report)
    assert exchange.get_position("PF_XBTUSD") is not None, "orphan was closed!"
    assert any("ORPHAN POSITION" in r.message for r in caplog.records)


# ==========================================================================
# Fill catch-up
# ==========================================================================


def test_fills_missed_by_the_engine_are_recorded(rig):
    engine, exchange, store, rec, trade_id, uid = rig
    plan = engine.plan_entries(spec=BTC, direction=Direction.LONG,
                               entry_prices=ENTRIES, signal_leverage=D(10), stop_loss=STOP)
    coids = engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=plan)
    # Fill without ever calling sync_entry_fills — simulates a crash after fill.
    exchange.fill(coids[0], plan.entries[0][1], D(64000))

    report = rec.run_once()
    assert DiscrepancyKind.FILL_NOT_RECORDED in kinds(report)
    assert Decimal(store.get_trade(trade_id)["actual_quantity"]) == plan.entries[0][1]


def test_ambiguous_order_is_reported_not_guessed(rig):
    """Not resting and never filled: cancelled, rejected, or never placed."""
    engine, exchange, store, rec, trade_id, uid = rig
    plan = engine.plan_entries(spec=BTC, direction=Direction.LONG,
                               entry_prices=ENTRIES, signal_leverage=D(10), stop_loss=STOP)
    engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=plan)
    exchange.orders.clear()  # vanished with no fills

    report = rec.run_once()
    assert DiscrepancyKind.ORDER_MISSING_ON_EXCHANGE in kinds(report)
    rows = store.orders_for_trade(trade_id, OrderPurpose.ENTRY)
    assert all(r["status"] == OrderStatus.UNKNOWN.value for r in rows), \
        "an ambiguous order must not be recorded as CANCELLED"


# ==========================================================================
# Stale entries (brief 12) — leaves PENDING_WIRING here
# ==========================================================================


def test_expired_entry_orders_cancelled_by_the_loop(rig):
    engine, exchange, store, rec, trade_id, uid = rig
    plan = engine.plan_entries(spec=BTC, direction=Direction.LONG,
                               entry_prices=ENTRIES, signal_leverage=D(10), stop_loss=STOP)
    coids = engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=plan)
    exchange.fill(coids[0], plan.entries[0][1], D(64000))
    engine.sync_entry_fills(trade_id)
    protect(engine, exchange, trade_id, uid)

    with store.transaction() as conn:
        conn.execute(
            "UPDATE orders SET expires_at = '2000-01-01T00:00:00+00:00' "
            "WHERE purpose = 'ENTRY' AND status NOT IN ('FILLED')"
        )

    report = rec.run_once()
    assert DiscrepancyKind.STALE_ENTRY_ORDER in kinds(report)
    assert not any(o.order_type == "limit" for o in exchange.get_open_orders())


def test_unexpired_entries_survive_reconciliation(rig):
    engine, exchange, store, rec, trade_id, uid = rig
    plan, coids = open_and_fill(engine, exchange, trade_id, uid, both=False)
    protect(engine, exchange, trade_id, uid)
    rec.run_once()
    assert any(o.order_type == "limit" for o in exchange.get_open_orders())


# ==========================================================================
# Leverage drift (Study B)
# ==========================================================================


def test_leverage_drift_detected_and_reasserted(rig, caplog):
    """Study B: a stale preference silently governs the next position."""
    engine, exchange, store, rec, trade_id, uid = rig
    open_and_fill(engine, exchange, trade_id, uid)
    protect(engine, exchange, trade_id, uid)
    exchange.leverage["PF_XBTUSD"] = D(3)  # leaked from a failed order

    with caplog.at_level(logging.WARNING):
        report = rec.run_once()

    assert DiscrepancyKind.LEVERAGE_DRIFT in kinds(report)
    assert exchange.leverage["PF_XBTUSD"] == D(10)
    assert any("leverage drift" in r.message for r in caplog.records)


def test_matching_leverage_is_not_flagged(rig):
    engine, exchange, store, rec, trade_id, uid = rig
    open_and_fill(engine, exchange, trade_id, uid)
    protect(engine, exchange, trade_id, uid)
    assert DiscrepancyKind.LEVERAGE_DRIFT not in kinds(rec.run_once())


def test_leverage_drift_refused_when_margin_already_thin(rig, caplog):
    """KRAKEN_NOTES 1C: live set-leverage measurably re-margins an open
    position, so reconciliation must not mutate leverage blind when margin
    is already thin."""
    engine, exchange, store, rec, trade_id, uid = rig
    open_and_fill(engine, exchange, trade_id, uid)
    protect(engine, exchange, trade_id, uid)
    exchange.leverage["PF_XBTUSD"] = D(3)  # leaked from a failed order
    exchange.available = D(10)  # below TRADE_MARGIN_USD=30

    with caplog.at_level(logging.CRITICAL):
        report = rec.run_once()

    assert DiscrepancyKind.LEVERAGE_DRIFT in kinds(report)
    assert exchange.leverage["PF_XBTUSD"] == D(3), (
        "leverage must be left untouched when margin is already too thin"
    )
    drift = next(d for d in report.discrepancies
                 if d.kind is DiscrepancyKind.LEVERAGE_DRIFT)
    assert drift.repaired is False
    assert any("refusing to re-assert" in r.message for r in caplog.records)


def test_leverage_drift_reassert_logs_before_after_margin(rig, caplog):
    engine, exchange, store, rec, trade_id, uid = rig
    open_and_fill(engine, exchange, trade_id, uid)
    protect(engine, exchange, trade_id, uid)
    exchange.leverage["PF_XBTUSD"] = D(3)
    exchange.available = D(500)

    with caplog.at_level(logging.WARNING):
        rec.run_once()

    assert exchange.leverage["PF_XBTUSD"] == D(10)
    records = [r for r in caplog.records
               if getattr(r, "action", None) == "leverage_drift"]
    assert records, "expected a leverage_drift log record"
    assert "$500 -> $500" in records[0].message


class MarginCostingExchange(FakeExchange):
    """A FakeExchange whose set_leverage_preference also consumes margin —
    models the live finding that the mutation itself is not free, so tests
    can exercise the post-mutation escalation path without a real account."""

    def __init__(self, *, margin_cost, **kw):
        super().__init__(**kw)
        self._margin_cost = margin_cost

    def set_leverage_preference(self, symbol, leverage):
        result = super().set_leverage_preference(symbol, leverage)
        self.available -= self._margin_cost
        return result


def test_leverage_drift_escalates_when_reassert_leaves_margin_thin(tmp_path, caplog):
    """Margin was fine going in, but the mutation itself — whose cost cannot
    be known in advance — leaves it thin. Reconciliation must not stay quiet
    about that."""
    config = load_config({
        "TRADING_MODE": "paper", "TRADE_MARGIN_USD": "30",
        "MAX_LEVERAGE": "10", "DEFAULT_LEVERAGE": "10",
        "MAX_ENTRY_DEVIATION_PERCENT": "1.5",
        "KILL_SWITCH_PATH": str(tmp_path / "KILL"),
    }, dotenv_path=None)
    store = Store(tmp_path / "r.db")
    exchange = MarginCostingExchange(mark=D(63800), margin_cost=D(480))
    exchange.available = D(500)  # above the $30 floor going in
    engine = ExecutionEngine(
        config=config, store=store, client=exchange,
        observer=PollingFillObserver(exchange, sleep=lambda s: None,
                                     default_timeout_s=0.05, default_poll_s=0.0),
        kill_switch=KillSwitch(path=tmp_path / "KILL"),
    )
    rec = Reconciler(config=config, store=store, client=exchange,
                     engine=engine, specs=SPECS)
    engine.mark_recovery_complete()
    msg = store.record_message(channel_id=-1, message_id=1, raw_text="BTC LONG")
    trade_id, uid = store.create_trade(
        origin_message_row_id=msg, origin_channel_id=-1, origin_message_id=1,
        asset="BTC", symbol="PF_XBTUSD", direction="LONG",
        intent_stop_loss=D(62500),
    )
    store.transition_trade(trade_id, TradeState.PARSED)
    store.transition_trade(trade_id, TradeState.VALIDATED)
    open_and_fill(engine, exchange, trade_id, uid)
    protect(engine, exchange, trade_id, uid)
    exchange.leverage["PF_XBTUSD"] = D(3)  # leaked from a failed order
    # Reset margin so the cost measured below is isolated to the
    # reconciliation-triggered mutation, not whatever open_and_fill/protect
    # already consumed via their own legitimate set-leverage calls.
    exchange.available = D(500)

    with caplog.at_level(logging.CRITICAL):
        report = rec.run_once()

    assert exchange.leverage["PF_XBTUSD"] == D(10), (
        "the mutation itself still happens — margin can only be checked "
        "before (on prior headroom) and after (on the observed result)"
    )
    drift = next(d for d in report.discrepancies
                 if d.kind is DiscrepancyKind.LEVERAGE_DRIFT)
    assert drift.repaired is True
    assert any(
        getattr(r, "action", None) == "leverage_drift_thin" for r in caplog.records
    )


# ==========================================================================
# Clean state
# ==========================================================================


def test_healthy_trade_reconciles_clean(rig):
    engine, exchange, store, rec, trade_id, uid = rig
    open_and_fill(engine, exchange, trade_id, uid)
    protect(engine, exchange, trade_id, uid)
    report = rec.run_once()
    assert report.clean, [d.kind.value for d in report.discrepancies]


def test_reconciliation_is_idempotent(rig):
    engine, exchange, store, rec, trade_id, uid = rig
    open_and_fill(engine, exchange, trade_id, uid)
    protect(engine, exchange, trade_id, uid)
    rec.run_once()
    assert rec.run_once().clean


def test_engine_observer_is_restored_after_a_cycle(rig):
    """The snapshot observer is scoped to the cycle, not left installed."""
    engine, exchange, store, rec, trade_id, uid = rig
    before = engine.observer
    rec.run_once()
    assert engine.observer is before


# ==========================================================================
# WebSocket fast path — wakes the loop, never mutates state
# ==========================================================================


def test_fill_stream_parses_a_json_event():
    seen = []
    stream = FillStream(on_event=seen.append)
    assert stream.handle_line('{"feed":"fills","fills":[{"order_id":"x"}]}')
    assert len(seen) == 1


@pytest.mark.parametrize("line", ["", "   ", "not json", "[1,2,3]",
                                  '{"error":"auth","message":"nope"}'])
def test_fill_stream_drops_unusable_lines_without_raising(line):
    """A parse failure must not take down the loop — the stream is an
    optimisation, not a source of truth."""
    seen = []
    stream = FillStream(on_event=seen.append)
    assert stream.handle_line(line) is False
    assert seen == []


def test_fill_event_wakes_the_loop_early(rig):
    engine, exchange, store, rec, trade_id, uid = rig
    stream = FillStream()
    loop = ReconciliationLoop(reconciler=rec, interval_seconds=3600, stream=stream)
    assert not loop._wake.is_set()
    stream.handle_line('{"feed":"fills"}')
    assert loop._wake.is_set(), "a fill should trigger an early cycle"


def test_stream_never_mutates_state_directly(rig):
    """The safety property: a stream bug degrades latency, not correctness."""
    engine, exchange, store, rec, trade_id, uid = rig
    open_and_fill(engine, exchange, trade_id, uid)
    before = dict(store.get_trade(trade_id))

    stream = FillStream()
    ReconciliationLoop(reconciler=rec, interval_seconds=3600, stream=stream)
    for _ in range(5):
        stream.handle_line('{"feed":"fills","fills":[{"size":"999"}]}')

    assert dict(store.get_trade(trade_id)) == before


def test_loop_survives_a_failing_cycle(rig):
    """The loop is the safety net; it must not die on one bad cycle."""
    engine, exchange, store, rec, trade_id, uid = rig

    def explode():
        raise RuntimeError("exchange unreachable")
    rec.run_once = explode

    loop = ReconciliationLoop(reconciler=rec, interval_seconds=60)
    report = loop.run_cycle()
    assert report.discrepancies == []
    assert loop.cycles == 1


def test_loop_cycle_counter_advances(rig):
    engine, exchange, store, rec, trade_id, uid = rig
    loop = ReconciliationLoop(reconciler=rec, interval_seconds=60)
    loop.run_cycle()
    loop.run_cycle()
    assert loop.cycles == 2


# ==========================================================================
# Flat-check must be data-driven, not state-allow-listed (soak finding)
# ==========================================================================


def test_quarantined_trade_whose_position_vanished_is_finalised(rig):
    """REGRESSION: the flat check used state.is_open, an allow-list omitting
    REQUIRES_REVIEW, so a quarantined trade's stale orders lingered forever."""
    engine, exchange, store, rec, trade_id, uid = rig
    open_and_fill(engine, exchange, trade_id, uid)
    protect(engine, exchange, trade_id, uid)
    store.transition_trade(trade_id, TradeState.REQUIRES_REVIEW)
    exchange.position = None

    rec.run_once()
    assert store.get_trade(trade_id)["state"] == TradeState.CLOSED.value
    assert exchange.get_open_orders() == []


def test_never_entered_trade_is_not_closed_by_the_flat_check(rig):
    """The other direction: a trade that never held exposure is not 'gone'."""
    engine, exchange, store, rec, trade_id, uid = rig
    engine.open_trade(
        trade_id=trade_id, trade_uid=uid, spec=BTC,
        plan=engine.plan_entries(spec=BTC, direction=Direction.LONG,
                                 entry_prices=ENTRIES, signal_leverage=D(10), stop_loss=STOP),
    )
    rec.run_once()
    assert store.get_trade(trade_id)["state"] != TradeState.CLOSED.value
    assert len(exchange.get_open_orders()) == 2


# ==========================================================================
# Orphan visibility: loud, then reduced — never silent (soak finding)
# ==========================================================================


def test_orphan_logs_loudly_on_first_sight(rig, caplog):
    engine, exchange, store, rec, trade_id, uid = rig
    store.transition_trade(trade_id, TradeState.CANCELLED)
    exchange.positions["PF_XBTUSD"] = Position(
        symbol="PF_XBTUSD", side="long", size=D("0.005"), entry_price=D(64000))
    with caplog.at_level(logging.ERROR):
        rec.run_once()
    assert any("ORPHAN POSITION" in r.message for r in caplog.records)


def test_orphan_does_not_log_every_cycle(rig, caplog):
    """Logging every cycle buries real alerts."""
    engine, exchange, store, rec, trade_id, uid = rig
    store.transition_trade(trade_id, TradeState.CANCELLED)
    exchange.positions["PF_XBTUSD"] = Position(
        symbol="PF_XBTUSD", side="long", size=D("0.005"), entry_price=D(64000))
    rec.run_once()
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        rec.run_once()
        rec.run_once()
    assert not [r for r in caplog.records if "ORPHAN POSITION" in r.message]


def test_orphan_reminder_fires_so_it_cannot_go_silent(rig, caplog):
    """And never fully silent: an unresolved orphan must not age out."""
    engine, exchange, store, rec, trade_id, uid = rig
    store.transition_trade(trade_id, TradeState.CANCELLED)
    exchange.positions["PF_XBTUSD"] = Position(
        symbol="PF_XBTUSD", side="long", size=D("0.005"), entry_price=D(64000))
    rec.run_once()
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        for _ in range(rec.ORPHAN_REMINDER_EVERY):
            rec.run_once()
    assert any("ORPHAN POSITION" in r.message for r in caplog.records), \
        "an unresolved orphan went completely silent"


def test_unresolved_findings_reported_every_cycle(rig, caplog):
    """A standing indicator, so nothing hides between reminders."""
    engine, exchange, store, rec, trade_id, uid = rig
    store.transition_trade(trade_id, TradeState.CANCELLED)
    exchange.positions["PF_XBTUSD"] = Position(
        symbol="PF_XBTUSD", side="long", size=D("0.005"), entry_price=D(64000))
    rec.run_once()
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        rec.run_once()
    assert any("unresolved finding" in r.message for r in caplog.records)
    assert rec.unresolved_findings


def test_resolved_orphan_clears_the_finding(rig):
    engine, exchange, store, rec, trade_id, uid = rig
    store.transition_trade(trade_id, TradeState.CANCELLED)
    exchange.positions["PF_XBTUSD"] = Position(
        symbol="PF_XBTUSD", side="long", size=D("0.005"), entry_price=D(64000))
    rec.run_once()
    assert rec.unresolved_findings
    exchange.positions.clear()
    rec.run_once()
    assert not rec.unresolved_findings


def test_orphan_size_change_relogs_immediately(rig, caplog):
    engine, exchange, store, rec, trade_id, uid = rig
    store.transition_trade(trade_id, TradeState.CANCELLED)
    exchange.positions["PF_XBTUSD"] = Position(
        symbol="PF_XBTUSD", side="long", size=D("0.005"), entry_price=D(64000))
    rec.run_once()
    caplog.clear()
    exchange.positions["PF_XBTUSD"] = Position(
        symbol="PF_XBTUSD", side="long", size=D("0.009"), entry_price=D(64000))
    with caplog.at_level(logging.ERROR):
        rec.run_once()
    assert any("ORPHAN POSITION" in r.message for r in caplog.records)


# ==========================================================================
# Fixed leverage and the account. KRAKEN_NOTES 1C: set-leverage re-margins an
# open position, so it must never be called when the account already holds the
# expected (fixed, 10x) leverage -- and a differing value is corrected to 10x,
# not "respected" as some per-trade figure.
# ==========================================================================


def open_at_fixed(engine, exchange, trade_id, uid):
    plan, _ = open_and_fill(engine, exchange, trade_id, uid, both=False)
    assert plan.leverage == D(10)
    protect(engine, exchange, trade_id, uid)


def count_set_leverage(exchange):
    calls = []
    original = exchange.set_leverage_preference

    def spy(symbol, leverage):
        calls.append((symbol, leverage))
        return original(symbol, leverage)

    exchange.set_leverage_preference = spy
    return calls


def test_open_position_at_the_fixed_leverage_is_never_re_margined(rig):
    engine, exchange, store, rec, trade_id, uid = rig
    open_at_fixed(engine, exchange, trade_id, uid)
    assert exchange.get_position("PF_XBTUSD") is not None
    calls = count_set_leverage(exchange)

    report = rec.run_once()

    assert DiscrepancyKind.LEVERAGE_DRIFT not in kinds(report)
    assert calls == [], "set-leverage on an open position already at 10x"
    assert exchange.leverage["PF_XBTUSD"] == D(10)


def test_drift_is_reasserted_to_the_fixed_ten_not_to_a_per_trade_value(rig):
    engine, exchange, store, rec, trade_id, uid = rig
    open_at_fixed(engine, exchange, trade_id, uid)
    exchange.leverage["PF_XBTUSD"] = D(8)
    exchange.available = D(500)

    report = rec.run_once()

    assert DiscrepancyKind.LEVERAGE_DRIFT in kinds(report)
    assert exchange.leverage["PF_XBTUSD"] == D(10)


def test_drift_is_still_refused_when_margin_is_thin(rig):
    engine, exchange, store, rec, trade_id, uid = rig
    open_at_fixed(engine, exchange, trade_id, uid)
    exchange.leverage["PF_XBTUSD"] = D(8)
    exchange.available = D(10)  # below TRADE_MARGIN_USD=30
    calls = count_set_leverage(exchange)

    report = rec.run_once()

    drift = next(d for d in report.discrepancies
                 if d.kind is DiscrepancyKind.LEVERAGE_DRIFT)
    assert drift.repaired is False
    assert calls == [] and exchange.leverage["PF_XBTUSD"] == D(8)


# ==========================================================================
# Expiry must reach trades that hold NO position (2026-10-06 incident)
# ==========================================================================

OVERDUE = "2000-01-01T00:00:00+00:00"


def _open_resting(engine, trade_id, uid):
    plan = engine.plan_entries(spec=BTC, direction=Direction.LONG,
                               entry_prices=ENTRIES, signal_leverage=D(10), stop_loss=STOP)
    return plan, engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=plan)


def _make_overdue(store):
    with store.transaction() as conn:
        conn.execute("UPDATE orders SET expires_at = ? WHERE purpose = 'ENTRY'",
                     (OVERDUE,))


def test_overdue_entries_of_a_flat_trade_are_cancelled(rig):
    """REGRESSION: expiry ran after `if position is None: return`, so a trade
    still WAITING_FOR_ENTRY never had its overdue entries cancelled."""
    engine, exchange, store, rec, trade_id, uid = rig
    _open_resting(engine, trade_id, uid)
    _make_overdue(store)
    assert exchange.get_position("PF_XBTUSD") is None

    report = rec.run_once()

    assert DiscrepancyKind.STALE_ENTRY_ORDER in kinds(report)
    assert exchange.get_open_orders() == []
    statuses = {r["status"] for r in store.orders_for_trade(trade_id, OrderPurpose.ENTRY)}
    assert statuses == {OrderStatus.EXPIRED.value}


def test_flat_trade_with_every_entry_expired_is_cancelled_and_frees_the_symbol(rig, tmp_path):
    engine, exchange, store, rec, trade_id, uid = rig
    _open_resting(engine, trade_id, uid)
    assert store.get_active_trade_for_symbol("PF_XBTUSD") is not None
    _make_overdue(store)

    rec.run_once()

    assert store.get_trade(trade_id)["state"] == TradeState.CANCELLED.value
    assert store.get_active_trade_for_symbol("PF_XBTUSD") is None


def test_stale_waiting_trade_no_longer_blocks_a_new_signal(rig, tmp_path):
    """End to end through the real admission gate."""
    from bot.admission import SignalAdmission
    from bot.models import RejectionReason
    from bot.parser.parse import fingerprint_for, parse_message

    engine, exchange, store, rec, trade_id, uid = rig
    _open_resting(engine, trade_id, uid)
    gate = SignalAdmission(
        config=engine.config, store=store,
        kill_switch=KillSwitch(path=tmp_path / "KILL"),
        symbol_for={CanonicalAsset.BTC: "PF_XBTUSD"}, position_for=lambda s: None,
    )
    text = ("🟢 BTC LONG\nEntry: 64000 - 63500\nTP1: 65000\nTP2: 66000\n"
            "TP3: 67000\nTP4: 68000\nSL: 62500\nLeverage: 10x")

    def attempt(message_id):
        row = store.record_message(channel_id=-100, message_id=message_id, raw_text=text)
        parsed = parse_message(channel_id=-100, message_id=message_id, text=text)
        store.update_message_parse(
            row, parsed_type=parsed.message_type.value,
            parsed_asset=parsed.asset.value if parsed.asset else None,
            signal_fingerprint=f"fp-{message_id}",
        )
        return gate.admit(parsed, row)

    blocked = attempt(50)
    assert blocked.refused and blocked.reason is RejectionReason.SYMBOL_ALREADY_ACTIVE

    _make_overdue(store)
    rec.run_once()

    assert attempt(51).admitted


def test_unexpired_flat_trade_keeps_waiting(rig):
    engine, exchange, store, rec, trade_id, uid = rig
    _open_resting(engine, trade_id, uid)
    rec.run_once()
    assert store.get_trade(trade_id)["state"] == TradeState.WAITING_FOR_ENTRY.value
    assert len(exchange.get_open_orders()) == 2


def test_flat_trade_with_an_unknown_entry_is_not_cancelled(rig):
    """UNKNOWN means 'could not tell'; it must never be read as 'dead'."""
    engine, exchange, store, rec, trade_id, uid = rig
    _open_resting(engine, trade_id, uid)
    rows = store.orders_for_trade(trade_id, OrderPurpose.ENTRY)
    for r in rows:
        store.update_order(r["id"], status=OrderStatus.UNKNOWN)
    exchange.orders.clear()
    rec.run_once()
    assert store.get_trade(trade_id)["state"] == TradeState.WAITING_FOR_ENTRY.value


def test_entries_that_filled_and_stopped_out_between_observations_are_recorded(rig):
    """REGRESSION (trade 10): both entries filled, the sweep placed a stop and
    it triggered, all before the trade's own sync ran. The trade stayed
    WAITING_FOR_ENTRY with quantity 0 and the loss was never recorded."""
    engine, exchange, store, rec, trade_id, uid = rig
    plan, coids = _open_resting(engine, trade_id, uid)
    exchange.fill(coids[0], plan.entries[0][1], D(64000))
    exchange.fill(coids[1], plan.entries[1][1], D(63500))
    # The protection sweep places the stop from the exchange position...
    protect(engine, exchange, trade_id, uid)
    stop = store.orders_for_trade(trade_id, OrderPurpose.STOP_LOSS)[0]
    # ...and it triggers before sync_entry_fills ever records a quantity.
    exchange.fill(stop["client_order_id"], D(stop["quantity"]), D(62500))
    assert exchange.get_position("PF_XBTUSD") is None
    assert D(store.get_trade(trade_id)["actual_quantity"]) == 0

    report = rec.run_once()

    t = store.get_trade(trade_id)
    total = plan.entries[0][1] + plan.entries[1][1]
    avg = (plan.entries[0][1] * D(64000) + plan.entries[1][1] * D(63500)) / total
    assert t["state"] == TradeState.CLOSED.value
    assert D(t["actual_quantity"]) == total
    expected = ((D(62500) - avg) * total).quantize(D("0.01"))
    assert expected < 0
    assert D(t["realized_pnl"]) == expected
    assert DiscrepancyKind.FILL_NOT_RECORDED in kinds(report)
    assert DiscrepancyKind.POSITION_GONE in kinds(report)
    assert store.get_active_trade_for_symbol("PF_XBTUSD") is None


def test_ghost_recovery_is_idempotent(rig):
    engine, exchange, store, rec, trade_id, uid = rig
    plan, coids = _open_resting(engine, trade_id, uid)
    exchange.fill(coids[0], plan.entries[0][1], D(64000))
    protect(engine, exchange, trade_id, uid)
    stop = store.orders_for_trade(trade_id, OrderPurpose.STOP_LOSS)[0]
    exchange.fill(stop["client_order_id"], D(stop["quantity"]), D(62500))
    rec.run_once()
    pnl = store.get_trade(trade_id)["realized_pnl"]
    rec.run_once()
    assert store.get_trade(trade_id)["realized_pnl"] == pnl
    assert store.get_trade(trade_id)["state"] == TradeState.CLOSED.value
