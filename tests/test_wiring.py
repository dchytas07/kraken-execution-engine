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
    "expire_stale_entries": "brief 12 — 24h entry lifetime",
    "run_once": "brief 28 — reconciliation pass",
    "require_recovered": "brief 29 — no orders before restart recovery",
    "mark_recovery_complete": "brief 29 — the only thing that opens the gate",
    "recover": "brief 29 — restart recovery",
    "handle_message": "the message -> trade pipeline entry point",
    "dispatch": "brief 5 — Telegram message dispatch",
    "backfill": "brief 5 — replay messages missed while disconnected",
    "last_message_id": "brief 5 — backfill high-water mark",
    # Stage 7 reconciler repairs. Registered individually, not just via
    # run_once: a refactor could drop one of these calls while run_once still
    # had a call site, and the guard would pass while a repair silently
    # disappeared.
    "_repair_unprotected": "brief 14/16 — restore protection on an open position",
    "_repair_position_gone": "brief 27 — close a trade the exchange says is flat",
    "_check_orphan_positions": "orphans are reported, never auto-closed",
    "_check_leverage_drift": "Study B — leverage drift detection",
    "_sync_order_states": "brief 28 — catch fills the engine missed",
    # Stage 8
    "_resolve_trade_intents": "brief 30 — resolve the crash window",
    # Admission
    "check_exchange_position": "block opening into an unrecognised position",
    # Found by test_registry_has_not_fallen_behind_the_codebase: this is what
    # actually refuses startup in live mode, and it was never registered.
    "enforce": "brief 25 — refuse startup on a failed safety check",
    "_refuse": "brief 7 — every refusal carries an enumerated reason",
}

# Properties are accessed without parentheses, so they need their own check.
SAFETY_PROPERTIES = {
    "is_executable_candidate": "brief 8 — spot signals never reach execution",
    "is_terminal": "trade lifecycle terminal detection",
}

# Mechanisms with no caller YET, each owned by a named future stage. Anything
# not on this list and not called is a failure. Entries must be removed as
# their stage lands — leaving one here after its stage ships is itself a bug.
PENDING_WIRING: dict[str, str] = {
    # Empty: every registered safety mechanism now has a production call site.
    # Add an entry only with a NAMED stage, and remove it when that stage lands
    # (test_pending_wiring_entries_are_still_actually_unwired enforces that).
}

# Wired in Stage 7 and removed from PENDING_WIRING:
#   expire_stale_entries     -> Reconciler._reconcile_trade
#   cancel_remaining_entries -> retained as a public helper; the close path is
#                               covered by _finalise_close (proven by
#                               test_close_cancels_entry_orders_satisfying_brief_11)
#                               and reconciliation cancels leftovers directly.


