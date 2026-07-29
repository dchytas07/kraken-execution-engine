"""Regex vocabulary for the parser.

Kept in one place so the matching surface is auditable. Every pattern here is a
potential false-positive source, so each is anchored or word-bounded rather
than a bare substring.
"""

from __future__ import annotations

import re

# A price: 100000, 100,000, 1.2345, 0.0001
NUMBER = r"\d[\d,]*(?:\.\d+)?"
_N = NUMBER

# -- direction --------------------------------------------------------------
# Plural forms matter: "close all longs" must yield a direction.
LONG_RE = re.compile(r"\b(longs?|buy|bullish)\b")
SHORT_RE = re.compile(r"\b(shorts?|sell|bearish)\b")

# -- labelled fields --------------------------------------------------------
# Entry range: "entry: 100000 - 101000", "entry zone 100000/101000"
ENTRY_RANGE_RE = re.compile(
    rf"\bentry\b[^\n\d]{{0,20}}({_N})\s*(?:-|to|/|–)\s*({_N})"
)
ENTRY_SINGLE_RE = re.compile(rf"\bentry\b[^\n\d]{{0,20}}({_N})")

# TP1..TP4 / target 1..4. Captures the index so ordering is explicit rather
# than positional.
TP_NUMBERED_RE = re.compile(
    rf"\b(?:tp|target|take\s*profit)\s*#?\s*([1-4])\b[^\n\d]{{0,15}}({_N})"
)
# Bare "TP: a, b, c, d" listing
TP_LIST_RE = re.compile(
    rf"\b(?:tp|targets?|take\s*profits?)\b\s*:?\s*"
    rf"({_N}(?:\s*[,/|-]\s*{_N}){{1,5}})"
)

SL_RE = re.compile(
    rf"\b(?:sl|stop\s*-?\s*loss|stoploss|stop)\b[^\n\d]{{0,20}}({_N})"
)

LEVERAGE_RE = re.compile(rf"\b(?:lev(?:erage)?\s*:?\s*)?({_N})\s*x\b")
LEVERAGE_LABELLED_RE = re.compile(rf"\blev(?:erage)?\b\s*:?\s*({_N})")

PERCENT_RE = re.compile(rf"({_N})\s*%")

# -- spot / investment language (brief 8) -----------------------------------
# A hard classification boundary, not a keyword veto. Matching any of these in
# a message that otherwise looks like a signal makes it SPOT_SIGNAL.
SPOT_RE = re.compile(
    r"\b(spot|spot\s*buy|buy\s*and\s*hold|hodl|accumulat(?:e|ing|ion)|"
    r"long[\s-]*term|investment|portfolio\s*allocation|dca\s*(?:into|zone)|"
    r"cold\s*storage|no\s*leverage|unleveraged)\b"
)

# -- exit vocabulary --------------------------------------------------------
# Matched against the DEOBFUSCATED text so Cl0se / EX!T are caught.
CLOSE_WORD_RE = re.compile(r"\b(close|exit|flatten|liquidate|dump)\b")

CLOSE_ALL_RE = re.compile(
    r"\b(close|exit|flatten)\s+(?:all|everything|every\s+position|all\s+positions)\b"
    r"|\b(?:all\s+out|full\s+exit|close\s+out\s+all)\b"
)
CLOSE_LONGS_RE = re.compile(r"\b(?:close|exit|flatten)\s+(?:all\s+)?longs?\b")
CLOSE_SHORTS_RE = re.compile(r"\b(?:close|exit|flatten)\s+(?:all\s+)?shorts?\b")

# -- partial close ----------------------------------------------------------
# Imperative only: "book 50%", "take 25% off", "close half".
PARTIAL_CLOSE_RE = re.compile(
    rf"\b(?:book|take|secure|close|exit|sell|trim|scale\s*out)\s+"
    rf"(?:profits?\s+)?(?:on\s+)?({_N})\s*%"
    rf"|\b(?:book|take|close|exit)\s+(half|a\s+third|a\s+quarter)\b"
)

# -- updates ----------------------------------------------------------------
SL_UPDATE_RE = re.compile(
    r"\b(?:move|moving|shift|update|change|adjust|raise|lower|trail)\b"
    r"[^\n]{0,25}\b(?:sl|stop\s*-?\s*loss|stoploss|stop)\b"
    r"|\b(?:sl|stop)\b\s*(?:to|->|=)\s*"
)
BREAKEVEN_RE = re.compile(r"\b(?:break\s*even|breakeven|be)\b")

ENTRY_UPDATE_RE = re.compile(
    r"\b(?:move|moving|update|change|adjust|revise)\b[^\n]{0,25}\bentry\b"
    r"|\bnew\s+entry\b"
)

# -- add / DCA --------------------------------------------------------------
ADD_RE = re.compile(
    r"\b(?:add|adding|dca|average\s+(?:in|down))\b"
)
# Imperative add: starts the clause, or has an explicit size.
ADD_IMPERATIVE_RE = re.compile(
    rf"^(?:add|dca)\b|\b(?:add|dca)\s+({_N})\s*%"
)

# -- commentary markers (false-positive defence, brief 18) ------------------
# Past tense / narration: the channel talks ABOUT trades constantly.
PAST_TENSE_RE = re.compile(
    r"\b(?:booked|closed|exited|added|took|secured|banked|"
    r"filled|hit|reached|tagged|smashed|nailed|"
    r"was|were|had|has\s+been|have\s+been|did|went)\b"
)
# Hedging / conditional / hypothetical
HEDGE_RE = re.compile(
    r"\b(?:might|maybe|perhaps|could|would|should\s+we|thinking|considering|"
    r"looking\s+to|planning|if\s+|when\s+|watch(?:ing)?\s+for|expect|"
    r"probably|likely|possibly|potential(?:ly)?|hopefully|let'?s\s+see)\b"
)
QUESTION_RE = re.compile(r"\?")

# Congratulatory / results chatter
RESULT_CHATTER_RE = re.compile(
    r"\b(?:profit|pnl|p&l|gains?|winner|wins?|congrats|congratulations|"
    r"well\s+done|easy\s+money|another\s+one|target\s+hit|tp\d?\s+hit)\b"
)
