"""Several trades on one symbol, sharing the exchange's netted position.

Kraken nets positions per symbol. These tests drive TWO long BTC trades through
the real pipeline, manager and reconciler against an exchange that nets, and
assert the properties that make sharing safe: every order belongs to one trade,
every stop/TP is sized to THAT trade's own quantity, one trade closing or
stopping out leaves the other intact and protected, and the per-trade
quantities are checked against the net position the exchange reports.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from bot.config import load_config
from bot.db.store import Store
from bot.exchange.specs import parse_instrument
from bot.execution.engine import ExecutionEngine
from bot.execution.manager import TradeManager
from bot.execution.observer import PollingFillObserver
from bot.killswitch import KillSwitch
from bot.models import CanonicalAsset, OrderPurpose, TradeState
from bot.pipeline import SignalPipeline, build_admission
from bot.reconcile import DiscrepancyKind, Reconciler
from tests.test_execution import FakeExchange

D = Decimal
CH = -1001000000001
RAW = {i["symbol"]: i for i in json.loads(
    (Path(__file__).parent / "fixtures" / "futures_instruments.json").read_text()
)["instruments"]}
BTC = parse_instrument(RAW["PF_XBTUSD"], CanonicalAsset.BTC)
SPECS = {CanonicalAsset.BTC: BTC}


def signal(stop="62500", entries=("64000", "63500"), tps=("65000", "66000", "67000", "68000")):
    t1, t2, t3, t4 = tps
    return (
        f"➡️ LONG BTCUSDT \n\n❇️ Entry: {entries[0]}.00000000 - {entries[1]}.00000000\n\n"
        f"☑️ Target 1: {t1}.00000000\n\n☑️ Target 2: {t2}.00000000\n\n"
        f"☑️ Target 3: {t3}.00000000\n\n☑️ Target 4: {t4}.00000000\n\n"
        f"⛔ Stoploss: {stop}.00000000\n\n💫 Leverage : 10x"
    )


def build_rig(tmp_path, monkeypatch, **env):
    alerts: list[dict] = []
    monkeypatch.setattr("bot.pipeline.send_review_alert",
                        lambda config, **kw: alerts.append(kw) or True)
    config = load_config({
        "TRADING_MODE": "paper", "TRADE_MARGIN_USD": "30", "MAX_LEVERAGE": "10",
        "MAX_TOTAL_OPEN_MARGIN": "200", "MAX_TRADES_PER_SYMBOL": "2",
        "MAX_ENTRY_DEVIATION_PERCENT": "1.5",
        "KILL_SWITCH_PATH": str(tmp_path / "KILL"), **env,
    }, dotenv_path=None)
    store = Store(tmp_path / "mt.db")
    exchange = FakeExchange(mark=D(63800))
    ks = KillSwitch(path=tmp_path / "KILL")
    engine = ExecutionEngine(
        config=config, store=store, client=exchange,
        observer=PollingFillObserver(exchange, sleep=lambda s: None,
                                     default_timeout_s=0.05, default_poll_s=0.0),
        kill_switch=ks,
    )
    engine.mark_recovery_complete()
    manager = TradeManager(config=config, store=store, client=exchange,
                           engine=engine, specs=SPECS)
    pipeline = SignalPipeline(
        config=config, store=store,
        admission=build_admission(config=config, store=store, kill_switch=ks,
                                  client=exchange, specs=SPECS, engine=engine),
        engine=engine, specs=SPECS, manager=manager,
    )
    reconciler = Reconciler(config=config, store=store, client=exchange,
                            engine=engine, specs=SPECS)

    class Rig:
        pass

    r = Rig()
    r.pipeline, r.store, r.exchange, r.engine = pipeline, store, exchange, engine
    r.manager, r.reconciler, r.config, r.alerts = manager, reconciler, config, alerts
    return r


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = build_rig(tmp_path, monkeypatch)
    yield r
    r.store.close()


def open_trade(rig, mid, text=None, *, fill=True):
    rig.exchange.autofill_entries = fill
    res = rig.pipeline.handle_message(channel_id=CH, message_id=mid,
                                      text=text or signal())
    rig.exchange.autofill_entries = False
    assert res.accepted, res.detail
    return res.trade_id


def two_trades(rig):
    a = open_trade(rig, 1, signal(stop="62500"))
    b = open_trade(rig, 2, signal(stop="62400"))     # different stop => not a duplicate
    return a, b


def orders_of(rig, trade_id, purpose=None):
    rows = rig.store.orders_for_trade(trade_id, purpose) if purpose else \
        rig.store.orders_for_trade(trade_id)
    return rows


def live(rig, trade_id, order_type):
    uid = rig.store.get_trade(trade_id)["trade_uid"]
    return [o for o in rig.exchange.get_open_orders()
            if o.order_type == order_type and (o.client_order_id or "").startswith(f"{uid}-")]


def qty(rig, trade_id):
    return D(rig.store.get_trade(trade_id)["actual_quantity"])


def net(rig):
    p = rig.exchange.get_position("PF_XBTUSD")
    return p.size if p else D(0)


# ==========================================================================
# Opening: every order belongs to one trade, sized to that trade
# ==========================================================================


def test_two_same_direction_trades_open_on_one_symbol(rig):
    a, b = two_trades(rig)
    assert len(rig.store.active_trades_for_symbol("PF_XBTUSD")) == 2
    assert qty(rig, a) == D("0.0046") and qty(rig, b) == D("0.0046")
    assert net(rig) == D("0.0092"), "the exchange holds the NET of both"


def test_every_order_carries_its_own_trades_uid(rig):
    a, b = two_trades(rig)
    ua = rig.store.get_trade(a)["trade_uid"]
    ub = rig.store.get_trade(b)["trade_uid"]
    assert ua != ub
    for tid, uid in ((a, ua), (b, ub)):
        rows = orders_of(rig, tid)
        assert rows
        assert all(r["client_order_id"].startswith(f"{uid}-") for r in rows)
    ids = [r["client_order_id"] for t in (a, b) for r in orders_of(rig, t)]
    assert len(ids) == len(set(ids)), "client order ids must be unique across trades"


def test_each_trade_gets_its_own_reduce_only_stop_sized_to_its_own_quantity(rig):
    a, b = two_trades(rig)
    sa, sb = live(rig, a, "stop"), live(rig, b, "stop")
    assert len(sa) == 1 and len(sb) == 1
    assert sa[0].size == qty(rig, a) and sb[0].size == qty(rig, b)
    assert sa[0].reduce_only and sb[0].reduce_only
    assert sa[0].size + sb[0].size == net(rig), "stops together cover the position, no more"


def test_each_trade_gets_its_own_tp_ladder_that_never_exceeds_its_quantity(rig):
    a, b = two_trades(rig)
    rig.manager.step_all()                       # places the ladders
    for tid in (a, b):
        tps = live(rig, tid, "take-profit")
        assert tps, "no TP ladder"
        assert all(o.reduce_only for o in tps)
        assert sum(o.size for o in tps) <= qty(rig, tid)
    all_tp = sum(o.size for t in (a, b) for o in live(rig, t, "take-profit"))
    assert all_tp <= net(rig), "ladders together must never exceed the position"


def test_a_stop_on_the_second_trade_is_not_mistaken_for_a_duplicate(rig):
    """_verify_stop used to count every stop on the ACCOUNT."""
    a, b = two_trades(rig)
    assert rig.engine._verify_stop(
        a, live(rig, a, "stop")[0].exchange_order_id, qty(rig, a)
    ), "two healthy stops on one symbol are not a duplicate"


def test_a_duplicate_stop_of_the_same_trade_is_still_caught(rig):
    from tests.test_execution import replace_order
    a, b = two_trades(rig)
    uid = rig.store.get_trade(a)["trade_uid"]
    stop = live(rig, a, "stop")[0]
    rig.exchange.orders["ROGUE"] = replace_order(
        stop, exchange_order_id="ROGUE", client_order_id=f"{uid}-SL-99")
    assert not rig.engine._verify_stop(a, stop.exchange_order_id, qty(rig, a))


def test_a_third_trade_on_the_symbol_is_refused(rig):
    two_trades(rig)
    r = rig.pipeline.handle_message(channel_id=CH, message_id=3,
                                    text=signal(stop="62300"))
    assert not r.accepted
    assert r.action == "REVIEW_ALERT"
    assert len(rig.store.active_trades_for_symbol("PF_XBTUSD")) == 2
    assert rig.alerts and "limit is 2" in rig.alerts[-1]["reason"]


def test_an_opposite_direction_signal_places_no_order_and_alerts(rig):
    open_trade(rig, 1)
    before = len(rig.exchange.orders)
    short = signal().replace("LONG", "SHORT").replace("62500", "65500")
    r = rig.pipeline.handle_message(channel_id=CH, message_id=2, text=short)
    assert not r.accepted and r.action == "REVIEW_ALERT"
    assert len(rig.exchange.orders) == before, "an opposite order would net the long down"
    assert "other way" in rig.alerts[-1]["reason"] or "nets" in rig.alerts[-1]["reason"]
    assert rig.store.get_message(CH, 2)["rejection_reason"] == "OPPOSITE_DIRECTION_ACTIVE"


def test_total_margin_cap_stops_a_third_trade_even_when_the_symbol_cap_would_allow_it(tmp_path, monkeypatch):
    """MAX_TOTAL_OPEN_MARGIN is $60 with up to 3 trades per symbol allowed: the
    third BTC trade is refused for MARGIN, not for the symbol count."""
    r = build_rig(tmp_path, monkeypatch, MAX_TOTAL_OPEN_MARGIN="60",
                  MAX_TRADES_PER_SYMBOL="3")
    open_trade(r, 1, signal(stop="62500"))
    open_trade(r, 2, signal(stop="62400"))
    res = r.pipeline.handle_message(channel_id=CH, message_id=3,
                                    text=signal(stop="62300"))
    assert not res.accepted and res.action == "REVIEW_ALERT"
    assert r.store.get_message(CH, 3)["rejection_reason"] == "TOTAL_MARGIN_LIMIT"
    assert "MAX_TOTAL_OPEN_MARGIN is $60" in r.alerts[-1]["reason"]
    r.store.close()


def test_without_a_margin_cap_configured_nothing_opens_at_all(tmp_path, monkeypatch):
    r = build_rig(tmp_path, monkeypatch, MAX_TOTAL_OPEN_MARGIN="")
    res = r.pipeline.handle_message(channel_id=CH, message_id=1, text=signal())
    assert not res.accepted and r.exchange.orders == {}
    assert r.store.get_message(CH, 1)["rejection_reason"] == "TOTAL_MARGIN_LIMIT"
    assert "not configured" in r.alerts[-1]["reason"]
    r.store.close()


# ==========================================================================
# The combined position has ONE liquidation price
# ==========================================================================


def test_a_second_trade_that_leaves_the_first_stop_beyond_combined_liquidation_is_refused(rig, monkeypatch):
    """Trade A alone: entry 64000, stop 59520 is exactly 7% away: fine. Trade B
    enters 66000. Liquidation is measured from the combined worst-case entry
    (66000): A's stop is now 9.8% away, beyond the 7% a fixed 10x allows."""
    sent = []
    monkeypatch.setattr("bot.execution.engine.send_alert",
                        lambda token, chat, text: sent.append(text) or True)
    open_trade(rig, 1, signal(stop="59520", entries=("64000", "63500")))
    r = rig.pipeline.handle_message(
        channel_id=CH, message_id=2,
        text=signal(stop="62000", entries=("66000", "65500"),
                    tps=("67000", "68000", "69000", "70000")))
    assert not r.accepted
    assert rig.store.get_message(CH, 2)["rejection_reason"] == "STOP_BEYOND_LIQUIDATION"
    assert any("trade #1" in t and "combined" in t for t in sent), sent
    assert len(rig.store.active_trades_for_symbol("PF_XBTUSD")) == 1


