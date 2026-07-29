"""Kill switch, CLI gateway error handling, and API-key permission checks."""

import json

import pytest

from bot.exchange.cli import ErrorCategory, KrakenCli, KrakenCliError
from bot.killswitch import KillSwitch
from bot.models import TradingMode
from bot.safety.permissions import (
    FUTURES_WITHDRAWAL_ATTESTATION,
    CheckOutcome,
    SafetyCheckFailed,
    check_connectivity,
    check_futures_withdrawal_attestation,
    check_spot_withdrawal_scope,
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
ATTESTED = FUTURES_WITHDRAWAL_ATTESTATION


def live_checks(cli, *, attestation=ATTESTED, spot=True):
    return run_permission_checks(
        cli,
        TradingMode.LIVE,
        has_futures_credentials=True,
        has_spot_credentials=spot,
        futures_withdrawal_attestation=attestation,
    )


# -- spot probe: positive detector only (KRAKEN_NOTES 1.2) -----------------


def test_withdraw_capable_spot_key_is_detected():
    """Success proves BOTH permissions are held — definitive and disqualifying."""
    cli = ScriptedCli(
        {
            "futures accounts": ACCOUNT_OK,
            "withdrawal methods": {"methods": [{"method": "Bitcoin"}]},
        }
    )
    result = check_spot_withdrawal_scope(cli)
    assert result.outcome is CheckOutcome.FAIL
    assert result.blocks_startup


def test_addresses_probe_also_disqualifies():
    """Either probe succeeding is enough to refuse."""
    cli = ScriptedCli(
        {
            "futures accounts": ACCOUNT_OK,
            "withdrawal methods": DENIED,
            "withdrawal addresses": {"addresses": []},
        }
    )
    assert check_spot_withdrawal_scope(cli).outcome is CheckOutcome.FAIL


def test_denial_is_not_verifiable_never_a_pass():
    """The correction from KRAKEN_NOTES 1.2.

    These endpoints require BOTH 'Query Funds' and 'Withdraw Funds', so a
    denial may mean the key merely lacks Query while still holding Withdraw.
    Reporting that as PASS would claim a guarantee we do not have.
    """
    cli = ScriptedCli(
        {
            "futures accounts": ACCOUNT_OK,
            "withdrawal methods": DENIED,
            "withdrawal addresses": DENIED,
        }
    )
    result = check_spot_withdrawal_scope(cli)
    assert result.outcome is CheckOutcome.NOT_VERIFIABLE
    assert result.outcome is not CheckOutcome.PASS
    assert "NOT proof" in result.detail


def test_network_failure_is_inconclusive_and_blocks():
    """A flaky network must never be read as any kind of conclusion."""
    cli = ScriptedCli(
        {
            "futures accounts": ACCOUNT_OK,
            "withdrawal methods": {"error": "network", "message": "timeout"},
        }
    )
    result = check_spot_withdrawal_scope(cli)
    assert result.outcome is CheckOutcome.INCONCLUSIVE
    assert result.blocks_startup


# -- futures attestation (KRAKEN_NOTES 1.4) --------------------------------


def test_futures_attestation_required():
    """No read-only endpoint exists behind the futures withdrawal permission."""
    result = check_futures_withdrawal_attestation(None)
    assert result.outcome is CheckOutcome.FAIL
    assert result.blocks_startup
    assert "No Access" in result.detail


def test_futures_attestation_rejects_wrong_phrase():
    assert (
        check_futures_withdrawal_attestation("yes I did it").outcome
        is CheckOutcome.FAIL
    )


def test_futures_attestation_accepted_with_exact_phrase():
    result = check_futures_withdrawal_attestation(ATTESTED)
    assert result.outcome is CheckOutcome.PASS
    assert "not machine-verifiable" in result.detail


def test_live_blocked_without_futures_attestation():
    """The futures key is the one that trades, so its attestation is required
    even when the spot probe looks clean."""
    cli = ScriptedCli(
        {
            "futures accounts": ACCOUNT_OK,
            "withdrawal methods": DENIED,
            "withdrawal addresses": DENIED,
        }
    )
    with pytest.raises(SafetyCheckFailed, match="LIVE"):
        enforce(live_checks(cli, attestation=None), TradingMode.LIVE)


def test_attestation_required_even_with_no_spot_key():
    results = live_checks(ScriptedCli({"futures accounts": ACCOUNT_OK}), spot=False)
    by_name = {r.name: r for r in results}
    assert by_name["spot_withdrawal_scope"].outcome is CheckOutcome.SKIPPED
    assert by_name["futures_withdrawal_attestation"].outcome is CheckOutcome.PASS


# -- separation of the two credentials (KRAKEN_NOTES 1.3) ------------------


def test_spot_probe_is_skipped_when_no_spot_key_configured():
    """The spot key is not used for trading; its absence is not a problem."""
    results = live_checks(ScriptedCli({"futures accounts": ACCOUNT_OK}), spot=False)
    spot = next(r for r in results if r.name == "spot_withdrawal_scope")
    assert spot.outcome is CheckOutcome.SKIPPED
    assert not spot.blocks_startup


def test_withdraw_capable_spot_key_blocks_even_when_futures_is_attested():
    cli = ScriptedCli(
        {
            "futures accounts": ACCOUNT_OK,
            "withdrawal methods": {"methods": []},
        }
    )
    with pytest.raises(SafetyCheckFailed, match="LIVE"):
        enforce(live_checks(cli), TradingMode.LIVE)


def test_connectivity_failure_is_reported():
    """An invalid futures key must surface as a connectivity failure."""
    results = live_checks(ScriptedCli({"futures accounts": DENIED}))
    by_name = {r.name: r for r in results}
    assert by_name["futures_connectivity"].outcome is CheckOutcome.FAIL


# -- mode gating -----------------------------------------------------------


def test_live_mode_without_credentials_is_refused():
    results = run_permission_checks(
        ScriptedCli({}), TradingMode.LIVE, has_futures_credentials=False
    )
    assert results[0].outcome is CheckOutcome.FAIL
    with pytest.raises(SafetyCheckFailed):
        enforce(results, TradingMode.LIVE)


def test_paper_mode_without_credentials_is_fine():
    results = run_permission_checks(
        ScriptedCli({}), TradingMode.PAPER, has_futures_credentials=False
    )
    assert results[0].outcome is CheckOutcome.SKIPPED
    enforce(results, TradingMode.PAPER)


def test_live_startup_allowed_when_everything_checks_out():
    cli = ScriptedCli(
        {
            "futures accounts": ACCOUNT_OK,
            "withdrawal methods": DENIED,
            "withdrawal addresses": DENIED,
        }
    )
    enforce(live_checks(cli), TradingMode.LIVE)  # must not raise


def test_not_verifiable_alone_does_not_block_live():
    """If NOT_VERIFIABLE blocked, live trading would be impossible forever —
    the futures withdrawal permission has no probeable endpoint."""
    cli = ScriptedCli(
        {
            "futures accounts": ACCOUNT_OK,
            "withdrawal methods": DENIED,
            "withdrawal addresses": DENIED,
        }
    )
    results = live_checks(cli)
    spot = next(r for r in results if r.name == "spot_withdrawal_scope")
    assert spot.outcome is CheckOutcome.NOT_VERIFIABLE
    assert not spot.blocks_startup


def test_paper_mode_warns_but_does_not_halt_on_bad_key():
    """Paper cannot place real orders, so a bad key is loud but not fatal."""
    cli = ScriptedCli(
        {
            "futures accounts": ACCOUNT_OK,
            "withdrawal methods": {"methods": []},
        }
    )
    results = run_permission_checks(
        cli,
        TradingMode.PAPER,
        has_futures_credentials=True,
        has_spot_credentials=True,
        futures_withdrawal_attestation=ATTESTED,
    )
    enforce(results, TradingMode.PAPER)  # must not raise
    assert any(r.blocks_startup for r in results)
