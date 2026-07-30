"""The bot process under soak. Started, SIGKILLed, and restarted by soak_run.py.

Runs the real startup path: recovery gate, then a manage loop (fast) and a
reconciliation loop (60s). Picks up injected messages from an inbox directory,
which stands in for Telegram without needing credentials — the messages take
the identical pipeline path a Telegram message would.
"""

from __future__ import annotations

import os
import sys
import time
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
from bot.logging_setup import get_logger, setup_logging  # noqa: E402
from bot.pipeline import SignalPipeline, build_admission  # noqa: E402
from bot.reconcile import Reconciler  # noqa: E402
from bot.runner import BotRunner, LoopIntervals  # noqa: E402
from bot.recovery import RecoveryManager  # noqa: E402

log = get_logger("soak.bot")

INBOX = Path(os.environ["SOAK_INBOX"])
DONE = Path(os.environ["SOAK_DONE"])
SWEEP_INTERVAL = float(os.environ.get("SOAK_SWEEP_INTERVAL", "2"))
MANAGE_INTERVAL = float(os.environ.get("SOAK_MANAGE_INTERVAL", "15"))
RECONCILE_INTERVAL = float(os.environ.get("SOAK_RECONCILE_INTERVAL", "60"))


def main() -> int:
    config = load_config()
    setup_logging(log_path=config.log_path, level=config.log_level, console=False)
    INBOX.mkdir(parents=True, exist_ok=True)
    DONE.mkdir(parents=True, exist_ok=True)

    cli = KrakenCli(binary=config.kraken_cli_path,
                    timeout=config.kraken_cli_timeout_seconds, env=dict(os.environ))
    adapter = KrakenCliAdapter(cli, config.trading_mode, config.tradeable_assets)
    specs = adapter.load_specs()
    store = Store(config.db_path)
    kill_switch = KillSwitch(path=config.kill_switch_path)

    engine = ExecutionEngine(
        config=config, store=store, client=adapter,
        observer=PollingFillObserver(adapter, default_timeout_s=20, default_poll_s=2),
        kill_switch=kill_switch,
    )
    manager = TradeManager(config=config, store=store, client=adapter,
                           engine=engine, specs=specs)
    reconciler = Reconciler(config=config, store=store, client=adapter,
                            engine=engine, specs=specs)
    recovery = RecoveryManager(config=config, store=store, client=adapter,
                               engine=engine, reconciler=reconciler, specs=specs)

    result = recovery.recover()
    log.info(
        f"recovery: completed={result.completed} safe={result.safe_to_trade} "
        f"trades={result.trades_examined} intents={len(result.intents_resolved)} "
        f"orphans={result.orphan_positions} blocked={result.blocking_reason!r}",
        extra={"action": "soak_recovery"},
    )
    if not result.safe_to_trade:
        log.critical("recovery unsafe; bot will manage but not open",
                     extra={"action": "soak_recovery_blocked"})

    pipeline = SignalPipeline(
        config=config, store=store,
        admission=build_admission(config=config, store=store,
                                  kill_switch=kill_switch, client=adapter,
                                  specs=specs),
        engine=engine, specs=specs, manager=manager,
    )

    runner = BotRunner(
        manager=manager, reconciler=reconciler,
        intervals=LoopIntervals(
            sweep_seconds=SWEEP_INTERVAL,
            manage_seconds=MANAGE_INTERVAL,
            reconcile_seconds=RECONCILE_INTERVAL,
        ),
    )
    log.info("soak bot ready", extra={"action": "soak_ready"})
    while True:
        # Inbox: stands in for Telegram, same pipeline path.
        for path in sorted(INBOX.glob("*.txt")):
            try:
                message_id = int(path.stem.split("-")[0])
                text = path.read_text()
                outcome = pipeline.handle_message(
                    channel_id=-999000001, message_id=message_id, text=text
                )
                log.info(
                    f"injected {path.name}: accepted={outcome.accepted} "
                    f"detail={outcome.detail!r}",
                    extra={"action": "soak_injected", "message_id": message_id},
                )
            except Exception:
                log.error(f"injection {path.name} failed", exc_info=True,
                          extra={"action": "soak_inject_failed"})
            finally:
                path.rename(DONE / path.name)

        # The REAL production loop, not a harness reimplementation.
        runner.tick()
        time.sleep(SWEEP_INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
