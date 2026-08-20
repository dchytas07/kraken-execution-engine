"""send_alert: the Bot API client itself must never raise.

Content and wiring for the two call sites (SAFE_ERROR, REQUIRES_REVIEW) are
covered in test_execution.py / test_recovery.py, closer to where the
transitions actually happen. This file is about send_alert in isolation:
every way it can fail, and proof that none of them propagate — plus the
daily digest, which is built on it.
"""

import json
import logging
import urllib.error
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from bot.alerting import (
    send_alert,
    send_daily_summary,
    send_unresolved_digest,
)
from bot.db.store import Store

SRC_PATH = __import__('pathlib').Path(__file__).parent.parent / 'src'
from bot.models import EventType, TradeState


def _fake_response(status, body=None):
    resp = MagicMock()
    resp.status = status
    resp.read = MagicMock(
        return_value=json.dumps({"ok": True} if body is None else body).encode()
    )
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_missing_credentials_skips_send_without_raising(caplog):
    with caplog.at_level(logging.WARNING):
        assert send_alert(None, None, "hello") is False
    assert any("not configured" in r.message for r in caplog.records)


def test_successful_send_posts_chat_id_and_text():
    with patch("bot.alerting.urllib.request.urlopen",
               return_value=_fake_response(200)) as mock_urlopen:
        assert send_alert("TOKEN", "12345", "hello world") is True

    request = mock_urlopen.call_args[0][0]
    assert request.full_url == "https://api.telegram.org/botTOKEN/sendMessage"
    assert json.loads(request.data) == {"chat_id": "12345", "text": "hello world"}


def test_non_200_response_returns_false_and_logs(caplog):
    with patch("bot.alerting.urllib.request.urlopen",
               return_value=_fake_response(500)):
        with caplog.at_level(logging.WARNING):
            assert send_alert("TOKEN", "12345", "hello") is False
    assert any("alert send failed" in r.message for r in caplog.records)


def test_http_200_with_ok_false_is_not_treated_as_sent(caplog):
    """HTTP 200 != Telegram sent it. Returning True on the status code alone
    would be the same submitted-equals-confirmed mistake brief 27 forbids on
    the order path."""
    body = {"ok": False, "description": "Forbidden: bot was blocked by the user"}
    with patch("bot.alerting.urllib.request.urlopen",
               return_value=_fake_response(200, body)):
        with caplog.at_level(logging.WARNING):
            assert send_alert("TOKEN", "12345", "hello") is False
    assert any("bot was blocked by the user" in r.message for r in caplog.records)


def test_unparseable_body_is_not_treated_as_sent(caplog):
    resp = MagicMock()
    resp.status = 200
    resp.read = MagicMock(return_value=b"<html>not json</html>")
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    with patch("bot.alerting.urllib.request.urlopen", return_value=resp):
        with caplog.at_level(logging.WARNING):
            assert send_alert("TOKEN", "12345", "hello") is False


def test_network_error_does_not_propagate(caplog):
    """The requirement: a failed send must never raise out of send_alert."""
    with patch("bot.alerting.urllib.request.urlopen",
               side_effect=urllib.error.URLError("connection refused")):
        with caplog.at_level(logging.WARNING):
            result = send_alert("TOKEN", "12345", "hello")  # must not raise
    assert result is False
    assert any("alert send failed" in r.message for r in caplog.records)


def test_unexpected_exception_does_not_propagate(caplog):
    """Broader than a network error: send_alert must survive anything."""
    with patch("bot.alerting.urllib.request.urlopen",
               side_effect=RuntimeError("something unrelated broke")):
        with caplog.at_level(logging.WARNING):
            result = send_alert("TOKEN", "12345", "hello")  # must not raise
    assert result is False


# ==========================================================================
# Daily digest of unresolved trades
# ==========================================================================


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "digest.db")
    yield s
    s.close()


def quarantine(store, state, symbol="PF_XBTUSD", reason="something went wrong"):
    """Create a trade and force it into an unresolved state."""
    msg = store.record_message(channel_id=-1, message_id=hash(symbol) % 10000,
                               raw_text="sig")
    trade_id, _ = store.create_trade(
        origin_message_row_id=msg, origin_channel_id=-1,
        origin_message_id=hash(symbol) % 10000,
        asset=symbol[3:6], symbol=symbol, direction="LONG",
    )
    store.transition_trade(trade_id, state, reason)
    return trade_id