def test_two_trades_whose_stops_both_clear_the_combined_liquidation_open(rig):
    open_trade(rig, 1, signal(stop="62500"))
    assert open_trade(rig, 2, signal(stop="62400", entries=("64200", "63700")))


# ==========================================================================
# One trade leaves; the other is untouched and still protected
# ==========================================================================


def trigger_stop(rig, trade_id, price=D(62400)):
    stop = live(rig, trade_id, "stop")[0]
    rig.exchange.fill(stop.client_order_id, stop.size, price)


def test_one_trades_stop_out_leaves_the_other_trade_whole_and_protected(rig):
    a, b = two_trades(rig)
    qb, sb_before = qty(rig, b), live(rig, b, "stop")[0]
    trigger_stop(rig, a)

    assert net(rig) == qb, "only A's quantity left the netted position"
    report = rig.reconciler.run_once()

    assert rig.store.get_trade(a)["state"] == TradeState.CLOSED.value
    assert rig.store.get_trade(b)["state"] != TradeState.CLOSED.value
    sb = live(rig, b, "stop")
    assert len(sb) == 1 and sb[0].exchange_order_id == sb_before.exchange_order_id
    assert sb[0].size == qb
    assert DiscrepancyKind.SYMBOL_TOTAL_MISMATCH not in {d.kind for d in report.discrepancies}
    assert rig.store.get_trade(a)["realized_pnl"] is not None
    assert D(rig.store.get_trade(a)["realized_pnl"]) < 0


