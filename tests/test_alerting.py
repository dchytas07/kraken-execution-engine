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
from unittest.mock import MagicMock, patch

import pytest

from bot.alerting import send_alert, send_unresolved_digest
from bot.db.store import Store
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
