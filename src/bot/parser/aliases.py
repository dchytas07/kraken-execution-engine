"""Asset alias resolution.

Maps the many ways a channel writes an asset onto a CanonicalAsset. The
exchange adapter — not this module — maps CanonicalAsset onto a Kraken symbol.
No Kraken symbol appears here.

Matching is whole-token only. Substring matching would resolve "SOLD" to SOL
and "ETHOS" to ETH, which is exactly the kind of false positive that turns
commentary into a trade.
"""

from __future__ import annotations

import re

from ..models import CanonicalAsset

# Canonical -> the tokens that unambiguously mean it.
_ALIASES: dict[CanonicalAsset, frozenset[str]] = {
    CanonicalAsset.BTC: frozenset(
        {"btc", "xbt", "bitcoin", "btcusd", "btcusdt", "xbtusd", "btcusdc",
         "btcperp", "btcusdperp"}
    ),
    CanonicalAsset.ETH: frozenset(
        {"eth", "ether", "ethereum", "ethusd", "ethusdt", "ethusdc",
         "ethperp", "ethusdperp"}
    ),
    CanonicalAsset.SOL: frozenset(
        {"sol", "solana", "solusd", "solusdt", "solusdc", "solperp",
         "solusdperp"}
    ),
    CanonicalAsset.BNB: frozenset(
        {"bnb", "binancecoin", "bnbusd", "bnbusdt", "bnbusdc", "bnbperp",
         "bnbusdperp"}
    ),
    CanonicalAsset.XRP: frozenset(
        {"xrp", "ripple", "xrpusd", "xrpusdt", "xrpusdc", "xrpperp",
         "xrpusdperp"}
    ),
    CanonicalAsset.DOGE: frozenset(
        {"doge", "dogecoin", "dogeusd", "dogeusdt", "dogeusdc",
         "dogeperp", "dogeusdperp"}
    ),
    CanonicalAsset.XAU: frozenset(
        {"xau", "gold", "xauusd", "xauusdt", "gld", "xaut", "xautusd"}
    ),
}

_TOKEN_TO_ASSET: dict[str, CanonicalAsset] = {
    token: asset for asset, tokens in _ALIASES.items() for token in tokens
}

# Tokens that look like our assets but are different instruments. Listed
# explicitly so they resolve to "known but unsupported" rather than silently
# matching a prefix.
_KNOWN_OTHER_ASSETS = frozenset(
    {
        # "bnb" was here until 2026-07-31, when PF_BNBUSD was verified as a
        # direct perpetual and BNB became supported. A token must never be in
        # both maps: _ALIASES means "trade this", this set means "recognised
        # but refuse", and the overlap would make the verdict depend on
        # lookup order.
        "ada", "cardano", "avax",
        "matic", "polygon", "dot", "polkadot", "link", "chainlink", "ltc",
        "litecoin", "bch", "trx", "tron", "shib", "pepe", "apt", "arb", "op",
        "sui", "near", "atom", "ton", "xag", "silver", "wti", "oil",
    }
)

# Import-time guard. Promoting an asset means adding it to _ALIASES AND
# removing it from _KNOWN_OTHER_ASSETS; forgetting the second half leaves a
# token that means both "trade this" and "refuse this", and which one wins is
# an implementation detail nobody should have to reason about.
_OVERLAP = set(_TOKEN_TO_ASSET) & _KNOWN_OTHER_ASSETS
if _OVERLAP:
    raise AssertionError(
        f"token(s) {sorted(_OVERLAP)} are both a supported alias and a known "
        "unsupported asset; remove them from _KNOWN_OTHER_ASSETS"
    )

# Splits on anything that is not alphanumeric, so "BTC/USD", "BTC-USD" and
# "$BTC" all yield a "btc" token.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(normalized_text: str) -> list[str]:
    return _TOKEN_RE.findall(normalized_text)


def resolve_asset(normalized_text: str) -> tuple[CanonicalAsset | None, list[str]]:
    """Resolve the asset mentioned in the text.

    Returns (asset, unsupported_tokens_found).

    Returns None for the asset when nothing matches OR when two different
    canonical assets are both mentioned — an ambiguous message must never be
    resolved by picking one (brief 40).
    """
    tokens = tokenize(normalized_text)

    found: list[CanonicalAsset] = []
    for token in tokens:
        asset = _TOKEN_TO_ASSET.get(token)
        if asset is not None and asset not in found:
            found.append(asset)

    unsupported = [t for t in tokens if t in _KNOWN_OTHER_ASSETS]

    if len(found) == 1:
        return found[0], unsupported
    # Zero matches, or two different assets in one message -> do not guess.
    return None, unsupported


def is_known_other_asset(normalized_text: str) -> bool:
    """True when the message names a real asset we deliberately do not trade.

    Used to classify as UNSUPPORTED_ASSET (recorded, visible) rather than
    UNKNOWN (noise), per brief 4.
    """
    return bool(set(tokenize(normalized_text)) & _KNOWN_OTHER_ASSETS)
