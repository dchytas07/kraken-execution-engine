"""Wiring guard: every safety mechanism must have a production call site.

This test exists because the same bug appeared four times: a safety-critical
function was implemented, unit-tested, and never called from the trading path.
It looked correct in review and passed its own tests, because its tests called
it directly.

Found this way, all four:
  * check_entry_freshness — brief 12 stale guard, never invoked by open_trade
  * resize_stop_to_position — brief 14, called only from the partial-close path
  * effective_leverage — brief 9 TEN-X CAP, zero call sites; a 50x signal
    would have been set on the account as 50x
  * the whole admission layer — spot boundary, whitelist, dedupe, symbol
    occupancy, decision recording: five mechanisms with no caller

A passing unit test proves a guard computes correctly. It says nothing about
whether the dangerous path invokes it. This test asserts the second thing.
"""

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent / "src" / "bot"

# name -> the brief requirement it enforces
SAFETY_FUNCTIONS = {
    "effective_leverage": "brief 9 — 10x leverage cap",
    "check_sufficient_margin": "brief 24 — margin available before opening",
    "is_entry_stale": "brief 12 — stale signal detection",
    "check_entry_freshness": "brief 12 — stale guard on the execution path",
    "validate_size": "brief 10 — exchange-valid order size",
    "split_take_profits": "brief 13 — TP total never exceeds position",
    "allows_new_positions": "brief 25 — kill switch",
    "fingerprint_for": "brief 23 — duplicate signal detection",
    "find_by_fingerprint": "brief 23 — duplicate lookup",
    "has_message": "brief 5 — message idempotency",
    "get_active_trade_for_symbol": "one active trade per symbol",
    "resize_stop_to_position": "brief 14 — stop tracks actual position",
    "_verify_stop": "brief 16 — stop replacement verification",
    "_enter_safe_error": "brief 32 — dangerous states are loud",
    "await_flat": "brief 27 — close confirmed before CLOSED",
    "record_decision": "brief 7 — every message gets a decision",
    "resolve_asset": "brief 4 — whitelist resolution",
    "resolve_specs": "brief 25 — instrument validation at startup",
    "run_permission_checks": "brief 25 — API key permissions",
    "set_leverage_preference": "Study B — leverage set-and-verify",
    "admit": "admission gate — spot boundary, dedupe, occupancy",
}

# Properties are accessed without parentheses, so they need their own check.
SAFETY_PROPERTIES = {
    "is_executable_candidate": "brief 8 — spot signals never reach execution",
    "is_terminal": "trade lifecycle terminal detection",
}

# Mechanisms with no caller YET, each owned by a named future stage. Anything
# not on this list and not called is a failure. Entries must be removed as
# their stage lands — leaving one here after its stage ships is itself a bug.
PENDING_WIRING = {
    "expire_stale_entries": "Stage 7 — needs the periodic reconciliation loop",
    "cancel_remaining_entries": "Stage 7 — reconciliation cleanup; _finalise_close "
                                "already covers the close path (proven by "
                                "test_close_cancels_entry_orders_satisfying_brief_11)",
    "handle_message": "Stage 9 — needs the Telegram listener",
}


def _production_call_sites(name: str, *, is_property: bool = False) -> list[str]:
    """Call sites in src/, excluding the definition itself."""
    pattern = (
        rf"(?:\.|\b){re.escape(name)}\b(?!\s*\()" if is_property
        else rf"(?:\.|\b){re.escape(name)}\s*\("
    )
    hits = []
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        lines = text.splitlines()
        for match in re.finditer(pattern, text):
            lineno = text[: match.start()].count("\n") + 1
            line = lines[lineno - 1].strip()
            if line.startswith(("def ", "async def ", "@")):
                continue
            if is_property and (line.startswith(("def ", "async def "))):
                continue
            hits.append(f"{path.relative_to(SRC)}:{lineno}")
    return hits


@pytest.mark.parametrize(
    "name,requirement", sorted(SAFETY_FUNCTIONS.items())
)
def test_safety_function_has_a_production_call_site(name, requirement):
    """A guard nothing calls is not a guard."""
    if name in PENDING_WIRING:
        pytest.skip(f"pending: {PENDING_WIRING[name]}")
    sites = _production_call_sites(name)
    assert sites, (
        f"{name}() enforces [{requirement}] but has ZERO call sites in src/. "
        "It is implemented and probably unit-tested, and the trading path never "
        "invokes it — the exact bug class this test exists to catch."
    )


@pytest.mark.parametrize(
    "name,requirement", sorted(SAFETY_PROPERTIES.items())
)
def test_safety_property_is_read_somewhere_in_production(name, requirement):
    sites = _production_call_sites(name, is_property=True)
    assert sites, f"{name} enforces [{requirement}] but is never read in src/"


def test_pending_wiring_entries_are_real_functions():
    """Stops PENDING_WIRING becoming a dumping ground for typos."""
    defined = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
    for name in PENDING_WIRING:
        assert name in defined, f"PENDING_WIRING names {name}, which does not exist"


def test_pending_wiring_entries_are_still_actually_unwired():
    """When a pending mechanism gets wired, it must leave this list.

    Otherwise the exemption silently outlives its reason and would mask a
    future regression of the same function.
    """
    for name, owner in PENDING_WIRING.items():
        sites = _production_call_sites(name)
        assert not sites, (
            f"{name} is now called from {sites} — remove it from PENDING_WIRING "
            f"so it is guarded like everything else (was: {owner})"
        )


def test_every_safety_function_actually_exists():
    defined = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
    missing = [n for n in SAFETY_FUNCTIONS if n not in defined]
    assert not missing, f"registered but undefined (renamed or deleted?): {missing}"
