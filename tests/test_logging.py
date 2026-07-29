"""Secret redaction — brief 32 forbids credentials ever reaching a log."""

import json
import logging

import pytest

from bot.logging_setup import REDACTED, SecretRedactor, setup_logging


@pytest.fixture
def redactor():
    return SecretRedactor({"SUPERSECRETVALUE12345", "another-secret-here"})


def test_known_secret_is_masked(redactor):
    assert "SUPERSECRETVALUE12345" not in redactor.scrub(
        "connecting with key SUPERSECRETVALUE12345 ok"
    )
    assert REDACTED in redactor.scrub("key SUPERSECRETVALUE12345")


def test_secret_masked_wherever_it_appears(redactor):
    text = redactor.scrub("a SUPERSECRETVALUE12345 b SUPERSECRETVALUE12345 c")
    assert "SUPERSECRETVALUE12345" not in text


def test_credential_shaped_assignments_masked_even_if_unknown(redactor):
    """We cannot enumerate every secret, so shape-based masking backs it up."""
    for text in (
        "api_secret=abcdef123456",
        "API_KEY: mykey123456",
        "password=hunter2hunter2",
        '"api_hash": "0123456789abcdef"',
    ):
        assert REDACTED in redactor.scrub(text), text


def test_long_base64_blob_masked(redactor):
    blob = "A" * 50 + "=="
    assert blob not in redactor.scrub(f"secret is {blob}")


def test_ordinary_text_survives(redactor):
    text = "placed entry order for PF_XBTUSD size 0.0046 at 64598"
    assert redactor.scrub(text) == text


def test_nested_structures_are_scrubbed(redactor):
    out = redactor._scrub_value(
        {"outer": {"key": "SUPERSECRETVALUE12345"}, "list": ["SUPERSECRETVALUE12345"]}
    )
    assert out["outer"]["key"] == REDACTED
    assert out["list"][0] == REDACTED


def test_secrets_can_be_registered_later(redactor):
    redactor.add_secret("discovered-secret-value")
    assert REDACTED in redactor.scrub("token discovered-secret-value")


def test_short_values_are_not_masked():
    """Avoid mangling logs by masking common short strings."""
    r = SecretRedactor({"abc"})
    assert r.scrub("abc def") == "abc def"


def test_redaction_applies_through_the_logging_pipeline(tmp_path):
    """End-to-end: a secret passed to log.info never lands in the file."""
    log_path = tmp_path / "test.jsonl"
    setup_logging(
        log_path=log_path, secrets={"SUPERSECRETVALUE12345"}, console=False
    )
    logging.getLogger("t").info(
        "auth with SUPERSECRETVALUE12345", extra={"trade_id": "T-1"}
    )
    logging.shutdown()

    contents = log_path.read_text()
    assert "SUPERSECRETVALUE12345" not in contents
    assert REDACTED in contents


def test_log_lines_are_json_with_context_fields(tmp_path):
    log_path = tmp_path / "test.jsonl"
    setup_logging(log_path=log_path, console=False)
    logging.getLogger("t").info(
        "entry submitted",
        extra={"trade_id": "T-1", "asset": "BTC", "symbol": "PF_XBTUSD",
               "action": "submit_entry"},
    )
    logging.shutdown()

    record = json.loads(log_path.read_text().strip().splitlines()[0])
    assert record["msg"] == "entry submitted"
    assert record["trade_id"] == "T-1"
    assert record["asset"] == "BTC"
    assert record["action"] == "submit_entry"
    assert record["level"] == "INFO"
    assert "ts" in record


def test_extra_fields_are_scrubbed_too(tmp_path):
    log_path = tmp_path / "test.jsonl"
    setup_logging(log_path=log_path, secrets={"SUPERSECRETVALUE12345"}, console=False)
    logging.getLogger("t").info("call", extra={"argv": ["--api-key", "SUPERSECRETVALUE12345"]})
    logging.shutdown()
    assert "SUPERSECRETVALUE12345" not in log_path.read_text()


def test_exception_tracebacks_are_scrubbed(tmp_path):
    log_path = tmp_path / "test.jsonl"
    setup_logging(log_path=log_path, secrets={"SUPERSECRETVALUE12345"}, console=False)
    try:
        raise ValueError("failed with SUPERSECRETVALUE12345")
    except ValueError:
        logging.getLogger("t").exception("boom")
    logging.shutdown()
    assert "SUPERSECRETVALUE12345" not in log_path.read_text()
