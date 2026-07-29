"""Kill switch, CLI gateway error handling, and API-key permission checks."""

import json

import pytest

from bot.exchange.cli import ErrorCategory, KrakenCli, KrakenCliError
from bot.killswitch import KillSwitch
from bot.models import TradingMode
from bot.safety.permissions import (
    CheckOutcome,
    SafetyCheckFailed,
    check_connectivity,
    check_no_withdrawal_permission,
    enforce,
    run_permission_checks,
)


# ==========================================================================
# Kill switch (brief 25)
# ==========================================================================


@pytest.fixture
def switch(tmp_path):
    return KillSwitch(path=tmp_path / "KILL_SWITCH")


def test_absent_file_means_trading_allowed(switch):
    assert not switch.is_engaged()
    assert switch.allows_new_positions()


def test_present_file_blocks_new_positions(switch):
    switch.engage()
    assert switch.is_engaged()
    assert not switch.allows_new_positions()


def test_engaged_switch_still_allows_managing_existing_positions(switch):
    """Documented behaviour: stop opening, keep protecting (brief 25)."""
    switch.engage()
    assert switch.allows_position_management()


def test_switch_is_read_from_disk_every_time_not_cached(switch):
    """An operator's `touch` must take effect without a restart."""
    assert switch.allows_new_positions()
    switch.engage()
    assert not switch.allows_new_positions()
    switch.release()
    assert switch.allows_new_positions()


def test_unreadable_path_fails_closed(tmp_path):
    """If state cannot be determined, assume engaged."""

    class Exploding:
        def exists(self):
            raise OSError("permission denied")

        @property
        def parent(self):
            return tmp_path

    ks = KillSwitch(path=Exploding())
    assert ks.is_engaged()
    assert not ks.allows_new_positions()


# ==========================================================================
# CLI gateway
# ==========================================================================


def fake_cli(payload, code=0, stderr=""):
    text = payload if isinstance(payload, str) else json.dumps(payload)

    def runner(argv, timeout):
        return code, text, stderr

    return KrakenCli(runner=runner)


def test_json_output_flag_is_always_appended():
    captured = {}

    def runner(argv, timeout):
        captured["argv"] = argv
        return 0, "{}", ""

    KrakenCli(runner=runner).run("futures", "positions")
    assert captured["argv"][-2:] == ["-o", "json"]


def test_successful_call_returns_parsed_data():
    result = fake_cli({"positions": [], "count": 0}).run("futures", "positions")
    assert result.ok
    assert result.data["count"] == 0


@pytest.mark.parametrize(
    "category,expected",
    [
        ("auth", ErrorCategory.AUTH),
        ("network", ErrorCategory.NETWORK),
        ("rate_limit", ErrorCategory.RATE_LIMIT),
        ("validation", ErrorCategory.VALIDATION),
    ],
)
def test_error_envelope_is_classified_by_field_not_message(category, expected):
    cli = fake_cli({"error": category, "message": "some unstable text"})
    with pytest.raises(KrakenCliError) as exc:
        cli.run("futures", "positions")
    assert exc.value.category is expected


def test_error_envelope_with_exit_zero_is_still_an_error():
    """kraken-cli can return an error envelope while exiting 0."""
    cli = fake_cli({"error": "validation", "message": "bad"}, code=0)
    with pytest.raises(KrakenCliError):
        cli.run("futures", "order", "buy")


def test_unknown_error_category_falls_back_to_api():
    cli = fake_cli({"error": "something_new", "message": "?"})
    with pytest.raises(KrakenCliError) as exc:
        cli.run("x")
    assert exc.value.category is ErrorCategory.API


def test_retryable_classification():
    assert ErrorCategory.NETWORK.retryable
    assert ErrorCategory.RATE_LIMIT.retryable
    assert not ErrorCategory.AUTH.retryable
    assert not ErrorCategory.VALIDATION.retryable


def test_invalid_json_is_a_parse_error():
    with pytest.raises(KrakenCliError) as exc:
        fake_cli("not json at all").run("x")
    assert exc.value.category is ErrorCategory.PARSE


def test_missing_binary_is_config_error():
    def runner(argv, timeout):
        raise FileNotFoundError()

    with pytest.raises(KrakenCliError) as exc:
        KrakenCli(binary="/nope/kraken", runner=runner).run("x")
    assert exc.value.category is ErrorCategory.CONFIG


def test_credentials_are_masked_in_error_argv():
    """A raised error must never carry a secret into a log."""
    cli = fake_cli({"error": "auth", "message": "bad key"})
    with pytest.raises(KrakenCliError) as exc:
        cli.run("futures", "accounts", "--api-key", "SUPERSECRETKEY123456")
    assert "SUPERSECRETKEY123456" not in " ".join(exc.value.argv)
    assert "***" in exc.value.argv


def test_try_run_returns_error_instead_of_raising():
    result, err = fake_cli({"error": "auth", "message": "nope"}).try_run("x")
    assert result is None
    assert err.category is ErrorCategory.AUTH


