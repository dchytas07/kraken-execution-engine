"""Message handling end to end: classify, persist, act or alert, edits, replies.

Driven through `SignalPipeline.handle_message` / `handle_edit` and the Telegram
listener, with signals in the channel's REAL template. The point of each test
is a decision the bot must get right with money on the line: what it will do on
its own, and what it must leave to a human.
"""

import asyncio
import json
import time
from datetime import UTC, datetime
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
from bot.models import CanonicalAsset, OrderPurpose, TradeState
from bot.pipeline import SignalPipeline, build_admission
from bot.telegram.listener import TelegramListener
from tests.test_execution import FakeExchange

D = Decimal
CH = -1001000000001
RAW = {i["symbol"]: i for i in json.loads(
    (Path(__file__).parent / "fixtures" / "futures_instruments.json").read_text()
)["instruments"]}
BTC = parse_instrument(RAW["PF_XBTUSD"], CanonicalAsset.BTC)
SPECS = {CanonicalAsset.BTC: BTC}

LONG = (
    "➡️ LONG BTCUSDT \n\n❇️ Entry: 64000.00000000 - 63500.00000000\n\n"
    "☑️ Target 1: 65000.00000000\n\n☑️ Target 2: 66000.00000000\n\n"
    "☑️ Target 3: 67000.00000000\n\n☑️ Target 4: 68000.00000000\n\n"
    "⛔ Stoploss: 62500.00000000\n\n💫 Leverage : 10x"
)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    alerts: list[dict] = []
    monkeypatch.setattr("bot.pipeline.send_review_alert",
                        lambda config, **kw: alerts.append(kw) or True)
    config = load_config({
        "TRADING_MODE": "paper", "TRADE_MARGIN_USD": "30",
        "MAX_LEVERAGE": "10", "DEFAULT_LEVERAGE": "10",
        "MAX_ENTRY_DEVIATION_PERCENT": "1.5",
        "KILL_SWITCH_PATH": str(tmp_path / "KILL"),
    }, dotenv_path=None)
    store = Store(tmp_path / "m.db")
    exchange = FakeExchange(mark=D(63800))
    ks = KillSwitch(path=tmp_path / "KILL")
    engine = ExecutionEngine(
        config=config, store=store, client=exchange,
        observer=PollingFillObserver(exchange, sleep=lambda s: None,
                                     default_timeout_s=0.05, default_poll_s=0.0),
        kill_switch=ks,
    )
    engine.mark_recovery_complete()
    manager = TradeManager(config=config, store=store, client=exchange,
                           engine=engine, specs=SPECS)
    pipeline = SignalPipeline(
        config=config, store=store,
        admission=build_admission(config=config, store=store, kill_switch=ks,
                                  client=exchange, specs=SPECS),
        engine=engine, specs=SPECS, manager=manager,
    )
    yield pipeline, store, exchange, alerts, config
    store.close()


def send(pipeline, mid, text, *, reply_to=None, media=None):
    return pipeline.handle_message(channel_id=CH, message_id=mid, text=text,
                                   reply_to_message_id=reply_to, media=media)


def edit(pipeline, mid, text, *, reply_to=None, media=None, timestamp=0.0):
    return pipeline.handle_edit(channel_id=CH, message_id=mid, text=text,
                                reply_to_message_id=reply_to, media=media,
                                edit_date=datetime.now(UTC).isoformat(),
                                timestamp=timestamp)


def open_filled(pipeline, exchange, mid=1, text=LONG):
    exchange.autofill_entries = True
    r = send(pipeline, mid, text)
    exchange.autofill_entries = False
    assert r.accepted, r.detail
    return r.trade_id


def stop_trigger(exchange):
    stops = [o for o in exchange.get_open_orders() if o.order_type == "stop"]
    assert len(stops) == 1
    return stops[0].trigger_price


def version_rows(store, mid):
    return [(v["version"], v["message_class"], v["action"])
            for v in store.message_versions(CH, mid)]


# ==========================================================================
# Every message is persisted with a class, an action and a reason
# ==========================================================================


