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
    pipeline = SignalPipeline(
        config=config, store=store,
        admission=build_admission(config=config, store=store, kill_switch=ks,
                                  client=exchange,
                                  specs={CanonicalAsset.BTC: BTC}),
        engine=engine, specs={CanonicalAsset.BTC: BTC},
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
