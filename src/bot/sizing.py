"""Sizing and take-profit allocation.

Two facts from Stage 4 shape this module:

1. **The exchange is not a safety net.** The paper engine accepted 0.000001 BTC
   against a 0.0001 increment (KRAKEN_NOTES 4.1), so a malformed size does not
   reliably bounce. Every constraint is enforced here, before submission.

2. **The minimum order size equals one size increment** (KRAKEN_NOTES 4.1.1),
   and is derived from `contractValueTradePrecision` rather than hardcoded, so
   a newly listed instrument gets a correct floor automatically.

All arithmetic is Decimal. No float appears anywhere in this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from .models import InstrumentSpec


class SizingError(Exception):
    """Sizing could not produce a valid order. Always fail closed (brief 40)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SizedOrder:
    """A validated, exchange-compatible order size."""

    size: Decimal
    price: Decimal
    notional: Decimal
    margin_used: Decimal
    leverage: Decimal
    intended_notional: Decimal
    deviation_percent: Decimal

    @property
    def is_exact(self) -> bool:
        return self.deviation_percent == 0


def effective_leverage(
    signal_leverage: Decimal | None,
    *,
    max_leverage: Decimal,
    default_leverage: Decimal,
    exchange_max_leverage: Decimal,
) -> Decimal:
    """effective = min(signal, internal cap, exchange max)  — brief 9.

    Validated here rather than trusted to the exchange, because the exchange
    enforcing a cap is not the same as us intending one.
    """
    requested = signal_leverage if signal_leverage is not None else default_leverage
    if requested <= 0:
        raise SizingError("LEVERAGE_INVALID", f"requested leverage {requested} <= 0")
    result = min(requested, max_leverage, exchange_max_leverage)
    if result <= 0:
        raise SizingError("LEVERAGE_INVALID", f"resolved leverage {result} <= 0")
    return result


# Maintenance margin rate used to ESTIMATE liquidation, in paper and live alike
# so paper rehearses live exactly. Measured on the paper engine (kraken 0.4.1,
# 2026-10-04): a flat 2% on every instrument and side — short liquidation is
# entry * (1 + 1/L - 0.02), e.g. 2702.3 short at 10x -> 2918.484. Kraken's real
# tier-1 rates are 0.5-1%, so on live this is conservative, which is the
# direction cross-margin (portfolio-level liquidation) requires.
LIQUIDATION_MAINTENANCE_RATE = Decimal("0.02")


def liquidation_distance(
    leverage: Decimal, maintenance_rate: Decimal = LIQUIDATION_MAINTENANCE_RATE
) -> Decimal:
    """Adverse move from entry, as a fraction, that liquidates at `leverage`."""
    if leverage <= 0:
        raise SizingError("LEVERAGE_INVALID", f"leverage {leverage} <= 0")
    return Decimal(1) / leverage - maintenance_rate


def estimated_liquidation_price(
    entry: Decimal,
    leverage: Decimal,
    *,
    is_short: bool,
    maintenance_rate: Decimal = LIQUIDATION_MAINTENANCE_RATE,
) -> Decimal:
    distance = liquidation_distance(leverage, maintenance_rate)
    return entry * (1 + distance) if is_short else entry * (1 - distance)


def stop_distance(
    entry_prices: tuple[Decimal, ...], stop_loss: Decimal, *, is_short: bool
) -> Decimal:
    """Worst-case adverse move from entry to stop, as a fraction.

    Measured from the entry FARTHEST from the stop: if only that entry fills,
    the position's average entry is that price, and the stop is as far away
    as it can ever be. Any blend of fills sits closer to the stop.
    """
    if not entry_prices:
        raise SizingError("NO_ENTRY_RANGE", "cannot measure stop distance")
    if is_short:
        far = min(entry_prices)
        return (stop_loss - far) / far
    far = max(entry_prices)
    return (far - stop_loss) / far


