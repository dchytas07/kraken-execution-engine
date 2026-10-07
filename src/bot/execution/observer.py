"""Fill observation — how the execution engine learns what actually happened.

## Why this exists in Stage 6

Reconciliation (Stage 7) owns continuous exchange-state polling, but Stage 6
cannot wait for it: brief 27 forbids advancing a trade on "the order was
submitted successfully", and names that shortcut as a historical bug. The
engine therefore needs a way to observe *confirmed* fills now.

This module is that minimum: a narrow, polling-based primitive that answers
three questions and nothing else.

    observe_order(client_order_id)  -> what has actually filled?
    observe_position(symbol)        -> what does the exchange say we hold?
    await_flat(symbol)              -> is the position genuinely gone?

## The contract Stage 7 must preserve

`FillObserver` is a Protocol. Stage 7 replaces `PollingFillObserver` with a
WebSocket-driven implementation plus mismatch/repair logic, and the engine does
not change, because the engine depends on this interface rather than on
polling. Anything Stage 7 adds goes behind these same three methods.

## Why fills, not order-status

Kraken's `order-status` searches OPEN orders only. Query a filled order and it
returns "not found" — the identical response to a cancelled order or one that
never existed. Verified in paper 2026-07-29 (KRAKEN_NOTES 6).

So order status is derived from two sources rather than trusted from one:

    fills          -> authoritative: what size actually executed
    open-orders    -> authoritative: what is still resting

    resting + no fills          -> OPEN
    resting + fills             -> PARTIALLY_FILLED
    not resting + fills == size -> FILLED
    not resting + 0 < fills     -> PARTIALLY_FILLED then cancelled
    not resting + no fills      -> CANCELLED / REJECTED / never placed  (ambiguous)

The final row is deliberately reported as `UNKNOWN`, not `CANCELLED`. Those
three cases are indistinguishable from the exchange's responses, and brief 40
requires reconciling rather than guessing.

Orders are keyed by **client order id**, not exchange id, because that is the
one identifier we choose before submitting — so it survives a crash between
"sent" and "recorded" (brief 30).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from ..exchange.base import ExchangeClient, Fill, Position
from ..logging_setup import get_logger
from ..models import OrderStatus

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ObservedOrder:
    """Exchange-confirmed order state.

    Only ever produced by a FillObserver, i.e. only ever from querying the
    exchange. It is deliberately NOT constructible from an OrderResult, so the
    engine cannot accidentally advance a trade on a submission response.
    """

    client_order_id: str
    exchange_order_id: str | None
    status: OrderStatus
    filled_size: Decimal
    avg_fill_price: Decimal | None
    resting: bool
    fills: tuple[Fill, ...] = field(default=())

    @property
    def is_filled(self) -> bool:
        return self.status is OrderStatus.FILLED

    @property
    def has_any_fill(self) -> bool:
        return self.filled_size > 0

    @property
    def is_ambiguous(self) -> bool:
        """Not resting and never filled — cancelled, rejected, or never placed.
        Requires reconciliation, never a guess."""
        return self.status is OrderStatus.UNKNOWN


class FillObserver(Protocol):
    """The seam Stage 7 upgrades behind."""

    def observe_order(
        self, client_order_id: str, *, expected_size: Decimal | None = None
    ) -> ObservedOrder: ...

    def observe_position(self, symbol: str) -> Position | None: ...

    def await_flat(
        self, symbol: str, *, timeout_s: float = 30.0, poll_s: float = 1.0
    ) -> bool: ...

    def await_true(
        self, predicate, *, what: str, timeout_s: float = 30.0, poll_s: float = 1.0
    ) -> bool: ...


class PollingFillObserver:
    """Stage 6 implementation: direct polling of fills and open orders.

    Intentionally simple. It does not cache across calls, does not repair
    state, and does not stream — those are Stage 7's job.
    """

    def __init__(
        self,
        client: ExchangeClient,
        *,
        sleep=time.sleep,
        default_timeout_s: float = 30.0,
        default_poll_s: float = 1.0,
    ) -> None:
        self.client = client
        self._sleep = sleep  # injected so tests never actually wait
        self.default_timeout_s = default_timeout_s
        self.default_poll_s = default_poll_s

    def observe_order(
        self, client_order_id: str, *, expected_size: Decimal | None = None
    ) -> ObservedOrder:
        fills = [
            f for f in self.client.get_fills() if f.client_order_id == client_order_id
        ]
        resting_order = next(
            (
                o
                for o in self.client.get_open_orders()
                if o.client_order_id == client_order_id
            ),
            None,
        )

        filled_size = sum((f.size for f in fills), Decimal(0))
        avg_price = None
        if filled_size > 0:
            avg_price = sum((f.price * f.size for f in fills), Decimal(0)) / filled_size

        target = expected_size if expected_size is not None else (
            resting_order.size if resting_order else None
        )

        if resting_order is not None:
            status = (
                OrderStatus.PARTIALLY_FILLED if filled_size > 0 else OrderStatus.OPEN
            )
        elif filled_size > 0:
            # Not resting and something filled. Fully filled only if it reached
            # the expected size; otherwise it was partially filled then removed.
            status = (
                OrderStatus.FILLED
                if target is not None and filled_size >= target
                else OrderStatus.PARTIALLY_FILLED
            )
        else:
            # Indistinguishable: cancelled, rejected, or never placed.
            status = OrderStatus.UNKNOWN

        return ObservedOrder(
            client_order_id=client_order_id,
            exchange_order_id=(
                resting_order.exchange_order_id
                if resting_order
                else next((f.order_id for f in fills if f.order_id), None)
            ),
            status=status,
            filled_size=filled_size,
            avg_fill_price=avg_price,
            resting=resting_order is not None,
            fills=tuple(fills),
        )

    def observe_position(self, symbol: str) -> Position | None:
        return self.client.get_position(symbol)

    def await_flat(
        self, symbol: str, *, timeout_s: float | None = None, poll_s: float | None = None
    ) -> bool:
        """Poll until the exchange reports the symbol flat, or time out.

        Returns True ONLY on a confirmed-flat reading. A timeout returns False
        and must never be treated as success — that is precisely the "marked
        closed because we sent a close" bug (brief 27).
        """
        timeout_s = self.default_timeout_s if timeout_s is None else timeout_s
        poll_s = self.default_poll_s if poll_s is None else poll_s
        deadline = time.monotonic() + timeout_s
        while True:
            position = self.observe_position(symbol)
            if position is None or position.is_flat:
                return True
            if time.monotonic() >= deadline:
                log.error(
                    f"{symbol}: position still open after {timeout_s}s awaiting flat "
                    f"(size {position.size}) — NOT treating as closed",
                    extra={"symbol": symbol, "action": "await_flat_timeout"},
                )
                return False
            self._sleep(poll_s)

    def await_true(
        self, predicate, *, what: str, timeout_s: float | None = None,
        poll_s: float | None = None,
    ) -> bool:
        """Poll `predicate` until it holds, or time out.

        The general form of await_flat, for conditions the symbol-level
        position cannot answer: with several trades sharing a netted position,
        'this trade is flat' is not 'the symbol is flat'. Returns True ONLY when
        the predicate was observed true; a timeout is False and must never be
        treated as success (brief 27).
        """
        timeout_s = self.default_timeout_s if timeout_s is None else timeout_s
        poll_s = self.default_poll_s if poll_s is None else poll_s
        deadline = time.monotonic() + timeout_s
        while True:
            if predicate():
                return True
            if time.monotonic() >= deadline:
                log.error(
                    f"{what}: not confirmed after {timeout_s}s — NOT treating "
                    "as done",
                    extra={"action": "await_true_timeout"},
                )
                return False
            self._sleep(poll_s)

    def weighted_average_entry(self, client_order_ids: list[str]) -> tuple[Decimal, Decimal]:
        """(total_size, weighted_avg_price) across several entry orders.

        Used for the breakeven price, which must reflect actual execution
        rather than the signal's stated entry (brief 15).
        """
        wanted = set(client_order_ids)
        fills = [f for f in self.client.get_fills() if f.client_order_id in wanted]
        total = sum((f.size for f in fills), Decimal(0))
        if total == 0:
            return Decimal(0), Decimal(0)
        avg = sum((f.price * f.size for f in fills), Decimal(0)) / total
        return total, avg
