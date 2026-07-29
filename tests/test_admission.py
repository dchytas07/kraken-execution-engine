"""Admission control tests.

Every test drives the REAL entry point (`SignalAdmission.admit`) with a message
that came out of the real parser — not the underlying check functions directly.
That is the whole point: the sweep found these mechanisms were correct in
isolation and simply never called.
"""

from decimal import Decimal

import pytest

from bot.admission import SignalAdmission
from bot.config import load_config
from bot.db.store import Store
from bot.killswitch import KillSwitch
from bot.models import (
    CanonicalAsset,
    ExecutionDecision,
    MessageType,
    RejectionReason,
    TradeState,
)
from bot.parser.parse import fingerprint_for, parse_message

D = Decimal

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

SYMBOLS = {
    CanonicalAsset.BTC: "PF_XBTUSD",
    CanonicalAsset.ETH: "PF_ETHUSD",
    CanonicalAsset.SOL: "PF_SOLUSD",
    CanonicalAsset.XAU: "PF_XAUTUSD",
}


@pytest.fixture
def gate(tmp_path):
    config = load_config(
        {
            "TRADING_MODE": "paper", "TRADE_MARGIN_USD": "30",
            "MAX_LEVERAGE": "10", "DEFAULT_LEVERAGE": "10",
            "KILL_SWITCH_PATH": str(tmp_path / "KILL"),
        },
        dotenv_path=None,
    )
    store = Store(tmp_path / "a.db")
    ks = KillSwitch(path=tmp_path / "KILL")
    admission = SignalAdmission(
        config=config, store=store, kill_switch=ks, symbol_for=SYMBOLS
    )
    yield admission, store, ks
    store.close()


def ingest(store, text, message_id=1, reply_to=None):
    """Record + parse + persist parse output, exactly as production would."""
    row_id = store.record_message(
        channel_id=-100, message_id=message_id, raw_text=text,
        reply_to_message_id=reply_to,
    )
    parsed = parse_message(
        channel_id=-100, message_id=message_id, text=text,
        reply_to_message_id=reply_to,
    )
    store.update_message_parse(
        row_id,
        parsed_type=parsed.message_type.value,
        parsed_asset=parsed.asset.value if parsed.asset else None,
        signal_fingerprint=fingerprint_for(parsed),
    )
    return parsed, row_id


# ==========================================================================
# The spot boundary, enforced on the real path (brief 8)
# ==========================================================================


def test_spot_signal_refused_at_admission(gate):
    admission, store, _ = gate
    parsed, row = ingest(store, "BTC spot buy and hold from 64000")
    result = admission.admit(parsed, row)

    assert result.refused
    assert result.reason is RejectionReason.SPOT_SIGNAL
    assert store.get_message(-100, 1)["execution_decision"] == "IGNORED"


def test_spot_signal_with_full_leveraged_params_still_refused(gate):
    """The hard boundary: spot wins even carrying every leveraged field."""
    admission, store, _ = gate
    parsed, row = ingest(store, "BTC SPOT accumulation\n" + SIGNAL)
    assert admission.admit(parsed, row).reason is RejectionReason.SPOT_SIGNAL


# ==========================================================================
# Whitelist (brief 4) and the gold pin
# ==========================================================================


def test_unsupported_asset_refused_and_recorded(gate):
    admission, store, _ = gate
    parsed, row = ingest(
        store, "XRP LONG entry 2.10-2.05 TP1 2.2 TP2 2.3 TP3 2.4 TP4 2.5 SL 2.0"
    )
    result = admission.admit(parsed, row)
    assert result.refused
    assert store.get_message(-100, 1)["rejection_reason"] is not None


def test_gold_refused_with_its_own_reason(gate):
    """The pin must be visible in the audit trail, not a generic rejection."""
    admission, store, _ = gate
    parsed, row = ingest(
        store,
        "GOLD LONG Entry: 4000 - 3990 TP1 4010 TP2 4020 TP3 4030 TP4 4040 "
        "SL 3960 10x",
    )
    result = admission.admit(parsed, row)
    assert result.refused
    assert result.reason is RejectionReason.GOLD_PINNED_OFF
    assert "tracking study" in result.detail


def test_btc_admitted(gate):
    admission, store, _ = gate
    parsed, row = ingest(store, SIGNAL)
    assert parsed.message_type is MessageType.NEW_SIGNAL
    assert admission.admit(parsed, row).admitted


# ==========================================================================
# Duplicate detection, wired (brief 23)
# ==========================================================================


def test_reposted_signal_refused_as_duplicate(gate):
    admission, store, _ = gate
    parsed1, row1 = ingest(store, SIGNAL, message_id=1)
    assert admission.admit(parsed1, row1).admitted
    store.record_decision(row1, ExecutionDecision.EXECUTED)

    # Same signal, reformatted — different text, identical parsed levels.
    reformatted = """
    btc   long!!
    entry:  64000 - 63500
    tp1 65000
    tp2 66000
    tp3 67000
    tp4 68000
    sl:62500
    leverage 10x
    """
    parsed2, row2 = ingest(store, reformatted, message_id=2)
    result = admission.admit(parsed2, row2)

    assert result.refused
    assert result.reason is RejectionReason.DUPLICATE_SIGNAL
    assert store.get_message(-100, 2)["execution_decision"] == "DUPLICATE"


def test_different_levels_are_not_suppressed_as_duplicates(gate):
    """A genuinely new signal must not be blocked by dedupe."""
    admission, store, _ = gate
    parsed1, row1 = ingest(store, SIGNAL, message_id=1)
    admission.admit(parsed1, row1)
    store.record_decision(row1, ExecutionDecision.EXECUTED)

    parsed2, row2 = ingest(store, SIGNAL.replace("SL: 62500", "SL: 62000"),
                           message_id=2)
    # Same symbol is occupied only if a trade exists; none created here.
    assert admission.admit(parsed2, row2).reason is not RejectionReason.DUPLICATE_SIGNAL


