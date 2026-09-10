"""The production run loop.

Four cadences, ordered by how urgent the work is and how much it costs.

    protection sweep   ~2s    TWO exchange reads, O(1) in orders
    forward step      ~15s    O(open orders) — measured 17-50s with 3 trades
    reconciliation      60s   full snapshot, the safety net
    digest check       300s   one indexed query; the real 24h interval is
                              persisted in the DB, not held in this process

The split exists because measurement forced it (tools/measure_tick.py): a full
`step_all` costs 17s median and 50s worst with only three trades, because it
reads per order. Protection riding on that loop meant a fill could sit
unprotected for ~53s — barely better than the 60s reconciler it was supposed to
front-run. `protection_sweep` asks only "what is open?" and "where are the
stops?", so its cost does not grow with the book and it can run every couple of
seconds.

This lives in `src/` rather than in the soak harness on purpose: the soak must
exercise the loop production actually runs, not a reimplementation of it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .alerting import send_daily_summary, send_unresolved_digest
from .execution.manager import PROTECTION_SLA_SECONDS, TradeManager
from .logging_setup import get_logger
from .reconcile import Reconciler

log = get_logger(__name__)


@dataclass(slots=True)
class LoopIntervals:
    sweep_seconds: float = 2.0
    manage_seconds: float = 15.0
    reconcile_seconds: float = 60.0
    # How often to CHECK whether the 24h digest is due — not the digest
    # interval itself, which is enforced by a persisted event marker so it
    # survives restarts (see alerting.send_unresolved_digest). The check is
    # one indexed query, so this is cheap.
    digest_check_seconds: float = 300.0

    def __post_init__(self) -> None:
        # The sweep is what delivers the SLA, so it must have room for several
        # attempts inside the budget rather than exactly one.
        if self.sweep_seconds * 3 > PROTECTION_SLA_SECONDS:
            raise ValueError(
                f"sweep interval {self.sweep_seconds}s is too slow to meet the "
                f"{PROTECTION_SLA_SECONDS}s protection SLA with retries"
            )


@dataclass(slots=True)
class LoopStats:
    sweeps: int = 0
    manages: int = 0
    reconciles: int = 0
    digests_sent: int = 0
    summaries_sent: int = 0
    protected: list[str] = field(default_factory=list)
    errors: int = 0


class BotRunner:
    """Drives protection, strategy and reconciliation at their own cadences."""

    def __init__(
        self,
        *,
        manager: TradeManager,
        reconciler: Reconciler,
        intervals: LoopIntervals | None = None,
        store=None,
        config=None,
    ) -> None:
        self.manager = manager
        self.reconciler = reconciler
        self.intervals = intervals or LoopIntervals()
        # Optional: the digest is skipped entirely unless both are supplied,
        # so a caller that does not want alerting simply omits them.
        self.store = store
        self.config = config
        self.stats = LoopStats()
        self._last_manage = 0.0
        self._last_reconcile = 0.0
        self._last_digest_check = 0.0
        self._stop = False

    def tick(self, now: float | None = None) -> LoopStats:
        """One iteration. Protection first, always.

        Each stage is isolated: a failure in strategy or reconciliation must
        not prevent the next protection sweep from running.
        """
        now = time.monotonic() if now is None else now

        try:
            for symbol in self.manager.protection_sweep():
                self.stats.protected.append(symbol)
                log.warning(
                    f"protection sweep placed a stop on {symbol}",
                    extra={"symbol": symbol, "action": "sweep_protected"},
                )
        except Exception:
            self.stats.errors += 1
            log.error("protection sweep failed", exc_info=True,
                      extra={"action": "sweep_failed"})
        self.stats.sweeps += 1

        if now - self._last_manage >= self.intervals.manage_seconds:
            self._last_manage = now
            try:
                self.manager.step_all()
            except Exception:
                self.stats.errors += 1
                log.error("forward step failed", exc_info=True,
                          extra={"action": "manage_failed"})
            self.stats.manages += 1

        if now - self._last_reconcile >= self.intervals.reconcile_seconds:
            self._last_reconcile = now
            try:
                self.reconciler.run_once()
            except Exception:
                self.stats.errors += 1
                log.error("reconciliation failed", exc_info=True,
                          extra={"action": "reconcile_failed"})
            self.stats.reconciles += 1

        if (
            self.store is not None
            and self.config is not None
            and now - self._last_digest_check >= self.intervals.digest_check_seconds
        ):
            self._last_digest_check = now
            # send_unresolved_digest never raises, but this stage is isolated
            # like the others so a future change cannot make observability
            # able to break protection.
            try:
                if send_unresolved_digest(
                    self.store,
                    self.config.alert_telegram_bot_token,
                    self.config.alert_telegram_chat_id,
                ):
                    self.stats.digests_sent += 1
                # Positions come from the exchange, so they are read here
                # rather than inside alerting -- that module stays free of
                # exchange dependencies.
                try:
                    positions = list(self.manager.client.get_open_positions())
                except Exception:  # noqa: BLE001 -- summary is best-effort
                    positions = []
                if send_daily_summary(self.store, self.config,
                                      positions=positions):
                    self.stats.summaries_sent += 1
            except Exception as exc:
                self.stats.errors += 1
                log.error(f"unresolved digest failed: {exc}", exc_info=True,
                          extra={"action": "digest_failed"})

        return self.stats

    def run_forever(self, *, on_tick=None) -> None:
        self._stop = False
        while not self._stop:
            self.tick()
            if on_tick is not None:
                on_tick()
            time.sleep(self.intervals.sweep_seconds)

    def stop(self) -> None:
        self._stop = True
