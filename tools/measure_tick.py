"""Measure manage-loop tick cost and CLI latency under realistic contention.

The protection-latency fix needs a real distribution, not one 12s sample. This
runs the actual TradeManager.step_all() against the paper engine with a
concurrent reader competing for the same paper-state lock — the condition the
soak runs under.
"""

from __future__ import annotations

import statistics
import subprocess
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bot.config import load_config  # noqa: E402
from bot.db.store import Store  # noqa: E402
from bot.exchange.cli import KrakenCli  # noqa: E402
from bot.exchange.kraken import KrakenCliAdapter  # noqa: E402
from bot.execution.engine import ExecutionEngine  # noqa: E402
from bot.execution.manager import TradeManager  # noqa: E402
from bot.execution.observer import PollingFillObserver  # noqa: E402
from bot.killswitch import KillSwitch  # noqa: E402
from bot.models import CanonicalAsset, Direction, TradeState, TradingMode  # noqa: E402

D = Decimal
TMP = Path("/tmp/tickmeasure")
ASSETS = frozenset({CanonicalAsset.BTC, CanonicalAsset.ETH, CanonicalAsset.SOL})

stop_noise = threading.Event()


def noise():
    """A concurrent reader, as the soak orchestrator is."""
    while not stop_noise.is_set():
        subprocess.run(["kraken", "futures", "paper", "positions", "-o", "json"],
                       capture_output=True)
        subprocess.run(["kraken", "futures", "paper", "orders", "-o", "json"],
                       capture_output=True)


def pct(values, p):
    s = sorted(values)
    return s[min(int(round(p / 100 * (len(s) - 1))), len(s) - 1)]


def main() -> int:
    import shutil
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)

    subprocess.run(["kraken", "futures", "paper", "reset", "--yes", "-o", "json"],
                   capture_output=True)

    cfg = load_config({
        "TRADING_MODE": "paper", "TRADE_MARGIN_USD": "30", "MAX_LEVERAGE": "10",
        "DEFAULT_LEVERAGE": "10", "MAX_ENTRY_DEVIATION_PERCENT": "90",
        "MAX_SIZING_DEVIATION_PERCENT": "20",
        "DB_PATH": str(TMP / "m.db"), "KILL_SWITCH_PATH": str(TMP / "K"),
    }, dotenv_path=None)

    cli = KrakenCli(timeout=60)
    ad = KrakenCliAdapter(cli, TradingMode.PAPER, ASSETS)
    specs = ad.load_specs()
    store = Store(TMP / "m.db")
    engine = ExecutionEngine(
        config=cfg, store=store, client=ad,
        observer=PollingFillObserver(ad, default_timeout_s=20, default_poll_s=1),
        kill_switch=KillSwitch(path=TMP / "K"),
    )
    engine.mark_recovery_complete()
    manager = TradeManager(config=cfg, store=store, client=ad, engine=engine,
                           specs=specs)

    # --- raw CLI latency, uncontended then contended
    def time_call(n=12):
        out = []
        for _ in range(n):
            t = time.monotonic()
            ad.get_open_orders()
            out.append(time.monotonic() - t)
        return out

    solo = time_call()
    t = threading.Thread(target=noise, daemon=True)
    t.start()
    time.sleep(1)
    contended = time_call()

    # --- realistic trade set: 3 symbols, all filled and managed
    for asset, word, ratio in ((CanonicalAsset.BTC, "BTC", D("1.002")),
                               (CanonicalAsset.ETH, "ETH", D("1.002")),
                               (CanonicalAsset.SOL, "SOL", D("1.002"))):
        spec = specs[asset]
        mark = ad.get_mark_price(spec.symbol)
        msg = store.record_message(channel_id=-1, message_id=len(store.open_trades()) + 1,
                                   raw_text="x")
        tid, uid = store.create_trade(
            origin_message_row_id=msg, origin_channel_id=-1,
            origin_message_id=len(store.open_trades()) + 1,
            asset=asset.value, symbol=spec.symbol, direction="LONG",
            intent_stop_loss=mark * D("0.95"),
            intent_take_profits=[mark * D(r) for r in ("1.02", "1.04", "1.06", "1.08")],
        )
        store.transition_trade(tid, TradeState.PARSED)
        store.transition_trade(tid, TradeState.VALIDATED)
        plan = engine.plan_entries(spec=spec, direction=Direction.LONG,
                                   entry_prices=(mark * ratio, mark * D("1.001")),
                                   signal_leverage=D(10))
        engine.open_trade(trade_id=tid, trade_uid=uid, spec=spec, plan=plan)

    # --- tick cost with a full managed book, under contention
    ticks = []
    for _ in range(15):
        t0 = time.monotonic()
        manager.step_all()
        ticks.append(time.monotonic() - t0)

    stop_noise.set()
    time.sleep(0.5)

    print("=== raw CLI read latency (get_open_orders) ===")
    print(f"  uncontended : median {statistics.median(solo):.2f}s  max {max(solo):.2f}s")
    print(f"  contended   : median {statistics.median(contended):.2f}s  "
          f"p95 {pct(contended,95):.2f}s  max {max(contended):.2f}s")
    print()
    print(f"=== manage-loop tick, {len(store.open_trades())} managed trades, contended ===")
    print(f"  median {statistics.median(ticks):.2f}s")
    print(f"  p95    {pct(ticks,95):.2f}s")
    print(f"  max    {max(ticks):.2f}s")
    print()
    worst = max(ticks) + 3.0  # + MANAGE_INTERVAL
    print(f"  worst-case fill->protect via manage loop alone: "
          f"~{worst:.1f}s (max tick + 3s interval)")
    store.close()
    subprocess.run(["kraken", "futures", "paper", "reset", "--yes", "-o", "json"],
                   capture_output=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
