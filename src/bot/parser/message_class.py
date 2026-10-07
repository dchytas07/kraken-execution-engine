"""Operational classification of an inbound message, and what to do about it.

`parse_message` answers "what does this text say?" at the parser's resolution
(MessageType). This module answers the operator's question: which of six things
is it, and is the bot allowed to ACT on it?

    NEW_SIGNAL               -> open a trade (through admission)
    SIGNAL_UPDATE            -> amend a stop on the trade it is linked to
    CANCEL_OR_CLOSE          -> close, or cancel an unentered trade
    RESULT_REPORT            -> informational; logged
    CHATTER                  -> logged
    UNPARSEABLE_SIGNAL_LIKE  -> images / odd formats; NO trade, a human is told

The governing rule is the parser's (brief 7, 18, 40): precision over recall.
This was tightened against 17.5k real channel messages, which showed the
parser's close/update detection is far too loose to execute on directly:

  * "We are close to Golden Cross" parsed as CLOSE_SYMBOL for BTC;
  * "Close all 3 #DOGE #PEPE #FLOKI in entry zone" parsed as CLOSE_ALL, which
    would have closed EVERY trade;
  * a fully-formed signal whose last target read "4978 / HODL" parsed as a spot
    message and was silently dropped.

So the parser's verdict is advisory here. Anything that acts must also pass a
strict grammar (`_STRICT_*`): a short, whole-message command with no leftover
words. A message that starts like an instruction but fails the grammar is not
executed and not ignored: it is REVIEW_ALERT. Prose that merely mentions a
command word mid-sentence is CHATTER and stays quiet, or the alert channel
would be noise within a day.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

from ..models import (
    CanonicalAsset,
    Direction,
    MessageAction,
    MessageClass,
    MessageType,
    ParsedMessage,
)
from . import aliases
from .extract import to_decimal
from .normalize import deobfuscate, strip_emoji

# Media with no text that could be a signal rendered as a picture.
_IMAGE_MEDIA = frozenset({"photo", "document"})


@dataclass(frozen=True, slots=True)
class Classification:
    message_class: MessageClass
    action: MessageAction
    reason: str
    # Set only for UPDATE_STOP.
    new_stop: Decimal | None = None
    # How a CLOSE_TRADE is scoped, and the direction filter if one was written.
    close_scope: MessageType | None = None
    asset: CanonicalAsset | None = None
    direction: Direction | None = None


def _c(cls, action, reason, **kw) -> Classification:
    return Classification(cls, action, reason, **kw)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"^\s*[#$]\w[\w/-]*\s*")      # leading "#BTC/USDT" / "$ETH"


def _lines(text: str) -> list[str]:
    """Lower-cased, emoji-free, non-empty lines (newlines preserved, unlike
    basic_normalize, which flattens them)."""
    text = unicodedata.normalize("NFKC", text)
    text = strip_emoji(text).lower()
    # Variation selectors / joiners left behind by emoji ("➡️" is U+27A1 U+FE0F).
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Mn", "Cf"))
    text = re.sub(r"[‐-―−]", "-", text)
    out = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip(" \t*_`>~")
        if line:
            out.append(line)
    return out


def _untag(line: str) -> str:
    """Drop a leading #ASSET/QUOTE tag: '#eth/usdt stopped out' -> 'stopped out'."""
    return _TAG_RE.sub("", line, count=1).strip(" :-")


_EXCHANGES = ("binance", "bingx", "bitget", "bybit", "hyperliquid", "kucoin",
              "okx", "mexc", "gate", "phemex")


def _is_header(line: str) -> bool:
    """The exchange list the tracker bot prefixes to some reports."""
    return "," in line and sum(x in line for x in _EXCHANGES) >= 2


# ---------------------------------------------------------------------------
# RESULT_REPORT
# ---------------------------------------------------------------------------

_RESULT_LINE = re.compile(
    r"^(?:"
    r"take-?\s*profit (?:target )?\d"
    r"|all (?:take-?\s*profit )?targets? achieved"
    r"|\d+ targets? achieved"
    r"|entry \d"
    r"|all entr(?:y|ies)(?: targets?)? achieved"
    r"|stop target hit"
    r"|closed at stop ?loss"
    r"|closed due to opposite direction signal"
    r"|stopped out"
    r"|sl trigger+r?ed"
    r"|then trigger+r?ed"
    r"|tp ?\d$"
    r"|closed (?:at|in|near|after)\b"
    r"|avg entry ?:"
    r"|stop ?loss hit"
    r"|hits? tp ?\d"
    r"|tp ?\d hit"
    r"|both entr(?:y|ies) filled"
    r"|profit ?:"
    r"|loss ?:"
    r"|period ?:"
    r"|average entry price ?:"
    r")"
)