def test_unexecuted_prior_signal_does_not_block(gate):
    """Only an EXECUTED prior counts — a refused one must not suppress a retry."""
    admission, store, _ = gate
    parsed1, row1 = ingest(store, SIGNAL, message_id=1)
    admission.admit(parsed1, row1)
    store.record_decision(row1, ExecutionDecision.REJECTED)

    parsed2, row2 = ingest(store, SIGNAL, message_id=2)
    assert admission.admit(parsed2, row2).admitted


def test_message_id_idempotency_is_enforced_at_ingest(gate):
    """Layer 1 of dedupe: the same message can never be recorded twice."""
    from bot.db.store import DuplicateMessage
    admission, store, _ = gate
    ingest(store, SIGNAL, message_id=7)
    with pytest.raises(DuplicateMessage):
        ingest(store, SIGNAL, message_id=7)


# ==========================================================================
# One active trade per symbol
# ==========================================================================


def test_second_signal_on_occupied_symbol_goes_to_review(gate):
    admission, store, _ = gate
    parsed1, row1 = ingest(store, SIGNAL, message_id=1)
    assert admission.admit(parsed1, row1).admitted
    store.create_trade(
        origin_message_row_id=row1, origin_channel_id=-100, origin_message_id=1,
        asset="BTC", symbol="PF_XBTUSD", direction="LONG",
    )

    parsed2, row2 = ingest(store, SIGNAL.replace("SL: 62500", "SL: 62100"),
                           message_id=2)
    result = admission.admit(parsed2, row2)
    assert result.refused
    assert result.reason is RejectionReason.SYMBOL_ALREADY_ACTIVE
    assert store.get_message(-100, 2)["execution_decision"] == "REQUIRES_REVIEW"


def test_other_symbols_unaffected_by_occupancy(gate):
    admission, store, _ = gate
    parsed1, row1 = ingest(store, SIGNAL, message_id=1)
    admission.admit(parsed1, row1)
    store.create_trade(
        origin_message_row_id=row1, origin_channel_id=-100, origin_message_id=1,
        asset="BTC", symbol="PF_XBTUSD", direction="LONG",
    )

    eth = SIGNAL.replace("BTC", "ETH").replace("64000", "1920").replace(
        "63500", "1910").replace("65000", "1930").replace("66000", "1940"
    ).replace("67000", "1950").replace("68000", "1960").replace("62500", "1890")
    parsed2, row2 = ingest(store, eth, message_id=2)
    assert admission.admit(parsed2, row2).admitted


def test_closed_trade_frees_the_symbol(gate):
    admission, store, _ = gate
    parsed1, row1 = ingest(store, SIGNAL, message_id=1)
    admission.admit(parsed1, row1)
    tid, _ = store.create_trade(
        origin_message_row_id=row1, origin_channel_id=-100, origin_message_id=1,
        asset="BTC", symbol="PF_XBTUSD", direction="LONG",
    )
    for state in (TradeState.PARSED, TradeState.VALIDATED,
                  TradeState.WAITING_FOR_ENTRY, TradeState.CLOSING,
                  TradeState.CLOSED):
        store.transition_trade(tid, state)

    parsed2, row2 = ingest(store, SIGNAL.replace("SL: 62500", "SL: 62200"),
                           message_id=2)
    assert admission.admit(parsed2, row2).admitted


# ==========================================================================
# Kill switch on the admission path
# ==========================================================================


def test_kill_switch_refuses_admission(gate):
    admission, store, ks = gate
    ks.engage()
    parsed, row = ingest(store, SIGNAL)
    result = admission.admit(parsed, row)
    assert result.refused
    assert result.reason is RejectionReason.KILL_SWITCH_ACTIVE


def test_kill_switch_does_not_mask_a_more_specific_reason(gate):
    """A spot signal under an engaged kill switch records SPOT_SIGNAL."""
    admission, store, ks = gate
    ks.engage()
    parsed, row = ingest(store, "BTC spot buy and hold")
    assert admission.admit(parsed, row).reason is RejectionReason.SPOT_SIGNAL


# ==========================================================================
# Every message gets a decision (brief 7)
# ==========================================================================


@pytest.mark.parametrize(
    "text",
    [
        "BTC spot buy and hold",
        "XRP LONG entry 2.1-2.0 TP1 2.2 TP2 2.3 TP3 2.4 TP4 2.5 SL 1.9",
        "gm everyone",
        "close it",
        "BTC LONG entry 64000-63500 SL 62500",
        SIGNAL.replace("BTC", "GOLD"),
    ],
)
def test_no_message_ends_without_a_recorded_decision(gate, text):
    admission, store, _ = gate
    parsed, row = ingest(store, text)
    admission.admit(parsed, row)
    stored = store.get_message(-100, 1)
    assert stored["execution_decision"] != "PENDING"
    if stored["execution_decision"] != "PENDING":
        assert stored["processed_at"] is not None


def test_refusals_carry_an_enumerated_reason(gate):
    admission, store, _ = gate
    parsed, row = ingest(store, "BTC spot hold")
    result = admission.admit(parsed, row)
    assert result.reason is not None
    assert isinstance(result.reason, RejectionReason)


def test_safety_review_recorded_as_requires_review(gate):
    admission, store, _ = gate
    parsed, row = ingest(store, "close it")
    assert parsed.message_type is MessageType.SAFETY_REVIEW
    result = admission.admit(parsed, row)
    assert result.decision is ExecutionDecision.REQUIRES_REVIEW
