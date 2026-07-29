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
                               entry_prices=ENTRIES, signal_leverage=D(10))
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
                               entry_prices=ENTRIES, signal_leverage=D(10))
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
                               entry_prices=ENTRIES, signal_leverage=D(10))
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
                               entry_prices=ENTRIES, signal_leverage=D(10))
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
                                 entry_prices=ENTRIES, signal_leverage=D(10)),
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