def test_every_message_gets_a_version_row_with_class_action_reason(rig):
    pipeline, store, exchange, alerts, _ = rig
    send(pipeline, 1, LONG)
    send(pipeline, 2, "#BTC/USDT Take-Profit target 1 ✅\nProfit: 20.5%", reply_to=1)
    send(pipeline, 3, "Long and Strong 💪")
    for mid, cls, act in [(1, "NEW_SIGNAL", "OPEN_TRADE"),
                          (2, "RESULT_REPORT", "LOG_ONLY"),
                          (3, "CHATTER", "LOG_ONLY")]:
        v = store.latest_message_version(CH, mid)
        assert (v["version"], v["message_class"], v["action"]) == (1, cls, act)
        assert v["reason"]
        assert v["text"]
    assert store.latest_message_version(CH, 1)["trade_id"] is not None
    assert alerts == []


def test_result_reports_and_chatter_never_alert_or_trade(rig):
    pipeline, store, exchange, alerts, _ = rig
    for i, t in enumerate([
        "#ETH/USDT Stop Target Hit ⛔\nLoss: 45.8539% 📉",
        "Binance Futures, BingX Futures, Bitget Futures\n#BTC/USDT All entry targets achieved\nAverage Entry Price: 77059 💵",
        "#SOL/USDT Closed at stoploss after reaching take profit ⚠",
        "#BTC Closed at Entry Price.",
        "👀 ETH",
    ], start=10):
        r = send(pipeline, i, t)
        assert not r.accepted and r.action == "LOG_ONLY", t
    assert alerts == [] and exchange.orders == {}


# ==========================================================================
# Signal-like but unparseable: no trade, a human is told
# ==========================================================================


def test_image_only_message_alerts_and_places_no_trade(rig):
    pipeline, store, exchange, alerts, _ = rig
    r = send(pipeline, 5, "", media="photo")
    assert r.message_class == "UNPARSEABLE_SIGNAL_LIKE" and r.action == "REVIEW_ALERT"
    assert exchange.orders == {}
    assert len(alerts) == 1 and alerts[0]["media"] == "photo"
    assert store.get_message(CH, 5)["execution_decision"] == "REQUIRES_REVIEW"


def test_image_attached_to_a_reply_is_chatter(rig):
    """A screenshot posted under a signal is a result picture, not a signal."""
    pipeline, store, exchange, alerts, _ = rig
    r = send(pipeline, 6, "", media="photo", reply_to=1)
    assert r.message_class == "CHATTER" and alerts == []


def test_video_and_sticker_are_chatter(rig):
    pipeline, *_ = rig
    assert send(pipeline, 7, "", media="video").message_class == "CHATTER"
    assert send(pipeline, 8, "", media="sticker").message_class == "CHATTER"


def test_template_signal_with_spot_wording_is_alerted_not_traded(rig):
    """'/ HODL' on the last target made the old parser drop a complete signal
    silently as spot. Brief 8 still forbids trading it, but a human must see it."""
    pipeline, store, exchange, alerts, _ = rig
    text = LONG.replace("68000.00000000", "68000 / HODL")
    r = send(pipeline, 9, text)
    assert r.message_class == "UNPARSEABLE_SIGNAL_LIKE" and r.action == "REVIEW_ALERT"
    assert exchange.orders == {} and len(alerts) == 1


def test_incomplete_template_signal_is_alerted(rig):
    pipeline, store, exchange, alerts, _ = rig
    text = LONG.replace("⛔ Stoploss: 62500.00000000\n\n", "")
    r = send(pipeline, 9, text)
    assert r.action == "REVIEW_ALERT" and exchange.orders == {}
    assert len(alerts) == 1


def test_signal_for_an_untraded_asset_is_refused_quietly(rig):
    """Hundreds of altcoin signals: understood, refused, recorded, not alerted."""
    pipeline, store, exchange, alerts, _ = rig
    text = LONG.replace("BTCUSDT", "ADAUSDT")
    r = send(pipeline, 9, text)
    assert r.message_class == "NEW_SIGNAL" and r.action == "REFUSED"
    assert alerts == [] and exchange.orders == {}
    assert store.get_message(CH, 9)["rejection_reason"] == "UNSUPPORTED_ASSET"


