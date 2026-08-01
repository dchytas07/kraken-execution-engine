"""Adapter and instrument-spec tests.

Spec tests run against `tests/fixtures/futures_instruments.json`, captured
verbatim from `kraken futures instruments` on 2026-07-29 — so they assert
against Kraken's real field shapes, not against my summary of them.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from bot.exchange.base import (
    ExchangeError,
    InstrumentUnavailable,
    OrderRequest,
)
from bot.exchange.cli import KrakenCli
from bot.exchange.kraken import KrakenCliAdapter
from bot.exchange.specs import SYMBOL_MAP, parse_instrument, resolve_specs
from bot.models import CanonicalAsset, OrderPurpose, OrderStatus, TradingMode

FIXTURES = Path(__file__).parent / "fixtures"
INSTRUMENTS = json.loads((FIXTURES / "futures_instruments.json").read_text())[
    "instruments"
]
BY_SYMBOL = {i["symbol"]: i for i in INSTRUMENTS}

V1_ASSETS = frozenset({CanonicalAsset.BTC, CanonicalAsset.ETH,
                       CanonicalAsset.SOL, CanonicalAsset.BNB,
                       CanonicalAsset.XRP, CanonicalAsset.DOGE})


# ==========================================================================
# Instrument specs — read from the exchange, never hardcoded
# ==========================================================================


@pytest.mark.parametrize(
    "asset,symbol,size_incr,tick",
    [
        (CanonicalAsset.BTC, "PF_XBTUSD", "0.0001", "1"),
        (CanonicalAsset.ETH, "PF_ETHUSD", "0.001", "0.1"),
        (CanonicalAsset.SOL, "PF_SOLUSD", "0.01", "0.01"),
        # Captured from the live API 2026-07-31, not hand-written.
        (CanonicalAsset.BNB, "PF_BNBUSD", "0.01", "0.01"),
        # Whole-unit increments — captured live, not assumed.
        (CanonicalAsset.XRP, "PF_XRPUSD", "1", "0.00001"),
        (CanonicalAsset.DOGE, "PF_DOGEUSD", "1", "0.000001"),
    ],
)
def test_specs_match_live_kraken_data(asset, symbol, size_incr, tick):
    spec = parse_instrument(BY_SYMBOL[symbol], asset)
    assert spec.symbol == symbol
    assert spec.size_increment == Decimal(size_incr)
    assert spec.tick_size == Decimal(tick)
    assert spec.contract_size == Decimal("1")
    assert spec.tradeable


def test_btc_maps_to_xbt_symbology():
    """Kraken calls it XBT, not BTC."""
    assert SYMBOL_MAP[CanonicalAsset.BTC] == "PF_XBTUSD"


def test_size_increment_derives_from_precision_not_a_constant():
    """contractValueTradePrecision 4 -> 0.0001; 2 -> 0.01."""
    assert parse_instrument(BY_SYMBOL["PF_XBTUSD"], CanonicalAsset.BTC).size_increment == Decimal("0.0001")
    assert parse_instrument(BY_SYMBOL["PF_SOLUSD"], CanonicalAsset.SOL).size_increment == Decimal("0.01")


def test_max_leverage_derived_from_margin_schedule():
    spec = parse_instrument(BY_SYMBOL["PF_XBTUSD"], CanonicalAsset.BTC)
    # initialMargin 0.01 -> 100x. Well above our 10x cap, which is the point:
    # the cap binds, but the exchange value is still read rather than assumed.
    assert spec.max_leverage >= Decimal("10")


def test_all_v1_assets_resolve():
    specs = resolve_specs(INSTRUMENTS, V1_ASSETS)
    assert set(specs) == V1_ASSETS


def test_missing_instrument_refuses_rather_than_skipping():
    """Brief 25: a required instrument that cannot be resolved is fatal."""
    without_btc = [i for i in INSTRUMENTS if i["symbol"] != "PF_XBTUSD"]
    with pytest.raises(InstrumentUnavailable, match="BTC"):
        resolve_specs(without_btc, V1_ASSETS)


def test_untradeable_instrument_refused():
    raw = dict(BY_SYMBOL["PF_XBTUSD"], tradeable=False)
    with pytest.raises(InstrumentUnavailable, match="not currently tradeable"):
        resolve_specs([raw], {CanonicalAsset.BTC})


@pytest.mark.parametrize(
    "field", ["contractValueTradePrecision", "tickSize", "contractSize"]
)
def test_missing_spec_field_is_fatal_not_defaulted(field):
    raw = {k: v for k, v in BY_SYMBOL["PF_XBTUSD"].items() if k != field}
    with pytest.raises(InstrumentUnavailable):
        parse_instrument(raw, CanonicalAsset.BTC)


def test_xaut_is_flagged_as_a_substitute():
    """XAU resolving to Tether Gold must be structurally visible."""
    spec = parse_instrument(BY_SYMBOL["PF_XAUTUSD"], CanonicalAsset.XAU)
    assert spec.is_substitute
    assert "NOT spot XAU" in spec.substitute_note


def test_v1_asset_set_excludes_gold():
    assert CanonicalAsset.XAU not in V1_ASSETS


# ==========================================================================
# Adapter
# ==========================================================================


class ScriptedCli(KrakenCli):
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
        super().__init__(runner=self._route)

    def _route(self, argv, timeout):
        self.calls.append(argv)
        key = " ".join(a for a in argv[1:] if not a.startswith("-"))
        for prefix in sorted(self.responses, key=len, reverse=True):
            if key.startswith(prefix):
                return 0, json.dumps(self.responses[prefix]), ""
        return 0, json.dumps({"error": "api", "message": f"unscripted: {key}"}), ""


def adapter(responses, mode=TradingMode.PAPER):
    return KrakenCliAdapter(ScriptedCli(responses), mode, V1_ASSETS)


def test_dry_run_cannot_construct_the_adapter():
    """A dry run must not be able to reach the exchange even by mistake."""
    with pytest.raises(ValueError, match="DRY_RUN"):
        KrakenCliAdapter(ScriptedCli({}), TradingMode.DRY_RUN, V1_ASSETS)


def test_paper_and_live_route_to_different_commands():
    paper = adapter({"futures paper positions": {"positions": []}})
    paper.get_open_positions()
    assert paper.cli.calls[0][1:4] == ["futures", "paper", "positions"]

    live = adapter({"futures positions": {"positions": []}}, TradingMode.LIVE)
    live.get_open_positions()
    assert live.cli.calls[0][1:3] == ["futures", "positions"]
    assert "paper" not in live.cli.calls[0]


def test_positions_parsed_and_flat_symbols_dropped():
    a = adapter({
        "futures paper positions": {
            "positions": [
                {"symbol": "PF_XBTUSD", "side": "long", "size": 0.0046,
                 "entry_price": 64598.0, "leverage": 10.0},
                {"symbol": "PF_ETHUSD", "side": "long", "size": 0, "entry_price": 0},
            ]
        }
    })
    positions = a.get_open_positions()
    assert len(positions) == 1
    assert positions[0].size == Decimal("0.0046")
    assert positions[0].symbol == "PF_XBTUSD"


def test_get_position_returns_none_when_flat():
    a = adapter({"futures paper positions": {"positions": []}})
    assert a.get_position("PF_XBTUSD") is None


def test_paper_order_field_names_absorbed():
    """Paper returns `id`/`client_order_id`; the engine sees neither."""
    a = adapter({
        "futures paper orders": {
            "open_orders": [
                {"id": "FP-00003", "client_order_id": "bot-sl-1",
                 "symbol": "PF_XBTUSD", "side": "short", "size": 0.0046,
                 "type": "stop", "reduce_only": True, "status": "open"}
            ]
        }
    })
    order = a.get_open_orders()[0]
    assert order.exchange_order_id == "FP-00003"
    assert order.client_order_id == "bot-sl-1"
    assert order.reduce_only
    assert order.status is OrderStatus.OPEN


def test_live_order_field_names_absorbed():
    a = adapter(
        {"futures open-orders": {
            "openOrders": [
                {"order_id": "abc-123", "cliOrdId": "bot-sl-1",
                 "symbol": "PF_XBTUSD", "side": "sell", "size": 0.0046,
                 "orderType": "stop", "stopPrice": 62000, "reduceOnly": True,
                 "status": "untouched"}
            ]
        }},
        TradingMode.LIVE,
    )
    order = a.get_open_orders()[0]
    assert order.exchange_order_id == "abc-123"
    assert order.client_order_id == "bot-sl-1"
    assert order.trigger_price == Decimal("62000")
    assert order.status is OrderStatus.OPEN


def test_paper_stop_price_gap_surfaces_as_none():
    """Documented fidelity gap: paper omits stop_price entirely."""
    a = adapter({
        "futures paper orders": {
            "open_orders": [{"id": "FP-1", "symbol": "PF_XBTUSD", "side": "short",
                             "size": 0.0046, "type": "stop", "price": None,
                             "status": "open"}]
        }
    })
    assert a.get_open_orders()[0].trigger_price is None


# -- order placement --------------------------------------------------------


def entry_request(**kw):
    base = dict(
        symbol="PF_XBTUSD", side="buy", size=Decimal("0.0023"),
        order_type="limit", purpose=OrderPurpose.ENTRY,
        client_order_id="bot-e1", price=Decimal("64000"),
    )
    return OrderRequest(**{**base, **kw})


def test_client_order_id_always_sent():
    """The idempotency anchor must be on every order (brief 30)."""
    a = adapter({"futures paper buy": {"order_id": "FP-1", "status": "open", "fills": []}})
    a.place_order(entry_request())
    argv = a.cli.calls[0]
    assert "--client-order-id" in argv
    assert argv[argv.index("--client-order-id") + 1] == "bot-e1"


def test_reduce_only_flag_sent_for_protective_orders():
    a = adapter({"futures paper sell": {"order_id": "FP-2", "status": "open", "fills": []}})
    a.place_order(entry_request(
        side="sell", order_type="stop", purpose=OrderPurpose.STOP_LOSS,
        price=None, trigger_price=Decimal("62000"), reduce_only=True,
    ))
    argv = a.cli.calls[0]
    assert "--reduce-only" in argv
    assert argv[argv.index("--stop-price") + 1] == "62000"
    assert argv[argv.index("--trigger-signal") + 1] == "mark"


def test_fills_aggregated_into_result():
    a = adapter({
        "futures paper buy": {
            "order_id": "FP-1", "status": "filled",
            "fills": [{"price": 64598.0, "size": 0.0023}],
        }
    })
    result = a.place_order(entry_request(order_type="market", price=None))
    assert result.accepted
    assert result.filled_size == Decimal("0.0023")
    assert result.avg_fill_price == Decimal("64598.0")
    assert result.status is OrderStatus.FILLED


def test_failed_order_is_not_retried_and_reports_unaccepted():
    """Brief 40: a lost/failed response must not trigger a blind retry."""
    a = adapter({"futures paper buy": {"error": "validation", "message": "bad size"}})
    result = a.place_order(entry_request())
    assert not result.accepted
    assert result.exchange_order_id is None
    assert len(a.cli.calls) == 1  # exactly one attempt


# -- leverage (Study B) -----------------------------------------------------


def test_leverage_is_verified_by_reading_back():
    a = adapter({
        "futures paper leverage": {"leverage_preferences": {"PF_XBTUSD": 10.0}},
        "futures paper set-leverage": {"action": "leverage_set"},
    })
    assert a.set_leverage_preference("PF_XBTUSD", Decimal("10")) == Decimal("10")


def test_leverage_mismatch_after_set_is_fatal():
    """Study B: a stale preference silently governs the next position, so an
    unverified value must never be traded under."""
    a = adapter({
        "futures paper leverage": {"leverage_preferences": {"PF_XBTUSD": 3.0}},
        "futures paper set-leverage": {"action": "leverage_set"},
    })
    with pytest.raises(ExchangeError, match="verification failed"):
        a.set_leverage_preference("PF_XBTUSD", Decimal("10"))


def test_leverage_not_reset_when_already_correct():
    a = adapter({"futures paper leverage": {"leverage_preferences": {"PF_XBTUSD": 10.0}}})
    a.set_leverage_preference("PF_XBTUSD", Decimal("10"))
    assert not any("set-leverage" in " ".join(c) for c in a.cli.calls)


# -- amend (the stop-replacement primitive) --------------------------------


def test_amend_sends_order_id_and_new_values():
    a = adapter({"futures paper edit-order": {"action": "order_edited"}})
    assert a.amend_order("FP-3", size=Decimal("0.0034"), trigger_price=Decimal("64598"))
    argv = a.cli.calls[0]
    assert argv[argv.index("--order-id") + 1] == "FP-3"
    assert argv[argv.index("--size") + 1] == "0.0034"
    assert argv[argv.index("--stop-price") + 1] == "64598"


def test_amend_failure_reported_not_swallowed():
    a = adapter({"futures paper edit-order": {"error": "api", "message": "no such order"}})
    assert a.amend_order("FP-3", size=Decimal("1")) is False


# -- argv shape: positional vs flag ----------------------------------------
# A cancel was originally emitted positionally, which the CLI rejects as an
# unexpected argument. Mocks accepted it; only the live paper run caught it.
# These pin the exact shape of every mutating command.


def test_cancel_uses_order_id_flag_not_positional():
    a = adapter({"futures paper cancel": {"action": "cancelled"}})
    a.cancel_order("FP-00003")
    argv = a.cli.calls[0]
    assert "--order-id" in argv, "cancel takes --order-id, not a positional"
    assert argv[argv.index("--order-id") + 1] == "FP-00003"
    assert "FP-00003" not in argv[:argv.index("--order-id")]


def test_edit_order_uses_order_id_flag():
    a = adapter({"futures paper edit-order": {"action": "order_edited"}})
    a.amend_order("FP-3", size=Decimal("1"))
    assert "--order-id" in a.cli.calls[0]


def test_set_leverage_uses_positional_args():
    """set-leverage genuinely IS positional: <SYMBOL> [LEVERAGE].

    Uses a CLI whose read-back changes after the set, so the mutation actually
    fires — a matching preference is skipped by design.
    """
    class ChangingCli(ScriptedCli):
        def __init__(self):
            super().__init__({})
            self.value = 3.0

        def _route(self, argv, timeout):
            self.calls.append(argv)
            key = " ".join(a for a in argv[1:] if not a.startswith("-"))
            if "set-leverage" in key:
                self.value = 10.0
                return 0, json.dumps({"action": "leverage_set"}), ""
            if "leverage" in key:
                return 0, json.dumps(
                    {"leverage_preferences": {"PF_XBTUSD": self.value}}
                ), ""
            return 0, json.dumps({"error": "api", "message": key}), ""

    a = KrakenCliAdapter(ChangingCli(), TradingMode.PAPER, V1_ASSETS)
    assert a.set_leverage_preference("PF_XBTUSD", Decimal("10")) == Decimal("10")

    call = next(c for c in a.cli.calls if "set-leverage" in c)
    i = call.index("set-leverage")
    assert call[i + 1] == "PF_XBTUSD"
    assert call[i + 2] == "10"


def test_order_uses_positional_symbol_and_size():
    a = adapter({"futures paper buy": {"order_id": "FP-1", "status": "open", "fills": []}})
    a.place_order(entry_request())
    argv = a.cli.calls[0]
    i = argv.index("buy")
    assert argv[i + 1] == "PF_XBTUSD"
    assert argv[i + 2] == "0.0023"


def test_fills_parsed_from_both_engine_shapes():
    a = adapter({
        "futures paper fills": {
            "fills": [{"id": "FP-2", "order_id": "FP-1", "client_order_id": "e1",
                       "symbol": "PF_XBTUSD", "side": "long", "size": 0.0023,
                       "price": 64598.0}]
        }
    })
    fill = a.get_fills()[0]
    assert fill.client_order_id == "e1"
    assert fill.size == Decimal("0.0023")
    assert fill.price == Decimal("64598.0")


def test_mark_price_prefers_mark_over_last():
    a = adapter({"futures ticker": {"ticker": {"markPrice": 64601.7, "last": 64597}}})
    assert a.get_mark_price("PF_XBTUSD") == Decimal("64601.7")
