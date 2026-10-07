"""Parser tests (brief 33).

Organised around the guarantee each group protects. The false-positive groups
matter most: a missed signal costs an opportunity, a false positive costs a
real unintended trade.
"""

from decimal import Decimal

import pytest

from bot.models import CanonicalAsset, Direction, MessageType
from bot.parser.parse import fingerprint_for, parse_message

SIGNAL = """
🟢 BTC LONG
Entry: 64000 - 63500
TP1: 65000
TP2: 66000
TP3: 67000
TP4: 68000
SL: 62500
Leverage: 10x
"""


def p(text, **kw):
    return parse_message(channel_id=-100, message_id=1, text=text, **kw)


# ==========================================================================
# Valid signals
# ==========================================================================


def test_valid_long_signal():
    r = p(SIGNAL)
    assert r.message_type is MessageType.NEW_SIGNAL
    assert r.asset is CanonicalAsset.BTC
    assert r.direction is Direction.LONG
    assert r.entry_range == (Decimal("64000"), Decimal("63500"))
    assert r.take_profits == (
        Decimal("65000"), Decimal("66000"), Decimal("67000"), Decimal("68000")
    )
    assert r.stop_loss == Decimal("62500")
    assert r.leverage == Decimal("10")
    assert not r.parse_errors


def test_valid_short_signal():
    r = p("""
    🔴 ETH SHORT
    Entry 1920 - 1935
    TP1 1900 TP2 1880 TP3 1860 TP4 1840
    SL 1960
    20x
    """)
    assert r.message_type is MessageType.NEW_SIGNAL
    assert r.asset is CanonicalAsset.ETH
    assert r.direction is Direction.SHORT
    assert r.stop_loss == Decimal("1960")
    assert r.leverage == Decimal("20")  # capping happens in validation, not here


def test_alternate_formatting_and_aliases():
    r = p("""
    SOLANA / buy
    entry zone: 73.50/72.80
    take profit 1: 75.00
    take profit 2: 76.50
    take profit 3: 78.00
    take profit 4: 80.00
    stop-loss: 71.00
    lev 5
    """)
    assert r.message_type is MessageType.NEW_SIGNAL
    assert r.asset is CanonicalAsset.SOL
    assert r.direction is Direction.LONG


@pytest.mark.parametrize(
    "alias,expected",
    [
        ("BTCUSDT", CanonicalAsset.BTC),
        ("BTC/USD", CanonicalAsset.BTC),
        ("Bitcoin", CanonicalAsset.BTC),
        ("XBT", CanonicalAsset.BTC),
        ("ETHUSDT", CanonicalAsset.ETH),
        ("Ethereum", CanonicalAsset.ETH),
        ("SOL/USD", CanonicalAsset.SOL),
        ("Solana", CanonicalAsset.SOL),
        ("GOLD", CanonicalAsset.XAU),
        ("XAU/USD", CanonicalAsset.XAU),
    ],
)
def test_asset_aliases_resolve(alias, expected):
    r = p(f"{alias} LONG entry 100 - 99 TP1 101 TP2 102 TP3 103 TP4 104 SL 98")
    assert r.asset is expected


def test_commas_in_prices_parsed():
    r = p("""BTC LONG entry: 100,000 - 99,500
           TP1 101,000 TP2 102,000 TP3 103,000 TP4 104,000 SL 98,000 10x""")
    assert r.entry_range == (Decimal("100000"), Decimal("99500"))
    assert r.stop_loss == Decimal("98000")


def test_emoji_and_whitespace_tolerated():
    r = p("🚀🚀  BTC   LONG 🟢\n\n  Entry:  64000-63500\n"
          "TP1 65000\nTP2 66000\nTP3 67000\nTP4 68000\n  SL:  62500  10x")
    assert r.message_type is MessageType.NEW_SIGNAL
    assert r.asset is CanonicalAsset.BTC