def test_stop_out_of_the_second_trade_does_not_touch_the_first(rig):
    a, b = two_trades(rig)
    qa = qty(rig, a)
    trigger_stop(rig, b)
    rig.reconciler.run_once()
    assert rig.store.get_trade(b)["state"] == TradeState.CLOSED.value
    assert rig.store.get_trade(a)["state"] != TradeState.CLOSED.value
    assert live(rig, a, "stop")[0].size == qa and net(rig) == qa


def test_closing_one_trade_by_command_closes_only_that_trades_quantity(rig):
    from tests.test_pipeline import fill_closes
    a, b = two_trades(rig)
    fill_closes(rig.exchange)
    qb = qty(rig, b)
    r = rig.pipeline.handle_message(channel_id=CH, message_id=5,
                                    text="Close in profit ☑️", reply_to_message_id=1)
    assert r.accepted, r.detail
    assert rig.store.get_trade(a)["state"] == TradeState.CLOSED.value
    assert rig.store.get_trade(b)["state"] != TradeState.CLOSED.value
    assert net(rig) == qb, "closing A must not close B's share of the netted position"
    assert live(rig, b, "stop")[0].size == qb
    assert not live(rig, a, "stop"), "A's orders are cancelled"


def test_close_by_symbol_closes_every_trade_on_it(rig):
    from tests.test_pipeline import fill_closes
    a, b = two_trades(rig)
    fill_closes(rig.exchange)
    r = rig.pipeline.handle_message(channel_id=CH, message_id=5, text="Close BTC Long")
    assert r.accepted
    assert net(rig) == D(0)
    for t in (a, b):
        assert rig.store.get_trade(t)["state"] == TradeState.CLOSED.value