def _result_lines(lines: list[str]) -> tuple[int, list[str]]:
    """(count of result lines, the lines that are neither result nor header)."""
    hits, other = 0, []
    for line in lines:
        bare = _untag(line)
        if _RESULT_LINE.match(bare) or _RESULT_LINE.match(line):
            hits += 1
        elif _is_header(line) or not bare:
            continue                              # header, or a bare #TAG
        else:
            other.append(line)
    return hits, other


# ---------------------------------------------------------------------------
# Instruction detection
# ---------------------------------------------------------------------------

# An exit verb opening a line (after an optional "action to take :" lead-in).
_EXIT_LEAD = re.compile(
    r"^(?:(?:action to take|urgent|update|alert)\s*:?\s*)?"
    r"(?:(?:fully|full|temporarily|temp)\s+)?"
    r"(close|exit|flatten|liquidate|cancel|book|take|secure|trim|scale)\b"
)
_PARTIAL_LEAD = re.compile(
    r"^(?:book|take|secure|trim|scale|sell|close|exit)\b[^\n]{0,24}?\d+\s*%"
    r"|^(?:book|take|close|exit)\s+(?:half|a third|a quarter)\b"
)
_STOP_WORD = re.compile(r"\b(?:sl|stop ?loss|stoploss)\b")
_UPDATE_VERB = re.compile(
    r"\b(?:update|change|move|moving|shift|adjust|raise|lower|trail|revise)\b"
)
_ENTRY_ADJUST = re.compile(
    r"^(?:adjust|update|change|move|revise|new)\b[^\n]{0,20}\bentry\b"
    r"|^entry (?:update|change|adjust)"
    r"|^adjust(?:ed)? to\b"
)
_STOP_MOVED = re.compile(
    r"\b(?:sl|stop ?loss|stoploss)\b[^\n]{0,15}\b"
    r"(?:moved|updated|changed|shifted|raised|lowered|trailed)\b"
)
_HOLD_NOTE = re.compile(
    r"\b(?:tp ?4|target ?4|last tp|last target)\b[^\n]*\bhold\b"
    r"|\bhold\b[^\n]*\b(?:tp ?4|target ?4|last tp|last target)\b"
)
_EXTENDED_TARGETS = re.compile(r"^(?:extended|new|updated) targets?\b")
_ADD_LEAD = re.compile(r"^(?:add|dca|average)\b")

_FILLER = frozenset({"now", "here", "asap", "please", "all", "everything",
                     "position", "positions", "trade", "trades",
                     "long", "longs", "short", "shorts", "in", "profit",
                     "profits"})
_DIRECTIONS = {"long": Direction.LONG, "longs": Direction.LONG,
               "short": Direction.SHORT, "shorts": Direction.SHORT}


