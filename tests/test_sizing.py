"""Sizing tests (brief 33).

Specs come from the real captured instrument fixture, so the arithmetic is
exercised against Kraken's actual increments rather than invented ones.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from bot.exchange.specs import parse_instrument
from bot.models import CanonicalAsset
from bot.sizing import (
    SizingError,
    calculate_size,
    check_sufficient_margin,
    effective_leverage,
    estimated_liquidation_price,
    is_entry_stale,
    liquidation_distance,
    max_leverage_for_stop,
    round_price_to_tick,
    round_to_increment,
    split_entry_allocations,
    split_take_profits,
    validate_size,
)

FIXTURES = Path(__file__).parent / "fixtures"
RAW = {
    i["symbol"]: i
    for i in json.loads((FIXTURES / "futures_instruments.json").read_text())["instruments"]
}

BTC = parse_instrument(RAW["PF_XBTUSD"], CanonicalAsset.BTC)   # incr 0.0001
ETH = parse_instrument(RAW["PF_ETHUSD"], CanonicalAsset.ETH)   # incr 0.001
SOL = parse_instrument(RAW["PF_SOLUSD"], CanonicalAsset.SOL)   # incr 0.01
BNB = parse_instrument(RAW["PF_BNBUSD"], CanonicalAsset.BNB)   # incr 0.01
XRP = parse_instrument(RAW["PF_XRPUSD"], CanonicalAsset.XRP)   # incr 1
DOGE = parse_instrument(RAW["PF_DOGEUSD"], CanonicalAsset.DOGE) # incr 1
XAUT = parse_instrument(RAW["PF_XAUTUSD"], CanonicalAsset.XAU)  # incr 0.001

D = Decimal
TOL = D("5.0")


# ==========================================================================
# Leverage cap (brief 9)
# ==========================================================================


@pytest.mark.parametrize("requested", ["20", "25", "50", "100"])
def test_leverage_capped_at_internal_maximum(requested):
    assert effective_leverage(
        D(requested), max_leverage=D(10), default_leverage=D(10),
        exchange_max_leverage=D(100),
    ) == D(10)


def test_lower_signal_leverage_respected():
    assert effective_leverage(
        D(3), max_leverage=D(10), default_leverage=D(10),
        exchange_max_leverage=D(100),
    ) == D(3)


def test_exchange_maximum_wins_when_lower():
    assert effective_leverage(
        D(10), max_leverage=D(10), default_leverage=D(10),
        exchange_max_leverage=D(5),
    ) == D(5)


def test_default_used_when_signal_omits_leverage():
    assert effective_leverage(
        None, max_leverage=D(10), default_leverage=D(7),
        exchange_max_leverage=D(100),
    ) == D(7)


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_non_positive_leverage_rejected(bad):
    with pytest.raises(SizingError, match="LEVERAGE_INVALID"):
        effective_leverage(D(bad), max_leverage=D(10), default_leverage=D(10),
                           exchange_max_leverage=D(100))


# ==========================================================================
# Core sizing
# ==========================================================================


def test_btc_default_allocation():
    """$30 margin at 10x = $300 notional. The brief's worked example."""
    r = calculate_size(margin_usd=D(30), leverage=D(10), price=D(64598),
                       spec=BTC, max_deviation_percent=TOL)
    assert r.intended_notional == D(300)
    # 300 / 64598 = 0.004643... -> rounds DOWN to 0.0046
    assert r.size == D("0.0046")
    assert r.notional == D("0.0046") * D(64598)
    assert r.notional < r.intended_notional  # never overshoots


def test_split_entry_sizing_matches_brief_example():
    """$30 total -> $15 per entry, sized independently at each entry price."""
    allocations = split_entry_allocations(D(30))
    assert allocations == (D(15), D(15))

    e1 = calculate_size(margin_usd=allocations[0], leverage=D(10), price=D(64000),
                        spec=BTC, max_deviation_percent=TOL)
    e2 = calculate_size(margin_usd=allocations[1], leverage=D(10), price=D(63500),
                        spec=BTC, max_deviation_percent=TOL)
    assert e1.size == D("0.0023")   # 150/64000 = 0.002344
    assert e2.size == D("0.0023")   # 150/63500 = 0.002362
    assert e1.size + e2.size <= D("0.0047")


