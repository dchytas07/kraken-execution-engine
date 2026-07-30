"""main.py --test-alert: confirm ALERT_TELEGRAM_BOT_TOKEN/CHAT_ID work
without running full startup or touching the exchange.

load_config() is monkeypatched to the real loader called with an explicit
env dict (dotenv_path=None) — never the real .env / os.environ — so these
tests can't accidentally pick up or exercise real credentials.
"""

import bot.config as config_module
import bot.main as main_module


def _fake_load_config(tmp_path, **env_overrides):
    env = {
        "TRADING_MODE": "paper", "TRADE_MARGIN_USD": "30",
        "MAX_LEVERAGE": "10", "DEFAULT_LEVERAGE": "10",
        "MAX_ENTRY_DEVIATION_PERCENT": "1.5",
        "KILL_SWITCH_PATH": str(tmp_path / "KILL"),
        "DB_PATH": str(tmp_path / "t.db"),
        "LOG_PATH": str(tmp_path / "t.jsonl"),
        "ALERT_TELEGRAM_BOT_TOKEN": "TEST_TOKEN",
        "ALERT_TELEGRAM_CHAT_ID": "TEST_CHAT",
    }
    env.update(env_overrides)
    return lambda: config_module.load_config(env, dotenv_path=None)


def _never_call(*a, **kw):
    raise AssertionError("--test-alert must not run full startup")


def test_test_alert_sends_and_never_runs_startup(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(main_module, "load_config", _fake_load_config(tmp_path))
    monkeypatch.setattr(main_module, "startup", _never_call)
    calls = []
    monkeypatch.setattr(
        main_module, "send_alert",
        lambda token, chat_id, text: calls.append((token, chat_id, text)) or True,
    )

    code = main_module.main(["--test-alert"])

    assert code == 0
    assert calls == [("TEST_TOKEN", "TEST_CHAT", calls[0][2])]
    assert "sent" in capsys.readouterr().out.lower()


def test_test_alert_failure_reports_nonzero_exit(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(main_module, "load_config", _fake_load_config(tmp_path))
    monkeypatch.setattr(main_module, "startup", _never_call)
    monkeypatch.setattr(main_module, "send_alert", lambda *a: False)

    code = main_module.main(["--test-alert"])

    assert code == 1
    assert "fail" in capsys.readouterr().err.lower()


class _StubStore:
    def close(self):
        pass


def test_check_flag_is_unaffected_by_test_alert_wiring(tmp_path, monkeypatch):
    """--check must still run the full startup path as before."""
    monkeypatch.setattr(main_module, "load_config", _fake_load_config(tmp_path))
    ran = []
    monkeypatch.setattr(
        main_module, "startup",
        lambda config: (ran.append(config), (_StubStore(), None))[1],
    )

    code = main_module.main(["--check"])

    assert code == 0
    assert len(ran) == 1