def test_a_trades_close_order_is_reduce_only_and_sized_to_the_trade(rig):
    from tests.test_pipeline import fill_closes
    a, b = two_trades(rig)
    fill_closes(rig.exchange)
    rig.pipeline.handle_message(channel_id=CH, message_id=5,
                                text="Close in profit ☑️", reply_to_message_id=1)
    closes = orders_of(rig, a, OrderPurpose.CLOSE)
    assert len(closes) == 1 and D(closes[0]["quantity"]) == D("0.0046")
    assert closes[0]["reduce_only"] == 1
    assert not orders_of(rig, b, OrderPurpose.CLOSE)


# ==========================================================================
# Breakeven, stop updates and TP fills are per trade
# ==========================================================================


def fill_tp(rig, trade_id, index):
    uid = rig.store.get_trade(trade_id)["trade_uid"]
    tp = next(o for o in rig.exchange.get_open_orders()
              if o.client_order_id == f"{uid}-TP-{index}")
    rig.exchange.fill(tp.client_order_id, tp.size, D(66000))
    return tp


def test_breakeven_moves_only_the_trade_whose_tp2_filled(rig):
    a, b = two_trades(rig)
    rig.manager.step_all()
    sb_before = live(rig, b, "stop")[0]
    fill_tp(rig, a, 1)
    fill_tp(rig, a, 2)
    rig.manager.step(a)

    assert rig.store.get_trade(a)["state"] == TradeState.BREAKEVEN_ACTIVE.value
    assert rig.store.get_trade(b)["state"] != TradeState.BREAKEVEN_ACTIVE.value
    sa = live(rig, a, "stop")[0]
    remaining_a = qty(rig, a)
    sb = live(rig, b, "stop")[0]
    assert sb.exchange_order_id == sb_before.exchange_order_id and sb.size == qty(rig, b)
    assert sa.size > 0 and sa.size <= remaining_a
    assert rig.store.get_trade(b)["breakeven_active"] == 0