def _strict_close(lines: list[str], has_reply: bool) -> tuple[Classification | None, str]:
    """A whole-message close command, or (None, why not).

    One line, verb first, and every remaining word must be an asset, a
    direction, 'all'/'positions', or a plain filler. Any other word ('at',
    'entry', a price, 'near', 'buy zone', a second ticker) means the sender
    attached a condition the bot cannot evaluate, so it must not execute.

    The verb is matched deobfuscated ('CL0SE'); everything after it stays plain,
    because folding would turn '$BTC' into 'sbtc' and a price into letters.
    """
    if len(lines) != 1:
        return None, "multi-line close message"
    line = lines[0]
    chunks = line.split(None, 1)
    if chunks and deobfuscate(chunks[0]) in ("fully", "full") and len(chunks) > 1:
        chunks = chunks[1].split(None, 1)
    if not chunks or deobfuscate(chunks[0]).strip(".!") not in ("close", "exit", "flatten"):
        return None, "not verb-first"
    rest = re.findall(r"[#$]?[a-z0-9]+", (chunks[1] if len(chunks) > 1 else "").replace(".", " "))
    assets: list[CanonicalAsset] = []
    direction = None
    all_flag = False
    for i, w in enumerate(rest):
        base = w.lstrip("#$")
        if base in _DIRECTIONS:
            direction = _DIRECTIONS[base]
        elif base in ("all", "everything"):
            all_flag = True
        elif base == "in":
            if i + 1 >= len(rest) or rest[i + 1] not in ("profit", "profits"):
                return None, "conditional wording after 'in'"
        elif base in _FILLER:
            continue
        elif base in aliases._TOKEN_TO_ASSET:
            a = aliases._TOKEN_TO_ASSET[base]
            if a not in assets:
                assets.append(a)
        else:
            return None, f"unrecognised word {base!r} (condition or second target)"
    if len(assets) > 1:
        return None, "names more than one asset"
    in_profit = "in" in rest
    bulk = not assets and (direction is not None or all_flag)
    if bulk and in_profit:
        return None, "bulk close with 'in profit': may mean only the winners"
    if bulk and has_reply:
        return None, "bulk close sent as a reply: scope is ambiguous"
    if assets:
        return _c(MessageClass.CANCEL_OR_CLOSE, MessageAction.CLOSE_TRADE,
                  "close command scoped to one asset",
                  close_scope=MessageType.CLOSE_SYMBOL, asset=assets[0],
                  direction=direction), ""
    if direction is not None:
        return _c(MessageClass.CANCEL_OR_CLOSE, MessageAction.CLOSE_TRADE,
                  "close command scoped to a direction",
                  close_scope=MessageType.CLOSE_DIRECTION,
                  direction=direction), ""
    if all_flag:
        return _c(MessageClass.CANCEL_OR_CLOSE, MessageAction.CLOSE_TRADE,
                  "explicit close-all", close_scope=MessageType.CLOSE_ALL), ""
    if has_reply:
        return _c(MessageClass.CANCEL_OR_CLOSE, MessageAction.CLOSE_TRADE,
                  "close command scoped by reply",
                  close_scope=MessageType.CLOSE_REPLY), ""
    return None, "no asset, direction, 'all' or reply to scope it"


_NUM = r"\$?(\d[\d,]*(?:\.\d+)?)"
_SL_ALLOWED = frozenset({"temp", "temporarily", "temporary", "update", "updates",
                         "change", "changes", "move", "moves", "shift", "shifts",
                         "adjust", "new", "to", "at", "now", "sl", "stop",
                         "loss", "stoploss", "back"})
# "SL Updated : 91.91" reads as narration; narration never executes (brief 18).
_PAST_STOP = frozenset({"updated", "changed", "moved", "shifted", "raised",
                        "lowered", "trailed", "adjusted"})


def _strict_stop_update(lines: list[str]) -> tuple[Classification | None, str]:
    """'SL Update : 645', 'ETH SL Update to $3600', 'Temp SL Update : 4575'."""
    if len(lines) != 1:
        return None, "multi-line stop update"
    line = lines[0]
    if not _STOP_WORD.search(line):
        return None, "no stop wording"
    nums = re.findall(_NUM, line)
    if len(nums) != 1:
        return None, "needs exactly one price"
    value = to_decimal(nums[0])
    if value is None or value <= 0:
        return None, "unusable price"
    words = re.findall(r"[#$]?[a-z]+", re.sub(_NUM, " ", line))
    asset = None
    asset_token = None
    for w in words:
        base = w.lstrip("#$")
        if base in _PAST_STOP:
            return None, "past-tense stop move (narration, not an instruction)"
        if base in _SL_ALLOWED:
            continue
        if base in aliases._TOKEN_TO_ASSET and asset is None:
            asset = aliases._TOKEN_TO_ASSET[base]
        elif (asset_token is None and base not in aliases._TOKEN_TO_ASSET
              and 2 <= len(base) <= 6):
            asset_token = base                     # a ticker we may not trade
        else:
            return None, f"unrecognised word {base!r}"
    if asset is None and asset_token is not None:
        # "apt shift sl to 14.55": not ours. Recorded, never acted on.
        return _c(MessageClass.SIGNAL_UPDATE, MessageAction.LOG_ONLY,
                  f"stop update for {asset_token!r}, which is not a traded asset"), ""
    if not _UPDATE_VERB.search(line) and not re.search(r"\b(?:sl|stop ?loss|stoploss)\s*(?:to|:|=|at)", line):
        return None, "no update verb"
    return _c(MessageClass.SIGNAL_UPDATE, MessageAction.UPDATE_STOP,
              "stop-loss update with an explicit level", new_stop=value,
              asset=asset), ""


# ---------------------------------------------------------------------------
# Signal shape
# ---------------------------------------------------------------------------