def max_leverage_for_stop(
    entry_prices: tuple[Decimal, ...],
    stop_loss: Decimal,
    *,
    is_short: bool,
    buffer_percent: Decimal,
    maintenance_rate: Decimal = LIQUIDATION_MAINTENANCE_RATE,
) -> Decimal:
    """Highest whole leverage at which the stop triggers before liquidation.

    Requires liquidation_distance(L) >= stop_distance + buffer, i.e.
    L <= 1 / (stop_distance + maintenance_rate + buffer). Trade 8 (ETH short
    2497, stop 2700, 10x) was liquidated at 2698.21 because the stop sat
    beyond the 2696.76 liquidation price; this returns 8 for it.
    """
    distance = stop_distance(entry_prices, stop_loss, is_short=is_short)
    if distance <= 0:
        raise SizingError(
            "STOP_WRONG_SIDE",
            f"stop {stop_loss} is not on the losing side of entries {entry_prices}",
        )
    required = distance + maintenance_rate + buffer_percent / 100
    return (Decimal(1) / required).to_integral_value(rounding=ROUND_DOWN)


def round_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    """Round DOWN to a multiple of `increment`.

    Down, never nearest: rounding up would exceed the intended margin, and
    brief 10 forbids overshooting the allocation through rounding.
    """
    if increment <= 0:
        raise SizingError("INVALID_INCREMENT", f"increment {increment} <= 0")
    return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment


def round_price_to_tick(price: Decimal, tick: Decimal, *, round_up: bool) -> Decimal:
    """Snap a price to the instrument tick.

    Direction is the caller's choice because it is directional-safety
    sensitive: a long's stop rounds DOWN (further from entry, never tighter
    than intended), a short's stop rounds UP.
    """
    if tick <= 0:
        raise SizingError("INVALID_TICK", f"tick {tick} <= 0")
    units = price / tick
    integral = units.to_integral_value(rounding=ROUND_DOWN)
    if round_up and units != integral:
        integral += 1
    return integral * tick


def validate_size(size: Decimal, spec: InstrumentSpec) -> None:
    """Reject anything the exchange should reject but might not."""
    if size <= 0:
        raise SizingError("SIZE_NOT_POSITIVE", f"{size}")
    if size < spec.size_increment:
        raise SizingError(
            "SIZE_BELOW_MINIMUM",
            f"{size} is below the minimum order size {spec.size_increment} "
            f"for {spec.symbol}",
        )
    remainder = (size / spec.size_increment) % 1
    if remainder != 0:
        raise SizingError(
            "SIZE_NOT_ON_INCREMENT",
            f"{size} is not a multiple of {spec.size_increment} for {spec.symbol}",
        )


def calculate_size(
    *,
    margin_usd: Decimal,
    leverage: Decimal,
    price: Decimal,
    spec: InstrumentSpec,
    max_deviation_percent: Decimal,
) -> SizedOrder:
    """Turn a margin allocation into a validated exchange size.

    For PF_ flexible futures, size is denominated in base-asset units and
    `contractSize` is 1 — but it is read from the spec and divided through
    anyway, so an instrument with a different contract size cannot silently
    mis-size (brief 3).
    """
    if margin_usd <= 0:
        raise SizingError("MARGIN_NOT_POSITIVE", f"{margin_usd}")
    if price <= 0:
        raise SizingError("PRICE_NOT_POSITIVE", f"{price}")
    if not spec.tradeable:
        raise SizingError("INSTRUMENT_NOT_TRADEABLE", spec.symbol)

    intended_notional = margin_usd * leverage
    raw_size = intended_notional / (price * spec.contract_size)

    size = round_to_increment(raw_size, spec.size_increment)

    if size <= 0:
        raise SizingError(
            "SIZE_BELOW_MINIMUM",
            f"${margin_usd} margin at {leverage}x on {spec.symbol} @ {price} "
            f"gives {raw_size}, which rounds to zero at increment "
            f"{spec.size_increment}. Increase TRADE_MARGIN_USD.",
        )

    validate_size(size, spec)

    actual_notional = size * price * spec.contract_size
    deviation = abs(actual_notional - intended_notional) / intended_notional * 100

    if deviation > max_deviation_percent:
        raise SizingError(
            "SIZING_DEVIATION",
            f"{spec.symbol}: rounding to increment {spec.size_increment} moved "
            f"notional from ${intended_notional} to ${actual_notional} "
            f"({deviation:.2f}% > {max_deviation_percent}% allowed). "
            "Refusing to place a materially mis-sized order.",
        )

    return SizedOrder(
        size=size,
        price=price,
        notional=actual_notional,
        margin_used=actual_notional / leverage,
        leverage=leverage,
        intended_notional=intended_notional,
        deviation_percent=deviation,
    )