def test_a_tp_fill_on_one_trade_resizes_only_that_trades_stop(rig):
    a, b = two_trades(rig)
    rig.manager.step_all()
    sb = live(rig, b, "stop")[0].size
    tp = fill_tp(rig, a, 1)
    rig.manager.step(a)
    assert live(rig, a, "stop")[0].size == qty(rig, a) == D("0.0046") - tp.size
    assert live(rig, b, "stop")[0].size == sb


def test_a_channel_stop_update_by_reply_moves_only_that_trades_stop(rig):
    a, b = two_trades(rig)
    sb = live(rig, b, "stop")[0]
    r = rig.pipeline.handle_message(channel_id=CH, message_id=7,
                                    text="SL Update : 62800", reply_to_message_id=1)
    assert r.accepted and r.trade_id == a
    assert D(rig.store.get_trade(a)["stop_trigger_price"]) == D(62800)
    assert live(rig, b, "stop")[0].exchange_order_id == sb.exchange_order_id
    assert D(rig.store.get_trade(b)["stop_trigger_price"]) == D(62400)


def test_a_stop_update_naming_only_the_symbol_is_ambiguous_with_two_trades(rig):
    """'BTC SL Update : 62800' could mean either trade: never guess."""
    a, b = two_trades(rig)
    r = rig.pipeline.handle_message(channel_id=CH, message_id=7,
                                    text="BTC SL Update : 62800")
    assert r.action == "REVIEW_ALERT" and "2 open trades" in rig.alerts[-1]["reason"]
    assert D(rig.store.get_trade(a)["stop_trigger_price"]) == D(62500)


# ==========================================================================
# Protection sweep: the second trade's fill is protected on its own
# ==========================================================================


def test_sweep_protects_a_second_trade_that_filled_after_the_first_was_protected(rig):
    """With a per-SYMBOL stop check the symbol already 'has a stop' and the new
    trade's quantity would sit naked until someone noticed."""
    a = open_trade(rig, 1)
    b = open_trade(rig, 2, signal(stop="62400"), fill=False)   # entries rest
    assert not live(rig, b, "stop")
    uid_b = rig.store.get_trade(b)["trade_uid"]
    for o in list(rig.exchange.get_open_orders()):
        if (o.client_order_id or "").startswith(f"{uid_b}-ENTRY"):
            rig.exchange.fill(o.client_order_id, o.size, o.price)

    protected = rig.manager.protection_sweep()

    assert protected == ["PF_XBTUSD"]
    sb = live(rig, b, "stop")
    assert len(sb) == 1 and sb[0].size == D("0.0046")
    assert len(live(rig, a, "stop")) == 1, "the first trade's stop is untouched"


def test_sweep_places_nothing_when_every_trade_already_has_its_own_stop(rig):
    two_trades(rig)
    assert rig.manager.protection_sweep() == []


def test_sweep_ignores_a_waiting_trade_that_holds_nothing(rig):
    a = open_trade(rig, 1)
    b = open_trade(rig, 2, signal(stop="62400"), fill=False)
    assert rig.manager.protection_sweep() == []
    assert not live(rig, b, "stop")


# ==========================================================================
# Reconciliation: each trade's expected quantity, and the sum vs the net
# ==========================================================================


def kinds(report):
    return {d.kind for d in report.discrepancies}