# ==========================================================================
# Incomplete signals — must never be executable
# ==========================================================================


@pytest.mark.parametrize(
    "text,missing",
    [
        ("BTC LONG entry 64000-63500 SL 62500 10x", "take_profits"),
        ("BTC LONG entry 64000-63500 TP1 65000 TP2 66000 10x", "stop_loss"),
        ("BTC LONG TP1 65000 TP2 66000 SL 62500 10x", "entry"),
    ],
)
def test_incomplete_signal_is_not_executable(text, missing):
    r = p(text)
    assert r.message_type is not MessageType.NEW_SIGNAL
    assert not r.message_type.is_executable_candidate
    assert any(missing in e for e in r.parse_errors)
    assert r.confidence == 0.0


def test_signal_without_direction_is_not_executable():
    r = p("BTC entry 64000-63500 TP1 65000 TP2 66000 TP3 67000 TP4 68000 SL 62500")
    assert not r.message_type.is_executable_candidate


def test_both_directions_is_ambiguous_not_guessed():
    """A message naming both must not resolve to whichever matched first."""
    r = p("BTC long or short? entry 64000 TP1 65000 SL 62500")
    assert r.direction is None
    assert not r.message_type.is_executable_candidate


def test_two_assets_is_ambiguous():
    r = p("BTC and ETH LONG entry 64000-63500 TP1 65000 TP2 66000 "
          "TP3 67000 TP4 68000 SL 62500")
    assert r.asset is None
    assert not r.message_type.is_executable_candidate


# ==========================================================================
# Spot boundary (brief 8) — the hard safety line
# ==========================================================================


@pytest.mark.parametrize(
    "text",
    [
        "BTC spot buy at 64000, long term hold",
        "Accumulating ETH here for the long term",
        "SOL spot accumulation zone 70-73, buy and hold",
        "Adding BTC to the portfolio allocation, no leverage",
        "HODL BTC from 64000",
    ],
)
def test_spot_signals_never_become_leveraged(text):
    r = p(text)
    assert r.message_type is MessageType.SPOT_SIGNAL
    assert not r.message_type.is_executable_candidate


def test_spot_wins_even_with_full_signal_parameters():
    """The spot boundary is checked before signal completeness, so a spot
    message carrying every leveraged field still cannot execute."""
    r = p("""
    BTC SPOT buy and hold
    Entry: 64000 - 63500
    TP1: 65000 TP2: 66000 TP3: 67000 TP4: 68000
    SL: 62500
    10x
    """)
    assert r.message_type is MessageType.SPOT_SIGNAL
    assert not r.message_type.is_executable_candidate


# ==========================================================================
# Close instructions
# ==========================================================================


def test_close_all():
    r = p("Close all positions")
    assert r.message_type is MessageType.CLOSE_ALL


def test_close_all_longs():
    r = p("Close all longs")
    assert r.message_type is MessageType.CLOSE_DIRECTION
    assert r.direction is Direction.LONG


def test_close_all_shorts():
    r = p("Exit all shorts now")
    assert r.message_type is MessageType.CLOSE_DIRECTION
    assert r.direction is Direction.SHORT


def test_close_specific_symbol():
    r = p("Close BTC")
    assert r.message_type is MessageType.CLOSE_SYMBOL
    assert r.asset is CanonicalAsset.BTC


def test_close_by_reply():
    r = p("close this one", reply_to_message_id=555)
    assert r.message_type is MessageType.CLOSE_REPLY
    assert r.referenced_message_id == 555


@pytest.mark.parametrize("text", ["Cl0se all", "CL0SE ALL", "Cl0se BTC"])
def test_obfuscated_close_recognised(text):
    r = p(text)
    assert r.message_type in (MessageType.CLOSE_ALL, MessageType.CLOSE_SYMBOL)


@pytest.mark.parametrize("text", ["EX!T all positions", "3X!T all", "e-x-i-t all"])
def test_obfuscated_exit_recognised(text):
    r = p(text)
    assert r.message_type is MessageType.CLOSE_ALL