class _CallGraph:
    """AST index of every call and attribute access in src/.

    Deliberately NOT regex. Regex produced two false negatives in this codebase
    already — it missed dotted calls (`self.foo()`) and property reads, which
    are exactly the shapes safety mechanisms are invoked through. A backstop
    that shares the blind spots of the tool it replaces is not a backstop.

    Collected per node type:
      * `calls`      — ast.Call whose func resolves to a name (bare or dotted)
      * `attributes` — ast.Attribute NOT in call position, i.e. property reads
    """

    def __init__(self, root: Path) -> None:
        self.calls: dict[str, list[str]] = {}
        self.attributes: dict[str, list[str]] = {}
        self.defined: set[str] = set()
        for path in sorted(root.rglob("*.py")):
            self._index(path, root)

    @staticmethod
    def _callee(node: ast.Call) -> str | None:
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None

    def _index(self, path: Path, root: Path) -> None:
        tree = ast.parse(path.read_text())
        rel = str(path.relative_to(root))

        # Map each node to the function it sits in, so a recursive self-call
        # is not mistaken for an external call site.
        enclosing: dict[int, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.defined.add(node.name)
                for child in ast.walk(node):
                    enclosing.setdefault(id(child), node.name)

        # Attributes appearing as the func of a Call are calls, not reads.
        call_funcs = {
            id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = self._callee(node)
                if name and enclosing.get(id(node)) != name:
                    self.calls.setdefault(name, []).append(f"{rel}:{node.lineno}")
            elif isinstance(node, ast.Attribute) and id(node) not in call_funcs:
                self.attributes.setdefault(node.attr, []).append(
                    f"{rel}:{node.lineno}"
                )


GRAPH = _CallGraph(SRC)


def _production_call_sites(name: str, *, is_property: bool = False) -> list[str]:
    if is_property:
        return GRAPH.attributes.get(name, [])
    return GRAPH.calls.get(name, [])


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
    for name in PENDING_WIRING:
        assert name in GRAPH.defined, (
            f"PENDING_WIRING names {name}, which does not exist"
        )


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
    missing = [n for n in SAFETY_FUNCTIONS if n not in GRAPH.defined]
    assert not missing, f"registered but undefined (renamed or deleted?): {missing}"


# ==========================================================================
# Meta-tests: the detector must handle the shapes regex got wrong
# ==========================================================================


def _graph_from(source: str, tmp_path):
    (tmp_path / "m.py").write_text(source)
    return _CallGraph(tmp_path)


def test_detector_finds_dotted_method_calls(tmp_path):
    """Regex false negative #1: `self.foo()` was missed by a `\\bfoo\\(` pattern."""
    g = _graph_from("class A:\n    def go(self):\n        self.guarded()\n", tmp_path)
    assert g.calls.get("guarded"), "dotted call not detected"


def test_detector_finds_deeply_chained_calls(tmp_path):
    g = _graph_from("def f(x):\n    return x.a.b.guarded()\n", tmp_path)
    assert g.calls.get("guarded")


def test_detector_finds_property_reads(tmp_path):
    """Regex false negative #2: properties have no parentheses."""
    g = _graph_from("def f(m):\n    return m.kind.is_executable_candidate\n", tmp_path)
    assert g.attributes.get("is_executable_candidate")


def test_detector_does_not_count_a_property_used_as_a_call(tmp_path):
    g = _graph_from("def f(x):\n    return x.thing()\n", tmp_path)
    assert not g.attributes.get("thing")
    assert g.calls.get("thing")


def test_detector_ignores_the_definition_itself(tmp_path):
    g = _graph_from("def guarded():\n    return 1\n", tmp_path)
    assert not g.calls.get("guarded"), "a definition is not a call site"


def test_detector_ignores_recursive_self_calls(tmp_path):
    """A function calling itself is not evidence anything else invokes it."""
    g = _graph_from("def guarded(n):\n    return guarded(n - 1)\n", tmp_path)
    assert not g.calls.get("guarded")


def test_detector_ignores_names_inside_strings_and_comments(tmp_path):
    """The failure mode of text matching: prose is not a call site."""
    g = _graph_from(
        'def f():\n    """we should call guarded() here"""\n'
        '    x = "guarded()"  # guarded()\n    return x\n',
        tmp_path,
    )
    assert not g.calls.get("guarded")


# ==========================================================================
# Order-construction funnel
# ==========================================================================

# Every order that reaches the exchange must be built inside the execution
# engine, which is where the caps and guards live. A second construction site
# would bypass them silently -- the same "correct in isolation, never wired"
# failure, arriving through a new surface instead of an old one.
ORDER_CONSTRUCTION_ALLOWED = {"execution/engine.py"}


def test_order_requests_are_only_built_inside_the_engine():
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = str(path.relative_to(SRC))
        if rel in ORDER_CONSTRUCTION_ALLOWED or rel == "exchange/base.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "OrderRequest":
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        f"OrderRequest built outside the engine at {offenders}. Every order must "
        "route through ExecutionEngine so the leverage cap, sizing validation, "
        "stale guard and kill switch cannot be bypassed. Add the new surface to "
        "the engine rather than building orders beside it."
    )


def test_leverage_is_only_set_through_the_engine():
    """set_leverage_preference mutates account state that outlives one trade
    (Study B). Only the engine may call it, after applying the cap."""
    allowed = {"execution/engine.py", "exchange/kraken.py", "exchange/base.py"}
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = str(path.relative_to(SRC))
        if rel in allowed:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _CallGraph._callee(node)
                if name == "set_leverage_preference":
                    offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"leverage set outside the engine at {offenders}"


# ==========================================================================
# The registry must GROW with the codebase
# ==========================================================================
#
# The registry was written in Stage 6 and by Stage 9 had fallen behind: the
# reconciler's repairs and recovery's intent resolver existed, were reachable
# only through a registered entry point, and were themselves unguarded. That is
# the same reasoning that produced the two worst findings in this build —
# "it's called from X" asserted about X, not about the thing itself.
#
# This test makes falling behind a failure. A new safety-shaped function must
# either be registered, or explicitly listed as not-a-guard with a reason.

NOT_SAFETY_MECHANISMS = {
    "_require": "config env-var reader, not a trading guard",
    # Report/data accessors — views over results, not guards.
    "repaired": "ReconcileReport accessor",
    "unrepaired": "ReconcileReport accessor",
    "clean": "ReconcileReport accessor",
    "ambiguous": "RecoveryResult accessor",
    "safe_to_trade": "RecoveryResult accessor",
    "refused": "AdmissionResult accessor",
    "blocks_startup": "CheckResult accessor",
    "is_ambiguous": "ObservedOrder accessor",
    "is_filled": "ObservedOrder accessor",
    "has_any_fill": "ObservedOrder accessor",
    "is_exact": "SizedOrder accessor",
    "is_flat": "Position accessor",
    "is_open": "TradeState accessor",
    "is_live": "TradingMode accessor",
    "is_resting": "OrderStatus accessor",
    "is_final": "OrderStatus accessor",
    "is_exit_like": "MessageType accessor",
    "touches_exchange": "TradingMode accessor",
    # Internal plumbing.
    "check_connectivity": "covered by run_permission_checks",
    "_check_leverage_drift": None,  # registered
    "_is_complete_signal": "parser helper, covered by classify",
    "is_imperative": "parser helper, covered by classify",
    "is_known_other_asset": "parser helper, covered by resolve_asset",
    "keyword_forms": "parser helper",
    "has_commentary_markers": "parser helper, covered by classify",
    "_detect_direction": "parser helper",
    "_confidence": "parser helper",
    "check_futures_withdrawal_attestation": None,  # registered
    "check_spot_withdrawal_scope": "covered by run_permission_checks",
    "has_futures_credentials": "startup helper",
    "has_spot_credentials": "startup helper",
    "is_engaged": "covered by allows_new_positions",
    "allows_position_management": "documented always-True invariant",
    "collect_secrets": "logging setup",
    "entry_allocation_usd": "config accessor",
    "is_tradeable": "config accessor",
    "startup_banner": "logging",
    "identity": "ParsedMessage accessor",
    "is_terminal": None,  # registered as a property
    "can_transition": "covered by assert_transition",
    "assert_transition": "covered by transition_trade",
    "transition_trade": "store primitive",
    "round_to_increment": "covered by calculate_size",
    "round_price_to_tick": "covered by calculate_size",
    "split_entry_allocations": "covered by plan_entries",
    "parse_instrument": "covered by resolve_specs",
    "observe_order": "FillObserver protocol method",
    "observe_position": "FillObserver protocol method",
    "weighted_average_entry": "FillObserver optional method",
    "capture": "snapshot constructor",
    "order_by_client_id": "snapshot accessor",
    "fills_for": "snapshot accessor",
    "to_inbound": "covered by dispatch",
    "handle_event": "covered by dispatch",
    "reassert_leverage": "covered by set_leverage_preference",
    "require_recovered": None,  # registered
    "build_admission": "construction, asserted by its own test",
}

# Name shapes that suggest a guard rather than plumbing.
_SAFETY_SHAPED = re.compile(
    r"^_?(check|verify|validate|guard|enforce|refuse|repair|resolve|recover|"
    r"admit|require|ensure|assert)_?", re.I
)


def test_registry_has_not_fallen_behind_the_codebase():
    """Every safety-shaped function is registered or explicitly excused."""
    unaccounted = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = node.name
            if not _SAFETY_SHAPED.match(name):
                continue
            if name in SAFETY_FUNCTIONS or name in SAFETY_PROPERTIES:
                continue
            if name in NOT_SAFETY_MECHANISMS:
                continue
            unaccounted.append(f"{path.relative_to(SRC)}:{name}")

    assert not unaccounted, (
        "safety-shaped functions are neither registered nor excused: "
        f"{unaccounted}. Add them to SAFETY_FUNCTIONS so the wiring guard "
        "covers them, or to NOT_SAFETY_MECHANISMS with a reason. The registry "
        "must grow with the codebase — it went stale once already."
    )


def test_excuse_list_has_no_dead_entries():
    """An excuse for a function that no longer exists hides nothing and rots."""
    stale = [
        n for n, reason in NOT_SAFETY_MECHANISMS.items()
        if reason is not None and n not in GRAPH.defined
    ]
    assert not stale, f"NOT_SAFETY_MECHANISMS names functions that no longer exist: {stale}"


def test_registry_covers_every_stage_module():
    """Each module that performs trading-critical work contributes at least one
    registered mechanism — a module with none is a sign it was skipped."""
    expected = {
        "sizing.py", "admission.py", "reconcile.py", "recovery.py",
        "execution/engine.py", "killswitch.py", "safety/permissions.py",
        "telegram/listener.py", "exchange/specs.py",
    }
    covered = set()
    for name in SAFETY_FUNCTIONS:
        for site in _production_call_sites(name):
            covered.add(site.rsplit(":", 1)[0])
        for path in SRC.rglob("*.py"):
            tree = ast.parse(path.read_text())
            if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == name for n in ast.walk(tree)):
                covered.add(str(path.relative_to(SRC)))
    missing = expected - covered
    assert not missing, f"modules with no registered safety mechanism: {missing}"