def test_healthy_shared_symbol_reconciles_clean(rig):
    two_trades(rig)
    rig.manager.step_all()
    report = rig.reconciler.run_once()
    assert report.clean, [d.detail for d in report.discrepancies]


def test_exposure_nobody_owns_is_reported_never_auto_closed(rig):
    """The exchange holds more than the trades account for."""
    two_trades(rig)
    # Someone adds 0.001 by hand: it enters the net but no trade's ledger.
    rig.exchange._apply("PF_XBTUSD", D("0.001"), D(64000))
    report = rig.reconciler.run_once()
    mismatch = [d for d in report.discrepancies
                if d.kind is DiscrepancyKind.SYMBOL_TOTAL_MISMATCH]
    assert mismatch and "UNOWNED" in mismatch[0].detail
    assert net(rig) == D("0.0102"), "nothing was closed to 'fix' it"
    assert "symbol-total:PF_XBTUSD" in rig.reconciler.unresolved_findings


def test_trades_that_expect_more_than_the_exchange_holds_are_reported(rig):
    two_trades(rig)
    rig.exchange._apply("PF_XBTUSD", D("-0.003"), D(64000))     # closed by hand
    report = rig.reconciler.run_once()
    assert DiscrepancyKind.SYMBOL_TOTAL_MISMATCH in kinds(report)


def test_the_mismatch_clears_once_the_numbers_agree_again(rig):
    two_trades(rig)
    rig.exchange._apply("PF_XBTUSD", D("0.001"), D(64000))
    rig.reconciler.run_once()
    rig.exchange._apply("PF_XBTUSD", D("-0.001"), D(64000))
    report = rig.reconciler.run_once()
    assert DiscrepancyKind.SYMBOL_TOTAL_MISMATCH not in kinds(report)
    assert "symbol-total:PF_XBTUSD" not in rig.reconciler.unresolved_findings


def test_a_lone_trade_with_an_orphan_beside_it_is_flagged_not_adopted(rig):
    """A position nobody opened used to be silently adopted by the one trade on
    the symbol (its stop sized to the whole net). Now the sum is checked."""
    open_trade(rig, 1, fill=False)                 # waiting, holds nothing
    rig.exchange._apply("PF_XBTUSD", D("0.002"), D(64000))
    report = rig.reconciler.run_once()
    assert DiscrepancyKind.SYMBOL_TOTAL_MISMATCH in kinds(report)


def test_missing_stop_on_one_trade_is_repaired_for_that_trade_only(rig):
    a, b = two_trades(rig)
    sb = live(rig, b, "stop")[0]
    stop_a = live(rig, a, "stop")[0]
    rig.exchange.cancel_order(stop_a.exchange_order_id)          # A loses its stop
    report = rig.reconciler.run_once()
    assert DiscrepancyKind.UNPROTECTED_POSITION in kinds(report)
    new_a = live(rig, a, "stop")
    assert len(new_a) == 1 and new_a[0].size == qty(rig, a)
    assert live(rig, b, "stop")[0].exchange_order_id == sb.exchange_order_id


def test_expiry_cancels_one_trades_entries_and_leaves_the_other_alone(rig):
    a = open_trade(rig, 1, fill=False)
    b = open_trade(rig, 2, signal(stop="62400"), fill=False)
    with rig.store.transaction() as conn:
        conn.execute(
            "UPDATE orders SET expires_at = '2000-01-01T00:00:00+00:00' "
            "WHERE trade_id = ? AND purpose = 'ENTRY'", (a,))
    rig.reconciler.run_once()
    assert rig.store.get_trade(a)["state"] == TradeState.CANCELLED.value
    assert rig.store.get_trade(b)["state"] == TradeState.WAITING_FOR_ENTRY.value
    assert len([o for o in rig.exchange.get_open_orders()
                if o.order_type == "limit"]) == 2, "B's two entries still rest"


# ==========================================================================
# Per-trade ledger and attribution
# ==========================================================================


