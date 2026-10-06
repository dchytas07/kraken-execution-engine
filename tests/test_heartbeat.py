"""send_heartbeat: best-effort, short timeout, never raises, never leaks the URL."""

import logging
import urllib.error

from bot import heartbeat
from bot.config import collect_secrets

URL = "https://hc.example/ping/SECRET-UUID-1234"


class Resp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_no_url_is_off(monkeypatch):
    monkeypatch.setattr(heartbeat.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    assert heartbeat.send_heartbeat(None) is False
    assert heartbeat.send_heartbeat("") is False


def test_success_uses_a_short_timeout(monkeypatch):
    seen = {}

    def fake(req, timeout):
        seen["url"], seen["timeout"] = req.full_url, timeout
        return Resp(200)

    monkeypatch.setattr(heartbeat.urllib.request, "urlopen", fake)
    assert heartbeat.send_heartbeat(URL) is True
    assert seen == {"url": URL, "timeout": heartbeat.HEARTBEAT_TIMEOUT_SECONDS}
    assert heartbeat.HEARTBEAT_TIMEOUT_SECONDS <= 5


def test_network_error_is_swallowed_and_url_not_logged(monkeypatch, caplog):
    def fake(req, timeout):
        raise urllib.error.URLError(f"cannot reach {URL}")

    monkeypatch.setattr(heartbeat.urllib.request, "urlopen", fake)
    with caplog.at_level(logging.DEBUG):
        assert heartbeat.send_heartbeat(URL) is False
    assert "heartbeat ping failed" in caplog.text
    assert "SECRET-UUID" not in caplog.text


def test_timeout_and_garbage_are_swallowed(monkeypatch):
    for exc in (TimeoutError("slow"), OSError("x"), ValueError("bad url")):
        monkeypatch.setattr(heartbeat.urllib.request, "urlopen",
                            lambda req, timeout, e=exc: (_ for _ in ()).throw(e))
        assert heartbeat.send_heartbeat(URL) is False
    assert heartbeat.send_heartbeat("not a url") is False


def test_non_2xx_is_a_failure(monkeypatch):
    monkeypatch.setattr(heartbeat.urllib.request, "urlopen",
                        lambda req, timeout: Resp(500))
    assert heartbeat.send_heartbeat(URL) is False


def test_url_is_scrubbed_from_logs():
    assert URL in collect_secrets({"HEALTHCHECK_URL": URL})
