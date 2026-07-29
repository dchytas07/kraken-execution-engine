"""Text normalisation, including deliberate-obfuscation folding.

The channel obfuscates instruction words (`Cl0se`, `EX!T`) to evade automated
readers. We fold a *bounded* set of homoglyph substitutions so those variants
are recognised — bounded because aggressive folding is how a parser starts
turning commentary into trades.

Folding is used ONLY for keyword matching. Numbers are always extracted from
the original text, never the folded form, so that `EX!T` folding cannot alter a
price.
"""

from __future__ import annotations

import re
import unicodedata

# Homoglyph -> letter. Deliberately narrow: only substitutions actually seen in
# obfuscated trading channels. Note '1' -> 'i' and '0' -> 'o' are applied only
# inside the keyword matcher, never to numeric extraction.
_HOMOGLYPHS = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "@": "a",
        "$": "s",
        "!": "i",
        "|": "i",
        "€": "e",
    }
)

_ZERO_WIDTH = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0xFEFF, 0x2060, 0x00AD], None
)


def strip_emoji(text: str) -> str:
    """Remove pictographic characters, keeping their surrounding text."""
    return "".join(
        ch for ch in text if unicodedata.category(ch) not in ("So", "Sk", "Cs")
    )


def basic_normalize(text: str) -> str:
    """Canonical form for general matching: NFKC, zero-width stripped,
    lowercased, whitespace collapsed. Digits are NOT touched."""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_ZERO_WIDTH)
    text = strip_emoji(text)
    text = text.lower()
    # Normalise unicode dashes to ASCII hyphen so entry ranges parse uniformly.
    text = re.sub(r"[‐-―−]", "-", text)
    return re.sub(r"\s+", " ", text).strip()


_WORDISH = re.compile(r"[^\s]+")


def _fold_token(token: str) -> str:
    """Fold a single token, but only if it contains a letter.

    This is what keeps numbers intact. `b00k` contains letters, so it folds to
    `book`; `50` is pure digits, so it survives untouched. Folding everything
    would rewrite `50%` into `so%` and silently destroy the percentage in a
    partial-close instruction.
    """
    if any(ch.isalpha() for ch in token):
        return token.translate(_HOMOGLYPHS)
    return token


def deobfuscate(text: str) -> str:
    """Fold for KEYWORD MATCHING ONLY.

    Applies homoglyph substitution and strips interior punctuation, so `Cl0se`,
    `C.L.O.S.E` and `EX!T` all reduce to their plain forms.

    Digits inside purely numeric tokens are preserved (see `_fold_token`), but
    prices should still be extracted from the plain form — a token like `1.5x`
    contains a letter and would be folded.
    """
    text = basic_normalize(text)
    text = _WORDISH.sub(lambda m: _fold_token(m.group(0)), text)
    # Collapse separators used to break up words: c.l.o.s.e -> close
    text = re.sub(r"(?<=\w)[.\-_*'’]+(?=\w)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def keyword_forms(text: str) -> tuple[str, str]:
    """Both views of the text: (plain, deobfuscated).

    A keyword matcher should check the plain form first and only fall back to
    the folded form for words it explicitly expects to be obfuscated. Matching
    everything against the folded form is how false positives creep in.
    """
    return basic_normalize(text), deobfuscate(text)
