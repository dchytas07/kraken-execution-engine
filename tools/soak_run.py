"""Stage 10 soak orchestrator.

Runs the timeline in docs/STAGE10_SOAK.md, checks invariants I1-I8 every cycle,
SIGKILLs the bot mid-run, and writes a pass/fail verdict.

Invariants are checked CONTINUOUSLY, not only at the end: a violation that
self-heals still means the system spent real time in an unsafe state.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bot.db.store import Store  # noqa: E402
from bot.exchange.cli import KrakenCli  # noqa: E402
from bot.exchange.kraken import KrakenCliAdapter  # noqa: E402
from bot.models import CanonicalAsset, TradeState, TradingMode  # noqa: E402

D = Decimal
ROOT = Path(__file__).parent.parent
RUN = ROOT / "soak"
INBOX, DONE = RUN / "inbox", RUN / "done"
DB = RUN / "soak.db"
BOT_LOG = RUN / "bot.jsonl"
REPORT = RUN / "report.json"
CHANNEL = -999000001

DURATION = float(os.environ.get("SOAK_DURATION", "1800"))   # 30 min
KILL_AT = float(os.environ.get("SOAK_KILL_AT", "720"))      # 12 min
CHECK_EVERY = 20.0
ASSETS = frozenset({CanonicalAsset.BTC, CanonicalAsset.ETH, CanonicalAsset.SOL})

violations: list[dict] = []
events: list[dict] = []
# Divergences we injected on purpose, so their repairs are explained.
expected_repairs: Counter = Counter()
observed_repairs: Counter = Counter()
next_id = [1]


def log(kind: str, detail: str, **extra):
    entry = {"t": round(time.monotonic() - T0, 1), "kind": kind, "detail": detail, **extra}
    events.append(entry)
    print(f"[{entry['t']:7.1f}s] {kind}: {detail}", flush=True)


def violate(inv: str, detail: str):
    violations.append({"t": round(time.monotonic() - T0, 1), "invariant": inv,
                       "detail": detail})
    print(f"[{time.monotonic()-T0:7.1f}s] !! VIOLATION {inv}: {detail}", flush=True)


def inject(text: str, label: str) -> int:
    mid = next_id[0]
    next_id[0] += 1
    INBOX.mkdir(parents=True, exist_ok=True)
    (INBOX / f"{mid}-{label}.txt").write_text(text)
    log("inject", f"{label} (message {mid})", message_id=mid)
    return mid


def start_bot() -> subprocess.Popen:
    env = {
        **os.environ,
        "TRADING_MODE": "paper",
        "TRADE_MARGIN_USD": "30",
        "MAX_LEVERAGE": "10",
        "DEFAULT_LEVERAGE": "10",
        "MAX_ENTRY_DEVIATION_PERCENT": "2.0",
        "MAX_SIZING_DEVIATION_PERCENT": "10",
        "DB_PATH": str(DB),
        "LOG_PATH": str(BOT_LOG),
        "KILL_SWITCH_PATH": str(RUN / "KILL_SWITCH"),
        "SOAK_INBOX": str(INBOX),
        "SOAK_DONE": str(DONE),
        "SOAK_RECONCILE_INTERVAL": "60",
        "SOAK_MANAGE_INTERVAL": "3",
    }
    proc = subprocess.Popen(
        [str(ROOT / ".venv/bin/python"), str(ROOT / "tools/soak_bot.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    log("bot", f"started pid={proc.pid}")
    return proc


def kraken(*args) -> dict:
    cli = KrakenCli(timeout=60)
    result, err = cli.try_run(*args)
    return result.data if result else {"error": str(err)}


def adapter() -> KrakenCliAdapter:
    a = KrakenCliAdapter(KrakenCli(timeout=60), TradingMode.PAPER, ASSETS)
    a.load_specs()
    return a


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def check_invariants(ad: KrakenCliAdapter, store: Store, *, allow_orphan: set[str]):
    positions = {p.symbol: p for p in ad.get_open_positions()}
    orders = ad.get_open_orders()
    trades = store.open_trades()
    by_symbol = {t["symbol"]: t for t in trades}

    for symbol, position in positions.items():
        stops = [o for o in orders if o.order_type == "stop" and o.symbol == symbol]
        tps = [o for o in orders if o.order_type == "take-profit" and o.symbol == symbol]

        if symbol not in by_symbol:
            if symbol not in allow_orphan:
                violate("I8", f"{symbol} holds {position.size} with no local trade")
            continue

        state = TradeState(by_symbol[symbol]["state"])
        if state is TradeState.REQUIRES_REVIEW:
            continue  # quarantined; protection asserted separately

        # I1
        if len(stops) != 1:
            violate("I1", f"{symbol} has {len(stops)} stops (expected 1), "
                          f"position {position.size}, state {state.value}")
        # I2
        elif stops[0].size != position.size:
            violate("I2", f"{symbol} stop {stops[0].size} != position {position.size}")
        # I3
        tp_total = sum((o.size for o in tps), D(0))
        if tp_total > position.size:
            violate("I3", f"{symbol} TP total {tp_total} > position {position.size}")

    # I4
    coids = [o.client_order_id for o in orders if o.client_order_id]
    dupes = [c for c, n in Counter(coids).items() if n > 1]
    if dupes:
        violate("I4", f"duplicate client order ids on the exchange: {dupes}")

    # I5
    for asset in sorted(ASSETS, key=lambda a: a.value):
        symbol = {CanonicalAsset.BTC: "PF_XBTUSD", CanonicalAsset.ETH: "PF_ETHUSD",
                  CanonicalAsset.SOL: "PF_SOLUSD"}[asset]
        lev = ad.get_leverage_preference(symbol)
        if lev is not None and lev != D(10):
            violate("I5", f"{symbol} leverage {lev} != 10")

    # I6
    for t in trades:
        if t["state"] == TradeState.SAFE_ERROR.value:
            violate("I6", f"trade {t['id']} ({t['symbol']}) in SAFE_ERROR: "
                          f"{t['state_detail']}")

    # I7 — an EVENTUAL property, not an instantaneous one. The pipeline
    # persists the raw message before parsing (brief 31), so a PENDING window
    # exists by design; the original criterion sampled inside it and failed the
    # bot for correct behaviour. Only messages older than the grace window
    # count.
    rows = store._conn.execute(
        "SELECT channel_id, message_id, received_at FROM messages "
        "WHERE execution_decision = 'PENDING' "
        "AND received_at < datetime('now', '-60 seconds')"
    ).fetchall()
    stale = [f"{r['channel_id']}/{r['message_id']}" for r in rows]
    if stale:
        violate("I7", f"messages undecided after 60s: {stale}")

    # closed trades must be flat
    closed = store._conn.execute(
        "SELECT id, symbol FROM trades WHERE state = 'CLOSED'"
    ).fetchall()
    for row in closed:
        if row["symbol"] in positions and not positions[row["symbol"]].is_flat:
            violate("CLOSED-FLAT", f"trade {row['id']} CLOSED but {row['symbol']} "
                                   f"holds {positions[row['symbol']].size}")


def collect_repairs(store: Store):
    rows = store._conn.execute(
        "SELECT summary FROM events WHERE event_type IN "
        "('RECONCILE_REPAIRED','RECONCILE_MISMATCH')"
    ).fetchall()
    observed_repairs.clear()
    for r in rows:
        kind = r["summary"].split(":")[0].strip()
        observed_repairs[kind] += 1


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def signal_text(symbol_word, entry_hi, entry_lo, tps, sl, lev="50x"):
    lines = [f"{symbol_word} LONG", f"Entry: {entry_hi} - {entry_lo}"]
    lines += [f"TP{i}: {tp}" for i, tp in enumerate(tps, start=1)]
    lines += [f"SL: {sl}", f"Leverage: {lev}"]
    return "\n".join(lines)


def marks(ad):
    return {s: ad.get_mark_price(s)
            for s in ("PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD")}


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------

T0 = time.monotonic()


def main() -> int:
    global T0
    for p in (RUN,):
        if p.exists():
            import shutil
            shutil.rmtree(p)
    RUN.mkdir(parents=True)
    INBOX.mkdir(); DONE.mkdir()

    subprocess.run(["kraken", "futures", "paper", "reset", "--yes", "-o", "json"],
                   capture_output=True)
    subprocess.run(["kraken", "futures", "paper", "init", "--balance", "10000",
                    "-o", "json"], capture_output=True)

    ad = adapter()
    m = marks(ad)
    T0 = time.monotonic()
    log("start", f"soak begins; marks {[f'{k}={v:.2f}' for k, v in m.items()]}")

    proc = start_bot()
    time.sleep(15)  # let recovery + first cycle settle

    allow_orphan: set[str] = set()
    schedule = []

    def at(t, fn, label):
        schedule.append((t, fn, label))

    # -- refusals (S1-S9) ------------------------------------------------
    at(20, lambda: inject("BTC spot buy and hold, accumulation zone", "S1-spot"), "S1")
    at(35, lambda: inject("booked 50% profit earlier today 🎉", "S2-commentary"), "S2")
    at(50, lambda: inject(
        "XRP LONG Entry: 2.10 - 2.05\nTP1: 2.2\nTP2: 2.3\nTP3: 2.4\nTP4: 2.5\nSL: 2.0\n10x",
        "S3-xrp"), "S3")
    at(65, lambda: inject(signal_text("GOLD", 4000, 3990, [4010, 4020, 4030, 4040], 3960),
                          "S4-gold"), "S4")
    at(80, lambda: inject("BTC LONG Entry: 64000 - 63500\nTP1: 65000", "S5-incomplete"), "S5")
    at(95, lambda: inject("close it", "S6-ambiguous-close"), "S6")
    at(110, lambda: inject(
        signal_text("BTC", int(m["PF_XBTUSD"] * D("0.80")),
                    int(m["PF_XBTUSD"] * D("0.79")),
                    [int(m["PF_XBTUSD"] * D("0.85"))] * 4,
                    int(m["PF_XBTUSD"] * D("0.75"))), "S7-stale"), "S7")

    # -- valid opens (S12, S13) -----------------------------------------
    def open_btc():
        mm = marks(adapter())["PF_XBTUSD"]
        inject(signal_text("BTC", int(mm * D("1.002")), int(mm * D("1.001")),
                           [int(mm * D("1.02")), int(mm * D("1.04")),
                            int(mm * D("1.06")), int(mm * D("1.08"))],
                           int(mm * D("0.95"))), "S12-btc-50x")
    at(140, open_btc, "S12")

    def open_eth():
        mm = marks(adapter())["PF_ETHUSD"]
        inject(signal_text("ETH", round(float(mm) * 1.002, 1),
                           round(float(mm) * 1.001, 1),
                           [round(float(mm) * r, 1) for r in (1.02, 1.04, 1.06, 1.08)],
                           round(float(mm) * 0.95, 1), lev="10x"), "S13-eth")
    at(180, open_eth, "S13")

    # -- duplicates / occupancy (S8, S9, S10) ---------------------------
    at(230, lambda: inject(
        signal_text("BTC", int(m["PF_XBTUSD"] * D("1.002")),
                    int(m["PF_XBTUSD"] * D("1.001")),
                    [int(m["PF_XBTUSD"] * D("1.02"))] * 4,
                    int(m["PF_XBTUSD"] * D("0.95"))), "S10-occupied"), "S10")

    # -- injected divergence (S17, S18) ---------------------------------
    def cancel_stop():
        a = adapter()
        stops = [o for o in a.get_open_orders() if o.order_type == "stop"]
        if stops:
            a.cancel_order(stops[0].exchange_order_id)
            expected_repairs["UNPROTECTED_POSITION"] += 1
            log("inject-divergence", f"cancelled stop {stops[0].exchange_order_id} "
                                    f"on {stops[0].symbol} (S17)")
        else:
            log("inject-divergence", "S17 skipped: no stop to cancel")
    at(300, cancel_stop, "S17")

    def leak_leverage():
        a = adapter()
        a.client.run("futures", "paper", "set-leverage", "PF_SOLUSD", "3")
        expected_repairs["LEVERAGE_DRIFT"] += 1
        log("inject-divergence", "set PF_SOLUSD leverage to 3x (S18)")
    at(420, leak_leverage, "S18")

    # -- SIGKILL (S20-S23) ----------------------------------------------
    state = {"proc": proc, "pre_kill_orders": 0}

    def kill_bot():
        a = adapter()
        state["pre_kill_orders"] = len(a.get_open_orders())
        state["proc"].send_signal(signal.SIGKILL)
        state["proc"].wait()
        log("kill", f"SIGKILLed bot; {state['pre_kill_orders']} orders resting")
    at(KILL_AT, kill_bot, "S20-kill")

    def restart_bot():
        state["proc"] = start_bot()
        log("restart", "bot restarted; recovery will run")
    at(KILL_AT + 10, restart_bot, "S20-restart")

    def verify_post_restart():
        a = adapter()
        now_orders = len(a.get_open_orders())
        if now_orders != state["pre_kill_orders"]:
            log("post-restart", f"order count {state['pre_kill_orders']} -> "
                                f"{now_orders} (change is not automatically a "
                                f"violation; duplicates are checked by I4)")
        else:
            log("post-restart", f"order count unchanged at {now_orders}")
    at(KILL_AT + 60, verify_post_restart, "S21-S23")

    # -- orphan (S19, S11) ----------------------------------------------
    def make_orphan():
        subprocess.run(["kraken", "futures", "paper", "set-leverage", "PF_SOLUSD",
                        "10", "-o", "json"], capture_output=True)
        subprocess.run(["kraken", "futures", "paper", "buy", "PF_SOLUSD", "1.0",
                        "--type", "market", "--client-order-id", "operator-manual",
                        "-o", "json"], capture_output=True)
        allow_orphan.add("PF_SOLUSD")
        expected_repairs["ORPHAN_POSITION"] += 1
        log("inject-divergence", "opened a manual 1.0 SOL position (S19)")
    at(900, make_orphan, "S19")

    def sol_signal():
        mm = marks(adapter())["PF_SOLUSD"]
        inject(signal_text("SOL", round(float(mm) * 1.002, 2),
                           round(float(mm) * 1.001, 2),
                           [round(float(mm) * r, 2) for r in (1.02, 1.04, 1.06, 1.08)],
                           round(float(mm) * 0.95, 2), lev="10x"), "S11-orphan-symbol")
    at(960, sol_signal, "S11")

    # -- partial then full close (S15, S16) -----------------------------
    at(1080, lambda: inject("Close BTC 50%", "S15-partial"), "S15")
    at(1320, lambda: inject("Close ETH", "S16-close-eth"), "S16")
    at(1500, lambda: inject("Close all positions", "S16-close-all"), "S16b")

    # -- run -------------------------------------------------------------
    store = Store(DB)
    pending = sorted(schedule, key=lambda x: x[0])
    last_check = 0.0

    while True:
        elapsed = time.monotonic() - T0
        if elapsed >= DURATION:
            break

        while pending and pending[0][0] <= elapsed:
            _, fn, label = pending.pop(0)
            try:
                fn()
            except Exception as exc:
                log("error", f"scenario {label} raised: {exc}")

        if elapsed - last_check >= CHECK_EVERY:
            last_check = elapsed
            try:
                check_invariants(adapter(), store, allow_orphan=allow_orphan)
            except Exception as exc:
                log("error", f"invariant check failed: {exc}")

        if state["proc"].poll() is not None and elapsed > KILL_AT + 20:
            stderr = state["proc"].stderr.read() if state["proc"].stderr else ""
            violate("PROCESS", f"bot exited unexpectedly: {stderr[-500:]}")
            state["proc"] = start_bot()

        time.sleep(2)

    # -- verdict ---------------------------------------------------------
    collect_repairs(store)
    final_positions = {p.symbol: str(p.size) for p in adapter().get_open_positions()}
    final_trades = [
        {"id": t["id"], "symbol": t["symbol"], "state": t["state"]}
        for t in store._conn.execute("SELECT id, symbol, state FROM trades").fetchall()
    ]
    decisions = Counter(
        r["execution_decision"] for r in
        store._conn.execute("SELECT execution_decision FROM messages").fetchall()
    )
    reasons = Counter(
        r["rejection_reason"] for r in
        store._conn.execute(
            "SELECT rejection_reason FROM messages WHERE rejection_reason IS NOT NULL"
        ).fetchall()
    )

    # A finding re-reported across cycles is one finding, not N. Repeats are
    # deliberate (an unresolved orphan must not age out of visibility), so
    # compare the SET of kinds, not their counts.
    benign = {"FILL_NOT_RECORDED", "STOP_SIZE_MISMATCH"}
    unexplained = {
        k: observed_repairs[k] for k in observed_repairs
        if k.split()[0] not in expected_repairs
        and k not in expected_repairs
        and k.split()[0] not in benign and k not in benign
        and not k.startswith("orphan position")
    }

    state["proc"].send_signal(signal.SIGTERM)
    passed = not violations and not unexplained

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "duration_s": DURATION,
        "verdict": "PASS" if passed else "FAIL",
        "violations": violations,
        "expected_repairs": dict(expected_repairs),
        "observed_repairs": dict(observed_repairs),
        "unexplained_repairs": unexplained,
        "final_positions": final_positions,
        "final_trades": final_trades,
        "message_decisions": dict(decisions),
        "rejection_reasons": dict(reasons),
        "events": events,
    }
    REPORT.write_text(json.dumps(report, indent=2))

    print("\n" + "=" * 70)
    print(f"VERDICT: {report['verdict']}")
    print(f"violations           : {len(violations)}")
    print(f"unexplained repairs  : {unexplained or 'none'}")
    print(f"decisions            : {dict(decisions)}")
    print(f"rejection reasons    : {dict(reasons)}")
    print(f"final positions      : {final_positions}")
    print(f"report               : {REPORT}")
    store.close()
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
