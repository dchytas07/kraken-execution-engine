"""Parser entry point: raw Telegram message -> ParsedMessage.

Deterministic and side-effect free. No network, no database, no clock. The
same text always produces the same result, which is what makes the parser
testable to the standard brief 33 demands.
"""

from __future__ import annotations

import hashlib

from ..models import (
    CanonicalAsset,
    Direction,
    MessageType,
    ParsedMessage,
    ParseWarning,
)
from . import aliases, extract, patterns as P
from .classify import Fields, classify, missing_signal_fields
from .normalize import basic_normalize, deobfuscate


def _detect_direction(plain: str) -> Direction | None:
    """Direction, or None when both or neither appear.

    A message naming both LONG and SHORT is ambiguous and must not resolve to
    whichever matched first.
    """
    is_long = bool(P.LONG_RE.search(plain))
    is_short = bool(P.SHORT_RE.search(plain))
    if is_long and not is_short:
        return Direction.LONG
    if is_short and not is_long:
        return Direction.SHORT
    return None


def _canon(value) -> str:
    """Numeric text with no trailing zeros: '68000.00000000' and '68000' are the
    same price, and a repost that only drops the padding must still collide."""
    return format(value.normalize(), "f")


def signal_fingerprint(
    asset: CanonicalAsset | None,
    direction: Direction | None,
    entry_range: tuple,
    take_profits: tuple,
    stop_loss,
) -> str | None:
    """Content fingerprint for duplicate detection (brief 23).

    Built from *parsed values*, not raw text, so a reposted signal with
    different emoji, spacing or capitalisation still collides — while a
    genuinely different signal (any differing level) does not. Text hashing
    alone would miss the first case, which the brief explicitly warns about.
    """
    if asset is None or direction is None:
        return None
    parts = [
        asset.value,
        direction.value,
        "|".join(_canon(p) for p in entry_range),
        "|".join(_canon(p) for p in take_profits),
        _canon(stop_loss) if stop_loss is not None else "",
    ]
    return hashlib.sha256("~".join(parts).encode()).hexdigest()[:32]


def parse_message(
    *,
    channel_id: int,
    message_id: int,
    text: str,
    timestamp: float = 0.0,
    reply_to_message_id: int | None = None,
) -> ParsedMessage:
    plain = basic_normalize(text)
    deobf = deobfuscate(text)

    asset, unsupported = aliases.resolve_asset(plain)
    direction = _detect_direction(plain)

    fields = Fields(
        asset=asset,
        direction=direction,
        entry_range=extract.extract_entry_range(plain),
        take_profits=extract.extract_take_profits(plain),
        stop_loss=extract.extract_stop_loss(plain),
        leverage=extract.extract_leverage(plain),
        percentage=extract.extract_percentage(plain),
        has_reply=reply_to_message_id is not None,
        unsupported_tokens=tuple(unsupported),
    )

    message_type, reasons = classify(plain, deobf, fields)

    warnings: list[ParseWarning] = []
    errors: list[str] = []

    if message_type is MessageType.UNKNOWN:
        missing = missing_signal_fields(fields)
        if direction is not None and len(missing) < 5:
            errors.extend(f"missing required field: {m}" for m in missing)

    if message_type is MessageType.NEW_SIGNAL:
        tp_count = len(fields.take_profits)
        if tp_count != 4:
            warnings.append(
                ParseWarning(
                    "unexpected_tp_count",
                    f"expected 4 take-profit levels, found {tp_count}",
                )
            )
        if fields.leverage is None:
            warnings.append(
                ParseWarning("no_leverage", "no leverage stated; default applies")
            )
        if len(fields.entry_range) == 1:
            warnings.append(
                ParseWarning(
                    "single_entry",
                    "one entry price found; two-entry split needs a range",
                )
            )

    for reason in reasons:
        warnings.append(ParseWarning("classification", reason))

    if unsupported:
        warnings.append(
            ParseWarning(
                "unsupported_asset_mentioned",
                f"message names non-whitelisted asset(s): {', '.join(unsupported)}",
            )
        )

    confidence = _confidence(message_type, fields, errors)

    return ParsedMessage(
        channel_id=channel_id,
        message_id=message_id,
        timestamp=timestamp,
        raw_text=text,
        message_type=message_type,
        asset=asset,
        direction=direction,
        entry_range=fields.entry_range,
        take_profits=fields.take_profits,
        stop_loss=fields.stop_loss,
        leverage=fields.leverage,
        percentage=fields.percentage,
        referenced_message_id=reply_to_message_id,
        confidence=confidence,
        parse_warnings=tuple(warnings),
        parse_errors=tuple(errors),
    )


def _confidence(message_type: MessageType, f: Fields, errors: list[str]) -> float:
    """Coarse confidence. Intentionally blunt — it informs review triage, and
    is never the thing that authorises a trade. Validation does that."""
    if errors:
        return 0.0
    match message_type:
        case MessageType.NEW_SIGNAL:
            score = 0.6
            if len(f.take_profits) == 4:
                score += 0.2
            if len(f.entry_range) == 2:
                score += 0.1
            if f.leverage is not None:
                score += 0.1
            return round(min(score, 1.0), 2)
        case MessageType.SPOT_SIGNAL:
            return 0.9
        case (
            MessageType.CLOSE_ALL
            | MessageType.CLOSE_DIRECTION
            | MessageType.CLOSE_SYMBOL
            | MessageType.CLOSE_REPLY
            | MessageType.PARTIAL_CLOSE
        ):
            return 0.8
        case MessageType.SL_UPDATE | MessageType.ENTRY_UPDATE | MessageType.ADD_POSITION:
            return 0.7
        case MessageType.SAFETY_REVIEW:
            return 0.3
        case _:
            return 0.1


def fingerprint_for(parsed: ParsedMessage) -> str | None:
    return signal_fingerprint(
        parsed.asset,
        parsed.direction,
        parsed.entry_range,
        parsed.take_profits,
        parsed.stop_loss,
    )