def split_take_profits(
    position_size: Decimal,
    spec: InstrumentSpec,
    *,
    levels: int = 4,
) -> tuple[Decimal, ...]:
    """Split a position across TP levels, 25% each with the remainder earliest.

    Policy (brief 13): ideal quarters, rounded to the increment, with any
    unavoidable remainder distributed from TP1 onward. TP4 takes whatever is
    left so the parts always sum to EXACTLY the position — never more, which
    would risk opening a reverse position, and never less, which would strand
    an unmanaged remainder.

    Returns fewer than `levels` entries when the position is too small to
    divide; the caller places only as many TP orders as are returned.
    """
    if position_size <= 0:
        raise SizingError("POSITION_NOT_POSITIVE", f"{position_size}")
    validate_size(position_size, spec)

    increment = spec.size_increment
    total_increments = int(position_size / increment)

    if total_increments < levels:
        # Cannot make `levels` parts of at least one increment each. Use as
        # many whole increments as we have, rather than emitting zero-size
        # orders the exchange would reject.
        parts = [increment] * total_increments
        if not parts:
            raise SizingError(
                "POSITION_TOO_SMALL_FOR_TP",
                f"{position_size} is smaller than one increment {increment}",
            )
        allocated = sum(parts[:-1])
        parts[-1] = position_size - allocated
        return tuple(parts)

    base = total_increments // levels
    remainder = total_increments % levels

    # Remainder goes to the EARLIEST levels, so profit is realised sooner.
    counts = [base + (1 if i < remainder else 0) for i in range(levels)]

    parts = [Decimal(c) * increment for c in counts]

    # Final level absorbs any residue, guaranteeing an exact sum even if the
    # position is not a clean multiple of the increment.
    parts[-1] = position_size - sum(parts[:-1])

    assert sum(parts) == position_size, "TP split must sum to the position"
    return tuple(parts)


def split_entry_allocations(
    total_margin: Decimal, entries: int = 2
) -> tuple[Decimal, ...]:
    """Split margin across entry orders (brief 11: 50/50 for two entries).

    Splits MARGIN, not contracts — the two entries sit at different prices, so
    equal margin gives different sizes, which is the intended behaviour.
    """
    if total_margin <= 0:
        raise SizingError("MARGIN_NOT_POSITIVE", f"{total_margin}")
    if entries < 1:
        raise SizingError("INVALID_ENTRY_COUNT", f"{entries}")
    each = total_margin / entries
    parts = [each] * entries
    parts[-1] = total_margin - sum(parts[:-1])  # exact sum
    return tuple(parts)


def check_sufficient_margin(
    required_margin: Decimal, available_margin: Decimal
) -> None:
    """Brief 24: verify margin before opening; never partially execute blindly."""
    if required_margin > available_margin:
        raise SizingError(
            "INSUFFICIENT_MARGIN",
            f"need ${required_margin} but only ${available_margin} is available",
        )


def is_entry_stale(
    current_price: Decimal,
    entry_range: tuple[Decimal, ...],
    max_deviation_percent: Decimal,
) -> tuple[bool, Decimal]:
    """Stale-signal guard (brief 12).

    Deviation is measured from the nearest edge of the entry range, so a price
    still inside the range is never stale. Returns (is_stale, deviation_pct).
    """
    if not entry_range:
        raise SizingError("NO_ENTRY_RANGE", "cannot evaluate staleness")

    low, high = min(entry_range), max(entry_range)
    if low <= current_price <= high:
        return False, Decimal(0)

    nearest = low if current_price < low else high
    deviation = abs(current_price - nearest) / nearest * 100
    return deviation > max_deviation_percent, deviation
