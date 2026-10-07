"""The single gateway through which every `kraken` invocation passes.

Nothing else in the codebase spawns a subprocess. Centralising it means the
JSON contract, timeout handling, error classification, retry policy and secret
redaction are implemented once and cannot be bypassed by a caller in a hurry.

Error categories are kraken-cli's own (AGENTS.md); we route on the `error`
field and never on message text, which the CLI documents as unstable.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from enum import StrEnum

from ..logging_setup import get_logger

log = get_logger(__name__)


class ErrorCategory(StrEnum):
    AUTH = "auth"
    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    VALIDATION = "validation"
    API = "api"
    CONFIG = "config"
    WEBSOCKET = "websocket"
    IO = "io"
    PARSE = "parse"
    # ours, for failures that never reached Kraken
    SUBPROCESS = "subprocess"
    TIMEOUT = "timeout"

    @property
    def retryable(self) -> bool:
        return self in _RETRYABLE


_RETRYABLE = frozenset(
    {
        ErrorCategory.NETWORK,
        ErrorCategory.RATE_LIMIT,
        ErrorCategory.WEBSOCKET,
        ErrorCategory.TIMEOUT,
    }
)


class KrakenCliError(Exception):
    """A kraken-cli invocation failed."""

    def __init__(self, category: ErrorCategory, message: str, argv: list[str]) -> None:
        super().__init__(f"[{category.value}] {message}")
        self.category = category
        self.message = message
        # Redacted form only — argv may carry credentials if a caller passed
        # them as flags rather than via the environment.
        self.argv = _safe_argv(argv)

    @property
    def retryable(self) -> bool:
        return self.category.retryable


def _safe_argv(argv: list[str]) -> list[str]:
    """Mask any value following a credential-bearing flag."""
    masked: list[str] = []
    skip_next = False
    sensitive = {"--api-key", "--api-secret", "--otp"}
    for arg in argv:
        if skip_next:
            masked.append("***")
            skip_next = False
            continue
        masked.append(arg)
        if arg in sensitive:
            skip_next = True
    return masked


# Retry policy (see KrakenCli.run).
#
# Only commands that CANNOT change anything are retried. A mutating command that
# times out may well have reached the exchange (the order may be live), so
# retrying it risks a duplicate; the codebase's rule (brief 40) is to reconcile
# against the client order id instead, and nothing here changes that.
READ_ONLY_VERBS = frozenset({
    "instruments", "ticker", "tickers", "balance", "accounts", "positions",
    "open-positions", "orders", "open-orders", "fills", "leverage", "status",
    "order-status", "history", "orderbook",
})
# Any of these anywhere in the argument list makes the command a mutation, even
# when a read verb is also present ("set-leverage" must never ride on "leverage").
MUTATING_VERBS = frozenset({
    "buy", "sell", "cancel", "cancel-all", "edit-order", "set-leverage",
    "batch-order", "init", "reset", "order", "withdraw", "transfer",
})
RETRY_CATEGORIES = frozenset({
    ErrorCategory.TIMEOUT, ErrorCategory.NETWORK, ErrorCategory.RATE_LIMIT,
})
READ_ATTEMPTS = 3
BACKOFF_SECONDS = (0.5, 1.0)


def is_read_only(args: tuple[str, ...]) -> bool:
    """True only for a command that provably changes nothing. Unknown verbs are
    treated as mutations: the safe default for a retry decision is 'no'."""
    tokens = set(args)
    return bool(tokens & READ_ONLY_VERBS) and not (tokens & MUTATING_VERBS)


@dataclass(frozen=True, slots=True)
class CliResult:
    ok: bool
    data: dict | list
    argv: list[str]


class KrakenCli:
    """Thin, testable wrapper around the `kraken` binary.

    `runner` is injectable so the entire test suite runs without a subprocess
    or a network — the adapter tests feed recorded JSON through this seam.
    """

    def __init__(
        self,
        binary: str = "kraken",
        timeout: int = 30,
        env: dict[str, str] | None = None,
        runner=None,
        sleep=time.sleep,
    ) -> None:
        self.binary = binary
        self.timeout = timeout
        self.env = env
        self._runner = runner or self._subprocess_runner
        self._sleep = sleep  # injected so tests never actually wait
        self.retries_used = 0

    def _subprocess_runner(self, argv: list[str], timeout: int) -> tuple[int, str, str]:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self.env,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def run(self, *args: str) -> CliResult:
        """Invoke the CLI and return parsed JSON, or raise KrakenCliError.

        READ-ONLY commands get READ_ATTEMPTS tries inside the SAME total budget
        (`timeout`, default 30s), so a stuck DNS lookup costs one short attempt
        instead of the whole budget, and a second process usually succeeds. After
        the last attempt the original error is raised unchanged, so every caller
        still fails CLOSED exactly as before: a read that never succeeds is
        never turned into "nothing there".

        Mutating commands are NEVER retried: one attempt, the full timeout.
        """
        if not is_read_only(args):
            return self._run_once(args, self.timeout)

        per_attempt = max(1, self.timeout // READ_ATTEMPTS)
        for attempt in range(1, READ_ATTEMPTS + 1):
            try:
                return self._run_once(args, per_attempt)
            except KrakenCliError as exc:
                if exc.category not in RETRY_CATEGORIES or attempt == READ_ATTEMPTS:
                    raise
                self.retries_used += 1
                delay = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
                log.warning(
                    f"kraken read failed ({exc.category.value}); retry "
                    f"{attempt}/{READ_ATTEMPTS - 1} in {delay}s",
                    extra={"action": "cli_retry", "argv": exc.argv[:4]},
                )
                self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _run_once(self, args: tuple[str, ...], timeout: int) -> CliResult:
        argv = [self.binary, *args, "-o", "json"]
        safe = _safe_argv(argv)

        try:
            code, stdout, stderr = self._runner(argv, timeout)
        except subprocess.TimeoutExpired as exc:
            raise KrakenCliError(
                ErrorCategory.TIMEOUT,
                f"timed out after {timeout}s",
                argv,
            ) from exc
        except FileNotFoundError as exc:
            raise KrakenCliError(
                ErrorCategory.CONFIG,
                f"kraken binary not found at {self.binary!r}",
                argv,
            ) from exc

        if not stdout.strip():
            if code != 0:
                raise KrakenCliError(
                    ErrorCategory.SUBPROCESS,
                    f"exit {code} with no stdout: {stderr.strip()[:200]}",
                    argv,
                )
            return CliResult(ok=True, data={}, argv=safe)

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise KrakenCliError(
                ErrorCategory.PARSE,
                f"stdout was not valid JSON: {stdout[:200]!r}",
                argv,
            ) from exc

        # kraken-cli signals failure with an error envelope, sometimes while
        # still exiting 0, so check the payload rather than only the exit code.
        if isinstance(data, dict) and "error" in data:
            raw = str(data.get("error", ""))
            try:
                category = ErrorCategory(raw)
            except ValueError:
                category = ErrorCategory.API
            raise KrakenCliError(
                category, str(data.get("message", raw)), argv
            )

        if code != 0:
            raise KrakenCliError(
                ErrorCategory.SUBPROCESS, f"exit {code}", argv
            )

        log.debug("kraken cli ok", extra={"action": "cli_call", "argv": safe})
        return CliResult(ok=True, data=data, argv=safe)

    def try_run(self, *args: str) -> tuple[CliResult | None, KrakenCliError | None]:
        """Non-raising variant, for probes where a failure is a valid answer."""
        try:
            return self.run(*args), None
        except KrakenCliError as exc:
            return None, exc
