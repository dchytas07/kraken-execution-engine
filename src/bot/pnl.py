"""Realised P&L arithmetic, kept pure so the engine and the repair tool agree.

All figures are GROSS of fees, signed by direction, and rounded to cents, the
same convention the engine has always used for `trades.realized_pnl`.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from .exchange.base import Fill


def exit_pnl(
    direction: str, entry: Decimal, exits: Iterable[tuple[Decimal, Decimal]]
) -> Decimal | None:
    """(avg exit - entry) x total size, negated for a SHORT.

    `exits` are (size, price) pairs. None when there is nothing to price: a
    confident 0.00 would read as breakeven, which is a different claim from
    "could not be worked out".
    """
    total = Decimal(0)
    notional = Decimal(0)
    for size, price in exits:
        total += size
        notional += size * price
    if total <= 0:
        return None
    move = notional / total - entry
    if direction == "SHORT":
        move = -move
    return (move * total).quantize(Decimal("0.01"))


def liquidation_exit(
    fills: Iterable[Fill], *, symbol: str, since: str, quantity: Decimal
) -> tuple[Decimal, Decimal] | None:
    """(size, average price) of the liquidation that ended `quantity` of a trade.

    A liquidation closes the position at the exchange's own initiative: its fill
    carries no client order id, so no order of ours accounts for it and P&L
    derived from our own exit orders reads None (trade 8, 2026-10-04: a $12.07
    loss recorded as nothing). `since` is the trade's creation time, so a
    liquidation from before the trade existed is never attributed to it.
    """
    if quantity <= 0:
        return None
    liqs = [
        f for f in fills
        if f.symbol == symbol and f.fill_type == "liquidation"
        and (f.filled_at or "")[:19] >= since[:19]
    ]
    pooled = sum((f.size for f in liqs), Decimal(0))
    if pooled <= 0:
        return None
    avg = sum((f.price * f.size for f in liqs), Decimal(0)) / pooled
    return min(quantity, pooled), avg
