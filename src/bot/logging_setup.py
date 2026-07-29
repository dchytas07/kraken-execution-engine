"""Structured JSON logging with secret redaction.

Brief 32: logs matter more than any UI, and secrets must never appear in them.
Redaction happens in a filter attached to the root logger, so it applies to
every record regardless of which module emitted it — including tracebacks and
third-party libraries that might echo a URL containing a key.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REDACTED = "***REDACTED***"

# Shapes that look like credentials even if we were never told their value.
# Deliberately conservative: these run on already-formatted text, so a false
# positive costs readability, while a false negative leaks a key.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # key=value / key: value for anything named like a secret
    re.compile(
        r"(?i)\b(api[-_]?key|api[-_]?secret|secret|token|password|passwd|"
        r"api[-_]?hash|session[-_]?string|private[-_]?key)\b"
        r"(\s*[=:]\s*|\"\s*:\s*\"?)([^\s,;\"'}\]]{6,})"
    ),
    # Kraken secrets are long base64 blobs
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
)

# Structured fields every trade-related log line should carry when available.
_CONTEXT_FIELDS = (
    "trade_id",
    "message_id",
    "channel_id",
    "asset",
    "symbol",
    "order_id",
    "client_order_id",
    "exchange_order_id",
    "action",
    "event",
    "purpose",
    "state",
    "decision",
    "reason",
)

_RESERVED = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    }
)


class SecretRedactor(logging.Filter):
    """Scrubs known secret values and credential-shaped strings from records."""

    def __init__(self, secrets: set[str] | None = None) -> None:
        super().__init__()
        # Longest first, so a secret containing another is masked completely.
        self._secrets = sorted(
            (s for s in (secrets or set()) if len(s) >= 8), key=len, reverse=True
        )

    def add_secret(self, value: str) -> None:
        if value and len(value) >= 8 and value not in self._secrets:
            self._secrets.append(value)
            self._secrets.sort(key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        for pattern in _PATTERNS:
            if pattern.groups >= 3:
                text = pattern.sub(rf"\1\2{REDACTED}", text)
            else:
                text = pattern.sub(REDACTED, text)
        return text

    def _scrub_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, dict):
            return {k: self._scrub_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self._scrub_value(v) for v in value)
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._scrub_value(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = self._scrub_value(record.args)
            else:
                record.args = tuple(self._scrub_value(a) for a in record.args)
        for key, value in list(record.__dict__.items()):
            if key not in _RESERVED and isinstance(value, (str, dict, list, tuple)):
                record.__dict__[key] = self._scrub_value(value)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line — greppable and machine-parseable.

    Takes the redactor and scrubs the *final* serialized string. A filter alone
    is not sufficient: `record.exc_info` holds live exception objects that only
    become text here, so a secret inside a traceback would otherwise bypass
    record-level scrubbing entirely.
    """

    def __init__(self, redactor: "SecretRedactor | None" = None) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        line = self._format_unscrubbed(record)
        return self._redactor.scrub(line) if self._redactor else line

    def _format_unscrubbed(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for f in _CONTEXT_FIELDS:
            value = getattr(record, f, None)
            if value is not None:
                payload[f] = value
        # Any other caller-supplied extras
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    """Readable console mirror of the JSON stream."""

    def __init__(self, redactor: "SecretRedactor | None" = None) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        line = self._format_unscrubbed(record)
        return self._redactor.scrub(line) if self._redactor else line

    def _format_unscrubbed(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, UTC).strftime("%H:%M:%S")
        bits = []
        for f in ("trade_id", "asset", "symbol", "message_id"):
            value = getattr(record, f, None)
            if value is not None:
                bits.append(f"{f}={value}")
        ctx = f" [{' '.join(bits)}]" if bits else ""
        line = f"{ts} {record.levelname:<7} {record.getMessage()}{ctx}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging(
    *,
    log_path: Path,
    level: str = "INFO",
    secrets: set[str] | None = None,
    console: bool = True,
) -> SecretRedactor:
    """Configure root logging. Returns the redactor so later-discovered secrets
    can be registered with it."""
    log_path.parent.mkdir(parents=True, exist_ok=True)

    redactor = SecretRedactor(secrets)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    # Redaction is applied twice on purpose: the filter scrubs structured
    # record fields, the formatter scrubs the final rendered line including
    # tracebacks. Either alone leaves a gap.
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter(redactor))
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(HumanFormatter(redactor))
        stream.addFilter(redactor)
        root.addHandler(stream)

    return redactor


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
