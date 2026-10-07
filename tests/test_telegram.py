"""Telegram listener tests.

Driven entirely through fakes: the listener is built so it can be exercised
without Telegram, because a component that can only be tested live is a
component that ships untested.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
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
        self.calls = []

    async def start(self):
        self.started = True

    async def get_messages(self, channel, limit=50, min_id=0, max_id=0):
        """Telethon semantics: min_id/max_id are exclusive bounds, newest first."""
        self.calls.append({"limit": limit, "min_id": min_id, "max_id": max_id})
        if self.fail_get_messages:
            raise RuntimeError("telegram unreachable")
        msgs = [m for m in self.history
                if m.id > min_id and (not max_id or m.id < max_id)]
        return list(reversed(msgs))[:limit]

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
        "MAX_LEVERAGE": "10", "MAX_TOTAL_OPEN_MARGIN": "1000",
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
    listener.initial_window = 10
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


# ==========================================================================
# Backfill must take the IDENTICAL path as a live message
# ==========================================================================


def test_backfill_and_live_use_the_same_dispatch(rig):
    """Structural: both funnel through dispatch -> pipeline.handle_message."""
    make, store, exchange = rig
    listener, _ = make(history=[FakeMessage(1, SIGNAL)])
    seen = []
    original = listener.dispatch
    listener.dispatch = lambda inbound: (seen.append(inbound), original(inbound))[1]

    run(listener.start())
    run(listener.handle_event(FakeEvent(FakeMessage(2, "gm"))))

    assert len(seen) == 2, "one of the two paths bypassed dispatch"


def test_stale_signal_arriving_via_backfill_is_refused(rig):
    """A signal stale because it arrived LATE must be refused exactly like one
    stale because price moved. Arriving via backfill is not an exemption."""
    make, store, exchange = rig
    exchange.mark = D(75000)  # far beyond the 64000/63500 entry range
    listener, _ = make(history=[FakeMessage(1, SIGNAL)])

    run(listener.start())

    assert store.get_message(CHANNEL_ID, 1)["rejection_reason"] == "STALE_ENTRY"
    assert exchange.orders == {}, "a stale backfilled signal reached the exchange"


def test_fresh_signal_via_backfill_still_executes(rig):
    """The guard must not over-reject: a still-valid replay should trade."""
    make, store, exchange = rig
    exchange.mark = D(63800)  # inside the range
    listener, _ = make(history=[FakeMessage(1, SIGNAL)])
    run(listener.start())
    assert len(exchange.orders) == 2


def test_spot_signal_via_backfill_is_still_refused(rig):
    make, store, exchange = rig
    listener, _ = make(history=[FakeMessage(1, "BTC spot buy and hold")])
    run(listener.start())
    assert store.get_message(CHANNEL_ID, 1)["rejection_reason"] == "SPOT_SIGNAL"
    assert exchange.orders == {}


def test_kill_switch_blocks_backfilled_signals_too(rig):
    make, store, exchange = rig
    listener, _ = make(history=[FakeMessage(1, SIGNAL)])
    KillSwitch(path=listener.pipeline.config.kill_switch_path).engage()
    run(listener.start())
    assert exchange.orders == {}


def test_backfilled_duplicate_signal_is_deduplicated(rig):
    """Content dedup applies to replays, not just live messages."""
    make, store, exchange = rig
    exchange.mark = D(63800)
    listener, _ = make()
    run(listener.handle_event(FakeEvent(FakeMessage(1, SIGNAL))))
    before = len(exchange.orders)

    reformatted = SIGNAL.lower().replace("\n", "\n ")
    listener2, _ = make(history=[FakeMessage(2, reformatted)])
    run(listener2.start())

    assert len(exchange.orders) == before, "a reformatted replay opened a second trade"


# ==========================================================================
# Catch-up resumes from the high-water mark under the STORED channel key
# (2026-10-07: TELEGRAM_CHANNEL=1000000001, rows stored under -1001000000001,
# so the lookup found nothing and every restart re-read 50 messages)
# ==========================================================================

BARE_ID = 1000000001           # as TELEGRAM_CHANNEL is configured in production
STORED_KEY = -1001000000001    # as Telethon stamps the channel's messages


@pytest.mark.parametrize("configured", [BARE_ID, str(BARE_ID), STORED_KEY,
                                        str(STORED_KEY)])
def test_channel_key_is_the_marked_id_rows_are_stored_under(configured):
    from bot.telegram.listener import channel_key
    assert channel_key(configured) == STORED_KEY


def test_channel_key_of_a_username_is_left_to_telegram():
    from bot.telegram.listener import channel_key
    assert channel_key("@somechannel") is None


def _real_channel_listener(rig, tmp_path, history, **kw):
    make, store, exchange = rig
    client = FakeClient(history=history)
    listener = TelegramListener(
        pipeline=make()[0].pipeline, api_id=1, api_hash="h",
        channel=BARE_ID,                        # what _parse_channel yields
        session_path=tmp_path / "secrets" / "t.session",
        client_factory=lambda: client, **kw,
    )
    return listener, client


def _msgs(ids, text="gm"):
    return [FakeMessage(i, text, chat_id=STORED_KEY) for i in ids]


def test_catch_up_resumes_after_the_highest_stored_id(rig, tmp_path):
    make, store, exchange = rig
    for m in _msgs(range(1, 61)):
        store.record_message(channel_id=STORED_KEY, message_id=m.id, raw_text="gm")
    assert store.last_message_id(STORED_KEY) == 60

    listener, client = _real_channel_listener(
        rig, tmp_path, _msgs(range(1, 63)))
    run(listener.start())

    assert client.calls[0]["min_id"] == 60, "catch-up did not resume from the mark"
    assert listener.messages_handled == 2, "already-stored messages were re-read"
    assert store.get_message(STORED_KEY, 62) is not None


def test_catch_up_with_nothing_missed_dispatches_nothing(rig, tmp_path):
    make, store, exchange = rig
    for i in range(1, 51):
        store.record_message(channel_id=STORED_KEY, message_id=i, raw_text="gm")
    listener, _ = _real_channel_listener(rig, tmp_path, _msgs(range(1, 51)))
    run(listener.start())
    assert listener.messages_handled == 0


def test_catch_up_cap_defaults_to_500(rig, tmp_path):
    listener, _ = _real_channel_listener(rig, tmp_path, [])
    assert listener.catch_up_cap == 500


def test_catch_up_beyond_the_cap_replays_newest_and_alerts(rig, tmp_path,
                                                           monkeypatch):
    make, store, exchange = rig
    sent = []
    monkeypatch.setattr("bot.telegram.listener.send_alert",
                        lambda token, chat, text: sent.append(text) or True)
    store.record_message(channel_id=STORED_KEY, message_id=1, raw_text="gm")
    listener, client = _real_channel_listener(
        rig, tmp_path, _msgs(range(1, 17)), catch_up_cap=10)   # 15 missed

    run(listener.start())

    assert client.calls[0]["limit"] == 11
    replayed = [i for i in range(2, 17) if store.get_message(STORED_KEY, i)]
    assert replayed == list(range(7, 17)), "must replay the NEWEST cap messages"
    assert len(sent) == 1 and "cap hit" in sent[0] and "before 7" in sent[0]


def test_catch_up_exactly_at_the_cap_does_not_alert(rig, tmp_path, monkeypatch):
    make, store, exchange = rig
    sent = []
    monkeypatch.setattr("bot.telegram.listener.send_alert",
                        lambda token, chat, text: sent.append(text) or True)
    store.record_message(channel_id=STORED_KEY, message_id=1, raw_text="gm")
    listener, _ = _real_channel_listener(
        rig, tmp_path, _msgs(range(1, 12)), catch_up_cap=10)   # exactly 10 missed
    run(listener.start())
    assert sent == []
    assert all(store.get_message(STORED_KEY, i) for i in range(2, 12))


# ==========================================================================
# Freshness gate: a NEW_SIGNAL older than MAX_SIGNAL_AGE_MINUTES (default 30)
# by its Telegram POST time is recorded and alerted, never traded — whether
# it arrives by catch-up after downtime or late on the live path.
# ==========================================================================


@pytest.fixture
def review_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr("bot.pipeline.send_review_alert",
                        lambda config, **kw: sent.append(kw) or True)
    return sent


def _posted(message_id, minutes_ago, text=SIGNAL):
    m = FakeMessage(message_id, text)
    m.date = (datetime.now(UTC) - timedelta(minutes=minutes_ago)
              if minutes_ago is not None else None)
    return m


def _assert_alerted_not_traded(store, exchange, review_alerts, message_id):
    row = store.get_message(CHANNEL_ID, message_id)
    assert row["rejection_reason"] == "SIGNAL_TOO_OLD"
    assert exchange.orders == {}, "an old signal reached the exchange"
    assert store.open_trades() == []
    assert len(review_alerts) == 1 and review_alerts[0]["message_id"] == message_id
    assert store.latest_message_version(CHANNEL_ID, message_id)["action"] == \
        "REVIEW_ALERT"


def test_backfilled_old_signal_is_alerted_not_traded(rig, review_alerts):
    make, store, exchange = rig
    exchange.mark = D(63800)                 # price still inside the entry range
    listener, _ = make(history=[_posted(1, minutes_ago=120)])
    run(listener.start())
    _assert_alerted_not_traded(store, exchange, review_alerts, 1)


def test_late_live_delivery_is_alerted_not_traded(rig, review_alerts):
    make, store, exchange = rig
    exchange.mark = D(63800)
    listener, _ = make()
    run(listener.handle_event(FakeEvent(_posted(1, minutes_ago=45))))
    _assert_alerted_not_traded(store, exchange, review_alerts, 1)


def test_fresh_backfilled_signal_still_trades(rig, review_alerts):
    make, store, exchange = rig
    exchange.mark = D(63800)
    listener, _ = make(history=[_posted(1, minutes_ago=5)])
    run(listener.start())
    assert len(exchange.orders) == 2 and review_alerts == []


def test_fresh_live_signal_still_trades(rig, review_alerts):
    make, store, exchange = rig
    exchange.mark = D(63800)
    listener, _ = make()
    run(listener.handle_event(FakeEvent(_posted(1, minutes_ago=1))))
    assert len(exchange.orders) == 2 and review_alerts == []


def test_signal_with_no_timestamp_is_alerted_not_traded(rig, review_alerts):
    make, store, exchange = rig
    exchange.mark = D(63800)
    listener, _ = make()
    run(listener.handle_event(FakeEvent(_posted(1, minutes_ago=None))))
    _assert_alerted_not_traded(store, exchange, review_alerts, 1)
    assert "fail closed" in store.get_message(CHANNEL_ID, 1)["rejection_detail"]


def test_old_chatter_is_not_alerted(rig, review_alerts):
    """The gate applies to signals only: old commentary stays quiet."""
    make, store, exchange = rig
    listener, _ = make(history=[_posted(1, minutes_ago=600, text="gm")])
    run(listener.start())
    assert review_alerts == [] and exchange.orders == {}