def test_goldusdt_resolves_to_gold(rig):
    """Twelve real GOLDUSDT/GOLDUSDC signals used to be silently UNKNOWN."""
    from bot.parser.parse import parse_message
    for sym in ("GOLDUSDT", "GOLDUSDC", "XAUUSDC"):
        p = parse_message(channel_id=CH, message_id=1,
                          text=LONG.replace("BTCUSDT", sym))
        assert p.asset is CanonicalAsset.XAU, sym


# ==========================================================================
# Stop updates: linked by reply, tighten-only
# ==========================================================================


def test_stop_update_by_reply_tightens_the_stop(rig):
    pipeline, store, exchange, alerts, _ = rig
    tid = open_filled(pipeline, exchange)
    assert stop_trigger(exchange) == D(62500)

    r = send(pipeline, 2, "SL Update : 62800", reply_to=1)

    assert r.accepted and r.action == "UPDATE_STOP"
    assert stop_trigger(exchange) == D(62800)
    t = store.get_trade(tid)
    assert D(t["stop_trigger_price"]) == D(62800)
    assert D(t["intent_stop_loss"]) == D(62800), "a repair must not restore the old stop"
    assert len(exchange.get_open_orders()) == len(
        [o for o in exchange.get_open_orders()]), "amend must not add orders"
    assert alerts == []
    assert version_rows(store, 2) == [(1, "SIGNAL_UPDATE", "UPDATE_STOP")]


def test_stop_update_leaves_exactly_one_stop(rig):
    pipeline, store, exchange, alerts, _ = rig
    open_filled(pipeline, exchange)
    send(pipeline, 2, "SL Update : 62800", reply_to=1)
    assert len([o for o in exchange.get_open_orders() if o.order_type == "stop"]) == 1


def test_widening_the_stop_is_refused_and_alerts(rig):
    pipeline, store, exchange, alerts, _ = rig
    open_filled(pipeline, exchange)
    r = send(pipeline, 2, "SL Update : 61000", reply_to=1)
    assert not r.accepted and r.action == "REVIEW_ALERT"
    assert stop_trigger(exchange) == D(62500)
    assert "WIDEN" in alerts[0]["reason"]


def test_stop_on_the_wrong_side_of_the_mark_is_refused(rig):
    """64000 is above the 63800 mark for a LONG: it would trigger instantly."""
    pipeline, store, exchange, alerts, _ = rig
    open_filled(pipeline, exchange)
    r = send(pipeline, 2, "SL Update : 64000", reply_to=1)
    assert r.action == "REVIEW_ALERT" and stop_trigger(exchange) == D(62500)
    assert "trigger immediately" in alerts[0]["reason"]


def test_failed_amend_leaves_the_original_stop_and_alerts(rig):
    pipeline, store, exchange, alerts, _ = rig
    open_filled(pipeline, exchange)
    exchange.amend_fails = True
    r = send(pipeline, 2, "SL Update : 62800", reply_to=1)
    assert r.action == "REVIEW_ALERT"
    assert stop_trigger(exchange) == D(62500)


def test_stop_update_before_entry_changes_intent_only(rig):
    pipeline, store, exchange, alerts, _ = rig
    r0 = send(pipeline, 1, LONG)          # entries rest, nothing filled
    r = send(pipeline, 2, "SL Update : 62800", reply_to=1)
    assert r.accepted
    assert D(store.get_trade(r0.trade_id)["intent_stop_loss"]) == D(62800)
    assert not [o for o in exchange.get_open_orders() if o.order_type == "stop"]


def test_stop_update_by_symbol_when_exactly_one_trade_matches(rig):
    pipeline, store, exchange, alerts, _ = rig
    open_filled(pipeline, exchange)
    r = send(pipeline, 2, "BTC SL Update to 62900")
    assert r.accepted and stop_trigger(exchange) == D(62900)