_TEMPLATE_HEAD = re.compile(
    r"^(?:swing\s+|scalp(?:ing)?\s+)?(long|short)\s+(?:swing\s+)?([a-z0-9]+)\b"
)
_TEMPLATE_BODY = re.compile(r"entry\s*:", re.I)


def _template_symbol(lines: list[str]) -> str | None:
    """The ticker token of a templated signal header, whatever it is."""
    if not lines:
        return None
    m = _TEMPLATE_HEAD.match(lines[0])
    if m and any(_TEMPLATE_BODY.search(x) for x in lines[1:3]):
        return m.group(2)
    return None


def _signal_like(plain_lines: list[str]) -> bool:
    """Direction word plus at least two of entry / target / stop labels."""
    blob = " ".join(plain_lines)
    if not re.search(r"\b(?:long|short)\b", blob):
        return False
    labels = [
        bool(re.search(r"\bentry\b[^a-z]{0,12}\d", blob)),
        bool(re.search(r"\b(?:tp ?\d?|targets?|take ?profit)\b[^a-z]{0,12}\d", blob)),
        bool(re.search(r"\b(?:sl|stop ?loss|stoploss)\b[^a-z]{0,12}\d", blob)),
    ]
    return sum(labels) >= 2


def _base_of(symbol_token: str) -> str:
    return re.sub(r"(usdt|usdc|usd|perp)$", "", symbol_token)


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------