@pytest.mark.parametrize(
    "spec,price,expected",
    [
        (BTC, D(64598), D("0.0046")),
        (ETH, D("1917.93"), D("0.156")),
        (SOL, D("73.96"), D("4.05")),
        (XAUT, D("4019.74"), D("0.074")),
    ],
)
def test_sizing_respects_each_instruments_increment(spec, price, expected):
    r = calculate_size(margin_usd=D(30), leverage=D(10), price=price,
                       spec=spec, max_deviation_percent=TOL)
    assert r.size == expected
    validate_size(r.size, spec)


@pytest.mark.parametrize(
    "spec,prices",
    [
        (BTC, ["64598", "70000.5", "33333.33", "101999.99"]),
        (ETH, ["1917.93", "2000.01", "3333.33"]),
        (SOL, ["73.96", "100.07", "251.11"]),
    ],
)
def test_size_always_rounds_down_never_up(spec, prices):
    """Rounding up would exceed the intended margin (brief 10)."""
    for price in prices:
        r = calculate_size(margin_usd=D(30), leverage=D(10), price=D(price),
                           spec=spec, max_deviation_percent=D(100))
        assert r.notional <= r.intended_notional, (spec.symbol, price)


def test_sizing_uses_contract_size_from_spec():
    """A different contract size must change the result, proving it is read
    rather than assumed to be 1."""
    from dataclasses import replace
    doubled = replace(BTC, contract_size=D(2))
    normal = calculate_size(margin_usd=D(30), leverage=D(10), price=D(60000),
                            spec=BTC, max_deviation_percent=TOL)
    halved = calculate_size(margin_usd=D(30), leverage=D(10), price=D(60000),
                            spec=doubled, max_deviation_percent=TOL)
    assert halved.size < normal.size


# ==========================================================================
# Rejections — the exchange is not a safety net (KRAKEN_NOTES 4.1)
# ==========================================================================


def test_size_below_minimum_rejected():
    """Paper would happily fill this; we must not send it."""
    with pytest.raises(SizingError, match="SIZE_BELOW_MINIMUM"):
        validate_size(D("0.000001"), BTC)


def test_off_increment_size_rejected():
    with pytest.raises(SizingError, match="SIZE_NOT_ON_INCREMENT"):
        validate_size(D("0.00015"), BTC)


def test_minimum_equals_one_increment():
    """KRAKEN_NOTES 4.1.1: min order size == size increment, derived from the
    API's contractValueTradePrecision rather than hardcoded."""
    for spec in (BTC, ETH, SOL, BNB, XRP, DOGE, XAUT):
        validate_size(spec.size_increment, spec)
        with pytest.raises(SizingError, match="SIZE_BELOW_MINIMUM"):
            validate_size(spec.size_increment / 2, spec)


def test_tiny_margin_that_rounds_to_zero_is_rejected():
    with pytest.raises(SizingError, match="SIZE_BELOW_MINIMUM"):
        calculate_size(margin_usd=D("0.10"), leverage=D(1), price=D(64598),
                       spec=BTC, max_deviation_percent=TOL)


def test_material_rounding_deviation_is_rejected():
    """Brief 10: fail safely rather than place a materially mis-sized order."""
    # SOL increment 0.01 at a high price makes rounding coarse.
    with pytest.raises(SizingError, match="SIZING_DEVIATION"):
        calculate_size(margin_usd=D("1"), leverage=D(1), price=D("73.96"),
                       spec=SOL, max_deviation_percent=D("1"))


def test_deviation_is_reported_on_success():
    r = calculate_size(margin_usd=D(30), leverage=D(10), price=D(64598),
                       spec=BTC, max_deviation_percent=TOL)
    assert 0 <= r.deviation_percent <= TOL