def test_ambiguous_close_refuses_to_guess():
    """Brief 21: no symbol, no direction, no 'all', no reply -> never guess."""
    r = p("close it")
    assert r.message_type is MessageType.SAFETY_REVIEW
    assert not r.message_type.is_executable_candidate
    assert any("cannot be determined" in w.detail for w in r.parse_warnings)


def test_partial_close_imperative():
    r = p("Book 50%")
    assert r.message_type is MessageType.PARTIAL_CLOSE
    assert r.percentage == Decimal("50")


def test_partial_close_half():
    r = p("Close half")
    assert r.message_type is MessageType.PARTIAL_CLOSE
    assert r.percentage == Decimal("50")


def test_partial_close_without_percentage_goes_to_review():
    r = p("Book some profit")
    assert r.message_type is not MessageType.PARTIAL_CLOSE
    assert not r.message_type.is_executable_candidate


# ==========================================================================
# FALSE POSITIVES — the group that matters most (brief 18)
# ==========================================================================


@pytest.mark.parametrize(
    "text",
    [
        "Booked 50% profit on BTC earlier today 🎉",
        "We booked 50% here",
        "Nice, TP1 hit and we booked some",
        "I have booked 25% already",
        "booked profits on the last one",
    ],
)
def test_commentary_containing_booked_does_not_execute(text):
    r = p(text)
    assert not r.message_type.is_executable_candidate, r.message_type


@pytest.mark.parametrize(
    "text",
    [
        "Added to my BTC position earlier",
        "We added here yesterday",
        "I added 20% more",
        "Thinking about adding here",
        "Looking to add on a dip",
        "might add more later",
    ],
)
def test_commentary_containing_added_does_not_execute(text):
    r = p(text)
    assert not r.message_type.is_executable_candidate, r.message_type


@pytest.mark.parametrize(
    "text",
    [
        "We closed all positions earlier",
        "Closed BTC at a profit",
        "That trade was closed yesterday",
        "Should we close all?",
        "We might close all soon",
        "if it breaks down we close all",
        "Everyone who exited earlier did well",
    ],
)
def test_commentary_containing_close_does_not_execute(text):
    r = p(text)
    assert not r.message_type.is_executable_candidate, (text, r.message_type)


@pytest.mark.parametrize(
    "text",
    [
        "BTC looking strong here, could push higher",
        "Great session everyone, big gains 🎉",
        "Market is choppy today",
        "gm",
        "What do you think about ETH?",
    ],
)
def test_pure_commentary_is_not_executable(text):
    r = p(text)
    assert not r.message_type.is_executable_candidate, r.message_type


def test_take_some_is_not_an_instruction():
    assert not p("take some profit off the table maybe").message_type.is_executable_candidate


def test_secured_profit_is_not_an_instruction():
    assert not p("secured profit on that one").message_type.is_executable_candidate


# ==========================================================================
# Updates
# ==========================================================================


def test_sl_update():
    r = p("Move SL to 63000")
    assert r.message_type is MessageType.SL_UPDATE
    assert r.stop_loss == Decimal("63000")


def test_sl_update_to_breakeven():
    r = p("Move stop loss to breakeven")
    assert r.message_type is MessageType.SL_UPDATE


def test_sl_update_without_level_goes_to_review():
    r = p("Update your stop")
    assert r.message_type is MessageType.SAFETY_REVIEW


def test_entry_update():
    r = p("Update entry to 63800")
    assert r.message_type is MessageType.ENTRY_UPDATE


def test_commentary_about_stops_does_not_update():
    assert not p("I moved my stop earlier").message_type.is_executable_candidate


# ==========================================================================
# Add / DCA (brief 19)
# ==========================================================================


def test_explicit_add_is_recognised():
    r = p("Add 25% BTC")
    assert r.message_type is MessageType.ADD_POSITION