def test_stop_update_with_no_reply_and_no_asset_alerts(rig):
    """Which trade? Never guess."""
    pipeline, store, exchange, alerts, _ = rig
    open_filled(pipeline, exchange)
    r = send(pipeline, 2, "SL Update : 62800")
    assert r.action == "REVIEW_ALERT" and stop_trigger(exchange) == D(62500)
    assert len(alerts) == 1


def test_stop_update_replying_to_someone_elses_signal_is_logged_not_applied(rig):
    pipeline, store, exchange, alerts, _ = rig
    open_filled(pipeline, exchange)
    r = send(pipeline, 2, "SL Update : 62800", reply_to=99999)
    assert r.action == "LOG_ONLY" and alerts == []
    assert stop_trigger(exchange) == D(62500)


def test_stop_update_for_an_untraded_asset_is_only_logged(rig):
    pipeline, store, exchange, alerts, _ = rig
    open_filled(pipeline, exchange)
    r = send(pipeline, 2, "APT Shift SL to 14.55", reply_to=1)
    assert r.action == "LOG_ONLY" and stop_trigger(exchange) == D(62500)


def test_stop_update_follows_a_reply_chain(rig):
    """A reply to a reply still leads back to the signal."""
    pipeline, store, exchange, alerts, _ = rig
    open_filled(pipeline, exchange)
    send(pipeline, 2, "#BTC/USDT Take-Profit target 1 ✅\nProfit: 20%", reply_to=1)
    r = send(pipeline, 3, "SL Update : 62800", reply_to=2)
    assert r.accepted and stop_trigger(exchange) == D(62800)


def test_past_tense_stop_move_is_not_executed(rig):
    pipeline, store, exchange, alerts, _ = rig
    open_filled(pipeline, exchange)
    r = send(pipeline, 2, "SL Updated : 62800", reply_to=1)
    assert r.action == "REVIEW_ALERT" and stop_trigger(exchange) == D(62500)


def test_move_sl_to_entry_is_left_to_a_human(rig):
    """Breakeven needs a CONFIRMED TP2 fill (brief 15); a chat message is not one."""
    pipeline, store, exchange, alerts, _ = rig
    open_filled(pipeline, exchange)
    r = send(pipeline, 2, "👆You can move SL to Entry", reply_to=1)
    assert r.action == "REVIEW_ALERT" and stop_trigger(exchange) == D(62500)


# ==========================================================================
# Cancel notices
# ==========================================================================


def test_manual_cancel_before_entry_cancels_the_trade(rig):
    pipeline, store, exchange, alerts, _ = rig
    r0 = send(pipeline, 1, LONG)
    assert exchange.get_open_orders()
    r = send(pipeline, 2, "#BTC/USDT Manually Cancelled", reply_to=1)
    assert r.accepted and r.action == "CANCEL_TRADE"
    assert store.get_trade(r0.trade_id)["state"] == TradeState.CANCELLED.value
    assert exchange.get_open_orders() == []
    assert store.get_active_trade_for_symbol("PF_XBTUSD") is None
    assert alerts == []


def test_manual_cancel_of_a_trade_holding_a_position_alerts_and_does_not_close(rig):
    """'Cancelled' after entry is a CLOSE in disguise: a human decides."""
    pipeline, store, exchange, alerts, _ = rig
    tid = open_filled(pipeline, exchange)
    size = exchange.get_position("PF_XBTUSD").size
    r = send(pipeline, 2, "#BTC/USDT Manually Cancelled", reply_to=1)
    assert r.action == "REVIEW_ALERT"
    assert exchange.get_position("PF_XBTUSD").size == size
    assert store.get_trade(tid)["state"] != TradeState.CLOSED.value
    assert len(alerts) == 1


def test_manual_cancel_for_a_signal_we_never_took_is_logged(rig):
    pipeline, store, exchange, alerts, _ = rig
    r = send(pipeline, 2, "#XPL/USDT Manually Cancelled", reply_to=555)
    assert r.action == "LOG_ONLY" and alerts == []


