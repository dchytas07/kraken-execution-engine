"""How a position ends when the exchange, not the bot, ends it.

Trade 8 (2026-10-04): an ETH short was LIQUIDATED at 2698.21 before its stop at
2700 could trigger. The trade closed with realised P&L NULL (a $12.07 loss
recorded as nothing, because the liquidation fill belongs to none of our
orders) and its stop row stayed UNKNOWN forever (the order vanished with the
position, so the cancel failed and nothing updated the row).
"""

from decimal import Decimal

import pytest

from bot.exchange.base import Fill
from bot.models import Direction, OrderPurpose, OrderStatus, TradeState
from bot.pnl import exit_pnl, liquidation_exit
from tests.test_multi_trade import (
    D, build_rig, live, net, open_trade, signal, two_trades,
)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = build_rig(tmp_path, monkeypatch)
    yield r
    r.store.close()


def liquidate(rig, price, *, at="2099-01-01T00:00:00Z"):
    """The exchange liquidates the whole position: a fill with no client order
    id, and every resting order on the symbol vanishes with it."""
    p = rig.exchange.get_position("PF_XBTUSD")
    rig.exchange.fills.append(Fill(
        fill_id="LIQ-1", order_id="LIQ-1", client_order_id=None,
        symbol="PF_XBTUSD", side="short", size=p.size, price=price,
        filled_at=at, fill_type="liquidation"))
    rig.exchange.positions.pop("PF_XBTUSD")
    rig.exchange.orders.clear()


def test_a_liquidated_trade_records_its_loss(rig):
    a = open_trade(rig, 1, signal(stop="62500"))
    entry = D(rig.store.get_trade(a)["actual_avg_entry_price"])
    qty = D(rig.store.get_trade(a)["actual_quantity"])
    liquidate(rig, D(58000))

    rig.reconciler.run_once()

    t = rig.store.get_trade(a)
    assert t["state"] == TradeState.CLOSED.value
    expected = ((D(58000) - entry) * qty).quantize(D("0.01"))
    assert expected < 0
    assert D(t["realized_pnl"]) == expected, "the liquidation loss must be recorded"


def test_a_vanished_stop_row_is_not_left_unknown(rig):
    """The stop disappeared WITH the position, so cancelling it fails; the row
    used to stay UNKNOWN forever."""
    a = open_trade(rig, 1)
    liquidate(rig, D(58000))
    rig.reconciler.run_once()
    for row in rig.store.orders_for_trade(a):
        assert OrderStatus(row["status"]).is_final, (row["purpose"], row["status"])
    sl = rig.store.orders_for_trade(a, OrderPurpose.STOP_LOSS)[0]
    assert sl["status"] == OrderStatus.CANCELLED.value
    assert "gone from the exchange" in (sl["status_detail"] or "")


def test_a_liquidation_is_split_between_the_trades_that_shared_the_position(rig):
    a, b = two_trades(rig)
    liquidate(rig, D(58000))
    rig.reconciler.run_once()
    for t in (a, b):
        row = rig.store.get_trade(t)
        assert row["state"] == TradeState.CLOSED.value
        qty = D(row["actual_quantity"])
        entry = D(row["actual_avg_entry_price"])
        assert D(row["realized_pnl"]) == ((D(58000) - entry) * qty).quantize(D("0.01"))


def test_an_earlier_stop_out_is_not_charged_with_a_later_liquidation(rig):
    """A is stopped out; the LATER liquidation belongs to B alone."""
    a, b = two_trades(rig)
    stop = live(rig, a, "stop")[0]
    rig.exchange.fill(stop.client_order_id, stop.size, D(62400))
    rig.reconciler.run_once()                      # A closes by its own stop
    pnl_a = rig.store.get_trade(a)["realized_pnl"]
    liquidate(rig, D(58000))
    rig.reconciler.run_once()
    assert rig.store.get_trade(a)["realized_pnl"] == pnl_a
    assert D(rig.store.get_trade(b)["realized_pnl"]) < D(pnl_a), "B took the bigger loss"


def test_a_liquidation_from_before_the_trade_existed_is_not_attributed(rig):
    a = open_trade(rig, 1)
    liquidate(rig, D(58000), at="2001-01-01T00:00:00Z")     # long before the trade
    rig.reconciler.run_once()
    assert rig.store.get_trade(a)["state"] == TradeState.CLOSED.value
    assert rig.store.get_trade(a)["realized_pnl"] is None, "unknown, not a guess"


# -- the pure arithmetic ----------------------------------------------------


def test_exit_pnl_is_signed_by_direction():
    assert exit_pnl("LONG", D(100), [(D(2), D(110))]) == D("20.00")
    assert exit_pnl("SHORT", D(100), [(D(2), D(110))]) == D("-20.00")
    assert exit_pnl("SHORT", D("2497"), [(D("0.06"), D("2698.21317879843"))]) == D("-12.07")


def test_exit_pnl_weights_several_exits_and_is_none_without_any():
    # (1 @ 110 + 3 @ 120) / 4 = 117.5 average, 17.5 above entry, x 4
    assert exit_pnl("LONG", D(100), [(D(1), D(110)), (D(3), D(120))]) == D("70.00")
    assert exit_pnl("LONG", D(100), []) is None


def test_liquidation_exit_caps_at_the_quantity_and_ignores_other_fills():
    fills = [
        Fill("1", None, None, "PF_XBTUSD", "short", D(5), D(58000), "2026-10-04T12:50:05Z", "liquidation"),
        Fill("2", None, "x-SL-1", "PF_XBTUSD", "short", D(1), D(60000), "2026-10-04T12:50:06Z", "triggered"),
        Fill("3", None, None, "PF_ETHUSD", "short", D(9), D(2000), "2026-10-04T12:50:07Z", "liquidation"),
    ]
    assert liquidation_exit(fills, symbol="PF_XBTUSD", since="2026-10-01T00:00:00Z",
                            quantity=D(2)) == (D(2), D(58000))
    assert liquidation_exit(fills, symbol="PF_XBTUSD", since="2026-10-05T00:00:00Z",
                            quantity=D(2)) is None
    assert liquidation_exit(fills, symbol="PF_XBTUSD", since="2026-10-01T00:00:00Z",
                            quantity=D(0)) is None
