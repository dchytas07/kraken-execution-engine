"""Instrument specification resolution.

Specs are read from `kraken futures instruments` and never hardcoded (brief 3,
45). The only constant here is the asset -> symbol mapping, which is an
identity decision rather than a spec.
"""

from __future__ import annotations

from decimal import Decimal

from ..logging_setup import get_logger
from ..models import CanonicalAsset, InstrumentSpec
from .base import InstrumentUnavailable

log = get_logger(__name__)

# Canonical asset -> Kraken perpetual symbol.
# Note BTC is XBT in Kraken symbology.
SYMBOL_MAP: dict[CanonicalAsset, str] = {
    CanonicalAsset.BTC: "PF_XBTUSD",
    CanonicalAsset.ETH: "PF_ETHUSD",
    CanonicalAsset.SOL: "PF_SOLUSD",
    # SUBSTITUTE, not the asset itself. Kraken lists no spot-gold perpetual;
    # PF_XAUTUSD is Tether Gold. Reachable only when the gold pin is lifted,
    # which Study A currently forbids (docs/KRAKEN_NOTES.md 1B).
    CanonicalAsset.XAU: "PF_XAUTUSD",
}

SUBSTITUTE_ASSETS: dict[CanonicalAsset, str] = {
    CanonicalAsset.XAU: (
        "PF_XAUTUSD is Tether Gold (XAUT), NOT spot XAU/USD. Signal levels "
        "quoted against spot gold do not reliably map onto this instrument."
    ),
}


def parse_instrument(raw: dict, asset: CanonicalAsset) -> InstrumentSpec:
    """Build a spec from one `futures instruments` record.

    Raises InstrumentUnavailable rather than defaulting any field — a missing
    tick size or precision must stop trading, not be guessed (brief 40).
    """
    symbol = raw.get("symbol", "?")

    precision = raw.get("contractValueTradePrecision")
    if precision is None:
        raise InstrumentUnavailable(
            f"{symbol}: contractValueTradePrecision missing; cannot determine "
            "the size increment"
        )
    # Size increment is 10^-precision. Precision 4 -> 0.0001.
    size_increment = Decimal(1).scaleb(-int(precision))

    tick_size = raw.get("tickSize")
    if tick_size is None:
        raise InstrumentUnavailable(f"{symbol}: tickSize missing")

    contract_size = raw.get("contractSize")
    if contract_size is None:
        raise InstrumentUnavailable(f"{symbol}: contractSize missing")

    max_leverage = _max_leverage(raw)
    if max_leverage is None:
        raise InstrumentUnavailable(
            f"{symbol}: no margin schedule; cannot determine max leverage"
        )

    return InstrumentSpec(
        symbol=symbol,
        asset=asset,
        base=str(raw.get("base", "")),
        quote=str(raw.get("quote", "")),
        contract_size=Decimal(str(contract_size)),
        size_increment=size_increment,
        tick_size=Decimal(str(tick_size)),
        max_leverage=max_leverage,
        tradeable=bool(raw.get("tradeable", False)),
        is_substitute=asset in SUBSTITUTE_ASSETS,
        substitute_note=SUBSTITUTE_ASSETS.get(asset, ""),
    )


def _max_leverage(raw: dict) -> Decimal | None:
    """Max leverage = 1 / initialMargin of the first (smallest) margin tier.

    Kraken exposes retailMarginLevels and/or marginLevels. Prefer retail, since
    that is what a non-institutional account is subject to; take the lower of
    the two when both exist, because using the higher would overstate what the
    account can actually do.
    """
    candidates: list[Decimal] = []
    for key in ("retailMarginLevels", "marginLevels"):
        levels = raw.get(key)
        if not levels:
            continue
        initial = levels[0].get("initialMargin")
        if initial:
            candidates.append(Decimal(1) / Decimal(str(initial)))
    return min(candidates) if candidates else None


def resolve_specs(
    instruments: list[dict],
    assets: frozenset[CanonicalAsset] | set[CanonicalAsset],
) -> dict[CanonicalAsset, InstrumentSpec]:
    """Resolve every requested asset, or raise.

    Brief 25: if a required instrument cannot be resolved, refuse to start —
    never silently skip it.
    """
    by_symbol = {i.get("symbol"): i for i in instruments}
    specs: dict[CanonicalAsset, InstrumentSpec] = {}

    for asset in sorted(assets, key=lambda a: a.value):
        symbol = SYMBOL_MAP.get(asset)
        if symbol is None:
            raise InstrumentUnavailable(f"{asset.value}: no Kraken symbol mapped")

        raw = by_symbol.get(symbol)
        if raw is None:
            raise InstrumentUnavailable(
                f"{asset.value}: {symbol} not present in the instrument list"
            )

        spec = parse_instrument(raw, asset)
        if not spec.tradeable:
            raise InstrumentUnavailable(
                f"{asset.value}: {symbol} is not currently tradeable"
            )

        if spec.is_substitute:
            log.warning(
                f"{asset.value} resolves to SUBSTITUTE instrument {symbol}: "
                f"{spec.substitute_note}",
                extra={"asset": asset.value, "symbol": symbol,
                       "action": "substitute_instrument"},
            )
        specs[asset] = spec

    return specs
