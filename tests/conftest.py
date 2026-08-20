"""Shared test setup.

The suite must never touch the network. It was: every test that opened,
closed or escalated a trade called notify_trade_event -> send_alert -> a
real HTTPS request to api.telegram.org, which returned 404 for the test
token after a full round-trip. ~0.6s per alerting test, and the suite grew
from 15s to 129s once alerts were wired into the lifecycle.

Worse than slow, it was wrong: the tests depended on connectivity and on a
third party, and they were quietly sending traffic on every run.

Patching at `urlopen` rather than at `send_alert` is deliberate. Several
modules do `from .alerting import send_alert` / `notify_trade_event`, which
binds the function in THEIR namespace at import time — patching
`bot.alerting.send_alert` would miss those references entirely. The network
call is the one choke point every path funnels through.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def no_real_telegram(monkeypatch):
    """Block outbound Telegram calls; return Telegram's success envelope.

    Tests that assert on send behaviour patch `urlopen` themselves inside
    the test body, which takes precedence over this.
    """
    def fake_urlopen(request, timeout=None):
        resp = MagicMock()
        resp.status = 200
        resp.read = MagicMock(return_value=json.dumps({"ok": True}).encode())
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    monkeypatch.setattr("bot.alerting.urllib.request.urlopen", fake_urlopen)
