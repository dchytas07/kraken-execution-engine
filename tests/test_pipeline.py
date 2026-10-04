"""Pipeline tests — drive handle_message, the real production entry point."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from bot.admission import SignalAdmission
from bot.config import load_config
from bot.db.store import Store
from bot.exchange.specs import parse_instrument
from bot.execution.engine import ExecutionEngine
from bot.execution.observer import PollingFillObserver
from bot.killswitch import KillSwitch
from bot.models import CanonicalAsset, TradeState
from bot.execution.manager import TradeManager
from bot.pipeline import SignalPipeline, build_admission
from tests.test_execution import FakeExchange

D = Decimal
RAW = {i["symbol"]: i for i in json.loads(
    (Path(__file__).parent / "fixtures" / "futures_instruments.json").read_text()
)["instruments"]}
BTC = parse_instrument(RAW["PF_XBTUSD"], CanonicalAsset.BTC)
SYMBOLS = {CanonicalAsset.BTC: "PF_XBTUSD"}

SIGNAL = """🟢 BTC LONG
Entry: 64000 - 63500
TP1: 65000
TP2: 66000
TP3: 67000
TP4: 68000
SL: 62500
Leverage: 50x
"""


@pytest.fixture
def pipe(tmp_path):
    config = load_config({
        "TRADING_MODE": "paper", "TRADE_MARGIN_USD": "30",
        "MAX_LEVERAGE": "10", "DEFAULT_LEVERAGE": "10",
        "MAX_ENTRY_DEVIATION_PERCENT": "1.5",
        "KILL_SWITCH_PATH": str(tmp_path / "KILL"),
    }, dotenv_path=None)
    store = Store(tmp_path / "p.db")
    exchange = FakeExchange(mark=D(63800))
    ks = KillSwitch(path=tmp_path / "KILL")
    engine = ExecutionEngine(
        config=config, store=store, client=exchange,
        observer=PollingFillObserver(exchange, sleep=lambda s: None,
                                     default_timeout_s=0.05, default_poll_s=0.0),
        kill_switch=ks,
    )
    engine.mark_recovery_complete()  # fixture bypasses startup recovery
    pipeline = SignalPipeline(
        config=config, store=store,
        admission=build_admission(config=config, store=store, kill_switch=ks,
                                  client=exchange,
                                  specs={CanonicalAsset.BTC: BTC}),
        engine=engine, specs={CanonicalAsset.BTC: BTC},
        manager=TradeManager(config=config, store=store, client=exchange,
                             engine=engine, specs={CanonicalAsset.BTC: BTC}),
    )
    yield pipeline, store, exchange, ks
    store.close()


def test_valid_signal_creates_and_opens_a_trade(pipe):
    pipeline, store, exchange, _ = pipe
    result = pipeline.handle_message(channel_id=-100, message_id=1, text=SIGNAL)
    assert result.accepted
    trade = store.get_trade(result.trade_id)
    assert trade["state"] == TradeState.WAITING_FOR_ENTRY.value
    assert len(exchange.orders) == 2
    assert store.get_message(-100, 1)["execution_decision"] == "EXECUTED"


def test_leverage_capped_end_to_end_from_a_50x_signal(pipe):
    """The signal says 50x; the account must be set to 10x."""
    pipeline, store, exchange, _ = pipe
    pipeline.handle_message(channel_id=-100, message_id=1, text=SIGNAL)
    assert exchange.leverage["PF_XBTUSD"] == D(10)


def test_spot_signal_never_reaches_execution(pipe):
    pipeline, store, exchange, _ = pipe
    result = pipeline.handle_message(
        channel_id=-100, message_id=1, text="BTC spot buy and hold from 64000")
    assert not result.accepted
    assert exchange.orders == {}
    assert store.get_message(-100, 1)["rejection_reason"] == "SPOT_SIGNAL"


def test_duplicate_message_id_is_not_reprocessed(pipe):
    pipeline, store, exchange, _ = pipe
    assert pipeline.handle_message(channel_id=-100, message_id=1, text=SIGNAL).accepted
    before = len(exchange.orders)
    again = pipeline.handle_message(channel_id=-100, message_id=1, text=SIGNAL)
    assert not again.accepted
    assert len(exchange.orders) == before, "duplicate message created more orders"


def test_reformatted_duplicate_signal_creates_no_second_trade(pipe):
    pipeline, store, exchange, _ = pipe
    pipeline.handle_message(channel_id=-100, message_id=1, text=SIGNAL)
    before = len(exchange.orders)
    result = pipeline.handle_message(
        channel_id=-100, message_id=2,
        text="btc  long!!\nentry: 64000 - 63500\ntp1 65000\ntp2 66000\n"
             "tp3 67000\ntp4 68000\nsl:62500\n50x")
    assert not result.accepted
    assert len(exchange.orders) == before


def test_kill_switch_blocks_the_whole_pipeline(pipe):
    pipeline, store, exchange, ks = pipe
    ks.engage()
    result = pipeline.handle_message(channel_id=-100, message_id=1, text=SIGNAL)
    assert not result.accepted
    assert exchange.orders == {}


def test_stale_signal_refused_through_the_pipeline(pipe):
    pipeline, store, exchange, _ = pipe
    exchange.mark = D(75000)
    result = pipeline.handle_message(channel_id=-100, message_id=1, text=SIGNAL)
    assert not result.accepted
    assert exchange.orders == {}
    assert store.get_message(-100, 1)["rejection_reason"] == "STALE_ENTRY"
    assert store.get_trade(result.trade_id)["state"] == TradeState.CANCELLED.value


def test_commentary_is_recorded_and_ignored(pipe):
    pipeline, store, exchange, _ = pipe
    result = pipeline.handle_message(
        channel_id=-100, message_id=1, text="booked 50% profit earlier 🎉")
    assert not result.accepted
    assert exchange.orders == {}
    assert store.get_message(-100, 1)["execution_decision"] != "PENDING"


def test_raw_message_persisted_before_parsing(pipe):
    pipeline, store, exchange, _ = pipe
    pipeline.handle_message(channel_id=-100, message_id=1, text="gm")
    assert store.get_message(-100, 1)["raw_text"] == "gm"


def test_second_signal_on_occupied_symbol_places_no_orders(pipe):
    pipeline, store, exchange, _ = pipe
    pipeline.handle_message(channel_id=-100, message_id=1, text=SIGNAL)
    before = len(exchange.orders)
    result = pipeline.handle_message(
        channel_id=-100, message_id=2, text=SIGNAL.replace("SL: 62500", "SL: 62200"))
    assert not result.accepted
    assert len(exchange.orders) == before


# ==========================================================================
# Exit-family routing (brief 17, 18, 21)
# ==========================================================================


def open_a_trade(pipeline, exchange, message_id=1, *, fill=True):
    """Open a trade, filling the entries so a real position exists."""
    exchange.autofill_entries = fill
    result = pipeline.handle_message(channel_id=-100, message_id=message_id,
                                     text=SIGNAL)
    exchange.autofill_entries = False
    assert result.accepted
    return result.trade_id


def fill_closes(exchange):
    from bot.models import OrderPurpose
    original = exchange.place_order

    def wrapped(request):
        res = original(request)
        if request.purpose is OrderPurpose.CLOSE:
            exchange.fill(request.client_order_id, request.size, D(65000))
        return res
    exchange.place_order = wrapped


def test_close_symbol_routes_to_an_actual_close(pipe):
    pipeline, store, exchange, _ = pipe
    trade_id = open_a_trade(pipeline, exchange)
    fill_closes(exchange)

    result = pipeline.handle_message(channel_id=-100, message_id=2, text="Close BTC")
    assert result.accepted
    assert store.get_trade(trade_id)["state"] == TradeState.CLOSED.value


def test_close_all_routes_to_a_close(pipe):
    pipeline, store, exchange, _ = pipe
    trade_id = open_a_trade(pipeline, exchange)
    fill_closes(exchange)
    assert pipeline.handle_message(channel_id=-100, message_id=2,
                                    text="Close all positions").accepted
    assert store.get_trade(trade_id)["state"] == TradeState.CLOSED.value


def test_ambiguous_close_refused_and_nothing_closes(pipe):
    """brief 21: a wrong close is worse than a missed close."""
    pipeline, store, exchange, _ = pipe
    trade_id = open_a_trade(pipeline, exchange)
    result = pipeline.handle_message(channel_id=-100, message_id=2, text="close it")
    assert not result.accepted
    assert store.get_trade(trade_id)["state"] != TradeState.CLOSED.value
    assert store.get_message(-100, 2)["execution_decision"] == "REQUIRES_REVIEW"


def test_partial_close_reduces_but_does_not_close(pipe):
    pipeline, store, exchange, _ = pipe
    trade_id = open_a_trade(pipeline, exchange)
    before = exchange.get_position("PF_XBTUSD").size
    fill_closes(exchange)

    pipeline.handle_message(channel_id=-100, message_id=2, text="Close BTC 50%")
    after = exchange.get_position("PF_XBTUSD")
    if after is not None:
        assert after.size < before
        assert store.get_trade(trade_id)["state"] != TradeState.CLOSED.value


def test_sl_update_is_flagged_for_review_not_silently_dropped(pipe):
    pipeline, store, exchange, _ = pipe
    open_a_trade(pipeline, exchange)
    result = pipeline.handle_message(channel_id=-100, message_id=2,
                                     text="Move BTC SL to 63000")
    assert not result.accepted
    assert store.get_message(-100, 2)["execution_decision"] == "REQUIRES_REVIEW"


def test_close_with_no_open_trades_is_recorded_as_ignored(pipe):
    pipeline, store, exchange, _ = pipe
    result = pipeline.handle_message(channel_id=-100, message_id=1,
                                     text="Close all positions")
    assert not result.accepted
    assert store.get_message(-100, 1)["execution_decision"] == "IGNORED"


def test_opening_a_trade_protects_it_same_request(pipe):
    """Same-cycle guarantee through the pipeline, not just the manager."""
    pipeline, store, exchange, _ = pipe
    open_a_trade(pipeline, exchange)
    stops = [o for o in exchange.get_open_orders() if o.order_type == "stop"]
    assert len(stops) == 1, "trade opened and filled without protection"


def test_stop_beyond_liquidation_refused_through_the_pipeline(pipe, monkeypatch):
    """SL 50000 is 21.9% below the 64000 entry: safe only at 4x, below the
    5x floor. Refused with an enumerated reason and an alert; nothing placed."""
    pipeline, store, exchange, _ = pipe
    sent = []
    monkeypatch.setattr("bot.execution.engine.send_alert",
                        lambda token, chat, text: sent.append(text) or True)
    result = pipeline.handle_message(
        channel_id=-100, message_id=1, text=SIGNAL.replace("SL: 62500", "SL: 50000"))
    assert not result.accepted
    assert exchange.orders == {}
    row = store.get_message(-100, 1)
    assert row["execution_decision"] == "REJECTED"
    assert row["rejection_reason"] == "STOP_BEYOND_LIQUIDATION"
    assert store.get_trade(result.trade_id)["state"] == TradeState.CANCELLED.value
    assert any(t.startswith("REFUSED PF_XBTUSD LONG") for t in sent)


def test_wide_stop_opens_at_lowered_leverage_through_the_pipeline(pipe):
    """SL 58240 is 9% below 64000: the 50x signal opens at 8x, not 10x."""
    pipeline, store, exchange, _ = pipe
    result = pipeline.handle_message(
        channel_id=-100, message_id=1, text=SIGNAL.replace("SL: 62500", "SL: 58240"))
    assert result.accepted
    assert exchange.leverage["PF_XBTUSD"] == D(8)
    assert store.get_trade(result.trade_id)["intent_leverage"] == "8"

