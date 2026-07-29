"""API key permission verification (brief 25).

Read docs/KRAKEN_NOTES.md section 1 before changing anything here. The short
version, established from Kraken's own documentation:

1. `WithdrawMethods` / `WithdrawAddresses` require BOTH "Funds permissions -
   Query" AND "Funds permissions - Withdraw". So a *denial* does not prove the
   key lacks withdrawal — it may merely lack Query. The probe is a **positive
   detector**: success proves danger, denial proves nothing.

2. Those endpoints authenticate with the **Spot** key. All trading here happens
   on the **Futures** key, a separate credential with a separate permission
   model. A spot probe says nothing about the futures key.

3. Kraken Futures keys gate withdrawal behind a separate "Withdrawal API"
   permission, and General "Full Access" explicitly excludes withdrawals. But
   no read-only endpoint sits behind that permission, so the futures key's
   withdrawal capability **cannot be verified via the API** without attempting
   a real withdrawal, which we will never do.

Hence the split: probe what is probeable, and require an explicit recorded
operator attestation for what is not. A denial is reported as NOT_VERIFIABLE,
never as PASS, so the log never claims a guarantee we do not have.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..exchange.cli import ErrorCategory, KrakenCli, KrakenCliError
from ..logging_setup import get_logger
from ..models import TradingMode

log = get_logger(__name__)

# Operator attestation phrase for the futures key (see module docstring 3).
FUTURES_WITHDRAWAL_ATTESTATION = "WITHDRAWAL_API_SET_TO_NO_ACCESS"


class CheckOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"
    INCONCLUSIVE = "INCONCLUSIVE"
    # Distinct from INCONCLUSIVE: the check ran cleanly, but the result is
    # structurally incapable of proving the property we want.
    NOT_VERIFIABLE = "NOT_VERIFIABLE"


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    outcome: CheckOutcome
    detail: str

    @property
    def blocks_startup(self) -> bool:
        """Only definitive danger and genuine ambiguity block.

        NOT_VERIFIABLE deliberately does not block: if it did, live trading
        would be impossible forever, because the futures withdrawal permission
        has no read-only endpoint. It is surfaced loudly instead, and the
        operator attestation carries that weight.
        """
        return self.outcome in (CheckOutcome.FAIL, CheckOutcome.INCONCLUSIVE)


# Read-only endpoints behind the Spot "Withdraw Funds" permission.
_WITHDRAW_SCOPE_PROBES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("spot_withdrawal_methods", ("withdrawal", "methods")),
    ("spot_withdrawal_addresses", ("withdrawal", "addresses")),
)


def check_connectivity(cli: KrakenCli) -> CheckResult:
    """Prove the futures credentials authenticate and can read the account."""
    result, err = cli.try_run("futures", "accounts")
    if err is None:
        return CheckResult(
            "futures_connectivity", CheckOutcome.PASS, "authenticated account read ok"
        )
    if err.category is ErrorCategory.AUTH:
        return CheckResult(
            "futures_connectivity",
            CheckOutcome.FAIL,
            f"futures credentials rejected: {err.message}",
        )
    return CheckResult(
        "futures_connectivity",
        CheckOutcome.INCONCLUSIVE,
        f"could not verify connectivity ({err.category.value}): {err.message}",
    )


def check_spot_withdrawal_scope(cli: KrakenCli) -> CheckResult:
    """Positive detector for a withdraw-capable SPOT key.

    Success on either probe means the key holds both Query and Withdraw funds
    permissions — definitive, and disqualifying. A denial is ambiguous and is
    reported as NOT_VERIFIABLE (see KRAKEN_NOTES 1.2).
    """
    denials: list[str] = []
    for name, args in _WITHDRAW_SCOPE_PROBES:
        result, err = cli.try_run(*args)

        if err is None:
            return CheckResult(
                "spot_withdrawal_scope",
                CheckOutcome.FAIL,
                f"{name} SUCCEEDED — this spot API key holds both 'Query Funds' "
                "and 'Withdraw Funds'. It can move funds off the exchange. "
                "Revoke 'Withdraw Funds' and restart.",
            )

        if err.category in (
            ErrorCategory.AUTH,
            ErrorCategory.VALIDATION,
            ErrorCategory.API,
        ):
            denials.append(f"{name}: denied ({err.category.value})")
            continue

        return CheckResult(
            "spot_withdrawal_scope",
            CheckOutcome.INCONCLUSIVE,
            f"{name} failed for a non-permission reason "
            f"({err.category.value}: {err.message}); cannot draw any conclusion",
        )

    return CheckResult(
        "spot_withdrawal_scope",
        CheckOutcome.NOT_VERIFIABLE,
        "withdrawal endpoints denied (" + "; ".join(denials) + "). NOTE: these "
        "endpoints require BOTH 'Query Funds' and 'Withdraw Funds', so a denial "
        "may mean the key merely lacks 'Query Funds' while still holding "
        "'Withdraw Funds'. This is NOT proof the key cannot withdraw.",
    )


def check_futures_withdrawal_attestation(attestation: str | None) -> CheckResult:
    """The futures withdrawal permission has no read-only endpoint.

    Nothing can be probed, so we require the operator to state that they set
    the key's Withdrawal API permission to "No Access". The claim is recorded
    in the audit log so it is attributable after the fact.
    """
    if attestation == FUTURES_WITHDRAWAL_ATTESTATION:
        return CheckResult(
            "futures_withdrawal_attestation",
            CheckOutcome.PASS,
            "operator attests the futures key's Withdrawal API permission is "
            "'No Access' (attestation recorded; not machine-verifiable)",
        )
    return CheckResult(
        "futures_withdrawal_attestation",
        CheckOutcome.FAIL,
        "the futures key's withdrawal permission cannot be probed via any "
        "read-only endpoint, so it must be attested. In the Kraken Futures UI "
        "confirm the key's 'Withdrawal API' permission is set to 'No Access', "
        f"then set KRAKEN_FUTURES_WITHDRAWAL_ATTESTATION={FUTURES_WITHDRAWAL_ATTESTATION}",
    )


def run_permission_checks(
    cli: KrakenCli,
    mode: TradingMode,
    *,
    has_futures_credentials: bool,
    has_spot_credentials: bool = False,
    futures_withdrawal_attestation: str | None = None,
) -> list[CheckResult]:
    """Run the credential safety checks appropriate to the mode."""
    if not has_futures_credentials:
        if mode.is_live:
            return [
                CheckResult(
                    "credentials_present",
                    CheckOutcome.FAIL,
                    "TRADING_MODE=live but no Kraken futures credentials are set",
                )
            ]
        return [
            CheckResult(
                "credentials_present",
                CheckOutcome.SKIPPED,
                f"no futures credentials set; not required in {mode.value} mode",
            )
        ]

    results = [check_connectivity(cli)]

    # The futures key is the one that trades, so its attestation is required
    # regardless of whether a spot key exists.
    results.append(
        check_futures_withdrawal_attestation(futures_withdrawal_attestation)
    )

    # The spot key is not needed for trading at all. Probe it only if present —
    # a withdraw-capable spot key sitting in the environment is worth catching,
    # but its absence is not a problem.
    if has_spot_credentials:
        results.append(check_spot_withdrawal_scope(cli))
    else:
        results.append(
            CheckResult(
                "spot_withdrawal_scope",
                CheckOutcome.SKIPPED,
                "no spot credentials configured; spot key is not used for trading",
            )
        )

    return results


class SafetyCheckFailed(Exception):
    """One or more startup safety checks did not pass."""


def enforce(results: list[CheckResult], mode: TradingMode) -> None:
    """Apply results to startup.

    Live: any blocking result is fatal.
    Paper/dry: blocking results are logged as errors but do not halt, because
    no real order can be placed. NOT_VERIFIABLE is always surfaced at WARNING
    so it is never mistaken for a clean pass.
    """
    blocking = [r for r in results if r.blocks_startup]

    for r in results:
        match r.outcome:
            case CheckOutcome.PASS | CheckOutcome.SKIPPED:
                emit = log.info
            case CheckOutcome.NOT_VERIFIABLE:
                emit = log.warning
            case _:
                emit = log.error
        emit(
            f"safety check {r.name}: {r.outcome.value} — {r.detail}",
            extra={
                "action": "safety_check",
                "check": r.name,
                "outcome": r.outcome.value,
            },
        )

    if not blocking:
        return

    summary = "; ".join(f"{r.name}={r.outcome.value}" for r in blocking)
    if mode.is_live:
        raise SafetyCheckFailed(
            f"refusing to start in LIVE mode — failed checks: {summary}"
        )
    log.error(
        f"safety checks failed ({summary}) — continuing only because mode is "
        f"{mode.value} and no real orders can be placed. These MUST pass before "
        "live trading.",
        extra={"action": "safety_check_degraded"},
    )
