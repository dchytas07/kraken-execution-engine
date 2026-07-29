"""Message classification.

The governing rule (brief 7, 18, 40): **precision over recall**. A missed
signal costs an opportunity; a false positive costs a real unintended trade.
Every ambiguity therefore resolves toward "do not execute".

Two mechanisms carry most of the weight:

1. **Imperative position test.** A command must appear at the start of a line
   (allowing a short prefix like "urgent: "). Signal channels write commands
   that way; commentary mentions them mid-sentence. "Close all" is an
   instruction, "we should probably close all soon" is not.

2. **Commentary veto.** Past-tense, hedging or interrogative markers appearing
   before the command on the same line disqualify it. The channel discusses
   trades constantly, so "booked 50%" (narration) must never execute while
   "Book 50%" (instruction) must.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..models import CanonicalAsset, Direction, MessageType
from . import aliases, patterns as P

# How much text may precede a command on its line and still count as
# imperative. Tolerates "urgent: ", "⚠️ " (already stripped), "2) ".
_MAX_COMMAND_PREFIX = 20


@dataclass(frozen=True, slots=True)
class Fields:
    """Extracted values, passed to the classifier so classification can depend
    on which fields are actually present."""

    asset: CanonicalAsset | None
    direction: Direction | None
    entry_range: tuple[Decimal, ...]
    take_profits: tuple[Decimal, ...]
    stop_loss: Decimal | None
    leverage: Decimal | None
    percentage: Decimal | None
    has_reply: bool
    unsupported_tokens: tuple[str, ...]


def _line_of(text: str, index: int) -> tuple[str, int]:
    """The line containing `index`, and the offset of the match within it."""
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    if end == -1:
        end = len(text)
    return text[start:end], index - start


def is_imperative(text: str, match) -> bool:
    """True when a matched command reads as an instruction, not narration."""
    if match is None:
        return False
    line, offset = _line_of(text, match.start())
    if offset > _MAX_COMMAND_PREFIX:
        return False
    prefix = line[:offset]
    # Anything hedged or past-tense before the verb disqualifies it.
    if P.PAST_TENSE_RE.search(prefix) or P.HEDGE_RE.search(prefix):
        return False
    return True


def has_commentary_markers(text: str) -> bool:
    return bool(
        P.PAST_TENSE_RE.search(text)
        or P.HEDGE_RE.search(text)
        or P.QUESTION_RE.search(text)
        or P.RESULT_CHATTER_RE.search(text)
    )


def _is_complete_signal(f: Fields) -> bool:
    """Required fields for a new leveraged entry (brief 7).

    Leverage is deliberately NOT required — config supplies a default. Every
    other field is, because guessing an entry, a target or a stop would be
    inventing trade parameters the channel never gave.
    """
    return bool(
        f.asset is not None
        and f.direction is not None
        and f.entry_range
        and f.take_profits
        and f.stop_loss is not None
    )


def missing_signal_fields(f: Fields) -> list[str]:
    missing = []
    if f.asset is None:
        missing.append("asset")
    if f.direction is None:
        missing.append("direction")
    if not f.entry_range:
        missing.append("entry")
    if not f.take_profits:
        missing.append("take_profits")
    if f.stop_loss is None:
        missing.append("stop_loss")
    return missing


def classify(plain: str, deobf: str, f: Fields) -> tuple[MessageType, list[str]]:
    """Return (message_type, reasons).

    `plain` is basic-normalised text; `deobf` is the homoglyph-folded form used
    only for exit vocabulary, which the channel deliberately obfuscates.
    """
    reasons: list[str] = []

    # -- 1. SPOT BOUNDARY (brief 8) -----------------------------------------
    # Checked first and unconditionally. A spot/accumulation message must never
    # fall through into the leveraged path, regardless of what else it holds.
    spot_match = P.SPOT_RE.search(plain)
    if spot_match and f.asset is not None:
        reasons.append(f"spot/investment language: {spot_match.group(0)!r}")
        return MessageType.SPOT_SIGNAL, reasons

    # -- 2. NEW SIGNAL ------------------------------------------------------
    # A signal is recognised by carrying a full parameter set, not by keywords.
    signal_shaped = (
        f.direction is not None
        and (f.entry_range or f.take_profits or f.stop_loss is not None)
    )
    if signal_shaped:
        missing = missing_signal_fields(f)
        if not missing:
            if f.unsupported_tokens:
                reasons.append(
                    f"names unsupported asset(s): {', '.join(f.unsupported_tokens)}"
                )
            return MessageType.NEW_SIGNAL, reasons
        # Signal-shaped but incomplete -> never executable, and the gap is
        # recorded rather than guessed at.
        reasons.append(f"incomplete signal, missing: {', '.join(missing)}")
        return MessageType.UNKNOWN, reasons

    # -- 3. EXIT FAMILY -----------------------------------------------------
    # Matched on the deobfuscated text so Cl0se / EX!T are caught.
    close_match = P.CLOSE_WORD_RE.search(deobf)
    partial_match = P.PARTIAL_CLOSE_RE.search(deobf)

    if partial_match and is_imperative(deobf, partial_match):
        if f.percentage is None:
            reasons.append("partial close without a determinable percentage")
            return MessageType.SAFETY_REVIEW, reasons
        reasons.append(f"imperative partial close: {partial_match.group(0)!r}")
        return MessageType.PARTIAL_CLOSE, reasons

    if close_match:
        if not is_imperative(deobf, close_match):
            # Exit vocabulary in a non-imperative position. If it reads as
            # narration, it is commentary; otherwise it is surfaced for review
            # rather than dropped (brief 22).
            if has_commentary_markers(plain):
                reasons.append("exit vocabulary in past-tense/hedged narration")
                return MessageType.COMMENTARY, reasons
            reasons.append("exit-like wording that is not clearly imperative")
            return MessageType.SAFETY_REVIEW, reasons

        if P.CLOSE_LONGS_RE.search(deobf) or P.CLOSE_SHORTS_RE.search(deobf):
            reasons.append("close by direction")
            return MessageType.CLOSE_DIRECTION, reasons
        if P.CLOSE_ALL_RE.search(deobf):
            reasons.append("explicit close-all")
            return MessageType.CLOSE_ALL, reasons
        if f.asset is not None:
            reasons.append("close scoped to an explicit asset")
            return MessageType.CLOSE_SYMBOL, reasons
        if f.has_reply:
            reasons.append("close scoped by Telegram reply")
            return MessageType.CLOSE_REPLY, reasons

        # Imperative close with no resolvable target. Brief 21: do not guess.
        reasons.append(
            "close instruction with no symbol, no direction, no 'all', and no "
            "reply — target cannot be determined"
        )
        return MessageType.SAFETY_REVIEW, reasons

    # -- 4. UPDATES ---------------------------------------------------------
    sl_update = P.SL_UPDATE_RE.search(plain)
    if sl_update and is_imperative(plain, sl_update):
        if f.stop_loss is None and not P.BREAKEVEN_RE.search(plain):
            reasons.append("stop update without a new level or breakeven")
            return MessageType.SAFETY_REVIEW, reasons
        reasons.append("imperative stop-loss update")
        return MessageType.SL_UPDATE, reasons

    entry_update = P.ENTRY_UPDATE_RE.search(plain)
    if entry_update and is_imperative(plain, entry_update):
        reasons.append("imperative entry update")
        return MessageType.ENTRY_UPDATE, reasons

    # -- 5. ADD / DCA (brief 19) -------------------------------------------
    add_match = P.ADD_IMPERATIVE_RE.search(plain)
    if add_match and is_imperative(plain, add_match) and not has_commentary_markers(plain):
        reasons.append("imperative add instruction")
        return MessageType.ADD_POSITION, reasons
    if P.ADD_RE.search(plain):
        reasons.append("add/DCA language that is not clearly imperative")
        return MessageType.COMMENTARY, reasons

    # -- 6. FALLBACK --------------------------------------------------------
    if has_commentary_markers(plain):
        reasons.append("commentary markers, no executable instruction")
        return MessageType.COMMENTARY, reasons

    if aliases.is_known_other_asset(plain):
        reasons.append("references an asset outside the whitelist")
        return MessageType.UNKNOWN, reasons

    reasons.append("no recognised instruction")
    return MessageType.UNKNOWN, reasons