def test_vague_add_is_not_executable():
    assert not p("adding here").message_type.is_executable_candidate


# ==========================================================================
# Unsupported assets (brief 4)
# ==========================================================================


def test_unsupported_asset_is_recorded_not_silently_dropped():
    r = p("ADA LONG entry 2.10-2.05 TP1 2.20 TP2 2.30 TP3 2.40 TP4 2.50 SL 2.00")
    assert r.asset is None
    assert not r.message_type.is_executable_candidate
    assert any("unsupported" in w.code or "ADA" in w.detail.upper()
               for w in r.parse_warnings)


def test_silver_is_not_confused_with_gold():
    """PF_XAGUSD exists on Kraken but is not whitelisted."""
    r = p("XAG LONG entry 30-29 TP1 31 TP2 32 TP3 33 TP4 34 SL 28")
    assert r.asset is not CanonicalAsset.XAU
    assert not r.message_type.is_executable_candidate


# ==========================================================================
# Substring false positives
# ==========================================================================


@pytest.mark.parametrize(
    "text,label",
    [
        ("SOLD everything last week", "SOLD contains SOL"),
        ("the ETHOS of this channel", "ETHOS contains ETH"),
    ],
)
def test_substring_matches_do_not_resolve_assets(text, label):
    r = p(text)
    assert r.asset is None, label


# ==========================================================================
# Duplicate detection (brief 23)
# ==========================================================================


def test_identical_signals_share_a_fingerprint():
    assert fingerprint_for(p(SIGNAL)) == fingerprint_for(p(SIGNAL))


def test_reformatted_duplicate_still_collides():
    """Text hashing alone would miss this — the brief warns about exactly it."""
    reformatted = """
    🟢🟢 btc   long!!
    entry:  64000 - 63500
    tp1 65000
    tp2 66000
    tp3 67000
    tp4 68000
    sl:62500
    leverage 10x
    """
    assert fingerprint_for(p(SIGNAL)) == fingerprint_for(p(reformatted))


def test_different_levels_do_not_collide():
    """A genuinely new signal must not be suppressed as a duplicate."""
    other = SIGNAL.replace("SL: 62500", "SL: 62000")
    assert fingerprint_for(p(SIGNAL)) != fingerprint_for(p(other))


def test_different_direction_does_not_collide():
    assert fingerprint_for(p(SIGNAL)) != fingerprint_for(
        p(SIGNAL.replace("LONG", "SHORT"))
    )


def test_fingerprint_none_without_identity_fields():
    assert fingerprint_for(p("gm")) is None


# ==========================================================================
# Determinism
# ==========================================================================


def test_parser_is_deterministic():
    a, b = p(SIGNAL), p(SIGNAL)
    assert (a.message_type, a.asset, a.entry_range, a.take_profits, a.stop_loss) == (
        b.message_type, b.asset, b.entry_range, b.take_profits, b.stop_loss
    )


def test_identity_is_channel_and_message_id():
    r = parse_message(channel_id=-100777, message_id=42, text="gm")
    assert r.identity == (-100777, 42)


def test_raw_text_is_preserved_verbatim():
    """The audit trail must hold what actually arrived."""
    assert p(SIGNAL).raw_text == SIGNAL


# -- BNB promotion (2026-07-31) --------------------------------------------


@pytest.mark.parametrize("alias", ["BNB/USDT", "BNB", "BNBUSDT", "Binancecoin"])
def test_bnb_aliases_resolve(alias):
    r = p(f"{alias} LONG entry 600 - 590 TP1 610 TP2 620 TP3 630 TP4 640 SL 580")
    assert r.asset is CanonicalAsset.BNB


def test_bnb_is_no_longer_reported_as_an_unsupported_asset():
    """Regression: `bnb` used to sit in _KNOWN_OTHER_ASSETS. Leaving it in
    both maps would make the verdict depend on lookup order."""
    from bot.parser.aliases import resolve_asset
    asset, unsupported = resolve_asset("long bnbusdt here")
    assert asset is CanonicalAsset.BNB
    assert "bnb" not in unsupported