@pytest.mark.parametrize("margin,price", [("0", "64598"), ("-30", "64598"), ("30", "0")])
def test_non_positive_inputs_rejected(margin, price):
    with pytest.raises(SizingError):
        calculate_size(margin_usd=D(margin), leverage=D(10), price=D(price),
                       spec=BTC, max_deviation_percent=TOL)


def test_untradeable_instrument_rejected():
    from dataclasses import replace
    with pytest.raises(SizingError, match="NOT_TRADEABLE"):
        calculate_size(margin_usd=D(30), leverage=D(10), price=D(64598),
                       spec=replace(BTC, tradeable=False), max_deviation_percent=TOL)


def test_insufficient_margin_rejected():
    with pytest.raises(SizingError, match="INSUFFICIENT_MARGIN"):
        check_sufficient_margin(D(30), D("29.99"))


def test_sufficient_margin_passes():
    check_sufficient_margin(D(30), D(30))


# ==========================================================================
# Take-profit split (brief 13)
# ==========================================================================


def test_tp_split_sums_exactly_to_position():
    """Never more than the position — that could open a reverse position."""
    for size in ("0.0046", "0.0047", "0.0100", "0.0123"):
        parts = split_take_profits(D(size), BTC)
        assert sum(parts) == D(size)


def test_tp_split_even_division():
    parts = split_take_profits(D("0.0100"), BTC)
    assert parts == (D("0.0025"), D("0.0025"), D("0.0025"), D("0.0025"))


def test_remainder_goes_to_earliest_levels():
    """Policy: unavoidable extra fraction is allocated earliest."""
    # 0.0046 = 46 increments; 46//4 = 11 remainder 2 -> 12,12,11,11
    parts = split_take_profits(D("0.0046"), BTC)
    assert parts == (D("0.0012"), D("0.0012"), D("0.0011"), D("0.0011"))
    assert parts[0] >= parts[-1]
    assert sum(parts) == D("0.0046")


def test_tp_parts_are_non_increasing():
    for size in ("0.0046", "0.0047", "0.0049", "0.0123"):
        parts = split_take_profits(D(size), BTC)
        assert list(parts) == sorted(parts, reverse=True), size


def test_every_tp_part_is_valid_for_the_exchange():
    for size in ("0.0046", "0.0100", "0.0123"):
        for part in split_take_profits(D(size), BTC):
            validate_size(part, BTC)


def test_tp_split_is_deterministic():
    assert split_take_profits(D("0.0047"), BTC) == split_take_profits(D("0.0047"), BTC)


def test_small_position_yields_fewer_levels_not_zero_size_orders():
    """3 increments cannot make 4 parts; emit 3 rather than a zero-size order."""
    parts = split_take_profits(D("0.0003"), BTC)
    assert len(parts) == 3
    assert sum(parts) == D("0.0003")
    for part in parts:
        validate_size(part, BTC)


def test_single_increment_position_yields_one_tp():
    parts = split_take_profits(D("0.0001"), BTC)
    assert parts == (D("0.0001"),)


def test_tp_split_rejects_sub_increment_position():
    with pytest.raises(SizingError):
        split_take_profits(D("0.00001"), BTC)


@pytest.mark.parametrize("spec,size", [(ETH, "0.156"), (SOL, "4.05"), (XAUT, "0.074")])
def test_tp_split_across_instruments(spec, size):
    parts = split_take_profits(D(size), spec)
    assert sum(parts) == D(size)
    for part in parts:
        validate_size(part, spec)


# ==========================================================================
# Rounding helpers
# ==========================================================================


def test_round_to_increment_rounds_down():
    assert round_to_increment(D("0.00469"), D("0.0001")) == D("0.0046")
    assert round_to_increment(D("4.059"), D("0.01")) == D("4.05")


def test_price_tick_rounding_direction_is_explicit():
    """A long's stop rounds down, a short's up — never tighter than intended."""
    assert round_price_to_tick(D("62345.7"), D(1), round_up=False) == D(62345)
    assert round_price_to_tick(D("62345.2"), D(1), round_up=True) == D(62346)


