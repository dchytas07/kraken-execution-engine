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
    ) -> None:
        self.binary = binary
        self.timeout = timeout
        self.env = env
        self._runner = runner or self._subprocess_runner

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
        """Invoke the CLI and return parsed JSON, or raise KrakenCliError."""
        argv = [self.binary, *args, "-o", "json"]
        safe = _safe_argv(argv)

        try:
            code, stdout, stderr = self._runner(argv, self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise KrakenCliError(
                ErrorCategory.TIMEOUT,
                f"timed out after {self.timeout}s",
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