def test_manual_cancel_with_no_link_alerts(rig):
    pipeline, store, exchange, alerts, _ = rig
    r = send(pipeline, 2, "Manually Cancelled")
    assert r.action == "REVIEW_ALERT"


# ==========================================================================
# Closes: only a bare command acts; anything conditional alerts
# ==========================================================================


@pytest.mark.parametrize("text", [
    "Close #BTC at Entry",                      # a condition the bot cannot evaluate
    "Close BTC near entry",
    "Close #BTC & #ETH in profit",              # two targets
    "Close all 3 #DOGE #PEPE #FLOKI in entry zone",
    "We are close to Golden Cross.\n\nBTC might pump",
    "Close BTC 50%",                            # partial: never automated
    "Close all shorts in profit",               # may mean only the winners
])
def test_conditional_or_ambiguous_close_does_not_close_anything(rig, text):
    pipeline, store, exchange, alerts, _ = rig
    tid = open_filled(pipeline, exchange)
    size = exchange.get_position("PF_XBTUSD").size
    r = send(pipeline, 2, text)
    assert exchange.get_position("PF_XBTUSD").size == size, text
    assert store.get_trade(tid)["state"] != TradeState.CLOSED.value
    assert r.action in ("REVIEW_ALERT", "LOG_ONLY")


def test_close_naming_the_wrong_direction_closes_nothing(rig):
    """'Close BTC Short' while holding a LONG is about a position we do not have."""
    pipeline, store, exchange, alerts, _ = rig
    tid = open_filled(pipeline, exchange)
    r = send(pipeline, 2, "Close BTC Short")
    assert r.action == "REVIEW_ALERT"
    assert store.get_trade(tid)["state"] != TradeState.CLOSED.value


def test_a_bare_close_command_closes_the_trade(rig):
    pipeline, store, exchange, alerts, _ = rig
    tid = open_filled(pipeline, exchange)
    from tests.test_pipeline import fill_closes
    fill_closes(exchange)
    r = send(pipeline, 2, "Close BTC Long")
    assert r.action == "CLOSE_TRADE"
    assert exchange.get_position("PF_XBTUSD") is None
    assert store.get_trade(tid)["state"] == TradeState.CLOSED.value


# ==========================================================================
# Edits
# ==========================================================================


def test_an_edit_with_identical_text_is_ignored(rig):
    pipeline, store, exchange, alerts, _ = rig
    send(pipeline, 1, LONG)
    r = edit(pipeline, 1, LONG)
    assert not r.accepted and "no text" in r.detail
    assert version_rows(store, 1) == [(1, "NEW_SIGNAL", "OPEN_TRADE")]


def test_editing_a_result_reply_logs_a_new_version_and_stays_quiet(rig):
    """Half the channel's messages are edited this way (the profit figure)."""
    pipeline, store, exchange, alerts, _ = rig
    send(pipeline, 2, "#BTC/USDT Take-Profit target 1 ✅\nProfit: 20.5% 📈", reply_to=1)
    edit(pipeline, 2, "#BTC/USDT Take-Profit target 1 ✅\nProfit: 22.1% 📈\n"
                      "Period: 3 Hours", reply_to=1)
    assert version_rows(store, 2) == [(1, "RESULT_REPORT", "LOG_ONLY"),
                                      (2, "RESULT_REPORT", "LOG_ONLY")]
    assert alerts == []


def test_adding_hold_to_a_live_signal_alerts_and_changes_no_orders(rig):
    """71 of the 130 edited signals since 2025 are exactly this edit."""
    pipeline, store, exchange, alerts, _ = rig
    tid = open_filled(pipeline, exchange)
    orders_before = {o.exchange_order_id for o in exchange.get_open_orders()}
    r = edit(pipeline, 1, LONG.replace("68000.00000000", "68000 / HOLD"))
    assert r.action == "REVIEW_ALERT" and r.version == 2
    assert {o.exchange_order_id for o in exchange.get_open_orders()} == orders_before
    assert "HOLD" in alerts[0]["reason"] and alerts[0]["trade_id"] == tid
    assert store.get_message(CH, 1)["raw_text"] == LONG, "original is never overwritten"