# ==========================================================================
# API key permission checks (brief 25) — moved into Stage 2
# ==========================================================================


class ScriptedCli(KrakenCli):
    """Routes each command prefix to a canned response."""

    def __init__(self, responses):
        self.responses = responses
        super().__init__(runner=self._route)

    def _route(self, argv, timeout):
        key = " ".join(a for a in argv[1:] if not a.startswith("-"))
        for prefix, payload in self.responses.items():
            if key.startswith(prefix):
                return 0, json.dumps(payload), ""
        return 0, json.dumps({"error": "api", "message": f"unscripted: {key}"}), ""


DENIED = {"error": "auth", "message": "EAPI:Invalid permissions"}
ACCOUNT_OK = {"accounts": {"flex": {"balances": {}}}}


def test_key_that_can_withdraw_is_rejected():
    """The central check: a successful withdraw-scope probe is disqualifying."""
    cli = ScriptedCli(
        {
            "futures accounts": ACCOUNT_OK,
            "withdrawal methods": {"methods": [{"method": "Bitcoin"}]},
        }
    )
    result = check_no_withdrawal_permission(cli)
    assert result.outcome is CheckOutcome.FAIL
    assert "SUCCEEDED" in result.detail
    assert result.blocks_startup


def test_restricted_key_passes():
    cli = ScriptedCli(
        {
            "futures accounts": ACCOUNT_OK,
            "withdrawal methods": DENIED,
            "withdrawal addresses": DENIED,
        }
    )
    result = check_no_withdrawal_permission(cli)
    assert result.outcome is CheckOutcome.PASS
    assert not result.blocks_startup


def test_addresses_probe_also_disqualifies():
    """Both probes must be denied — one succeeding is enough to refuse."""
    cli = ScriptedCli(
        {
            "futures accounts": ACCOUNT_OK,
            "withdrawal methods": DENIED,
            "withdrawal addresses": {"addresses": []},
        }
    )
    assert check_no_withdrawal_permission(cli).outcome is CheckOutcome.FAIL


def test_network_failure_is_inconclusive_not_a_pass():
    """A flaky network must never be read as proof of a restricted key."""
    cli = ScriptedCli(
        {
            "futures accounts": ACCOUNT_OK,
            "withdrawal methods": {"error": "network", "message": "timeout"},
        }
    )
    result = check_no_withdrawal_permission(cli)
    assert result.outcome is CheckOutcome.INCONCLUSIVE
    assert result.blocks_startup


def test_connectivity_proven_before_conclusions_are_drawn():
    """An invalid key is denied everywhere, which must not read as 'safe'."""
    cli = ScriptedCli(
        {"futures accounts": DENIED, "withdrawal methods": DENIED}
    )
    results = run_permission_checks(cli, TradingMode.LIVE, has_credentials=True)
    by_name = {r.name: r for r in results}
    assert by_name["futures_connectivity"].outcome is CheckOutcome.FAIL
    assert by_name["no_withdrawal_permission"].outcome is CheckOutcome.INCONCLUSIVE


def test_live_mode_without_credentials_is_refused():
    results = run_permission_checks(
        ScriptedCli({}), TradingMode.LIVE, has_credentials=False
    )
    assert results[0].outcome is CheckOutcome.FAIL
    with pytest.raises(SafetyCheckFailed):
        enforce(results, TradingMode.LIVE)


def test_paper_mode_without_credentials_is_fine():
    results = run_permission_checks(
        ScriptedCli({}), TradingMode.PAPER, has_credentials=False
    )
    assert results[0].outcome is CheckOutcome.SKIPPED
    enforce(results, TradingMode.PAPER)


def test_live_startup_blocked_by_withdraw_capable_key():
    cli = ScriptedCli(
        {
            "futures accounts": ACCOUNT_OK,
            "withdrawal methods": {"methods": []},
        }
    )
    results = run_permission_checks(cli, TradingMode.LIVE, has_credentials=True)
    with pytest.raises(SafetyCheckFailed, match="LIVE"):
        enforce(results, TradingMode.LIVE)


def test_live_startup_allowed_with_restricted_key():
    cli = ScriptedCli(
        {
            "futures accounts": ACCOUNT_OK,
            "withdrawal methods": DENIED,
            "withdrawal addresses": DENIED,
        }
    )
    results = run_permission_checks(cli, TradingMode.LIVE, has_credentials=True)
    enforce(results, TradingMode.LIVE)  # must not raise


def test_paper_mode_warns_but_does_not_halt_on_bad_key():
    """Paper cannot place real orders, so a bad key is loud but not fatal."""
    cli = ScriptedCli(
        {
            "futures accounts": ACCOUNT_OK,
            "withdrawal methods": {"methods": []},
        }
    )
    results = run_permission_checks(cli, TradingMode.PAPER, has_credentials=True)
    enforce(results, TradingMode.PAPER)  # must not raise
    assert any(r.blocks_startup for r in results)