@pytest.fixture
def sent(monkeypatch):
    """Capture digest sends without touching the network."""
    calls = []
    monkeypatch.setattr(
        "bot.alerting.send_alert",
        lambda token, chat_id, text: calls.append((token, chat_id, text)) or True,
    )
    return calls


def test_digest_stays_silent_when_nothing_is_unresolved(store, sent):
    """A channel that pings daily to say "all fine" trains you to ignore it."""
    quarantine(store, TradeState.PARSED)  # healthy trade, not unresolved

    assert send_unresolved_digest(store, "TOKEN", "CHAT") is False
    assert sent == []


def test_digest_lists_each_unresolved_trade_with_id_symbol_state_age_reason(
    store, sent
):
    review = quarantine(store, TradeState.REQUIRES_REVIEW, "PF_XBTUSD",
                        "unresolvable order intent after restart")
    safe = quarantine(store, TradeState.SAFE_ERROR, "PF_ETHUSD",
                      "UNPROTECTED: stop resize failed")
    quarantine(store, TradeState.PARSED, "PF_SOLUSD")  # healthy, must be absent

    assert send_unresolved_digest(store, "TOKEN", "CHAT") is True

    assert len(sent) == 1
    token, chat_id, text = sent[0]
    assert (token, chat_id) == ("TOKEN", "CHAT")
    assert "2 trade(s)" in text
    # Both unresolved trades, with every field the operator needs.
    assert f"trade {review} (PF_XBTUSD)" in text
    assert "REQUIRES_REVIEW" in text
    assert "unresolvable order intent after restart" in text
    assert f"trade {safe} (PF_ETHUSD)" in text
    assert "SAFE_ERROR" in text
    assert "UNPROTECTED: stop resize failed" in text
    assert "stuck ~" in text  # age is reported
    # The healthy trade must not appear.
    assert "PF_SOLUSD" not in text


def test_digest_reports_age_from_state_change_not_last_touch(store, sent):
    """`updated_at` is bumped by update_trade_actuals, so it would make a
    long-quarantined trade look freshly changed. Age must come from the
    TRADE_STATE_CHANGED event instead.

    The events table is append-only (enforced by a trigger), so rather than
    backdating the event this advances the clock 3 days and simulates the
    row being touched at that later moment — which is exactly what
    update_trade_actuals does to a still-quarantined trade.
    """
    trade_id = quarantine(store, TradeState.SAFE_ERROR)
    three_days_on = datetime.now(UTC) + timedelta(days=3)

    # Simulate update_trade_actuals running "now" (3 days after the state
    # change): updated_at moves, the state-change event does not.
    with store.transaction() as conn:
        conn.execute(
            "UPDATE trades SET updated_at = ? WHERE id = ?",
            (three_days_on.isoformat(), trade_id),
        )

    assert send_unresolved_digest(store, "TOKEN", "CHAT", now=three_days_on) is True
    text = sent[0][2]
    assert "stuck ~3d" in text, (
        f"age came from updated_at, not the state-change event: {text!r}"
    )


def test_digest_sends_only_once_per_24h(store, sent):
    quarantine(store, TradeState.SAFE_ERROR)

    assert send_unresolved_digest(store, "TOKEN", "CHAT") is True
    assert send_unresolved_digest(store, "TOKEN", "CHAT") is False, "sent twice"
    assert len(sent) == 1


def test_digest_sends_again_after_the_window_elapses(store, sent):
    quarantine(store, TradeState.SAFE_ERROR)
    assert send_unresolved_digest(store, "TOKEN", "CHAT") is True

    later = datetime.now(UTC) + timedelta(hours=25)
    assert send_unresolved_digest(store, "TOKEN", "CHAT", now=later) is True
    assert len(sent) == 2