def test_exact_price_unchanged_by_tick_rounding():
    assert round_price_to_tick(D(62345), D(1), round_up=True) == D(62345)


def test_zero_increment_rejected():
    with pytest.raises(SizingError, match="INVALID_INCREMENT"):
        round_to_increment(D(1), D(0))


# ==========================================================================
# Entry allocation and staleness
# ==========================================================================


def test_entry_allocations_sum_exactly():
    for total in ("30", "31", "100", "0.03"):
        parts = split_entry_allocations(D(total))
        assert sum(parts) == D(total)


def test_price_inside_entry_range_is_never_stale():
    stale, dev = is_entry_stale(D(63800), (D(64000), D(63500)), D("1.5"))
    assert not stale
    assert dev == 0


def test_price_just_outside_range_within_tolerance_is_not_stale():
    stale, _ = is_entry_stale(D("64300"), (D(64000), D(63500)), D("1.5"))
    assert not stale


def test_price_far_beyond_range_is_stale():
    stale, dev = is_entry_stale(D(70000), (D(64000), D(63500)), D("1.5"))
    assert stale
    assert dev > D("1.5")


def test_staleness_measured_from_nearest_edge():
    _, dev = is_entry_stale(D(64640), (D(64000), D(63500)), D("1.5"))
    assert dev == pytest.approx(D(1), abs=D("0.01"))  # 1% above the 64000 edge


def test_staleness_works_below_the_range():
    stale, dev = is_entry_stale(D(60000), (D(64000), D(63500)), D("1.5"))
    assert stale
    assert dev > 0


def test_staleness_requires_an_entry_range():
    with pytest.raises(SizingError, match="NO_ENTRY_RANGE"):
        is_entry_stale(D(64000), (), D("1.5"))


# ==========================================================================
# No floats anywhere
# ==========================================================================


def test_all_outputs_are_decimal():
    r = calculate_size(margin_usd=D(30), leverage=D(10), price=D(64598),
                       spec=BTC, max_deviation_percent=TOL)
    for value in (r.size, r.notional, r.margin_used, r.intended_notional,
                  r.deviation_percent):
        assert isinstance(value, Decimal)
    for part in split_take_profits(r.size, BTC):
        assert isinstance(part, Decimal)


# -- BNB (added 2026-07-31 after live spec verification) --------------------


def test_bnb_sizes_against_its_real_increment():
    """PF_BNBUSD has a 0.01 increment; a $30/10x order at ~589 must round
    DOWN onto it, never up (rounding up would overshoot intended margin)."""
    sized = calculate_size(margin_usd=D(30), leverage=D(10), price=D("589.38"),
                           spec=BNB, max_deviation_percent=TOL)
    assert sized.size == D("0.50")
    assert sized.size % BNB.size_increment == 0
    assert sized.notional <= D(300)


def test_bnb_respects_its_50x_exchange_cap():
    """BNB's exchange max is 50x, not BTC's 100x — read from the spec, so a
    signal asking for more is capped by the instrument, not a constant."""
    assert BNB.max_leverage == D(50)
    assert effective_leverage(D(80), max_leverage=D(100), default_leverage=D(10),
                              exchange_max_leverage=BNB.max_leverage) == D(50)


# -- XRP / DOGE (added 2026-07-31 after live verification) ------------------


def test_xrp_and_doge_size_in_whole_units():
    """Both have a size increment of 1 — fractional sizes are invalid, so a
    $300 notional must floor to a whole number of coins."""
    assert XRP.size_increment == D(1)
    assert DOGE.size_increment == D(1)
    xrp = calculate_size(margin_usd=D(30), leverage=D(10), price=D("1.0513"),
                         spec=XRP, max_deviation_percent=TOL)
    assert xrp.size == D(285)
    assert xrp.size == xrp.size.to_integral_value()

    doge = calculate_size(margin_usd=D(30), leverage=D(10), price=D("0.068209"),
                          spec=DOGE, max_deviation_percent=TOL)
    assert doge.size == D(4398)
    assert doge.size == doge.size.to_integral_value()


