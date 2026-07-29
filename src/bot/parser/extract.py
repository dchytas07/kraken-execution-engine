"""Numeric field extraction.

Operates on the *basic-normalised* text only — never the deobfuscated form,
because deobfuscation rewrites digits (`0`->`o`) and would corrupt prices.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from . import patterns as P


def to_decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def extract_entry_range(text: str) -> tuple[Decimal, ...]:
    """Entry prices, in the order written. Two for a range, one for a point."""
    match = P.ENTRY_RANGE_RE.search(text)
    if match:
        lo, hi = to_decimal(match.group(1)), to_decimal(match.group(2))
        if lo is not None and hi is not None:
            return (lo, hi)
    match = P.ENTRY_SINGLE_RE.search(text)
    if match:
        value = to_decimal(match.group(1))
        if value is not None:
            return (value,)
    return ()


def extract_take_profits(text: str) -> tuple[Decimal, ...]:
    """TP levels ordered TP1..TP4.

    Numbered form wins over the list form: an explicit `TP2:` label is stronger
    evidence than position in a comma list.
    """
    numbered: dict[int, Decimal] = {}
    for match in P.TP_NUMBERED_RE.finditer(text):
        index = int(match.group(1))
        value = to_decimal(match.group(2))
        if value is not None:
            numbered.setdefault(index, value)

    if numbered:
        return tuple(numbered[i] for i in sorted(numbered))

    match = P.TP_LIST_RE.search(text)
    if match:
        parts = [
            to_decimal(p)
            for p in __import__("re").split(r"\s*[,/|-]\s*", match.group(1))
        ]
        values = [p for p in parts if p is not None]
        if len(values) >= 2:
            return tuple(values)
    return ()


def extract_stop_loss(text: str) -> Decimal | None:
    match = P.SL_RE.search(text)
    return to_decimal(match.group(1)) if match else None


def extract_leverage(text: str) -> Decimal | None:
    match = P.LEVERAGE_RE.search(text)
    if match:
        return to_decimal(match.group(1))
    match = P.LEVERAGE_LABELLED_RE.search(text)
    return to_decimal(match.group(1)) if match else None


def extract_percentage(text: str) -> Decimal | None:
    """Percentage for a partial close. Fractions map to their percentage."""
    match = P.PARTIAL_CLOSE_RE.search(text)
    if match:
        if match.group(1):
            return to_decimal(match.group(1))
        word = (match.group(2) or "").strip()
        return {
            "half": Decimal(50),
            "a third": Decimal(33),
            "a quarter": Decimal(25),
        }.get(word)
    match = P.PERCENT_RE.search(text)
    return to_decimal(match.group(1)) if match else None