def test_trade_position_is_the_trades_own_quantity_when_the_symbol_is_shared(rig):
    a, b = two_trades(rig)
    pa = rig.engine.trade_position(a)
    assert pa.size == D("0.0046") and net(rig) == D("0.0092")
    trigger_stop(rig, a)
    assert rig.engine.trade_position(a) is None, "A has nothing left"
    assert rig.engine.trade_position(b).size == D("0.0046") == net(rig)


def test_trade_position_equals_the_net_position_for_a_lone_trade(rig):
    a = open_trade(rig, 1)
    assert rig.engine.trade_position(a).size == net(rig) == D("0.0046")


def test_trade_position_is_never_larger_than_the_exchange_holds(rig):
    a, b = two_trades(rig)
    rig.exchange._apply("PF_XBTUSD", D("-0.0070"), D(64000))     # 0.0022 left
    assert rig.engine.trade_position(a).size <= D("0.0022")


def test_symbol_exposure_is_the_sum_of_the_trades(rig):
    two_trades(rig)
    assert rig.engine.symbol_exposure("PF_XBTUSD") == D("0.0092")


def test_admission_accepts_a_netted_position_only_when_the_trades_explain_it(rig):
    two_trades(rig)
    adm = rig.pipeline.admission
    # Pretend the cap allowed one more: the netted position is fully explained.
    assert adm.check_exchange_position("PF_XBTUSD", 0) is None
    rig.exchange._apply("PF_XBTUSD", D("0.001"), D(64000))       # unexplained extra
    parsed = rig.store.record_message(channel_id=CH, message_id=50, raw_text="x")
    refusal = adm.check_exchange_position("PF_XBTUSD", parsed)
    assert refusal is not None and refusal.reason.value == "UNRECOGNISED_POSITION"


# ==========================================================================
# Restart recovery with trades sharing a symbol
# ==========================================================================


def _recovery(rig):
    from bot.recovery import RecoveryManager
    return RecoveryManager(config=rig.config, store=rig.store, client=rig.exchange,
                           engine=rig.engine, reconciler=rig.reconciler, specs=SPECS)


def _crash_window_order(rig, trade_id):
    """An order that was recorded (INTENT) but never reached the exchange."""
    uid = rig.store.get_trade(trade_id)["trade_uid"]
    rig.store.record_order_intent(
        trade_id=trade_id, client_order_id=f"{uid}-TP-9",
        purpose=OrderPurpose.TAKE_PROFIT, symbol="PF_XBTUSD", side="sell",
        order_type="take-profit", quantity=D("0.0010"), trigger_price=D(69000),
        reduce_only=True, sequence=9,
    )
    return f"{uid}-TP-9"


def test_recovery_with_trades_that_account_for_the_whole_position_is_not_ambiguous(rig):
    """B has an unresolvable intent. A single trade would be quarantined
    (a position exists, so absence proves nothing); here the two trades explain
    the netted position exactly, so the order cannot have executed."""
    a, b = two_trades(rig)
    coid = _crash_window_order(rig, b)
    result = _recovery(rig).recover()
    assert result.safe_to_trade
    assert not result.ambiguous, [(i.client_order_id, i.detail) for i in result.ambiguous]
    assert any(i.client_order_id == coid and i.outcome.value == "NEVER_PLACED"
               for i in result.intents_resolved)
    assert rig.store.get_trade(b)["state"] != TradeState.REQUIRES_REVIEW.value
    assert not result.orphan_positions


def test_recovery_still_quarantines_when_the_exchange_holds_more_than_the_trades_explain(rig):
    a, b = two_trades(rig)
    _crash_window_order(rig, b)
    rig.exchange._apply("PF_XBTUSD", D("0.0010"), D(64000))    # unexplained extra
    result = _recovery(rig).recover()
    assert len(result.ambiguous) == 1
    assert rig.store.get_trade(b)["state"] == TradeState.REQUIRES_REVIEW.value
    assert rig.store.get_trade(a)["state"] != TradeState.REQUIRES_REVIEW.value, \
        "the other trade is not dragged into quarantine"