def test_quiet_check_still_marks_the_window(store, sent):
    """A no-op check records the cadence marker, so a trade that goes bad
    right afterwards is not double-reported alongside its own immediate
    transition alert — it waits for the next window."""
    assert send_unresolved_digest(store, "TOKEN", "CHAT") is False
    quarantine(store, TradeState.SAFE_ERROR)

    assert send_unresolved_digest(store, "TOKEN", "CHAT") is False
    assert sent == []


def test_failed_send_does_not_consume_the_window(store, monkeypatch):
    """One transient blip must not cost a whole day of visibility."""
    quarantine(store, TradeState.SAFE_ERROR)
    monkeypatch.setattr("bot.alerting.send_alert", lambda *a: False)

    assert send_unresolved_digest(store, "TOKEN", "CHAT") is False
    assert store.last_event_at(EventType.ALERT_DIGEST) is None, (
        "a failed send recorded the cadence marker and skipped the day"
    )

    # The next attempt retries immediately rather than waiting 24h.
    calls = []
    monkeypatch.setattr(
        "bot.alerting.send_alert",
        lambda t, c, text: calls.append(text) or True,
    )
    assert send_unresolved_digest(store, "TOKEN", "CHAT") is True
    assert len(calls) == 1


def test_digest_never_raises_when_the_store_is_broken(caplog):
    """Observability must not be able to crash the bot."""
    class BrokenStore:
        def last_event_at(self, _):
            raise RuntimeError("database is gone")

    with caplog.at_level(logging.WARNING):
        result = send_unresolved_digest(BrokenStore(), "TOKEN", "CHAT")

    assert result is False
    assert any("digest failed" in r.message for r in caplog.records)


def test_injected_clock_is_used_for_the_cadence_marker_too(store, sent):
    """Found by a live demo send, not by the suite.

    `now` is injectable, but the ALERT_DIGEST marker was stamped with the
    store's real clock. Passing a `now` in the future therefore wrote a
    marker in the past, so the very next call saw the window as elapsed and
    sent a duplicate. The injected clock must govern both, or it only half
    works — and the caller gets two messages.
    """
    quarantine(store, TradeState.SAFE_ERROR)
    now = datetime.now(UTC) + timedelta(days=2, hours=7)

    assert send_unresolved_digest(store, "TOKEN", "CHAT", now=now) is True
    assert send_unresolved_digest(store, "TOKEN", "CHAT", now=now) is False, (
        "duplicate digest: the marker did not use the injected clock"
    )
    assert len(sent) == 1


# ==========================================================================
# Trading-action notifications
# ==========================================================================


class Cfg:
    alert_telegram_bot_token = "TOKEN"
    alert_telegram_chat_id = "CHAT"


def test_greeting_matches_the_time_of_day():
    from bot.alerting import greeting
    assert any(w in greeting(datetime(2026, 7, 31, 8, 0)) for w in ("Morning", "Rise", "Early"))
    assert any(w in greeting(datetime(2026, 7, 31, 14, 0)) for w in ("Afternoon", "Midday", "Hey"))
    assert any(w in greeting(datetime(2026, 7, 31, 19, 0)) for w in ("Evening", "Winding", "Hey"))
    assert any(w in greeting(datetime(2026, 7, 31, 3, 0)) for w in ("Late", "Night", "midnight", "Overnight", "Still"))


def test_greeting_rotates_rather_than_repeating():
    """A fixed opener every time reads like a stuck machine."""
    from bot.alerting import greeting
    seen = {greeting(datetime(2026, 7, 31, 9, m)) for m in range(0, 60, 7)}
    assert len(seen) > 1


def test_trade_opened_alert_names_symbol_side_and_size(sent):
    from bot.alerting import notify_trade_event
    assert notify_trade_event(Cfg(), "opened", symbol="PF_XBTUSD",
                              side="LONG", size="0.0046") is True
    text = sent[0][2]
    assert "Trade opened" in text and "PF_XBTUSD" in text
    assert "LONG" in text and "0.0046" in text


def test_tp_and_close_alerts_report_pnl_with_a_sign(sent):
    from bot.alerting import notify_trade_event
    notify_trade_event(Cfg(), "tp_filled", symbol="PF_ETHUSD", side="SHORT",
                       size="0.5", pnl="12.5")
    notify_trade_event(Cfg(), "closed", symbol="PF_ETHUSD", side="SHORT",
                       size="0.5", pnl="-3.25")
    assert "+$12.50" in sent[0][2]
    assert "-$3.25" in sent[1][2]


