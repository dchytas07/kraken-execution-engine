"""`--status`: read-only, and safe beside the live service.

The safety properties are the point of this module, so they are asserted
directly rather than assumed: no write to the database, and no use of the
`kraken futures paper` subcommands that take the paper-state lock.
"""

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from bot import status as status_mod
from bot.config import load_config
from bot.db.store import Store
from bot.models import OrderPurpose, OrderStatus, TradeState


@pytest.fixture
def rig(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    store = Store(db)
    msg = store.record_message(channel_id=-1, message_id=1, raw_text="sig")
    tid, uid = store.create_trade(
        origin_message_row_id=msg, origin_channel_id=-1, origin_message_id=1,
        asset="BTC", symbol="PF_XBTUSD", direction="SHORT",
    )
    store.transition_trade(tid, TradeState.PARSED)
    store.transition_trade(tid, TradeState.VALIDATED)
    store.transition_trade(tid, TradeState.WAITING_FOR_ENTRY)
    store.transition_trade(tid, TradeState.FULLY_ENTERED)
    store.update_trade_actuals(tid, actual_quantity=Decimal("0.0046"),
                               actual_avg_entry_price=Decimal("63630"))
    oid, coid = store.record_order_intent(
        trade_id=tid, client_order_id=f"{uid}-SL-1", purpose=OrderPurpose.STOP_LOSS,
        symbol="PF_XBTUSD", side="buy", order_type="stop",
        quantity=Decimal("0.0046"), sequence=1,
    )
    store.update_order(oid, status=OrderStatus.OPEN,
                   exchange_order_id="FP-1", submitted=True)
    store.close()

    state = tmp_path / "futures_state.json"
    state.write_text(json.dumps({
        "positions": [
            # Capitalised, as the real state file writes it. The CLI lowercases
            # it, and comparing the two forms naively reported every position
            # as mismatched.
            {"symbol": "PF_XBTUSD", "side": "Short", "size": 0.0046,
             "entry_price": 63630.0},
        ],
        "open_orders": [
            {"client_order_id": f"{uid}-SL-1", "symbol": "PF_XBTUSD",
             "side": "buy", "size": 0.0046, "status": "open"},
        ],
    }))
    monkeypatch.setattr(status_mod, "PAPER_STATE", state)
    # No mark lookup in tests: it shells out, and this must stay hermetic.
    monkeypatch.setattr(status_mod, "_marks", lambda syms, binary: {})

    config = load_config({
        "TRADING_MODE": "paper", "TRADE_MARGIN_USD": "30",
        "MAX_LEVERAGE": "10", "DEFAULT_LEVERAGE": "10",
        "MAX_ENTRY_DEVIATION_PERCENT": "1.5",
        "DB_PATH": str(db), "KILL_SWITCH_PATH": str(tmp_path / "K"),
    }, dotenv_path=None)
    return config, db, state, tid, uid


def test_reports_the_open_trade_with_protection(rig):
    config, *_ = rig
    out = status_mod.build_status(config)
    assert "OPEN TRADES (1)" in out
    assert "PF_XBTUSD" in out and "SHORT" in out
    assert "0.0046" in out and "63630" in out
    assert "FULLY_ENTERED" in out
    assert "[PROTECTED]" in out


def test_matching_state_reports_ok_not_a_mismatch(rig):
    """Regression: the state file capitalises side ("Short") while the CLI
    lowercases it, which made every position look mismatched."""
    config, *_ = rig
    out = status_mod.build_status(config)
    assert "OK —" in out
    assert "MISMATCH" not in out


def test_a_real_size_divergence_is_flagged(rig):
    config, db, state, *_ = rig
    d = json.loads(state.read_text())
    d["positions"][0]["size"] = 0.0023          # exchange holds half
    state.write_text(json.dumps(d))
    out = status_mod.build_status(config)
    assert "MISMATCH" in out
    assert "size local 0.0046 vs exchange 0.0023" in out


def test_missing_stop_is_called_out(rig):
    config, db, *_ = rig
    with sqlite3.connect(db) as c:
        c.execute("UPDATE orders SET status='CANCELLED' WHERE purpose='SL'")
    out = status_mod.build_status(config)
    assert "** NO STOP **" in out


def test_orphan_position_is_reported(rig):
    config, db, state, *_ = rig
    d = json.loads(state.read_text())
    d["positions"].append({"symbol": "PF_SOLUSD", "side": "Long", "size": 1.0,
                           "entry_price": 73.94})
    state.write_text(json.dumps(d))
    out = status_mod.build_status(config)
    assert "ORPHAN — PF_SOLUSD" in out


def test_unresolved_trades_are_listed_with_their_age(rig):
    config, db, state, tid, _ = rig
    store = Store(db)
    store.transition_trade(tid, TradeState.REQUIRES_REVIEW, "needs a human")
    store.close()
    out = status_mod.build_status(config)
    assert "NEEDS REVIEW (1)" in out
    assert "REQUIRES_REVIEW" in out
    assert "stuck ~" in out
    assert "needs a human" in out


def test_orders_on_terminal_trades_are_history_not_divergence(rig):
    """Regression: a non-final order row on an already-CLOSED trade was
    reported as "live locally but not on the exchange"."""
    config, db, state, tid, uid = rig
    store = Store(db)
    store.transition_trade(tid, TradeState.CLOSING)
    store.transition_trade(tid, TradeState.CLOSED)
    store.close()
    d = json.loads(state.read_text())
    d["positions"], d["open_orders"] = [], []
    state.write_text(json.dumps(d))

    out = status_mod.build_status(config)
    assert "MISMATCH" not in out
    assert "left non-final on already-closed trades" in out


def test_status_never_writes_to_the_database(rig):
    """The whole point: safe to run beside the live service."""
    config, db, *_ = rig
    before = db.read_bytes()
    status_mod.build_status(config)
    assert db.read_bytes() == before, "status mutated the database"


def test_status_never_shells_out_to_a_paper_subcommand(rig, monkeypatch):
    """`kraken futures paper ...` takes the paper-state lock; a call killed
    mid-flight orphans it and wedges the running bot. Status must never use
    those subcommands."""
    calls = []
    real = status_mod.subprocess.run
    monkeypatch.setattr(status_mod.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or real(
                            ["true"], capture_output=True, text=True))
    monkeypatch.setattr(status_mod, "_marks", status_mod._marks)  # un-stub
    status_mod.build_status(rig[0])
    for argv in calls:
        assert "paper" not in argv, f"status used a paper subcommand: {argv}"


def test_unreadable_paper_state_is_reported_not_raised(rig):
    config, db, state, *_ = rig
    state.write_text("{ this is not json")
    out = status_mod.build_status(config)   # must not raise
    assert "UNAVAILABLE" in out


# ---------------------------------------------------------------------------
# State-file path resolution: same as kraken-cli, per OS
# ---------------------------------------------------------------------------

def test_linux_path_is_dot_config(tmp_path):
    p = status_mod.paper_state_path({"HOME": str(tmp_path)}, "linux")
    assert p == tmp_path / ".config/kraken/paper/futures_state.json"


def test_linux_honours_absolute_xdg_config_home(tmp_path):
    env = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    assert status_mod.paper_state_path(env, "linux") == (
        tmp_path / "xdg/kraken/paper/futures_state.json")


def test_linux_ignores_relative_xdg_config_home(tmp_path):
    env = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": "relative/dir"}
    assert status_mod.paper_state_path(env, "linux") == (
        tmp_path / ".config/kraken/paper/futures_state.json")


def test_macos_path_is_application_support(tmp_path):
    p = status_mod.paper_state_path({"HOME": str(tmp_path)}, "darwin")
    assert p == (tmp_path
                 / "Library/Application Support/kraken/paper/futures_state.json")


def test_windows_path_uses_appdata():
    p = status_mod.paper_state_path({"HOME": "/h", "APPDATA": "/appdata"}, "win32")
    assert p == Path("/appdata/kraken/paper/futures_state.json")


def test_default_resolution_is_used_when_no_override(monkeypatch, tmp_path):
    state = tmp_path / ".config/kraken/paper/futures_state.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"positions": [], "open_orders": []}')
    monkeypatch.setattr(status_mod, "PAPER_STATE", None)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(status_mod.sys, "platform", "linux")
    got, note = status_mod._read_paper_state()
    assert got == {"positions": [], "open_orders": []} and note == ""


def test_reading_state_is_lock_free_and_non_mutating(monkeypatch, tmp_path):
    """Read-only: no lock file created, state file bytes and mtime untouched,
    and no `kraken ... paper` subprocess is spawned."""
    state = tmp_path / ".config/kraken/paper/futures_state.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"positions": [], "open_orders": []}')
    before = (state.read_bytes(), state.stat().st_mtime_ns)
    monkeypatch.setattr(status_mod, "PAPER_STATE", None)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(status_mod.sys, "platform", "linux")
    monkeypatch.setattr(status_mod.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    status_mod._read_paper_state()
    assert sorted(p.name for p in state.parent.iterdir()) == ["futures_state.json"]
    assert (state.read_bytes(), state.stat().st_mtime_ns) == before