def test_no_token_is_both_supported_and_unsupported():
    """The import-time guard in aliases.py, asserted explicitly so the rule
    is visible in the suite and not only at import."""
    from bot.parser.aliases import _KNOWN_OTHER_ASSETS, _TOKEN_TO_ASSET
    assert set(_TOKEN_TO_ASSET) & _KNOWN_OTHER_ASSETS == set()


def test_a_real_bnb_signal_parses_as_a_new_signal():
    """The exact shape the channel posts — previously UNKNOWN at confidence
    0.0 because BNB was unsupported (docs/MESSAGE_REVIEW.md)."""
    r = parse_message(channel_id=-1, message_id=1, text=(
        "\u27a1\ufe0f SHORT BNBUSDT\n\n\u2747\ufe0f Entry: 600.00 - 610.00\n\n"
        "\u2611\ufe0f Target 1: 590.00\n\u2611\ufe0f Target 2: 580.00\n"
        "\u2611\ufe0f Target 3: 570.00\n\u2611\ufe0f Target 4: 560.00\n\n"
        "\u26d4 Stoploss: 620.00\n\n\U0001f4ab Leverage : 20x"
    ))
    assert r.message_type is MessageType.NEW_SIGNAL
    assert r.asset is CanonicalAsset.BNB
    assert r.direction is Direction.SHORT
    assert r.confidence == 1.0


# -- XRP / DOGE promotion (2026-07-31) --------------------------------------


@pytest.mark.parametrize("alias,expected", [
    ("XRP", CanonicalAsset.XRP), ("XRP/USDT", CanonicalAsset.XRP),
    ("Ripple", CanonicalAsset.XRP), ("DOGE", CanonicalAsset.DOGE),
    ("DOGE/USDT", CanonicalAsset.DOGE), ("Dogecoin", CanonicalAsset.DOGE),
])
def test_xrp_doge_aliases_resolve(alias, expected):
    r = p(f"{alias} LONG entry 100 - 99 TP1 101 TP2 102 TP3 103 TP4 104 SL 98")
    assert r.asset is expected


def test_xrp_and_doge_no_longer_reported_unsupported():
    from bot.parser.aliases import resolve_asset
    for text, expected in (("long xrpusdt", CanonicalAsset.XRP),
                           ("short dogeusdt", CanonicalAsset.DOGE)):
        asset, unsupported = resolve_asset(text)
        assert asset is expected
        assert not unsupported


def test_fingerprint_ignores_trailing_zero_padding():
    """REGRESSION: the channel pads prices ('68000.00000000') and sometimes
    drops the padding on an edit ('68000'). The fingerprint hashed str(Decimal),
    so the same signal escaped duplicate detection (brief 23)."""
    from bot.parser.parse import fingerprint_for, parse_message

    padded = ("➡️ LONG BTCUSDT \n\n❇️ Entry: 64000.00000000 - 63500.00000000\n\n"
              "☑️ Target 1: 65000.00000000\n\n☑️ Target 2: 66000.00000000\n\n"
              "☑️ Target 3: 67000.00000000\n\n☑️ Target 4: 68000.00000000\n\n"
              "⛔ Stoploss: 62500.00000000\n\n💫 Leverage : 10x")
    bare = (padded.replace("64000.00000000", "64000").replace("68000.00000000", "68000")
            .replace("62500.00000000", "62500"))
    a = parse_message(channel_id=1, message_id=1, text=padded)
    b = parse_message(channel_id=1, message_id=2, text=bare)
    assert fingerprint_for(a) == fingerprint_for(b)
    c = parse_message(channel_id=1, message_id=3,
                      text=padded.replace("62500.00000000", "62400.00000000"))
    assert fingerprint_for(a) != fingerprint_for(c), "a different stop is a different signal"
