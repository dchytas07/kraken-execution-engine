"""Entrypoint and startup validation.

Stage 2 provides `--check`, which performs startup validation and exits. The
trading loops arrive in later stages; this deliberately does nothing else yet
rather than pretending to trade.

    python -m bot.main --check
"""

from __future__ import annotations

import argparse
import os
import sys

from .config import Config, ConfigError, collect_secrets, load_config
from .db.store import Store
from .exchange.cli import KrakenCli
from .killswitch import KillSwitch
from .logging_setup import get_logger, setup_logging
from .models import EventType
from .safety.permissions import SafetyCheckFailed, enforce, run_permission_checks

log = get_logger("bot.main")


def build_cli(config: Config) -> KrakenCli:
    return KrakenCli(
        binary=config.kraken_cli_path,
        timeout=config.kraken_cli_timeout_seconds,
        env=dict(os.environ),
    )


def has_futures_credentials(env: dict[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    return bool(
        (source.get("KRAKEN_FUTURES_API_KEY") or "").strip()
        and (source.get("KRAKEN_FUTURES_API_SECRET") or "").strip()
    )


def has_spot_credentials(env: dict[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    return bool(
        (source.get("KRAKEN_API_KEY") or "").strip()
        and (source.get("KRAKEN_API_SECRET") or "").strip()
    )


def startup(config: Config) -> tuple[Store, KillSwitch]:
    """Run every startup gate. Raises rather than degrading silently."""
    for line in config.startup_banner():
        log.info(line, extra={"action": "startup_banner"})

    store = Store(config.db_path)
    store.append_event(
        EventType.STARTUP,
        f"startup in {config.trading_mode.value} mode",
        detail={
            "mode": config.trading_mode.value,
            "margin_usd": str(config.trade_margin_usd),
            "max_leverage": str(config.max_leverage),
            "gold_enabled": config.allow_xaut_for_gold,
        },
    )

    kill_switch = KillSwitch(path=config.kill_switch_path)
    if kill_switch.is_engaged():
        log.warning(
            "starting with the kill switch ENGAGED — existing positions will be "
            "managed, no new positions will be opened",
            extra={"action": "startup"},
        )

    results = run_permission_checks(
        build_cli(config),
        config.trading_mode,
        has_futures_credentials=has_futures_credentials(),
        has_spot_credentials=has_spot_credentials(),
        futures_withdrawal_attestation=os.environ.get(
            "KRAKEN_FUTURES_WITHDRAWAL_ATTESTATION"
        ),
    )
    enforce(results, config.trading_mode)

    # The attestation is an operator claim, not a machine fact, so record it in
    # the append-only log where it is attributable after the fact.
    # -- exchange wiring + restart recovery (brief 29) -------------------
    # No trading loop may run before recovery has reconciled against the
    # exchange, so this happens during startup, not after it.
    if config.trading_mode.touches_exchange:
        from .exchange.kraken import KrakenCliAdapter
        from .execution.engine import ExecutionEngine
        from .execution.observer import PollingFillObserver
        from .reconcile import Reconciler
        from .recovery import RecoveryManager

        adapter = KrakenCliAdapter(
            build_cli(config), config.trading_mode, config.tradeable_assets
        )
        specs = adapter.load_specs()
        for asset, spec in sorted(specs.items(), key=lambda kv: kv[0].value):
            log.info(
                f"instrument {asset.value} -> {spec.symbol} "
                f"(increment {spec.size_increment}, tick {spec.tick_size}, "
                f"max {spec.max_leverage}x)",
                extra={"asset": asset.value, "symbol": spec.symbol,
                       "action": "instrument_resolved"},
            )

        engine = ExecutionEngine(
            config=config, store=store, client=adapter,
            observer=PollingFillObserver(adapter),
            kill_switch=kill_switch,
        )
        reconciler = Reconciler(
            config=config, store=store, client=adapter, engine=engine,
            specs=specs,
        )
        recovery = RecoveryManager(
            config=config, store=store, client=adapter, engine=engine,
            reconciler=reconciler, specs=specs,
        )
        result = recovery.recover()
        if not result.safe_to_trade:
            raise SafetyCheckFailed(
                f"restart recovery did not complete safely: "
                f"{result.blocking_reason or 'unknown'}"
            )
        if result.orphan_positions:
            log.error(
                f"orphan position(s) present: {', '.join(result.orphan_positions)} "
                "— signals on those symbols will be refused until resolved",
                extra={"action": "startup_orphans"},
            )

    store.append_event(
        EventType.SAFETY_CHECK_PASSED,
        "startup safety checks complete",
        detail={
            **{r.name: r.outcome.value for r in results},
            "note": "futures withdrawal permission is operator-attested, "
            "not API-verifiable (docs/KRAKEN_NOTES.md 1.4)",
        },
    )
    return store, kill_switch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="run startup validation and exit",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2

    setup_logging(
        log_path=config.log_path,
        level=config.log_level,
        secrets=collect_secrets(),
    )

    try:
        store, _ = startup(config)
    except SafetyCheckFailed as exc:
        log.critical(str(exc), extra={"action": "startup_refused"})
        return 3
    except Exception:
        log.critical("startup failed", exc_info=True, extra={"action": "startup_failed"})
        return 1

    if args.check:
        log.info("startup validation complete", extra={"action": "check_ok"})
        store.close()
        return 0

    log.info(
        "Stage 2 skeleton only — no trading loops are wired up yet.",
        extra={"action": "not_implemented"},
    )
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
