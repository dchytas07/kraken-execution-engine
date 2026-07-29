"""API key permission verification (brief 25).

The rule: the trading key must not be able to withdraw funds, and if that
cannot be positively established, the engine does not start.

How we establish it, given we cannot introspect a Kraken key's permission set
directly: probe a **read-only** endpoint that Kraken gates behind the "Withdraw
funds" permission, and require it to be *denied*.

  * `kraken withdrawal methods` -> requires "Withdraw funds"
  * `kraken withdrawal addresses` -> requires "Withdraw funds"

Neither moves money; both merely list configuration. If either SUCCEEDS the key
can withdraw, and we refuse to start.

Ordering matters. A key that is simply invalid also fails these probes, which
would look identical to "correctly restricted". So connectivity is proven
first with an authenticated read that the trading key must have anyway. Only
against a key we know works does a denial actually mean "permission absent".

    1. prove the key authenticates          (futures accounts)
    2. prove it CANNOT reach withdraw scope (withdrawal methods/addresses)

An inconclusive result is treated as failure, per brief 40's fail-closed rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..exchange.cli import ErrorCategory, KrakenCli, KrakenCliError
from ..logging_setup import get_logger
from ..models import TradingMode

log = get_logger(__name__)


class CheckOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    outcome: CheckOutcome
    detail: str

    @property
    def blocks_startup(self) -> bool:
        """Anything not positively PASS or deliberately SKIPPED blocks."""
        return self.outcome in (CheckOutcome.FAIL, CheckOutcome.INCONCLUSIVE)


# Probes that require the "Withdraw funds" permission. Both are read-only.
_WITHDRAW_SCOPE_PROBES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("spot_withdrawal_methods", ("withdrawal", "methods")),
    ("spot_withdrawal_addresses", ("withdrawal", "addresses")),
)


def check_connectivity(cli: KrakenCli) -> CheckResult:
    """Prove the futures credentials authenticate before drawing any
    conclusions from a denial elsewhere."""
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


def check_no_withdrawal_permission(cli: KrakenCli) -> CheckResult:
    """Require every withdraw-scoped probe to be denied.

    A single probe succeeding is disqualifying. A probe failing for a reason
    unrelated to permissions (network, rate limit) is inconclusive, not a pass —
    we must not read a flaky network as proof of a safe key.
    """
    denials: list[str] = []
    for name, args in _WITHDRAW_SCOPE_PROBES:
        result, err = cli.try_run(*args)

        if err is None:
            return CheckResult(
                "no_withdrawal_permission",
                CheckOutcome.FAIL,
                f"{name} SUCCEEDED — this API key can access withdrawal scope. "
                "Revoke 'Withdraw funds' on the key and restart. Refusing to run "
                "with a key that can move funds off the exchange.",
            )

        if err.category in (ErrorCategory.AUTH, ErrorCategory.VALIDATION, ErrorCategory.API):
            # Denied — the outcome we want.
            denials.append(f"{name}: denied ({err.category.value})")
            continue

        return CheckResult(
            "no_withdrawal_permission",
            CheckOutcome.INCONCLUSIVE,
            f"{name} failed for a non-permission reason "
            f"({err.category.value}: {err.message}); cannot confirm the key is "
            "restricted, so refusing to start",
        )

    return CheckResult(
        "no_withdrawal_permission",
        CheckOutcome.PASS,
        "withdrawal scope denied on all probes — " + "; ".join(denials),
    )


def run_permission_checks(
    cli: KrakenCli,
    mode: TradingMode,
    *,
    has_credentials: bool,
) -> list[CheckResult]:
    """Run the credential safety checks appropriate to the mode.

    DRY_RUN and PAPER do not send real orders, so missing credentials are not
    an error there. But if credentials ARE present we still probe them and warn
    loudly — a withdraw-capable key sitting in the environment is worth knowing
    about before it is ever pointed at live.
    """
    if not has_credentials:
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
                f"no credentials set; not required in {mode.value} mode",
            )
        ]

    results = [check_connectivity(cli)]
    if results[0].outcome is CheckOutcome.PASS:
        results.append(check_no_withdrawal_permission(cli))
    else:
        results.append(
            CheckResult(
                "no_withdrawal_permission",
                CheckOutcome.INCONCLUSIVE,
                "skipped because connectivity could not be proven; a denial from "
                "an unauthenticated key would not demonstrate a restricted key",
            )
        )
    return results


class SafetyCheckFailed(Exception):
    """One or more startup safety checks did not pass."""


def enforce(results: list[CheckResult], mode: TradingMode) -> None:
    """Apply results to startup.

    Live: any blocking result is fatal.
    Paper/dry: blocking results are logged as errors but do not halt, because
    no real order can be placed in those modes. The distinction is deliberate
    and documented so it cannot be mistaken for the check being optional.
    """
    blocking = [r for r in results if r.blocks_startup]

    for r in results:
        level = log.info if r.outcome is CheckOutcome.PASS else log.error
        if r.outcome is CheckOutcome.SKIPPED:
            level = log.info
        level(
            f"safety check {r.name}: {r.outcome.value} — {r.detail}",
            extra={"action": "safety_check", "check": r.name, "outcome": r.outcome.value},
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