def test_whole_unit_rounding_stays_inside_the_deviation_cap():
    """A whole-unit increment is coarse at a low price; confirm the loss is
    small enough that a normal order is not refused."""
    for spec, price in ((XRP, D("1.0513")), (DOGE, D("0.068209"))):
        s = calculate_size(margin_usd=D(30), leverage=D(10), price=price,
                           spec=spec, max_deviation_percent=TOL)
        assert s.deviation_percent < D("0.5"), spec.symbol


def test_xrp_and_doge_respect_their_50x_caps():
    for spec in (XRP, DOGE):
        assert spec.max_leverage == D(50)
        assert effective_leverage(D(80), max_leverage=D(100), default_leverage=D(10),
                                  exchange_max_leverage=spec.max_leverage) == D(50)


# -- Gold override (operator decision 2026-07-31) ---------------------------


def test_gold_keeps_its_substitute_flag_after_the_pin_was_lifted():
    """Enabling gold must not quietly drop the disclosure that PF_XAUTUSD is
    Tether Gold rather than spot XAU."""
    assert XAUT.is_substitute is True
    assert "NOT spot XAU" in XAUT.substitute_note


# ==========================================================================
# Stop vs liquidation (trade 8, 2026-10-04)
# ==========================================================================


@pytest.mark.parametrize("entry,leverage,is_short,expected", [
    # Measured on the paper engine (kraken 0.4.1) in an isolated HOME.
    ("2702.3", "10", True, "2918.484"),
    ("2702.3", "5", True, "3188.714"),
    ("2702.3", "20", True, "2783.369"),
    ("2702.5", "10", False, "2486.3"),
])
def test_liquidation_estimate_matches_the_paper_engine(entry, leverage, is_short, expected):
    got = estimated_liquidation_price(D(entry), D(leverage), is_short=is_short)
    assert got == D(expected)


def test_trade_8_stop_sat_beyond_its_10x_liquidation():
    """REGRESSION: ETH short 2497 at 10x liquidates at 2696.76 — the 2700
    stop could never fire first."""
    liq = estimated_liquidation_price(D(2497), D(10), is_short=True)
    assert liq == D("2696.76")
    assert liq < D(2700)


def test_trade_8_needs_8x_with_a_one_point_buffer():
    entries = (D(2497), D(2625))
    assert max_leverage_for_stop(entries, D(2700), is_short=True,
                                 buffer_percent=D("1.0")) == D(8)
    assert max_leverage_for_stop(entries, D(2700), is_short=True,
                                 buffer_percent=D(0)) == D(9)


def test_stop_distance_uses_the_entry_farthest_from_the_stop():
    """If only that entry fills, the stop is as far away as it ever gets."""
    near_only = max_leverage_for_stop((D(2625),), D(2700), is_short=True,
                                      buffer_percent=D(1))
    both = max_leverage_for_stop((D(2625), D(2497)), D(2700), is_short=True,
                                 buffer_percent=D(1))
    assert both == D(8) < near_only


def test_long_side_is_symmetric():
    # far entry 100, stop 92: 8% + 2% + 1% = 11% -> 1/0.11 = 9.09 -> 9x
    assert max_leverage_for_stop((D(98), D(100)), D(92), is_short=False,
                                 buffer_percent=D(1)) == D(9)


def test_exact_boundary_is_allowed():
    # 7% + 2% + 1% = exactly 10% -> exactly 10x
    assert max_leverage_for_stop((D(100),), D(107), is_short=True,
                                 buffer_percent=D(1)) == D(10)


def test_liquidation_distance_at_10x_is_8_percent():
    assert liquidation_distance(D(10)) == D("0.08")


@pytest.mark.parametrize("stop,is_short", [(D(2400), True), (D(2600), False)])
def test_stop_on_the_wrong_side_is_refused(stop, is_short):
    with pytest.raises(SizingError, match="STOP_WRONG_SIDE"):
        max_leverage_for_stop((D(2497),), stop, is_short=is_short,
                              buffer_percent=D(1))