def test_unknown_pnl_is_never_reported_as_zero(sent):
    """A confident 0.00 reads as breakeven; unknown must read as unknown."""
    from bot.alerting import _money
    assert _money(None) == "—"
    assert _money("not-a-number") == "—"


def test_notification_failure_never_propagates(monkeypatch):
    from bot.alerting import notify_trade_event
    monkeypatch.setattr("bot.alerting.send_alert",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    assert notify_trade_event(Cfg(), "opened", symbol="PF_XBTUSD") is False


def test_daily_summary_reports_realised_pnl_and_open_trades(store, sent):
    from bot.alerting import send_daily_summary
    quarantine(store, TradeState.PARSED, "PF_XBTUSD")  # an open, healthy trade
    assert send_daily_summary(store, Cfg()) is True
    text = sent[0][2]
    assert "Daily summary" in text
    assert "realised P&L" in text
    assert "PF_XBTUSD" in text


def test_daily_summary_sends_even_on_a_quiet_day(store, sent):
    """Unlike the unresolved digest, "nothing happened" IS the daily report."""
    assert send_daily_summary(store, Cfg()) is True
    assert "no open trades" in sent[0][2]


def test_daily_summary_is_once_per_24h(store, sent):
    from bot.alerting import send_daily_summary
    assert send_daily_summary(store, Cfg()) is True
    assert send_daily_summary(store, Cfg()) is False
    later = datetime.now(UTC) + timedelta(hours=25)
    assert send_daily_summary(store, Cfg(), now=later) is True
    assert len(sent) == 2


def test_daily_summary_never_raises_on_a_broken_store(caplog):
    from bot.alerting import send_daily_summary
    class Broken:
        def last_event_at(self, _): raise RuntimeError("db gone")
    with caplog.at_level(logging.WARNING):
        assert send_daily_summary(Broken(), Cfg()) is False


# ==========================================================================
# Daily summary: cadence must survive restarts, and show open positions
# ==========================================================================


class FakePos:
    def __init__(self, symbol, side, size, entry, mark=None, pnl=None):
        self.symbol, self.side, self.size = symbol, side, Decimal(str(size))
        self.entry_price = Decimal(str(entry))
        self.mark_price = None if mark is None else Decimal(str(mark))
        self.unrealized_pnl = None if pnl is None else Decimal(str(pnl))


def _fresh_store(path):
    """A NEW Store over the same file — what a restart actually is."""
    return Store(path)


def test_cadence_marker_is_written_to_the_database(tmp_path, sent):
    """Persistence, asserted on the DB itself rather than inferred."""
    db = tmp_path / "c.db"
    s1 = Store(db)
    assert send_daily_summary(s1, Cfg()) is True
    s1.close()

    # A brand-new Store over the same file — what a restart actually is.
    s2 = Store(db)
    assert s2.last_event_at(EventType.DAILY_SUMMARY) is not None, (
        "no DAILY_SUMMARY marker in the database: the cadence cannot survive "
        "a restart"
    )
    assert send_daily_summary(s2, Cfg()) is False
    s2.close()
    assert len(sent) == 1


def test_cadence_is_read_from_the_store_not_a_process_timer(sent):
    """THE regression, and it must be able to FAIL.

    An earlier version of this test reopened a Store in-process and asserted
    no second send. That passes even with a module-global timer, because
    module globals survive within one pytest process — a check that cannot
    fail. This instead hands over a store whose last_event_at reports a
    recent send: anything reading a process-local timer ignores it and sends
    anyway.
    """
    class StoreSayingItSentAnHourAgo:
        # These return VALID data on purpose. An earlier version raised here
        # to prove they were not reached, but the raise was swallowed by
        # send_daily_summary's own except and the test passed for the wrong
        # reason. With valid data, the ONLY thing that can produce False is
        # the cadence check actually consulting last_event_at.
        def last_event_at(self, event_type):
            return (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        def realized_pnl_since(self, since):
            return (Decimal("0"), 0, 0)
        def open_trades(self):
            return []
        def append_event(self, *a, **kw):
            return 1

    assert send_daily_summary(StoreSayingItSentAnHourAgo(), Cfg()) is False
    assert sent == [], "sent despite the store reporting a send 1 hour ago"


def test_a_restart_does_not_reset_the_window_in_a_fresh_interpreter(tmp_path, sent):
    """The faithful version: a REAL second process, so nothing in-process
    can carry the cadence over."""
    import subprocess, sys, textwrap
    db = tmp_path / "c.db"
    s1 = Store(db)
    assert send_daily_summary(s1, Cfg()) is True
    s1.close()

    script = (
        f"import sys; sys.path.insert(0, {str(SRC_PATH)!r})\n"
        "from bot.alerting import send_daily_summary\n"
        "from bot.db.store import Store\n"
        "from pathlib import Path\n"
        "import bot.alerting as a\n"
        "a.send_alert = lambda *args: (_ for _ in ()).throw("
        "AssertionError('SENT AGAIN AFTER RESTART'))\n"
        "class C:\n"
        "    alert_telegram_bot_token = 'T'\n"
        "    alert_telegram_chat_id = 'C'\n"
        f"s = Store(Path({str(db)!r}))\n"
        "print('RESULT', send_daily_summary(s, C()))\n"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                         text=True, timeout=60)
    assert "RESULT False" in out.stdout, (
        f"a fresh interpreter re-sent the summary.\nstdout={out.stdout}\n"
        f"stderr={out.stderr[-500:]}"
    )


def test_cadence_reopens_after_24h_even_across_restarts(tmp_path, sent):
    db = tmp_path / "c.db"
    s1 = _fresh_store(db)
    assert send_daily_summary(s1, Cfg()) is True
    s1.close()
    later = datetime.now(UTC) + timedelta(hours=25)
    s2 = _fresh_store(db)
    assert send_daily_summary(s2, Cfg(), now=later) is True
    s2.close()
    assert len(sent) == 2


def test_summary_shows_open_positions_with_dollar_and_percent_pnl(store, sent):
    quarantine(store, TradeState.PARSED, "PF_XBTUSD")
    store.update_trade_actuals(1, actual_quantity=Decimal("0.0046"),
                               actual_avg_entry_price=Decimal("63630"))
    pos = [FakePos("PF_XBTUSD", "short", "0.0046", "63630",
                   mark="64456.24", pnl="-3.80")]
    assert send_daily_summary(store, Cfg(), positions=pos) is True
    text = sent[0][2]
    assert "open trades (1)" in text
    assert "PF_XBTUSD" in text
    assert "-$3.80" in text, "dollar P&L missing"
    assert "%" in text and "of entry notional" in text, "percent P&L missing"
    assert "mark 64456" in text


def test_summary_derives_pnl_when_the_exchange_omits_it(store, sent):
    quarantine(store, TradeState.PARSED, "PF_XBTUSD")
    store.update_trade_actuals(1, actual_quantity=Decimal("1"),
                               actual_avg_entry_price=Decimal("100"))
    # No unrealized_pnl from the exchange, but a mark is available.
    pos = [FakePos("PF_XBTUSD", "long", "1", "100", mark="110", pnl=None)]
    assert send_daily_summary(store, Cfg(), positions=pos) is True
    assert "+$10.00" in sent[0][2]


def test_unrecorded_pnl_is_flagged_not_silently_counted_as_zero(store, sent):
    """SUM() skips NULLs, so a stop-out with no computed P&L used to be
    reported as $0.00 — indistinguishable from a genuine breakeven. This
    happened for real: trade 3, a -$5.86 stop-out, summarised as
    "+$0.00 across 1 closed trade"."""
    tid = quarantine(store, TradeState.PARSED, "PF_XBTUSD")
    store.transition_trade(tid, TradeState.CLOSING)
    store.transition_trade(tid, TradeState.CLOSED)   # closed, realized_pnl NULL

    assert send_daily_summary(store, Cfg()) is True
    text = sent[0][2]
    assert "1 closed trade(s)" in text
    assert "no recorded P&L" in text, (
        "a closed trade with NULL P&L was folded into the total as zero"
    )
