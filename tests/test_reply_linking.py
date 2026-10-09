"""Strict reply linking with two live trades on one symbol.

The channel answers its own signal posts: result reports, stop updates and
cancels arrive as Telegram replies. With MAX_TRADES_PER_SYMBOL=2 the symbol no
longer identifies a trade, so:

  * a reply acts ONLY on the trade its own signal opened. If that signal has
    no live trade (never traded, refused, expired, cancelled) nothing is done
    and a human is alerted; it never falls back to the symbol;
  * a non-reply instruction may use the symbol only when exactly one live
    trade exists on it; with two it does nothing and alerts;
  * every message, result reports included, records the trade it is about.

Message texts are the channel's real formats (corpus/channel_history.jsonl).
"""

import json
import sqlite3
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
from bot.models import CanonicalAsset, TradeState
from bot.pipeline import SignalPipeline, build_admission
from tests.test_execution import FakeExchange

D = Decimal
CH = -1001000000001
RAW = {i["symbol"]: i for i in json.loads(
    (Path(__file__).parent / "fixtures" / "futures_instruments.json").read_text()
)["instruments"]}
SOL = parse_instrument(RAW["PF_SOLUSD"], CanonicalAsset.SOL)
SPECS = {CanonicalAsset.SOL: SOL}

SIGNAL_A, SIGNAL_B = 1001, 1002


def short_sol(stop: str) -> str:
    return (
        "➡️ SHORT SOLUSDT \n\n❇️ Entry: 150.00000000 - 151.00000000\n\n"
        "☑️ Target 1: 148.00000000 \n\n☑️ Target 2: 146.00000000 \n\n"
        "☑️ Target 3: 144.00000000 \n\n☑️ Target 4: 142.00000000 \n\n"
        f"⛔ Stoploss: {stop}.00000000  \n\n💫 Leverage : 10x"
    )


TP2 = ("Binance Futures, BitMEX\n#SOL/USDT Take-Profit target 2 ✅\n"
       "Profit: 93.1831% 📈\nPeriod: 28 Minutes ⏰")
TP3 = ("Binance Futures, BitMEX\n#SOL/USDT Take-Profit target 3 ✅\n"
       "Profit: 126.4% 📈\nPeriod: 2 Hours 5 Minutes ⏰")
CANCEL = "#SOL/USDT Manually Cancelled"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    alerts: list[dict] = []
    monkeypatch.setattr("bot.pipeline.send_review_alert",
                        lambda config, **kw: alerts.append(kw) or True)
    config = load_config({
        "TRADING_MODE": "paper", "TRADE_MARGIN_USD": "30", "MAX_LEVERAGE": "10",
        "MAX_TOTAL_OPEN_MARGIN": "200", "MAX_TRADES_PER_SYMBOL": "2",
        "MAX_ENTRY_DEVIATION_PERCENT": "1.5",
        "KILL_SWITCH_PATH": str(tmp_path / "KILL"),
    }, dotenv_path=None)
    store = Store(tmp_path / "links.db")
    exchange = FakeExchange(mark=D("150.5"))
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
                                  client=exchange, specs=SPECS, engine=engine),
        engine=engine, specs=SPECS, manager=manager,
    )

    class Rig:
        pass

    r = Rig()
    r.pipeline, r.store, r.exchange, r.alerts = pipeline, store, exchange, alerts
    yield r
    store.close()


def send(rig, mid, text, *, reply_to=None, timestamp=None):
    return rig.pipeline.handle_message(
        channel_id=CH, message_id=mid, text=text, reply_to_message_id=reply_to,
        timestamp=time.time() if timestamp is None else timestamp)


def open_short(rig, mid, stop, *, fill=True):
    rig.exchange.autofill_entries = fill
    r = send(rig, mid, short_sol(stop))
    rig.exchange.autofill_entries = False
    assert r.accepted, r.detail
    return r.trade_id


@pytest.fixture
def two_shorts(rig):
    """Two open SOL shorts from different signals, stops 155 and 156."""
    a = open_short(rig, SIGNAL_A, "155")
    b = open_short(rig, SIGNAL_B, "156")
    assert a != b
    assert len(rig.store.active_trades_for_symbol("PF_SOLUSD")) == 2
    rig.alerts.clear()
    return a, b


def stop_of(rig, trade_id):
    uid = rig.store.get_trade(trade_id)["trade_uid"]
    stops = [o for o in rig.exchange.get_open_orders()
             if o.order_type == "stop" and o.client_order_id.startswith(f"{uid}-")]
    assert len(stops) == 1
    return stops[0]


def link_of(rig, mid):
    m = rig.store.get_message(CH, mid)
    return m["reply_to_message_id"], m["linked_trade_id"]