def test_changing_a_live_signals_stop_alerts_and_does_not_amend(rig):
    pipeline, store, exchange, alerts, _ = rig
    open_filled(pipeline, exchange)
    r = edit(pipeline, 1, LONG.replace("62500.00000000", "62000.00000000"))
    assert r.action == "REVIEW_ALERT"
    assert stop_trigger(exchange) == D(62500)
    assert "stop" in alerts[0]["reason"] and "did NOT amend" in alerts[0]["reason"]


def test_an_edit_that_leaves_the_levels_alone_is_only_logged(rig):
    pipeline, store, exchange, alerts, _ = rig
    open_filled(pipeline, exchange)
    r = edit(pipeline, 1, LONG + " ")           # cosmetic
    assert version_rows(store, 1) in ([(1, "NEW_SIGNAL", "OPEN_TRADE")],) or r.action == "LOG_ONLY"
    assert alerts == []


def test_an_edit_never_opens_a_second_trade_for_the_same_signal(rig):
    pipeline, store, exchange, alerts, _ = rig
    open_filled(pipeline, exchange)
    n = len(store.open_trades())
    edit(pipeline, 1, LONG.replace("62500.00000000", "62400.00000000"))
    assert len(store.open_trades()) == n


def test_an_incomplete_signal_completed_by_an_edit_opens_through_admission(rig):
    pipeline, store, exchange, alerts, _ = rig
    broken = LONG.replace("⛔ Stoploss: 62500.00000000\n\n", "")
    assert send(pipeline, 1, broken).action == "REVIEW_ALERT"
    assert exchange.orders == {}

    r = edit(pipeline, 1, LONG, timestamp=time.time())

    assert r.accepted and r.action == "OPEN_TRADE" and r.version == 2
    assert len(exchange.orders) == 2
    assert version_rows(store, 1) == [(1, "UNPARSEABLE_SIGNAL_LIKE", "REVIEW_ALERT"),
                                      (2, "NEW_SIGNAL", "OPEN_TRADE")]


def test_an_edit_that_completes_a_day_old_signal_is_refused(rig):
    pipeline, store, exchange, alerts, _ = rig
    broken = LONG.replace("⛔ Stoploss: 62500.00000000\n\n", "")
    send(pipeline, 1, broken)
    r = edit(pipeline, 1, LONG, timestamp=time.time() - 30 * 3600)
    assert r.action == "REFUSED" and exchange.orders == {}


def test_a_stop_update_arriving_as_an_edit_is_never_executed(rig):
    pipeline, store, exchange, alerts, _ = rig
    open_filled(pipeline, exchange)
    send(pipeline, 2, "hmm", reply_to=1)
    r = edit(pipeline, 2, "SL Update : 62800", reply_to=1)
    assert r.action == "REVIEW_ALERT" and stop_trigger(exchange) == D(62500)
    assert "EDIT" in alerts[-1]["reason"]


def test_a_close_arriving_as_an_edit_is_never_executed(rig):
    pipeline, store, exchange, alerts, _ = rig
    tid = open_filled(pipeline, exchange)
    send(pipeline, 2, "market looking weak")
    r = edit(pipeline, 2, "Close BTC Long")
    assert r.action == "REVIEW_ALERT"
    assert store.get_trade(tid)["state"] != TradeState.CLOSED.value


def test_edit_of_a_message_we_never_saw_is_processed_as_new(rig):
    pipeline, store, exchange, alerts, _ = rig
    r = edit(pipeline, 77, "#BTC/USDT Take-Profit target 2 ✅\nProfit: 40%", reply_to=1)
    assert r.message_class == "RESULT_REPORT"
    assert store.get_message(CH, 77) is not None
    assert store.latest_message_version(CH, 77)["edit_date"] is not None


