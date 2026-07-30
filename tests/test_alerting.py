"""send_alert: the Bot API client itself must never raise.

Content and wiring for the two call sites (SAFE_ERROR, REQUIRES_REVIEW) are
covered in test_execution.py / test_recovery.py, closer to where the
transitions actually happen. This file is about send_alert in isolation:
every way it can fail, and proof that none of them propagate.
"""

import json
import logging
import urllib.error
from unittest.mock import MagicMock, patch

from bot.alerting import send_alert


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