# ==========================================================================
# The real scenario: two SOL shorts, replies to each
# ==========================================================================


def test_result_replies_link_to_their_own_trade_and_only_log(rig, two_shorts):
    a, b = two_shorts
    r2 = send(rig, 1010, TP2, reply_to=SIGNAL_A)
    r3 = send(rig, 1011, TP3, reply_to=SIGNAL_B)

    assert (r2.message_class, r2.action, r2.trade_id) == ("RESULT_REPORT", "LOG_ONLY", a)
    assert (r3.message_class, r3.action, r3.trade_id) == ("RESULT_REPORT", "LOG_ONLY", b)
    assert link_of(rig, 1010) == (SIGNAL_A, a)
    assert link_of(rig, 1011) == (SIGNAL_B, b)
    assert rig.store.latest_message_version(CH, 1010)["trade_id"] == a
    assert rig.store.latest_message_version(CH, 1011)["trade_id"] == b
    assert rig.alerts == []
    assert stop_of(rig, a).trigger_price == D(155)
    assert stop_of(rig, b).trigger_price == D(156)


def test_stop_update_replying_to_a_changes_only_a(rig, two_shorts):
    a, b = two_shorts
    stop_b = stop_of(rig, b)
    r = send(rig, 1020, "SL Update : 153", reply_to=SIGNAL_A)

    assert r.accepted and r.action == "UPDATE_STOP" and r.trade_id == a
    assert stop_of(rig, a).trigger_price == D(153)
    after_b = stop_of(rig, b)
    assert after_b.exchange_order_id == stop_b.exchange_order_id
    assert after_b.trigger_price == D(156)
    assert D(rig.store.get_trade(b)["stop_trigger_price"]) == D(156)
    assert link_of(rig, 1020) == (SIGNAL_A, a)


def test_cancel_replying_to_a_signal_we_never_traded_does_nothing_and_alerts(rig):
    """Signal 1001 arrived too late and was refused (no trade). B is the ONLY
    live SOL trade and is still unentered, so a symbol fallback would cancel
    it. The reply must not fall back."""
    stale = send(rig, SIGNAL_A, short_sol("155"), timestamp=time.time() - 7200)
    assert not stale.accepted and rig.store.trade_for_origin_message(CH, SIGNAL_A) is None
    b = open_short(rig, SIGNAL_B, "156", fill=False)
    assert rig.store.get_trade(b)["state"] == TradeState.WAITING_FOR_ENTRY.value
    rig.alerts.clear()

    r = send(rig, 1030, CANCEL, reply_to=SIGNAL_A)

    assert not r.accepted and r.action == "REVIEW_ALERT" and r.trade_id is None
    assert len(rig.alerts) == 1
    assert f"signal {SIGNAL_A}, which never opened a trade" in rig.alerts[0]["reason"]
    assert rig.store.get_trade(b)["state"] == TradeState.WAITING_FOR_ENTRY.value
    assert link_of(rig, 1030) == (SIGNAL_A, None)


def test_non_reply_update_with_two_live_trades_does_nothing_and_alerts(rig, two_shorts):
    a, b = two_shorts
    r = send(rig, 1040, "SOL SL Update : 153")

    assert not r.accepted and r.action == "REVIEW_ALERT"
    assert "2 open trades on PF_SOLUSD" in rig.alerts[-1]["reason"]
    assert stop_of(rig, a).trigger_price == D(155)
    assert stop_of(rig, b).trigger_price == D(156)
    assert link_of(rig, 1040) == (None, None)


# ==========================================================================
# Edges of the rule
# ==========================================================================


def test_non_reply_update_with_exactly_one_live_trade_applies(rig):
    a = open_short(rig, SIGNAL_A, "155")
    r = send(rig, 1050, "SOL SL Update : 153")
    assert r.accepted and r.trade_id == a
    assert stop_of(rig, a).trigger_price == D(153)


def test_reply_to_a_signal_whose_trade_is_finished_alerts(rig):
    """A cancelled (or expired, or closed) trade still exists as a row; a reply
    to its signal must not act on the OTHER live trade on the symbol."""
    a = open_short(rig, SIGNAL_A, "155", fill=False)
    b = open_short(rig, SIGNAL_B, "156")
    cancelled = send(rig, 1059, CANCEL, reply_to=SIGNAL_A)
    assert cancelled.accepted and cancelled.trade_id == a
    assert rig.store.get_trade(a)["state"] == TradeState.CANCELLED.value

    r = send(rig, 1060, "SL Update : 153", reply_to=SIGNAL_A)
    assert r.action == "REVIEW_ALERT" and "already CANCELLED" in rig.alerts[-1]["reason"]
    assert stop_of(rig, b).trigger_price == D(156)
    assert link_of(rig, 1060) == (SIGNAL_A, a), "still linked, for the record"