def test_recovery_leaves_both_trades_protected_and_does_not_disturb_their_stops(rig):
    a, b = two_trades(rig)
    ids = {live(rig, t, "stop")[0].exchange_order_id for t in (a, b)}
    result = _recovery(rig).recover()
    assert result.safe_to_trade and result.unprotected_repaired == 0
    assert {live(rig, t, "stop")[0].exchange_order_id for t in (a, b)} == ids


def _status_cfg(rig):
    from dataclasses import replace as dc_replace
    return dc_replace(rig.config, db_path=Path(rig.store.db_path))


def test_status_cross_check_sums_trades_per_symbol(rig, monkeypatch, tmp_path):
    """--status compared each trade with the whole netted position and called a
    healthy shared symbol a mismatch."""
    from bot import status as status_mod
    two_trades(rig)
    state = tmp_path / "futures_state.json"
    uid_orders = [
        {"client_order_id": r["client_order_id"], "symbol": "PF_XBTUSD",
         "side": r["side"], "size": float(r["quantity"]), "status": "open"}
        for t in rig.store.open_trades()
        for r in rig.store.orders_for_trade(t["id"])
        if r["status"] in ("OPEN", "SUBMITTED", "PARTIALLY_FILLED")
    ]
    state.write_text(json.dumps({
        "positions": [{"symbol": "PF_XBTUSD", "side": "Long",
                       "size": float(net(rig)), "entry_price": 63750.0}],
        "open_orders": uid_orders,
    }))
    monkeypatch.setattr(status_mod, "PAPER_STATE", state)
    monkeypatch.setattr(status_mod, "_marks", lambda symbols, binary: {})
    out = status_mod.build_status(_status_cfg(rig))
    assert "MISMATCH" not in out, out
    assert "OK —" in out
    assert out.count("[PROTECTED]") == 2


# ==========================================================================
# Findings from the paper netting experiment (2026-10-07)
# ==========================================================================


def uid_of(rig, tid):
    return rig.store.get_trade(tid)["trade_uid"]


def resting_for(rig, tid):
    return [o for o in rig.exchange.get_open_orders()
            if (o.client_order_id or "").startswith(f"{uid_of(rig, tid)}-")]


def test_a_stopped_out_trade_leaves_no_order_behind_to_fire_against_its_sibling(rig):
    """Measured on paper: reduce-only orders belong to the NET position. A's
    leftover stop (or TP) would fire against B's quantity."""
    a, b = two_trades(rig)
    rig.manager.step_all()
    assert resting_for(rig, a), "setup: A has a stop and a TP ladder resting"
    trigger_stop(rig, a)
    rig.reconciler.run_once()
    assert rig.store.get_trade(a)["state"] == TradeState.CLOSED.value
    assert resting_for(rig, a) == [], "A's TP ladder must be cancelled with it"
    assert len(resting_for(rig, b)) >= 1


def test_a_trade_whose_leftover_orders_cannot_be_cancelled_is_not_marked_closed(rig):
    a, b = two_trades(rig)
    rig.manager.step_all()
    trigger_stop(rig, a)
    rig.exchange.cancel_fails = True

    report = rig.reconciler.run_once()

    assert rig.store.get_trade(a)["state"] == TradeState.CLOSING.value
    pos_gone = [d for d in report.discrepancies
                if d.kind is DiscrepancyKind.POSITION_GONE and d.trade_id == a]
    assert pos_gone and not pos_gone[0].repaired
    assert rig.store.get_trade(b)["state"] != TradeState.CLOSED.value

    rig.exchange.cancel_fails = False
    rig.reconciler.run_once()
    assert rig.store.get_trade(a)["state"] == TradeState.CLOSED.value
    assert resting_for(rig, a) == []


def test_reduce_only_orders_are_all_the_exits_the_bot_ever_places(rig):
    """Every exit is reduce-only, so an opposite-direction FILL can never open a
    reverse position (paper: a plain opposite order shrinks and then FLIPS the
    netted position, which is why opposite-direction SIGNALS are refused)."""
    a, b = two_trades(rig)
    rig.manager.step_all()
    for t in (a, b):
        for row in rig.store.orders_for_trade(t):
            if row["purpose"] != OrderPurpose.ENTRY.value:
                assert row["reduce_only"] == 1, (row["purpose"], row["client_order_id"])
