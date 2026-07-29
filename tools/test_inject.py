"""Inject synthetic Telegram messages into the real pipeline (brief 34).

    python tools/test_inject.py --dry-run "BTC LONG Entry: 64000-63500 ..."
    python tools/test_inject.py --file signals.txt
    echo "..." | python tools/test_inject.py --dry-run -

--dry-run parses and sizes without touching the exchange. Without it, the
message goes through the SAME pipeline the Telegram listener uses, so what you
see is what production does.
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from bot.config import load_config  # noqa: E402
from bot.db.store import Store  # noqa: E402
from bot.exchange.cli import KrakenCli  # noqa: E402
from bot.exchange.kraken import KrakenCliAdapter  # noqa: E402
from bot.execution.engine import ExecutionEngine  # noqa: E402
from bot.execution.observer import PollingFillObserver  # noqa: E402
from bot.killswitch import KillSwitch  # noqa: E402
from bot.models import Direction  # noqa: E402
from bot.parser.parse import fingerprint_for, parse_message  # noqa: E402
from bot.pipeline import SignalPipeline, build_admission  # noqa: E402
from bot.reconcile import Reconciler  # noqa: E402
from bot.recovery import RecoveryManager  # noqa: E402
from bot.sizing import SizingError, effective_leverage  # noqa: E402

INJECT_CHANNEL = -999_000_001


def show_parse(text: str, message_id: int) -> object:
    parsed = parse_message(
        channel_id=INJECT_CHANNEL, message_id=message_id, text=text
    )
    print("Parsed message")
    print(f"  Type          : {parsed.message_type.value}")
    print(f"  Asset         : {parsed.asset.value if parsed.asset else '—'}")
    print(f"  Direction     : {parsed.direction.value if parsed.direction else '—'}")
    print(f"  Entry         : {', '.join(str(p) for p in parsed.entry_range) or '—'}")
    for i, tp in enumerate(parsed.take_profits, start=1):
        print(f"  TP{i}           : {tp}")
    print(f"  Stop loss     : {parsed.stop_loss if parsed.stop_loss else '—'}")
    print(f"  Leverage      : {parsed.leverage if parsed.leverage else '— (default)'}")
    print(f"  Percentage    : {parsed.percentage if parsed.percentage else '—'}")
    print(f"  Confidence    : {parsed.confidence}")
    print(f"  Fingerprint   : {fingerprint_for(parsed) or '—'}")
    if parsed.parse_errors:
        print("  Errors        :")
        for e in parsed.parse_errors:
            print(f"     - {e}")
    if parsed.parse_warnings:
        print("  Notes         :")
        for w in parsed.parse_warnings:
            print(f"     - [{w.code}] {w.detail}")
    return parsed


def show_sizing(config, parsed, adapter) -> None:
    if parsed.asset is None or not parsed.entry_range:
        print("\nSizing: not applicable (no asset or entry range)")
        return
    if not config.is_tradeable(parsed.asset):
        print(f"\nSizing: {parsed.asset.value} is not tradeable in this build")
        return
    try:
        specs = adapter.load_specs()
        spec = specs[parsed.asset]
        leverage = effective_leverage(
            parsed.leverage,
            max_leverage=config.max_leverage,
            default_leverage=config.default_leverage,
            exchange_max_leverage=spec.max_leverage,
        )
        mark = adapter.get_mark_price(spec.symbol)
        print(f"\nInstrument {spec.symbol} (mark {mark})")
        print(f"  Requested lev : {parsed.leverage or '— (default)'}")
        print(f"  Effective lev : {leverage}x")
        print(f"  Margin        : ${config.trade_margin_usd}")
        print(f"  Notional      : ${config.trade_margin_usd * leverage}")

        from bot.sizing import split_entry_allocations, calculate_size
        allocations = split_entry_allocations(
            config.trade_margin_usd, len(parsed.entry_range)
        )
        print("\nWould submit")
        total = Decimal(0)
        for i, (price, margin) in enumerate(
            zip(parsed.entry_range, allocations), start=1
        ):
            sized = calculate_size(
                margin_usd=margin, leverage=leverage, price=price, spec=spec,
                max_deviation_percent=config.max_sizing_deviation_percent,
            )
            total += sized.size
            print(f"  Entry {i}       : {sized.size} @ {price} "
                  f"(${margin} margin, dev {sized.deviation_percent:.2f}%)")
        if parsed.take_profits:
            from bot.sizing import split_take_profits
            parts = split_take_profits(total, spec, levels=len(parsed.take_profits))
            for i, (tp, size) in enumerate(zip(parsed.take_profits, parts), start=1):
                print(f"  TP{i}           : {size} @ {tp}")
        print(f"  Stop loss     : {total} @ {parsed.stop_loss}")
    except SizingError as exc:
        print(f"\nSizing REFUSED: {exc}")
    except Exception as exc:
        print(f"\nSizing unavailable: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(prog="test_inject")
    ap.add_argument("text", nargs="?", help="message text, or - for stdin")
    ap.add_argument("--file", help="file of messages separated by blank lines")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and size only; never touches the exchange")
    ap.add_argument("--message-id", type=int, default=None)
    args = ap.parse_args()

    if args.file:
        blocks = [b.strip() for b in Path(args.file).read_text().split("\n\n") if b.strip()]
    elif args.text == "-":
        blocks = [sys.stdin.read()]
    elif args.text:
        blocks = [args.text]
    else:
        ap.error("provide message text, --file, or - for stdin")

    config = load_config()
    print(f"TRADING MODE: {config.trading_mode.value.upper()}"
          f"{'  (dry-run: nothing will be sent)' if args.dry_run else ''}\n")

    adapter = None
    if config.trading_mode.touches_exchange:
        adapter = KrakenCliAdapter(
            KrakenCli(binary=config.kraken_cli_path,
                      timeout=config.kraken_cli_timeout_seconds, env=dict(os.environ)),
            config.trading_mode, config.tradeable_assets,
        )

    store = Store(config.db_path)
    kill_switch = KillSwitch(path=config.kill_switch_path)
    base_id = args.message_id or (store.last_message_id(INJECT_CHANNEL) or 0) + 1

    pipeline = None
    if not args.dry_run and adapter is not None:
        specs = adapter.load_specs()
        engine = ExecutionEngine(
            config=config, store=store, client=adapter,
            observer=PollingFillObserver(adapter), kill_switch=kill_switch,
        )
        reconciler = Reconciler(config=config, store=store, client=adapter,
                                engine=engine, specs=specs)
        recovery = RecoveryManager(config=config, store=store, client=adapter,
                                   engine=engine, reconciler=reconciler, specs=specs)
        result = recovery.recover()
        if not result.safe_to_trade:
            print(f"REFUSING TO INJECT: recovery incomplete — {result.blocking_reason}")
            return 3
        pipeline = SignalPipeline(
            config=config, store=store,
            admission=build_admission(config=config, store=store,
                                      kill_switch=kill_switch, client=adapter,
                                      specs=specs),
            engine=engine, specs=specs,
        )

    for offset, block in enumerate(blocks):
        message_id = base_id + offset
        print("=" * 70)
        parsed = show_parse(block, message_id)
        if adapter is not None:
            show_sizing(config, parsed, adapter)

        if pipeline is not None:
            outcome = pipeline.handle_message(
                channel_id=INJECT_CHANNEL, message_id=message_id, text=block
            )
            print(f"\nPipeline: accepted={outcome.accepted} "
                  f"trade_id={outcome.trade_id} {outcome.detail}")
        print()

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