def test_versions_are_numbered_and_every_one_is_kept(rig):
    pipeline, store, exchange, alerts, _ = rig
    send(pipeline, 2, "#BTC/USDT Take-Profit target 1 ✅\nProfit: 10%", reply_to=1)
    for pct in (11, 12, 13):
        edit(pipeline, 2, f"#BTC/USDT Take-Profit target 1 ✅\nProfit: {pct}%", reply_to=1)
    rows = store.message_versions(CH, 2)
    assert [r["version"] for r in rows] == [1, 2, 3, 4]
    assert "10%" in rows[0]["text"] and "13%" in rows[-1]["text"]


# ==========================================================================
# Listener: edit events, media-only messages, edits missed while down
# ==========================================================================


class FakeMsg:
    def __init__(self, mid, text, *, reply_to=None, photo=False, edit_date=None):
        self.id = mid
        self.message = text
        self.chat_id = CH
        self.date = datetime.now(UTC)
        self.reply_to_msg_id = reply_to
        self.photo = photo
        self.edit_date = edit_date


class FakeEvt:
    def __init__(self, msg):
        self.message = msg
        self.chat_id = msg.chat_id


class FakeTg:
    def __init__(self, history=()):
        self.history = list(history)

    async def start(self): ...
    async def disconnect(self): ...
    def on(self, _e):
        return lambda fn: fn

    async def get_messages(self, channel, limit=50):
        return list(reversed(self.history))[:limit]


def make_listener(pipeline, tmp_path, history=()):
    client = FakeTg(history)
    return TelegramListener(
        pipeline=pipeline, api_id=1, api_hash="h", channel=str(CH),
        session_path=tmp_path / "s" / "t.session", client_factory=lambda: client,
    ), client


def test_listener_routes_an_edit_event_to_handle_edit(rig, tmp_path):
    pipeline, store, exchange, alerts, _ = rig
    listener, _ = make_listener(pipeline, tmp_path)
    asyncio.run(listener.handle_event(FakeEvt(FakeMsg(
        3, "#BTC/USDT Take-Profit target 1 ✅\nProfit: 10%", reply_to=1))))
    asyncio.run(listener.handle_event(FakeEvt(FakeMsg(
        3, "#BTC/USDT Take-Profit target 1 ✅\nProfit: 12%", reply_to=1,
        edit_date=datetime.now(UTC))), edited=True))
    rows = store.message_versions(CH, 3)
    assert [r["version"] for r in rows] == [1, 2]
    assert rows[1]["edit_date"] is not None


def test_listener_passes_an_image_only_message_through(rig, tmp_path):
    pipeline, store, exchange, alerts, _ = rig
    listener, _ = make_listener(pipeline, tmp_path)
    assert asyncio.run(listener.handle_event(FakeEvt(FakeMsg(4, "", photo=True))))
    assert store.latest_message_version(CH, 4)["media"] == "photo"
    assert len(alerts) == 1


def test_listener_still_drops_a_message_with_neither_text_nor_media(rig, tmp_path):
    pipeline, store, exchange, alerts, _ = rig
    listener, _ = make_listener(pipeline, tmp_path)
    assert not asyncio.run(listener.handle_event(FakeEvt(FakeMsg(4, ""))))


def test_backfill_catches_an_edit_made_while_the_bot_was_down(rig, tmp_path):
    pipeline, store, exchange, alerts, _ = rig
    send(pipeline, 1, LONG)
    open_trades = len(store.open_trades())
    edited = FakeMsg(1, LONG.replace("68000.00000000", "68000 / HOLD"),
                     edit_date=datetime.now(UTC))
    listener, _ = make_listener(pipeline, tmp_path, history=[edited])

    asyncio.run(listener.start())

    assert [v["version"] for v in store.message_versions(CH, 1)] == [1, 2]
    assert len(alerts) == 1 and len(store.open_trades()) == open_trades


def test_backfill_ignores_an_unchanged_message(rig, tmp_path):
    pipeline, store, exchange, alerts, _ = rig
    send(pipeline, 1, LONG)
    same = FakeMsg(1, LONG, edit_date=datetime.now(UTC))
    listener, _ = make_listener(pipeline, tmp_path, history=[same])
    asyncio.run(listener.start())
    assert [v["version"] for v in store.message_versions(CH, 1)] == [1]
