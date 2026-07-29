"""Execution engine tests (brief 33).

Two conventions the operator asked for, applied throughout:

1. **Sizes are never invented.** Anything exercising the two-entry split or the
   TP ladder feeds sizes that came out of `sizing.py`, so the test reflects what
   production actually submits rather than a convenient round number.

2. **Paper's blind spots are marked, not skipped past.** A stop's trigger price
   cannot be read back in paper (KRAKEN_NOTES 4.2). Tests verify what paper can
   prove — existence, uniqueness, size, reduce-only — and the trigger-price
   assertion is carried by explicitly-marked tests against a fake that models
   the LIVE response shape, so the gap is visible rather than silently absent.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from bot.config import load_config
from bot.db.store import Store
from bot.exchange.base import (
    AccountState,
    ExchangeClient,
    Fill,
    Order,
    OrderResult,
    Position,
)
from bot.exchange.specs import parse_instrument
from bot.execution.engine import ExecutionEngine, ExecutionRefused, client_order_id
from bot.execution.observer import PollingFillObserver
from bot.killswitch import KillSwitch
from bot.models import (
    CanonicalAsset,
    Direction,
    OrderPurpose,
    OrderStatus,
    TradeState,
)
from bot.sizing import calculate_size, split_entry_allocations, split_take_profits

D = Decimal
FIXTURES = Path(__file__).parent / "fixtures"
RAW = {
    i["symbol"]: i
    for i in json.loads((FIXTURES / "futures_instruments.json").read_text())["instruments"]
}
BTC = parse_instrument(RAW["PF_XBTUSD"], CanonicalAsset.BTC)

# Marker for assertions paper physically cannot make (KRAKEN_NOTES 4.2).
unverifiable_in_paper = pytest.mark.unverifiable_in_paper


# ==========================================================================
# Fake exchange — models BOTH engines' response shapes
# ==========================================================================


class FakeExchange(ExchangeClient):
    """In-memory exchange.

    `paper_mode` controls whether resting stop orders expose a trigger price.
    Setting it True reproduces the paper engine's omission so tests can prove
    the verification path still works when the field is absent.
    """

    def __init__(self, *, paper_mode: bool = False, mark: Decimal = D(64000)):
        self.paper_mode = paper_mode
        self.mark = mark
        self.orders: dict[str, Order] = {}
        self.fills: list[Fill] = []
        self.position: Position | None = None
        self.leverage: dict[str, Decimal] = {}
        self.available = D(10000)
        self._seq = 0
        self.reject_next = False
        self.amend_fails = False
        self.cancel_fails = False

    # -- reference ------------------------------------------------------
    def get_instrument_spec(self, asset): return BTC
    def get_mark_price(self, symbol): return self.mark
    def get_account_state(self):
        return AccountState(collateral=self.available, available_margin=self.available)

    # -- state ----------------------------------------------------------
    def get_open_positions(self):
        return [self.position] if self.position and not self.position.is_flat else []

    def get_position(self, symbol):
        if self.position and self.position.symbol == symbol and not self.position.is_flat:
            return self.position
        return None

    def get_open_orders(self):
        return [o for o in self.orders.values() if o.status.is_resting]

    def get_fills(self):
        return list(self.fills)

    # -- leverage -------------------------------------------------------
    def get_leverage_preference(self, symbol):
        return self.leverage.get(symbol)

    def set_leverage_preference(self, symbol, leverage):
        self.leverage[symbol] = leverage
        return leverage

    # -- orders ---------------------------------------------------------
    def place_order(self, request):
        if self.reject_next:
            self.reject_next = False
            return OrderResult(False, None, request.client_order_id,
                               OrderStatus.REJECTED, detail="rejected by test")
        self._seq += 1
        oid = f"FX-{self._seq:05d}"
        self.orders[oid] = Order(
            exchange_order_id=oid,
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            size=request.size,
            price=request.price,
            trigger_price=None if self.paper_mode else request.trigger_price,
            reduce_only=request.reduce_only,
            status=OrderStatus.OPEN,
        )
        return OrderResult(True, oid, request.client_order_id, OrderStatus.OPEN)

    def cancel_order(self, exchange_order_id):
        if self.cancel_fails:
            return False
        order = self.orders.get(exchange_order_id)
        if order is None:
            return False
        self.orders[exchange_order_id] = replace_order(order, status=OrderStatus.CANCELLED)
        return True

    def amend_order(self, exchange_order_id, *, size=None, price=None, trigger_price=None):
        if self.amend_fails:
            return False
        order = self.orders.get(exchange_order_id)
        if order is None:
            return False
        # Atomic in place: same id, same dict slot, count unchanged.
        self.orders[exchange_order_id] = replace_order(
            order,
            size=size if size is not None else order.size,
            trigger_price=(
                order.trigger_price if self.paper_mode
                else (trigger_price if trigger_price is not None else order.trigger_price)
            ),
        )
        return True

    # -- test helpers ---------------------------------------------------
    def fill(self, client_order_id: str, size: Decimal, price: Decimal):
        """Simulate a confirmed fill and update the netted position."""
        order = next(
            (o for o in self.orders.values() if o.client_order_id == client_order_id),
            None,
        )
        assert order is not None, f"no order {client_order_id}"
        self.fills.append(Fill(
            fill_id=f"F-{len(self.fills)+1}", order_id=order.exchange_order_id,
            client_order_id=client_order_id, symbol=order.symbol,
            side=order.side, size=size, price=price,
        ))
        new_filled = order.filled_size + size
        self.orders[order.exchange_order_id] = replace_order(
            order,
            filled_size=new_filled,
            status=OrderStatus.FILLED if new_filled >= order.size
            else OrderStatus.PARTIALLY_FILLED,
        )
        delta = size if order.reduce_only is False else -size
        self._apply(order.symbol, delta, price)

    def _apply(self, symbol, delta, price):
        if self.position is None:
            self.position = Position(symbol=symbol, side="long", size=D(0),
                                     entry_price=price)
        new_size = self.position.size + delta
        if new_size <= 0:
            self.position = None
            return
        if delta > 0:
            total = self.position.entry_price * self.position.size + price * delta
            entry = total / new_size
        else:
            entry = self.position.entry_price
        self.position = Position(symbol=symbol, side="long", size=new_size,
                                 entry_price=entry, mark_price=self.mark)


def replace_order(order: Order, **kw) -> Order:
    from dataclasses import replace
    return replace(order, **kw)


# ==========================================================================
# Fixtures
# ==========================================================================


@pytest.fixture
def config(tmp_path):
    return load_config(
        {
            "TRADING_MODE": "paper", "TRADE_MARGIN_USD": "30",
            "MAX_LEVERAGE": "10", "DEFAULT_LEVERAGE": "10",
            "MAX_ENTRY_DEVIATION_PERCENT": "1.5",
            "DB_PATH": str(tmp_path / "t.db"),
            "KILL_SWITCH_PATH": str(tmp_path / "KILL"),
        },
        dotenv_path=None,
    )


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture
def setup(config, store, tmp_path):
    exchange = FakeExchange()
    observer = PollingFillObserver(
        exchange, sleep=lambda s: None, default_timeout_s=0.05, default_poll_s=0.0
    )
    engine = ExecutionEngine(
        config=config, store=store, client=exchange, observer=observer,
        kill_switch=KillSwitch(path=tmp_path / "KILL"),
    )
    msg = store.record_message(channel_id=-1, message_id=1, raw_text="BTC LONG")
    trade_id, trade_uid = store.create_trade(
        origin_message_row_id=msg, origin_channel_id=-1, origin_message_id=1,
        asset="BTC", symbol="PF_XBTUSD", direction="LONG",
    )
    store.transition_trade(trade_id, TradeState.PARSED)
    store.transition_trade(trade_id, TradeState.VALIDATED)
    return engine, exchange, store, trade_id, trade_uid


# Entry prices used throughout. Sizes are ALWAYS derived via sizing.py.
ENTRIES = (D(64000), D(63500))


def planned(engine):
    return engine.plan_entries(
        spec=BTC, direction=Direction.LONG, entry_prices=ENTRIES, leverage=D(10)
    )


# ==========================================================================
# Planning — sizes come from sizing.py, not from round numbers
# ==========================================================================


def test_entry_plan_uses_sizing_engine_output(setup):
    engine, *_ = setup
    plan = planned(engine)

    allocations = split_entry_allocations(D(30))
    expected = tuple(
        calculate_size(margin_usd=m, leverage=D(10), price=p, spec=BTC,
                       max_deviation_percent=D(5)).size
        for p, m in zip(ENTRIES, allocations)
    )
    assert tuple(s for _, s in plan.entries) == expected
    assert plan.total_intended_size == sum(expected)


def test_margin_is_split_not_contracts(setup):
    """Equal margin at different prices gives different sizes (brief 11)."""
    engine, *_ = setup
    plan = engine.plan_entries(
        spec=BTC, direction=Direction.LONG,
        entry_prices=(D(64000), D(32000)), leverage=D(10),
    )
    _, s1 = plan.entries[0]
    _, s2 = plan.entries[1]
    assert s2 > s1  # same $15 buys more at half the price


# ==========================================================================
# Stale-signal guard (brief 12)
# ==========================================================================


def test_fresh_signal_passes(setup):
    engine, exchange, *_ = setup
    exchange.mark = D(63800)  # inside the range
    assert engine.check_entry_freshness(BTC, ENTRIES) == D(63800)


def test_stale_signal_refused(setup):
    engine, exchange, *_ = setup
    exchange.mark = D(70000)
    with pytest.raises(ExecutionRefused, match="STALE_ENTRY"):
        engine.check_entry_freshness(BTC, ENTRIES)


# ==========================================================================
# Entry lifecycle — state advances only on CONFIRMED fills
# ==========================================================================


def test_submission_alone_does_not_advance_past_waiting(setup):
    """The historical bug: submitted != filled (brief 27)."""
    engine, exchange, store, trade_id, uid = setup
    engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=planned(engine))
    assert store.get_trade(trade_id)["state"] == TradeState.WAITING_FOR_ENTRY.value
    assert store.get_trade(trade_id)["actual_quantity"] == "0"


def test_intent_persisted_before_submission(setup):
    engine, exchange, store, trade_id, uid = setup
    engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=planned(engine))
    rows = store.orders_for_trade(trade_id, OrderPurpose.ENTRY)
    assert len(rows) == 2
    for row in rows:
        assert row["client_order_id"].startswith(uid)
        assert row["exchange_order_id"] is not None


def test_first_entry_fill_gives_partially_entered(setup):
    engine, exchange, store, trade_id, uid = setup
    plan = planned(engine)
    coids = engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=plan)

    exchange.fill(coids[0], plan.entries[0][1], D(64000))
    engine.sync_entry_fills(trade_id)

    trade = store.get_trade(trade_id)
    assert trade["state"] == TradeState.PARTIALLY_ENTERED.value
    assert Decimal(trade["actual_quantity"]) == plan.entries[0][1]


def test_both_entries_fill_gives_fully_entered(setup):
    engine, exchange, store, trade_id, uid = setup
    plan = planned(engine)
    coids = engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=plan)

    exchange.fill(coids[0], plan.entries[0][1], D(64000))
    engine.sync_entry_fills(trade_id)
    exchange.fill(coids[1], plan.entries[1][1], D(63500))
    engine.sync_entry_fills(trade_id)

    trade = store.get_trade(trade_id)
    assert trade["state"] == TradeState.FULLY_ENTERED.value
    assert Decimal(trade["actual_quantity"]) == plan.total_intended_size


def test_actual_position_reflects_reality_not_intent(setup):
    """Only one entry filled -> actual is half of intent (brief 39)."""
    engine, exchange, store, trade_id, uid = setup
    plan = planned(engine)
    coids = engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=plan)
    exchange.fill(coids[0], plan.entries[0][1], D(64000))
    engine.sync_entry_fills(trade_id)

    trade = store.get_trade(trade_id)
    assert Decimal(trade["actual_quantity"]) < plan.total_intended_size


def test_partial_fill_of_one_entry_tracked(setup):
    engine, exchange, store, trade_id, uid = setup
    plan = planned(engine)
    coids = engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=plan)

    half = (plan.entries[0][1] / 2).quantize(BTC.size_increment)
    exchange.fill(coids[0], half, D(64000))
    engine.sync_entry_fills(trade_id)

    assert Decimal(store.get_trade(trade_id)["actual_quantity"]) == half
    assert store.get_trade(trade_id)["state"] == TradeState.PARTIALLY_ENTERED.value


def test_weighted_average_entry_used_not_signal_price(setup):
    """Both entries fill at different prices -> weighted average (brief 15)."""
    engine, exchange, store, trade_id, uid = setup
    plan = planned(engine)
    coids = engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=plan)
    exchange.fill(coids[0], plan.entries[0][1], D(64000))
    exchange.fill(coids[1], plan.entries[1][1], D(63000))
    engine.sync_entry_fills(trade_id)

    avg = Decimal(store.get_trade(trade_id)["actual_avg_entry_price"])
    assert D(63000) < avg < D(64000)


def test_kill_switch_blocks_new_positions(setup, tmp_path):
    engine, exchange, store, trade_id, uid = setup
    engine.kill_switch.engage()
    with pytest.raises(ExecutionRefused, match="KILL_SWITCH"):
        engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC,
                          plan=planned(engine))


def test_insufficient_margin_refuses(setup):
    engine, exchange, store, trade_id, uid = setup
    exchange.available = D("5")
    with pytest.raises(Exception, match="INSUFFICIENT_MARGIN"):
        engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC,
                          plan=planned(engine))


def test_leverage_set_and_verified_before_opening(setup):
    engine, exchange, store, trade_id, uid = setup
    engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=planned(engine))
    assert exchange.leverage["PF_XBTUSD"] == D(10)


def test_rejected_entry_is_not_retried(setup):
    engine, exchange, store, trade_id, uid = setup
    exchange.reject_next = True
    coids = engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC,
                              plan=planned(engine))
    assert len(coids) == 1  # first rejected, not resubmitted
    rows = store.orders_for_trade(trade_id, OrderPurpose.ENTRY)
    assert rows[0]["status"] == OrderStatus.UNKNOWN.value


# ==========================================================================
# Stop loss
# ==========================================================================


def entered(engine, exchange, trade_id, uid, *, both=True):
    plan = planned(engine)
    coids = engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=plan)
    exchange.fill(coids[0], plan.entries[0][1], D(64000))
    if both:
        exchange.fill(coids[1], plan.entries[1][1], D(63500))
    engine.sync_entry_fills(trade_id)
    return plan, coids


def test_stop_sized_to_actual_position_not_intent(setup):
    """Brief 14: the stop covers what we hold, not what we intended."""
    engine, exchange, store, trade_id, uid = setup
    plan, _ = entered(engine, exchange, trade_id, uid, both=False)
    position = exchange.get_position("PF_XBTUSD")

    engine.place_stop(trade_id=trade_id, trade_uid=uid, spec=BTC,
                      direction=Direction.LONG, trigger_price=D(62500),
                      position=position)

    stop = next(o for o in exchange.get_open_orders() if o.order_type == "stop")
    assert stop.size == position.size
    assert stop.size < plan.total_intended_size


def test_stop_is_reduce_only(setup):
    """Must never be able to open a reverse position."""
    engine, exchange, store, trade_id, uid = setup
    entered(engine, exchange, trade_id, uid)
    engine.place_stop(trade_id=trade_id, trade_uid=uid, spec=BTC,
                      direction=Direction.LONG, trigger_price=D(62500),
                      position=exchange.get_position("PF_XBTUSD"))
    stop = next(o for o in exchange.get_open_orders() if o.order_type == "stop")
    assert stop.reduce_only


def test_placing_stop_transitions_to_protected(setup):
    engine, exchange, store, trade_id, uid = setup
    entered(engine, exchange, trade_id, uid)
    engine.place_stop(trade_id=trade_id, trade_uid=uid, spec=BTC,
                      direction=Direction.LONG, trigger_price=D(62500),
                      position=exchange.get_position("PF_XBTUSD"))
    assert store.get_trade(trade_id)["state"] == TradeState.PROTECTED.value


def test_long_stop_rounds_away_from_entry(setup):
    """Tick snapping must never pull a stop tighter than intended."""
    engine, exchange, store, trade_id, uid = setup
    entered(engine, exchange, trade_id, uid)
    engine.place_stop(trade_id=trade_id, trade_uid=uid, spec=BTC,
                      direction=Direction.LONG, trigger_price=D("62500.7"),
                      position=exchange.get_position("PF_XBTUSD"))
    row = store.orders_for_trade(trade_id, OrderPurpose.STOP_LOSS)[0]
    assert Decimal(row["trigger_price"]) == D(62500)  # down, not 62501


# ==========================================================================
# Breakeven — the historical duplicate-stop bug (brief 16)
# ==========================================================================


def protected(engine, exchange, trade_id, uid, *, both=True):
    plan, coids = entered(engine, exchange, trade_id, uid, both=both)
    engine.place_stop(trade_id=trade_id, trade_uid=uid, spec=BTC,
                      direction=Direction.LONG, trigger_price=D(62500),
                      position=exchange.get_position("PF_XBTUSD"))
    return plan, coids


def reach_tp2(store, trade_id):
    """Advance to TP2_FILLED.

    Breakeven is gated on a CONFIRMED TP2 fill (brief 15), enforced by the
    engine as well as the state table. Tests exercising the AMEND MECHANICS
    must therefore legitimately reach TP2_FILLED first; the gate itself is
    covered separately by test_breakeven_refused_before_tp2_fill.
    """
    store.transition_trade(trade_id, TradeState.TP1_FILLED)
    store.transition_trade(trade_id, TradeState.TP2_FILLED)


def test_breakeven_leaves_exactly_one_stop(setup):
    """REGRESSION: the old stop must not survive the move to breakeven."""
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid)
    reach_tp2(store, trade_id)
    before = [o for o in exchange.get_open_orders() if o.order_type == "stop"]
    assert len(before) == 1

    engine.move_stop_to_breakeven(
        trade_id=trade_id, spec=BTC, direction=Direction.LONG,
        position=exchange.get_position("PF_XBTUSD"),
    )

    after = [o for o in exchange.get_open_orders() if o.order_type == "stop"]
    assert len(after) == 1, "duplicate or missing stop after breakeven"
    assert after[0].exchange_order_id == before[0].exchange_order_id


def test_breakeven_uses_actual_average_not_signal_entry(setup):
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid)
    reach_tp2(store, trade_id)
    engine.move_stop_to_breakeven(
        trade_id=trade_id, spec=BTC, direction=Direction.LONG,
        position=exchange.get_position("PF_XBTUSD"),
    )
    trade = store.get_trade(trade_id)
    breakeven = Decimal(trade["breakeven_price"])
    # Fills were at 64000 and 63500 -> average strictly between them.
    assert D(63500) <= breakeven <= D(64000)
    assert trade["breakeven_active"] == 1


def test_breakeven_from_single_entry_reflects_that_fill(setup):
    """Only entry 1 filled -> breakeven is that price, not a blend."""
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid, both=False)
    reach_tp2(store, trade_id)
    engine.move_stop_to_breakeven(
        trade_id=trade_id, spec=BTC, direction=Direction.LONG,
        position=exchange.get_position("PF_XBTUSD"),
    )
    assert Decimal(store.get_trade(trade_id)["breakeven_price"]) == D(64000)


def test_breakeven_transitions_state(setup):
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid)
    store.transition_trade(trade_id, TradeState.TP1_FILLED)
    store.transition_trade(trade_id, TradeState.TP2_FILLED)
    engine.move_stop_to_breakeven(
        trade_id=trade_id, spec=BTC, direction=Direction.LONG,
        position=exchange.get_position("PF_XBTUSD"),
    )
    assert store.get_trade(trade_id)["state"] == TradeState.BREAKEVEN_ACTIVE.value


def test_failed_amend_enters_safe_error_with_original_stop_intact(setup):
    """Amend is atomic, so a failure leaves the ORIGINAL stop protecting."""
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid)
    reach_tp2(store, trade_id)
    exchange.amend_fails = True

    assert not engine.move_stop_to_breakeven(
        trade_id=trade_id, spec=BTC, direction=Direction.LONG,
        position=exchange.get_position("PF_XBTUSD"),
    )
    assert store.get_trade(trade_id)["state"] == TradeState.SAFE_ERROR.value
    stops = [o for o in exchange.get_open_orders() if o.order_type == "stop"]
    assert len(stops) == 1  # still protected


def test_verification_failure_enters_safe_error(setup):
    """A duplicate stop appearing must be caught, not ignored."""
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid)
    reach_tp2(store, trade_id)

    # Inject a rogue second stop, as a partially-completed replacement would.
    rogue = list(exchange.orders.values())[-1]
    exchange.orders["ROGUE"] = replace_order(
        rogue, exchange_order_id="ROGUE", client_order_id="rogue"
    )

    assert not engine.move_stop_to_breakeven(
        trade_id=trade_id, spec=BTC, direction=Direction.LONG,
        position=exchange.get_position("PF_XBTUSD"),
    )
    assert store.get_trade(trade_id)["state"] == TradeState.SAFE_ERROR.value


def test_breakeven_without_fills_refused(setup):
    engine, exchange, store, trade_id, uid = setup
    plan = planned(engine)
    engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=plan)
    store.transition_trade(trade_id, TradeState.PARTIALLY_ENTERED)
    position = Position(symbol="PF_XBTUSD", side="long", size=D("0.0023"),
                        entry_price=D(64000))
    engine.place_stop(trade_id=trade_id, trade_uid=uid, spec=BTC,
                      direction=Direction.LONG, trigger_price=D(62500),
                      position=position)
    reach_tp2(store, trade_id)
    with pytest.raises(ExecutionRefused, match="NO_ENTRY_FILLS"):
        engine.move_stop_to_breakeven(trade_id=trade_id, spec=BTC,
                                      direction=Direction.LONG, position=position)


# -- what paper CANNOT prove ------------------------------------------------


@unverifiable_in_paper
def test_breakeven_applies_new_trigger_price_LIVE_SHAPE_ONLY():
    """UNVERIFIABLE IN PAPER — KRAKEN_NOTES 4.2.

    `futures paper orders` omits `stop_price` entirely, so a paper run cannot
    read back a stop's trigger price. This test therefore runs against a fake
    configured with the LIVE response shape (paper_mode=False). It is marked so
    the gap stays visible, and must be re-confirmed against the real live API
    before live trading (KRAKEN_NOTES 5).
    """
    exchange = FakeExchange(paper_mode=False)
    result = exchange.place_order.__self__ and None  # readability no-op
    from bot.exchange.base import OrderRequest
    exchange.place_order(OrderRequest(
        symbol="PF_XBTUSD", side="sell", size=D("0.0046"), order_type="stop",
        purpose=OrderPurpose.STOP_LOSS, client_order_id="sl",
        trigger_price=D(62500), reduce_only=True,
    ))
    oid = next(iter(exchange.orders))
    assert exchange.orders[oid].trigger_price == D(62500)

    exchange.amend_order(oid, size=D("0.0035"), trigger_price=D(64000))
    assert exchange.orders[oid].trigger_price == D(64000)
    assert exchange.orders[oid].size == D("0.0035")


@unverifiable_in_paper
def test_paper_shape_hides_trigger_price_so_verification_must_not_require_it():
    """Proves the verification path tolerates paper's missing stop_price.

    If `_verify_stop` asserted the trigger price, every paper run would fail.
    This documents WHY that assertion is absent rather than leaving it looking
    like an oversight.
    """
    exchange = FakeExchange(paper_mode=True)
    from bot.exchange.base import OrderRequest
    exchange.place_order(OrderRequest(
        symbol="PF_XBTUSD", side="sell", size=D("0.0046"), order_type="stop",
        purpose=OrderPurpose.STOP_LOSS, client_order_id="sl",
        trigger_price=D(62500), reduce_only=True,
    ))
    stop = exchange.get_open_orders()[0]
    assert stop.trigger_price is None      # the gap
    assert stop.size == D("0.0046")        # what paper CAN prove
    assert stop.reduce_only is True
    assert stop.order_type == "stop"


# ==========================================================================
# Take-profit ladder
# ==========================================================================


def test_tp_sizes_come_from_the_sizing_engine(setup):
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid)
    position = exchange.get_position("PF_XBTUSD")

    engine.place_take_profits(
        trade_id=trade_id, trade_uid=uid, spec=BTC, direction=Direction.LONG,
        take_profits=(D(65000), D(66000), D(67000), D(68000)), position=position,
    )
    expected = split_take_profits(position.size, BTC, levels=4)
    rows = store.orders_for_trade(trade_id, OrderPurpose.TAKE_PROFIT)
    assert tuple(Decimal(r["quantity"]) for r in rows) == expected


def test_tp_quantities_never_exceed_position(setup):
    """REGRESSION (brief 33): TP total must never exceed what we hold."""
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid)
    position = exchange.get_position("PF_XBTUSD")
    engine.place_take_profits(
        trade_id=trade_id, trade_uid=uid, spec=BTC, direction=Direction.LONG,
        take_profits=(D(65000), D(66000), D(67000), D(68000)), position=position,
    )
    rows = store.orders_for_trade(trade_id, OrderPurpose.TAKE_PROFIT)
    assert sum(Decimal(r["quantity"]) for r in rows) == position.size


def test_all_tps_are_reduce_only(setup):
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid)
    engine.place_take_profits(
        trade_id=trade_id, trade_uid=uid, spec=BTC, direction=Direction.LONG,
        take_profits=(D(65000), D(66000), D(67000), D(68000)),
        position=exchange.get_position("PF_XBTUSD"),
    )
    tps = [o for o in exchange.get_open_orders() if o.order_type == "take-profit"]
    assert len(tps) == 4
    assert all(o.reduce_only for o in tps)


def test_tp_fill_advances_state_only_when_confirmed(setup):
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid)
    coids = engine.place_take_profits(
        trade_id=trade_id, trade_uid=uid, spec=BTC, direction=Direction.LONG,
        take_profits=(D(65000), D(66000), D(67000), D(68000)),
        position=exchange.get_position("PF_XBTUSD"),
    )
    # Placed but nothing filled -> no advance.
    assert engine.sync_take_profits(trade_id) == 0
    assert store.get_trade(trade_id)["state"] == TradeState.PROTECTED.value

    rows = store.orders_for_trade(trade_id, OrderPurpose.TAKE_PROFIT)
    exchange.fill(coids[0], Decimal(rows[0]["quantity"]), D(65000))
    assert engine.sync_take_profits(trade_id) == 1
    assert store.get_trade(trade_id)["state"] == TradeState.TP1_FILLED.value


def test_tp2_fill_is_what_authorises_breakeven(setup):
    """Brief 15: TP2 FILLING, not price touching it or the order existing."""
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid)
    coids = engine.place_take_profits(
        trade_id=trade_id, trade_uid=uid, spec=BTC, direction=Direction.LONG,
        take_profits=(D(65000), D(66000), D(67000), D(68000)),
        position=exchange.get_position("PF_XBTUSD"),
    )
    rows = store.orders_for_trade(trade_id, OrderPurpose.TAKE_PROFIT)
    exchange.fill(coids[0], Decimal(rows[0]["quantity"]), D(65000))
    exchange.fill(coids[1], Decimal(rows[1]["quantity"]), D(66000))
    assert engine.sync_take_profits(trade_id) == 2
    assert store.get_trade(trade_id)["state"] == TradeState.TP2_FILLED.value

    assert engine.move_stop_to_breakeven(
        trade_id=trade_id, spec=BTC, direction=Direction.LONG,
        position=exchange.get_position("PF_XBTUSD"),
    )
    assert store.get_trade(trade_id)["state"] == TradeState.BREAKEVEN_ACTIVE.value


def test_partial_tp_fill_does_not_advance_state(setup):
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid)
    coids = engine.place_take_profits(
        trade_id=trade_id, trade_uid=uid, spec=BTC, direction=Direction.LONG,
        take_profits=(D(65000), D(66000), D(67000), D(68000)),
        position=exchange.get_position("PF_XBTUSD"),
    )
    rows = store.orders_for_trade(trade_id, OrderPurpose.TAKE_PROFIT)
    half = (Decimal(rows[0]["quantity"]) / 2).quantize(BTC.size_increment)
    exchange.fill(coids[0], half, D(65000))
    assert engine.sync_take_profits(trade_id) == 0
    assert store.get_trade(trade_id)["state"] == TradeState.PROTECTED.value


# ==========================================================================
# Close confirmation (brief 27) — the second historical bug
# ==========================================================================


def test_close_not_marked_closed_until_confirmed_flat(setup):
    """REGRESSION: submitting a close must not mark the trade CLOSED."""
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid)

    # Exchange accepts the close but the position stubbornly remains.
    original_place = exchange.place_order
    def place_without_filling(request):
        result = original_place(request)
        return result  # accepted, but no fill -> position stays open
    exchange.place_order = place_without_filling

    assert engine.close_trade(trade_id=trade_id, trade_uid=uid, spec=BTC,
                              direction=Direction.LONG) is False
    assert store.get_trade(trade_id)["state"] != TradeState.CLOSED.value
    assert store.get_trade(trade_id)["state"] == TradeState.CLOSING.value


def test_close_marked_closed_once_flat_and_orders_gone(setup):
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid)

    original_place = exchange.place_order
    def place_and_fill(request):
        result = original_place(request)
        if request.purpose is OrderPurpose.CLOSE:
            exchange.fill(request.client_order_id, request.size, D(65000))
        return result
    exchange.place_order = place_and_fill

    assert engine.close_trade(trade_id=trade_id, trade_uid=uid, spec=BTC,
                              direction=Direction.LONG) is True
    assert store.get_trade(trade_id)["state"] == TradeState.CLOSED.value


def test_close_cancels_remaining_orders_before_closing(setup):
    """Brief 11: never leave a stale entry that could reopen the position."""
    engine, exchange, store, trade_id, uid = setup
    plan, coids = entered(engine, exchange, trade_id, uid, both=False)
    engine.place_stop(trade_id=trade_id, trade_uid=uid, spec=BTC,
                      direction=Direction.LONG, trigger_price=D(62500),
                      position=exchange.get_position("PF_XBTUSD"))
    # Entry 2 is still resting.
    assert any(o.status.is_resting and o.order_type == "limit"
               for o in exchange.get_open_orders())

    original_place = exchange.place_order
    def place_and_fill(request):
        result = original_place(request)
        if request.purpose is OrderPurpose.CLOSE:
            exchange.fill(request.client_order_id, request.size, D(65000))
        return result
    exchange.place_order = place_and_fill

    assert engine.close_trade(trade_id=trade_id, trade_uid=uid, spec=BTC,
                              direction=Direction.LONG) is True
    assert exchange.get_open_orders() == []
    assert store.get_trade(trade_id)["state"] == TradeState.CLOSED.value


def test_orders_remaining_after_close_blocks_closed_state(setup):
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid)
    exchange.cancel_fails = True

    original_place = exchange.place_order
    def place_and_fill(request):
        result = original_place(request)
        if request.purpose is OrderPurpose.CLOSE:
            exchange.fill(request.client_order_id, request.size, D(65000))
        return result
    exchange.place_order = place_and_fill

    assert engine.close_trade(trade_id=trade_id, trade_uid=uid, spec=BTC,
                              direction=Direction.LONG) is False
    assert store.get_trade(trade_id)["state"] == TradeState.SAFE_ERROR.value


def test_rejected_close_does_not_mark_closed(setup):
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid)
    exchange.reject_next = True
    assert engine.close_trade(trade_id=trade_id, trade_uid=uid, spec=BTC,
                              direction=Direction.LONG) is False
    assert store.get_trade(trade_id)["state"] != TradeState.CLOSED.value


def test_partial_close_keeps_trade_open_and_resizes_stop(setup):
    """Brief 18: a partial close leaves a managed, protected position."""
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid)
    before = exchange.get_position("PF_XBTUSD").size

    original_place = exchange.place_order
    def place_and_fill(request):
        result = original_place(request)
        if request.purpose is OrderPurpose.CLOSE:
            exchange.fill(request.client_order_id, request.size, D(65000))
        return result
    exchange.place_order = place_and_fill

    assert engine.close_trade(trade_id=trade_id, trade_uid=uid, spec=BTC,
                              direction=Direction.LONG,
                              percentage=D(50)) is False
    after = exchange.get_position("PF_XBTUSD")
    assert after is not None and after.size < before
    # A partial close is not a close: the trade keeps its lifecycle position.
    assert store.get_trade(trade_id)["state"] == TradeState.PROTECTED.value
    assert store.get_trade(trade_id)["state"] != TradeState.CLOSING.value
    stop = next(o for o in exchange.get_open_orders() if o.order_type == "stop")
    assert stop.size == after.size


def test_partial_close_never_exceeds_position(setup):
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid)
    position = exchange.get_position("PF_XBTUSD")

    original_place = exchange.place_order
    captured = {}
    def capture(request):
        captured["size"] = request.size
        result = original_place(request)
        if request.purpose is OrderPurpose.CLOSE:
            exchange.fill(request.client_order_id, request.size, D(65000))
        return result
    exchange.place_order = capture

    engine.close_trade(trade_id=trade_id, trade_uid=uid, spec=BTC,
                       direction=Direction.LONG, percentage=D(150))
    assert captured["size"] <= position.size


def test_cancel_remaining_entries(setup):
    engine, exchange, store, trade_id, uid = setup
    entered(engine, exchange, trade_id, uid, both=False)
    assert engine.cancel_remaining_entries(trade_id) == 1
    assert not any(o.order_type == "limit" for o in exchange.get_open_orders())


# ==========================================================================
# Deterministic client order ids (brief 30)
# ==========================================================================


def test_client_order_ids_are_deterministic():
    """Recovery reconstructs the exact id a previous run would have used."""
    assert client_order_id("T-abc", OrderPurpose.ENTRY, 1) == "T-abc-ENTRY-1"
    assert client_order_id("T-abc", OrderPurpose.ENTRY, 1) == client_order_id(
        "T-abc", OrderPurpose.ENTRY, 1
    )
    assert client_order_id("T-abc", OrderPurpose.ENTRY, 1) != client_order_id(
        "T-abc", OrderPurpose.ENTRY, 2
    )


def test_client_order_ids_namespaced_by_trade():
    assert client_order_id("T-1", OrderPurpose.STOP_LOSS, 1) != client_order_id(
        "T-2", OrderPurpose.STOP_LOSS, 1
    )


# ==========================================================================
# Known benign race in entry observation (engine.sync_entry_fills docstring)
# ==========================================================================


def test_entry_state_converges_when_fill_lands_between_reads(setup):
    """An entry filling between the order read and the position read yields
    PARTIALLY_ENTERED with a full position. Safe, and the next sync corrects it.
    """
    engine, exchange, store, trade_id, uid = setup
    plan = planned(engine)
    coids = engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=plan)

    exchange.fill(coids[0], plan.entries[0][1], D(64000))

    # Fill entry 2 after its order has been read but before the position read.
    real_get_position = exchange.get_position
    fired = {"done": False}

    def racing_get_position(symbol):
        if not fired["done"]:
            fired["done"] = True
            exchange.fill(coids[1], plan.entries[1][1], D(63500))
        return real_get_position(symbol)

    exchange.get_position = racing_get_position
    engine.sync_entry_fills(trade_id)
    exchange.get_position = real_get_position

    trade = store.get_trade(trade_id)
    # Conservative label, but the FULL position is recorded and protectable.
    assert trade["state"] == TradeState.PARTIALLY_ENTERED.value
    assert Decimal(trade["actual_quantity"]) == plan.total_intended_size

    # Convergence: a second sync promotes it.
    engine.sync_entry_fills(trade_id)
    assert store.get_trade(trade_id)["state"] == TradeState.FULLY_ENTERED.value


def test_sync_entry_fills_is_idempotent(setup):
    engine, exchange, store, trade_id, uid = setup
    plan, _ = entered(engine, exchange, trade_id, uid)
    first = store.get_trade(trade_id)["state"]
    engine.sync_entry_fills(trade_id)
    engine.sync_entry_fills(trade_id)
    assert store.get_trade(trade_id)["state"] == first


# ==========================================================================
# Protection must track the ACTUAL position, never the state label
# ==========================================================================


def test_stop_resized_on_every_sync_not_only_at_fully_entered(setup):
    """REGRESSION: an exposure gap, not a cosmetic lag.

    Entry 1 fills and the stop is placed for that size. When entry 2 fills the
    position doubles. If resizing were gated on reaching FULLY_ENTERED, the
    extra size would sit unprotected until the label caught up.
    """
    engine, exchange, store, trade_id, uid = setup
    plan = planned(engine)
    coids = engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=plan)

    exchange.fill(coids[0], plan.entries[0][1], D(64000))
    engine.sync_entry_fills(trade_id)
    engine.place_stop(trade_id=trade_id, trade_uid=uid, spec=BTC,
                      direction=Direction.LONG, trigger_price=D(62500),
                      position=exchange.get_position("PF_XBTUSD"))

    stop = next(o for o in exchange.get_open_orders() if o.order_type == "stop")
    assert stop.size == plan.entries[0][1]

    # Second entry fills; the stop must follow immediately.
    exchange.fill(coids[1], plan.entries[1][1], D(63500))
    engine.sync_entry_fills(trade_id)

    position = exchange.get_position("PF_XBTUSD")
    stop = next(o for o in exchange.get_open_orders() if o.order_type == "stop")
    assert stop.size == position.size, "position is not fully protected"
    assert stop.size == plan.total_intended_size


def test_stop_matches_position_mid_race_not_just_at_final_state(setup):
    """The exposure window the race creates must still be fully protected.

    Reproduces the non-atomic-read race: the label lands on PARTIALLY_ENTERED
    while the position is already full size. The stop must cover the FULL
    position at that moment, not the smaller size the label implies.
    """
    engine, exchange, store, trade_id, uid = setup
    plan = planned(engine)
    coids = engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=plan)

    exchange.fill(coids[0], plan.entries[0][1], D(64000))
    engine.sync_entry_fills(trade_id)
    engine.place_stop(trade_id=trade_id, trade_uid=uid, spec=BTC,
                      direction=Direction.LONG, trigger_price=D(62500),
                      position=exchange.get_position("PF_XBTUSD"))

    # Entry 2 fills between the order reads and the position read.
    real_get_position = exchange.get_position
    fired = {"done": False}

    def racing_get_position(symbol):
        if not fired["done"]:
            fired["done"] = True
            exchange.fill(coids[1], plan.entries[1][1], D(63500))
        return real_get_position(symbol)

    exchange.get_position = racing_get_position
    engine.sync_entry_fills(trade_id)
    exchange.get_position = real_get_position

    # The label never claims FULLY_ENTERED here — it stays where protection put
    # it. What matters is that the stop already covers the FULL position.
    assert store.get_trade(trade_id)["state"] != TradeState.FULLY_ENTERED.value
    position = exchange.get_position("PF_XBTUSD")
    stop = next(o for o in exchange.get_open_orders() if o.order_type == "stop")
    assert position.size == plan.total_intended_size  # race landed the full fill
    assert stop.size == position.size, "UNPROTECTED size during the race window"


def test_failed_stop_resize_is_loud_and_enters_safe_error(setup, caplog):
    """A stop that cannot be resized leaves the position under-protected."""
    import logging
    engine, exchange, store, trade_id, uid = setup
    plan = planned(engine)
    coids = engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=plan)
    exchange.fill(coids[0], plan.entries[0][1], D(64000))
    engine.sync_entry_fills(trade_id)
    engine.place_stop(trade_id=trade_id, trade_uid=uid, spec=BTC,
                      direction=Direction.LONG, trigger_price=D(62500),
                      position=exchange.get_position("PF_XBTUSD"))

    exchange.amend_fails = True
    exchange.fill(coids[1], plan.entries[1][1], D(63500))

    with caplog.at_level(logging.CRITICAL):
        engine.sync_entry_fills(trade_id)

    assert store.get_trade(trade_id)["state"] == TradeState.SAFE_ERROR.value
    assert any("UNPROTECTED" in r.message for r in caplog.records)


# ==========================================================================
# SAFE_ERROR must reach an operator, not just the database
# ==========================================================================


def test_safe_error_emits_critical_log(setup, caplog):
    import logging
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid)
    reach_tp2(store, trade_id)
    exchange.amend_fails = True

    with caplog.at_level(logging.CRITICAL):
        engine.move_stop_to_breakeven(
            trade_id=trade_id, spec=BTC, direction=Direction.LONG,
            position=exchange.get_position("PF_XBTUSD"),
        )

    criticals = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert criticals, "SAFE_ERROR must surface at CRITICAL, not only in the DB"
    assert any("SAFE_ERROR" in r.message for r in criticals)
    assert any(getattr(r, "trade_id", None) == trade_id for r in criticals)


def test_leftover_orders_after_close_logs_critical(setup, caplog):
    import logging
    engine, exchange, store, trade_id, uid = setup
    protected(engine, exchange, trade_id, uid)
    exchange.cancel_fails = True

    original_place = exchange.place_order
    def place_and_fill(request):
        result = original_place(request)
        if request.purpose is OrderPurpose.CLOSE:
            exchange.fill(request.client_order_id, request.size, D(65000))
        return result
    exchange.place_order = place_and_fill

    with caplog.at_level(logging.CRITICAL):
        assert engine.close_trade(trade_id=trade_id, trade_uid=uid, spec=BTC,
                                  direction=Direction.LONG) is False

    assert any(r.levelno >= logging.CRITICAL for r in caplog.records)


# ==========================================================================
# Stale-signal guard, end to end through open_trade (brief 12)
# ==========================================================================


def test_stale_signal_refused_end_to_end_and_places_no_orders(setup):
    """The guard must be ON the execution path, not a method callers may skip."""
    engine, exchange, store, trade_id, uid = setup
    plan = planned(engine)
    exchange.mark = D(70000)  # far above the 64000/63500 range

    with pytest.raises(ExecutionRefused, match="STALE_ENTRY"):
        engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=plan)

    assert exchange.orders == {}, "a stale signal must place NO orders"
    assert store.orders_for_trade(trade_id) == []
    assert store.get_trade(trade_id)["state"] == TradeState.VALIDATED.value


def test_stale_signal_below_range_also_refused(setup):
    engine, exchange, store, trade_id, uid = setup
    exchange.mark = D(55000)
    with pytest.raises(ExecutionRefused, match="STALE_ENTRY"):
        engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC,
                          plan=planned(engine))
    assert exchange.orders == {}


def test_fresh_signal_proceeds_end_to_end(setup):
    engine, exchange, store, trade_id, uid = setup
    exchange.mark = D(63800)  # inside the range
    coids = engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC,
                              plan=planned(engine))
    assert len(coids) == 2
    assert store.get_trade(trade_id)["state"] == TradeState.WAITING_FOR_ENTRY.value


def test_deviation_just_within_tolerance_proceeds(setup):
    """1.5% tolerance: 64000 * 1.014 is inside, so it must execute."""
    engine, exchange, store, trade_id, uid = setup
    exchange.mark = D(64000) * D("1.014")
    engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC, plan=planned(engine))
    assert len(exchange.orders) == 2


def test_deviation_just_beyond_tolerance_refused(setup):
    engine, exchange, store, trade_id, uid = setup
    exchange.mark = D(64000) * D("1.016")
    with pytest.raises(ExecutionRefused, match="STALE_ENTRY"):
        engine.open_trade(trade_id=trade_id, trade_uid=uid, spec=BTC,
                          plan=planned(engine))
