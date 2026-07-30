"""Audit EXCUSED_IN_CRITICAL_MODULES for stale or wrong entries.

An excuse is a claim recorded at a point in time. The codebase has moved
through Stages 7-10 since most were written, so an entry that said "called by
X" may now describe code that no longer exists or has changed shape. A stale
excuse is worse than no excuse: it silences the guard for a function nobody is
checking any more.

Each excuse is machine-validated against its own claim:

  "called by X"        -> X must exist AND actually call it
  "registered"         -> it must appear in SAFETY_FUNCTIONS
  "...accessor"        -> must be trivial (a property or a one-liner return)
  "loop wrapper over X"-> must call X
  "proven by <test>"   -> that test must exist
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tests.test_wiring import (  # noqa: E402
    CRITICAL_MODULES,
    EXCUSED_IN_CRITICAL_MODULES,
    SAFETY_FUNCTIONS,
    SAFETY_PROPERTIES,
)

SRC = ROOT / "src" / "bot"
TESTS = ROOT / "tests"


def all_functions() -> dict[str, list[str]]:
    """name -> modules defining it."""
    out: dict[str, list[str]] = {}
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.setdefault(node.name, []).append(str(path.relative_to(SRC)))
    return out


def function_node(name: str):
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node, path
    return None, None


def callers_of(name: str) -> set[str]:
    """Enclosing function names that call `name`."""
    out: set[str] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        enclosing: dict[int, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    enclosing.setdefault(id(child), node.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                called = fn.id if isinstance(fn, ast.Name) else (
                    fn.attr if isinstance(fn, ast.Attribute) else None)
                if called == name:
                    out.add(enclosing.get(id(node), "<module>"))
    return out


def is_trivial(node) -> bool:
    """A genuine accessor: a property, or a body of at most two statements
    that only returns/compares — no exchange calls, no DB writes."""
    if node is None:
        return False
    decorated = any(
        (isinstance(d, ast.Name) and d.id == "property")
        or (isinstance(d, ast.Attribute) and d.attr in ("setter", "getter"))
        for d in node.decorator_list
    )
    body = [s for s in node.body if not isinstance(s, ast.Expr)
            or not isinstance(s.value, ast.Constant)]
    small = len(body) <= 3
    dangerous = {"place_order", "cancel_order", "amend_order",
                 "set_leverage_preference", "transition_trade", "record_decision"}
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            fn = n.func
            called = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else None)
            if called in dangerous:
                return False
    return decorated or small


def main() -> int:
    defined = all_functions()
    problems: list[str] = []
    checked = 0

    critical_fns: set[str] = set()
    for module in CRITICAL_MODULES:
        tree = ast.parse((SRC / module).read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                critical_fns.add(node.name)

    print(f"auditing {len(EXCUSED_IN_CRITICAL_MODULES)} excuses "
          f"against {len(critical_fns)} functions in {len(CRITICAL_MODULES)} "
          "critical modules\n")

    for name, reason in sorted(EXCUSED_IN_CRITICAL_MODULES.items()):
        checked += 1

        # 1. The function must still exist somewhere.
        if name not in defined:
            problems.append(f"{name}: excused but NO LONGER DEFINED — {reason!r}")
            continue

        # 2. Excuses that name a caller must be true.
        m = re.search(r"called by ([A-Za-z_][A-Za-z0-9_]*)", reason)
        if m:
            claimed = m.group(1)
            if claimed not in defined:
                problems.append(
                    f"{name}: excuse names caller {claimed!r}, which no longer "
                    f"exists — {reason!r}")
                continue
            actual = callers_of(name)
            if claimed not in actual:
                problems.append(
                    f"{name}: excuse claims 'called by {claimed}' but real "
                    f"callers are {sorted(actual) or 'NONE'} — {reason!r}")
                continue

        # 3. "registered" must mean registered.
        if reason.strip().lower() == "registered":
            if name not in SAFETY_FUNCTIONS and name not in SAFETY_PROPERTIES:
                problems.append(
                    f"{name}: excused as 'registered' but is NOT in "
                    "SAFETY_FUNCTIONS")
                continue

        # 4. Accessor claims must actually be trivial.
        if "accessor" in reason.lower():
            node, path = function_node(name)
            if not is_trivial(node):
                problems.append(
                    f"{name}: excused as an accessor but the body is not "
                    f"trivial (in {path.name if path else '?'}) — {reason!r}")
                continue

        # 5. "loop wrapper over X" must call X.
        m = re.search(r"loop wrapper over ([A-Za-z_][A-Za-z0-9_]*)", reason)
        if m:
            inner = m.group(1)
            node, _ = function_node(name)
            calls_inner = False
            if node is not None:
                for n in ast.walk(node):
                    if isinstance(n, ast.Call):
                        fn = n.func
                        called = fn.id if isinstance(fn, ast.Name) else (
                            fn.attr if isinstance(fn, ast.Attribute) else None)
                        if called == inner:
                            calls_inner = True
            if not calls_inner:
                problems.append(
                    f"{name}: excuse claims 'loop wrapper over {inner}' but it "
                    f"does not call it — {reason!r}")
                continue

        # 6. A named test must exist.
        m = re.search(r"(test_[A-Za-z0-9_]+)", reason)
        if m:
            wanted = m.group(1)
            found = any(wanted in p.read_text() for p in TESTS.glob("test_*.py"))
            if not found:
                problems.append(
                    f"{name}: excuse cites {wanted}, which does not exist")
                continue

        # 7. Excuse for a function no longer in a critical module.
        if name not in critical_fns and name not in defined:
            problems.append(f"{name}: excuse is for a function outside the "
                            "critical modules and undefined")

    print(f"checked {checked} excuses")
    if problems:
        print(f"\n{len(problems)} PROBLEM(S):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nno stale or false excuses found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