def classify_message(
    parsed: ParsedMessage,
    *,
    media: str | None = None,
) -> Classification:
    """Pure: the same inputs always give the same answer. No I/O, no clock."""
    text = parsed.raw_text or ""
    lines = _lines(text)
    has_reply = parsed.referenced_message_id is not None

    # -- 0. no text at all ----------------------------------------------------
    if not lines:
        if media in _IMAGE_MEDIA and not has_reply:
            return _c(MessageClass.UNPARSEABLE_SIGNAL_LIKE,
                      MessageAction.REVIEW_ALERT,
                      f"{media} with no text: a signal could be inside the image")
        return _c(MessageClass.CHATTER, MessageAction.LOG_ONLY,
                  f"no text ({media or 'empty'})")

    # -- 1. result reports ----------------------------------------------------
    hits, other = _result_lines(lines)
    if hits and not other:
        return _c(MessageClass.RESULT_REPORT, MessageAction.LOG_ONLY,
                  "tracker result report")
    if hits and other:
        # A result line sharing a message with free text. If the free text opens
        # with an instruction it must not be swallowed as a report.
        if any(_EXIT_LEAD.match(_untag(deobfuscate(x)))
               or (_STOP_WORD.search(x) and _UPDATE_VERB.search(x))
               for x in other):
            return _c(MessageClass.CANCEL_OR_CLOSE, MessageAction.REVIEW_ALERT,
                      "result report mixed with an instruction")
        return _c(MessageClass.RESULT_REPORT, MessageAction.LOG_ONLY,
                  "result report with commentary")

    # -- 2. cancellation by the tracker --------------------------------------
    if len(lines) == 1 and re.fullmatch(r"manually cancell?ed\.?", _untag(lines[0])):
        return _c(MessageClass.CANCEL_OR_CLOSE, MessageAction.CANCEL_TRADE,
                  "signal manually cancelled by the channel",
                  asset=parsed.asset)

    # -- 3. a new signal ------------------------------------------------------
    token = _template_symbol(lines)
    if parsed.message_type is MessageType.NEW_SIGNAL:
        return _c(MessageClass.NEW_SIGNAL, MessageAction.OPEN_TRADE,
                  "complete signal", asset=parsed.asset,
                  direction=parsed.direction)
    if token is not None:
        base = _base_of(token)
        full = bool(parsed.entry_range and parsed.take_profits
                    and parsed.stop_loss is not None)
        if parsed.message_type is MessageType.SPOT_SIGNAL and full:
            return _c(MessageClass.UNPARSEABLE_SIGNAL_LIKE,
                      MessageAction.REVIEW_ALERT,
                      "complete leveraged template carrying spot wording "
                      "(e.g. 'HODL'): brief 8 forbids trading it, a human "
                      "should look")
        if parsed.asset is None and full:
            known = base in aliases._TOKEN_TO_ASSET or token in aliases._TOKEN_TO_ASSET
            if not known:
                # Well-formed signal for an instrument we do not trade. The
                # common case by far (hundreds of altcoin signals): refused and
                # recorded, not alerted.
                return _c(MessageClass.NEW_SIGNAL, MessageAction.REFUSED,
                          f"signal for {base.upper()}, which is not a traded asset")
        return _c(MessageClass.UNPARSEABLE_SIGNAL_LIKE,
                  MessageAction.REVIEW_ALERT,
                  "signal template that did not parse completely: "
                  + ("; ".join(parsed.parse_errors) or "missing fields"))

    # -- 4. spot talk is out of scope by design (brief 8) ---------------------
    if parsed.message_type is MessageType.SPOT_SIGNAL:
        if _signal_like(lines):
            return _c(MessageClass.UNPARSEABLE_SIGNAL_LIKE,
                      MessageAction.REVIEW_ALERT,
                      "signal-like text with spot wording; not traded")
        return _c(MessageClass.CHATTER, MessageAction.LOG_ONLY,
                  "spot/accumulation talk (out of scope)")

    # -- 5. instructions ------------------------------------------------------
    first = _untag(lines[0])
    # Exit verbs are matched on the deobfuscated form: the channel has written
    # 'CL0SE' and 'EX!T' to dodge automated readers (2020).
    dlines = [_untag(deobfuscate(x)) for x in lines]
    any_exit_lead = any(_EXIT_LEAD.match(x) for x in dlines)

    if any(_PARTIAL_LEAD.match(x) for x in dlines):
        return _c(MessageClass.CANCEL_OR_CLOSE, MessageAction.REVIEW_ALERT,
                  "partial close: never automated, a human decides")

    if any_exit_lead:
        verb_first = re.match(r"^(?:(?:fully|full)\s+)?(close|exit|flatten)\b", dlines[0])
        if verb_first:
            strict, why = _strict_close(
                [first] if len(lines) == 1 else lines, has_reply)
            if strict is not None:
                return strict
            return _c(MessageClass.CANCEL_OR_CLOSE, MessageAction.REVIEW_ALERT,
                      f"close-like instruction not safe to execute: {why}")
        return _c(MessageClass.CANCEL_OR_CLOSE, MessageAction.REVIEW_ALERT,
                  "exit-like instruction not safe to execute")

    stop_lines = [x for x in lines if _STOP_WORD.search(x) and _UPDATE_VERB.search(x)]
    sl_cmd = re.match(r"^(?:temp\w*\s+)?(?:\w+\s+)?(?:sl|stop ?loss|stoploss)\s*(?:update|change|to|:)", first)
    if stop_lines or sl_cmd or re.match(r"^(?:👆|☝)?you can move (?:sl|stop)", first):
        strict, why = _strict_stop_update(lines)
        if strict is not None:
            return strict
        return _c(MessageClass.SIGNAL_UPDATE, MessageAction.REVIEW_ALERT,
                  f"stop-loss instruction not safe to execute: {why}")

    if any(_STOP_MOVED.search(x) for x in lines) and len(lines) <= 3:
        return _c(MessageClass.SIGNAL_UPDATE, MessageAction.REVIEW_ALERT,
                  "the channel says it moved its stop: not followed automatically")

    if any(_ENTRY_ADJUST.match(_untag(x)) for x in lines):
        return _c(MessageClass.SIGNAL_UPDATE, MessageAction.REVIEW_ALERT,
                  "entry adjustment: not automated")

    if any(_HOLD_NOTE.search(x) for x in lines) and len(lines) <= 3:
        return _c(MessageClass.SIGNAL_UPDATE, MessageAction.REVIEW_ALERT,
                  "TP4 / last-target HOLD note: not automated")
    if any(_EXTENDED_TARGETS.match(_untag(x)) for x in lines):
        return _c(MessageClass.SIGNAL_UPDATE, MessageAction.REVIEW_ALERT,
                  "target change: not automated")
    if any(_ADD_LEAD.match(_untag(x)) for x in lines) and \
            parsed.message_type is MessageType.ADD_POSITION:
        return _c(MessageClass.SIGNAL_UPDATE, MessageAction.REVIEW_ALERT,
                  "add-to-position instruction: not automated")

    # -- 6. signal-like but not a signal ---------------------------------------
    if _signal_like(lines):
        return _c(MessageClass.UNPARSEABLE_SIGNAL_LIKE,
                  MessageAction.REVIEW_ALERT,
                  "reads like a trade setup but is not in the signal format")

    return _c(MessageClass.CHATTER, MessageAction.LOG_ONLY, "no instruction")