def test_reply_to_an_edited_signal_still_targets_its_trade(rig, two_shorts):
    a, b = two_shorts
    edited = short_sol("155").replace("142.00000000", "142.00000000 / HOLD")
    rig.pipeline.handle_edit(channel_id=CH, message_id=SIGNAL_A, text=edited,
                             edit_date=datetime.now(UTC).isoformat(),
                             timestamp=time.time())
    assert rig.store.message_versions(CH, SIGNAL_A)[-1]["version"] == 2
    r = send(rig, 1070, "SL Update : 153", reply_to=SIGNAL_A)
    assert r.accepted and r.trade_id == a
    assert stop_of(rig, a).trigger_price == D(153)
    assert stop_of(rig, b).trigger_price == D(156)


def test_reply_to_a_reply_follows_the_chain_to_the_original_signal(rig, two_shorts):
    a, b = two_shorts
    send(rig, 1080, TP2, reply_to=SIGNAL_B)
    r = send(rig, 1081, "SL Update : 154", reply_to=1080)
    assert r.accepted and r.trade_id == b
    assert stop_of(rig, b).trigger_price == D(154)
    assert stop_of(rig, a).trigger_price == D(155)
    assert link_of(rig, 1080) == (SIGNAL_B, b)
    assert link_of(rig, 1081) == (1080, b)


def test_reply_chain_through_a_message_we_never_saw_alerts(rig, two_shorts):
    a, b = two_shorts
    send(rig, 1090, TP2, reply_to=777)              # 777 predates our history
    r = send(rig, 1091, "SL Update : 153", reply_to=1090)
    assert r.action == "REVIEW_ALERT" and "never stored" in rig.alerts[-1]["reason"]
    assert stop_of(rig, a).trigger_price == D(155)
    assert stop_of(rig, b).trigger_price == D(156)
    assert link_of(rig, 1090) == (777, None)


def test_reply_chain_stops_at_an_untraded_signal_and_never_reaches_an_older_trade(rig, two_shorts):
    """Signal C replies to signal A (the channel re-posting a setup) and was
    refused. A reply to C is about C, not A."""
    a, b = two_shorts
    c = send(rig, 1100, short_sol("157"), reply_to=SIGNAL_A)   # third SOL trade: refused
    assert not c.accepted and rig.store.trade_for_origin_message(CH, 1100) is None
    r = send(rig, 1101, "SL Update : 153", reply_to=1100)
    assert r.action == "REVIEW_ALERT"
    assert "signal 1100, which never opened a trade" in rig.alerts[-1]["reason"]
    assert stop_of(rig, a).trigger_price == D(155)


def test_close_replying_to_a_closes_only_a(rig, two_shorts):
    from tests.test_pipeline import fill_closes
    a, b = two_shorts
    fill_closes(rig.exchange)
    r = send(rig, 1110, "Close SOL", reply_to=SIGNAL_A)
    assert r.accepted and r.trade_id == a
    assert rig.store.get_trade(a)["state"] == TradeState.CLOSED.value
    assert rig.store.get_trade(b)["state"] != TradeState.CLOSED.value


def test_close_replying_to_an_untraded_signal_closes_nothing(rig, two_shorts):
    from tests.test_pipeline import fill_closes
    a, b = two_shorts
    fill_closes(rig.exchange)
    r = send(rig, 1120, "Close SOL", reply_to=999)
    assert r.action == "REVIEW_ALERT"
    for t in (a, b):
        assert rig.store.get_trade(t)["state"] != TradeState.CLOSED.value


# ==========================================================================
# Storage
# ==========================================================================


def test_a_signal_is_linked_to_the_trade_it_opened(rig):
    a = open_short(rig, SIGNAL_A, "155")
    assert link_of(rig, SIGNAL_A) == (None, a)


def test_an_existing_database_gains_the_link_column(tmp_path):
    """The live database predates linked_trade_id: opening it must add the
    column without touching the rows already there."""
    path = tmp_path / "old.db"
    Store(path).close()
    conn = sqlite3.connect(path)
    conn.execute("DROP INDEX idx_messages_linked_trade")
    conn.execute("ALTER TABLE messages DROP COLUMN linked_trade_id")
    conn.execute("INSERT INTO messages (channel_id, message_id, received_at, raw_text) "
                  "VALUES (1, 2, 'now', 'old row')")
    conn.commit()
    conn.close()

    store = Store(path)
    row = store.get_message(1, 2)
    assert row["raw_text"] == "old row" and row["linked_trade_id"] is None
    store.close()
