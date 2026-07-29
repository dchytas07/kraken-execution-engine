"""Telegram listener tests.

Driven entirely through fakes: the listener is built so it can be exercised
without Telegram, because a component that can only be tested live is a
component that ships untested.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
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
from bot.models import CanonicalAsset
from bot.pipeline import SignalPipeline, build_admission
from bot.telegram.listener import InboundMessage, TelegramListener
from tests.test_execution import FakeExchange

D = Decimal
RAW = {i["symbol"]: i for i in json.loads(
    (Path(__file__).parent / "fixtures" / "futures_instruments.json").read_text()
)["instruments"]}
BTC = parse_instrument(RAW["PF_XBTUSD"], CanonicalAsset.BTC)
CHANNEL_ID = -1001234567890

SIGNAL = """🟢 BTC LONG
Entry: 64000 - 63500
TP1: 65000
TP2: 66000
TP3: 67000
TP4: 68000
SL: 62500
Leverage: 10x
"""


class FakeMessage:
    def __init__(self, message_id, text, *, chat_id=CHANNEL_ID, reply_to=None):
        self.id = message_id
        self.message = text
        self.chat_id = chat_id
        self.date = datetime.now(UTC)
        self.reply_to_msg_id = reply_to


class FakeEvent:
    def __init__(self, message):
        self.message = message
        self.chat_id = message.chat_id


class FakeClient:
    """Stands in for TelegramClient. Records calls, replays history."""

    def __init__(self, history=None, fail_get_messages=False):
        self.history = history or []
        self.started = False
        self.disconnected = False
        self.fail_get_messages = fail_get_messages

    async def start(self):
        self.started = True

    async def get_messages(self, channel, limit=50):
        if self.fail_get_messages:
            raise RuntimeError("telegram unreachable")
        return list(reversed(self.history))[:limit]  # newest first

    def on(self, _event):
        def decorator(fn):
            return fn
        return decorator

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
def rig(tmp_path):
    config = load_config({
        "TRADING_MODE": "paper", "TRADE_MARGIN_USD": "30",
        "MAX_LEVERAGE": "10", "DEFAULT_LEVERAGE": "10",
        "MAX_ENTRY_DEVIATION_PERCENT": "1.5",
        "KILL_SWITCH_PATH": str(tmp_path / "KILL"),
    }, dotenv_path=None)
    store = Store(tmp_path / "t.db")
    exchange = FakeExchange(mark=D(63800))
    ks = KillSwitch(path=tmp_path / "KILL")
    engine = ExecutionEngine(
        config=config, store=store, client=exchange,
        observer=PollingFillObserver(exchange, sleep=lambda s: None,
                                     default_timeout_s=0.05, default_poll_s=0.0),
        kill_switch=ks,
    )
    engine.mark_recovery_complete()
    pipeline = SignalPipeline(
        config=config, store=store,
        admission=build_admission(config=config, store=store, kill_switch=ks,
                                  client=exchange, specs={CanonicalAsset.BTC: BTC}),
        engine=engine, specs={CanonicalAsset.BTC: BTC},
    )

    def make(history=None, **kw):
        client = FakeClient(history=history, **kw)
        listener = TelegramListener(
            pipeline=pipeline, api_id=1, api_hash="h", channel=str(CHANNEL_ID),
            session_path=tmp_path / "secrets" / "t.session",
            client_factory=lambda: client,
        )
        return listener, client

    yield make, store, exchange
    store.close()


def run(coro):
    return asyncio.run(coro)


# ==========================================================================
# Event -> pipeline
# ==========================================================================


def test_signal_flows_from_telegram_to_the_exchange(rig):
    make, store, exchange = rig
    listener, _ = make()
    assert run(listener.handle_event(FakeEvent(FakeMessage(1, SIGNAL))))

    assert store.get_message(CHANNEL_ID, 1) is not None
    assert len(exchange.orders) == 2, "signal did not reach the exchange"


def test_identity_is_channel_and_message_id(rig):
    make, store, exchange = rig
    listener, _ = make()
    inbound = listener.to_inbound(FakeEvent(FakeMessage(42, SIGNAL)))
    assert inbound.channel_id == CHANNEL_ID
    assert inbound.message_id == 42


def test_reply_metadata_is_preserved(rig):
    """Close-by-reply resolution depends on this."""
    make, store, exchange = rig
    listener, _ = make()
    inbound = listener.to_inbound(FakeEvent(FakeMessage(2, "close", reply_to=1)))
    assert inbound.reply_to_message_id == 1


@pytest.mark.parametrize("text", ["", "   ", None])
def test_empty_messages_are_skipped(rig, text):
    make, store, exchange = rig
    listener, _ = make()
    assert not run(listener.handle_event(FakeEvent(FakeMessage(1, text))))
    assert listener.messages_skipped == 1


def test_commentary_is_recorded_but_places_nothing(rig):
    make, store, exchange = rig
    listener, _ = make()
    run(listener.handle_event(FakeEvent(FakeMessage(1, "booked 50% earlier 🎉"))))
    assert store.get_message(CHANNEL_ID, 1)["execution_decision"] != "PENDING"
    assert exchange.orders == {}


# ==========================================================================
# Idempotency (brief 5)
# ==========================================================================


def test_same_message_delivered_twice_trades_once(rig):
    make, store, exchange = rig
    listener, _ = make()
    run(listener.handle_event(FakeEvent(FakeMessage(1, SIGNAL))))
    before = len(exchange.orders)
    run(listener.handle_event(FakeEvent(FakeMessage(1, SIGNAL))))
    assert len(exchange.orders) == before, "duplicate delivery placed more orders"


def test_identical_text_with_a_new_id_is_not_confused_with_a_replay(rig):
    """Identity is the id pair, never the text."""
    make, store, exchange = rig
    listener, _ = make()
    run(listener.handle_event(FakeEvent(FakeMessage(1, "gm"))))
    run(listener.handle_event(FakeEvent(FakeMessage(2, "gm"))))
    assert store.get_message(CHANNEL_ID, 1) is not None
    assert store.get_message(CHANNEL_ID, 2) is not None


# ==========================================================================
# Backfill after a disconnect
# ==========================================================================


def test_backfill_replays_messages_missed_while_down(rig):
    make, store, exchange = rig
    history = [FakeMessage(1, "gm"), FakeMessage(2, SIGNAL), FakeMessage(3, "nice")]
    listener, _ = make(history=history)

    run(listener.start())

    assert store.get_message(CHANNEL_ID, 2) is not None, "missed signal not replayed"
    assert len(exchange.orders) == 2


def test_backfill_skips_messages_already_processed(rig):
    make, store, exchange = rig
    listener, _ = make()
    run(listener.handle_event(FakeEvent(FakeMessage(1, SIGNAL))))
    before = len(exchange.orders)

    listener2, _ = make(history=[FakeMessage(1, SIGNAL)])
    run(listener2.start())

    assert len(exchange.orders) == before, "backfill re-traded a stored message"


def test_backfill_replays_oldest_first(rig):
    """Ordering must match how the channel posted, not newest-first."""
    make, store, exchange = rig
    listener, _ = make(history=[FakeMessage(1, "first"), FakeMessage(2, "second"),
                                FakeMessage(3, "third")])
    run(listener.start())
    rows = [store.get_message(CHANNEL_ID, i)["raw_text"] for i in (1, 2, 3)]
    assert rows == ["first", "second", "third"]


def test_backfill_failure_does_not_prevent_live_processing(rig):
    make, store, exchange = rig
    listener, _ = make(fail_get_messages=True)
    run(listener.start())  # must not raise
    assert run(listener.handle_event(FakeEvent(FakeMessage(1, SIGNAL))))


def test_backfill_is_bounded(rig):
    make, store, exchange = rig
    listener, client = make(history=[FakeMessage(i, "gm") for i in range(1, 500)])
    listener.backfill_limit = 10
    run(listener.start())
    assert len(store.open_trades()) == 0
    stored = [i for i in range(1, 500) if store.get_message(CHANNEL_ID, i)]
    assert len(stored) <= 10


# ==========================================================================
# Resilience: the listener must never die
# ==========================================================================


def test_pipeline_exception_does_not_propagate(rig):
    """An exception here would kill the Telethon loop and stop all reception."""
    make, store, exchange = rig
    listener, _ = make()

    def explode(**kw):
        raise RuntimeError("pipeline blew up")
    listener.pipeline.handle_message = explode

    assert run(listener.handle_event(FakeEvent(FakeMessage(1, SIGNAL)))) is False


def test_pipeline_exception_is_logged(rig, caplog):
    make, store, exchange = rig
    listener, _ = make()

    def explode(**kw):
        raise RuntimeError("pipeline blew up")
    listener.pipeline.handle_message = explode

    with caplog.at_level(logging.ERROR):
        run(listener.handle_event(FakeEvent(FakeMessage(1, SIGNAL))))
    assert any("pipeline raised" in r.message for r in caplog.records)


def test_listener_survives_a_malformed_event(rig):
    make, store, exchange = rig
    listener, _ = make()

    class Broken:
        message = None
    assert run(listener.handle_event(Broken())) is False


# ==========================================================================
# Session handling
# ==========================================================================


def test_session_directory_is_created_privately(tmp_path, rig):
    """Telegram session data authenticates as the user (brief 36)."""
    make, store, exchange = rig
    listener, _ = make()
    listener._client_factory = None
    listener.session_path = tmp_path / "secrets" / "tg.session"
    try:
        listener._build_client()
    except Exception:
        pass  # constructing a real client may fail; the directory work is done
    assert listener.session_path.parent.exists()
    assert oct(listener.session_path.parent.stat().st_mode)[-3:] == "700"


def test_last_message_id_tracks_the_high_water_mark(rig):
    make, store, exchange = rig
    assert store.last_message_id(CHANNEL_ID) is None
    listener, _ = make()
    run(listener.handle_event(FakeEvent(FakeMessage(5, "gm"))))
    run(listener.handle_event(FakeEvent(FakeMessage(9, "gm"))))
    assert store.last_message_id(CHANNEL_ID) == 9
