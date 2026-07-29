"""Manual kill switch (brief 25).

Semantics, stated explicitly because the brief asks for them to be documented:

    ENGAGED  -> the bot opens NO new positions and executes no new entry or
                add/DCA orders.
    ENGAGED  -> existing positions CONTINUE to be managed and protected. Stops,
                take-profits, breakeven moves, closes, cancellations and
                reconciliation all keep running.

Rationale: an operator hitting the kill switch wants to stop taking on new
risk, not to abandon live positions with unmanaged stops. Refusing to manage
open positions would be strictly more dangerous than doing nothing.

Activation is the presence of a file, so it works from a shell, Finder, or a
script with no access to the running process:

    touch ./KILL_SWITCH      # engage
    rm ./KILL_SWITCH         # release
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .logging_setup import get_logger
from .models import EventType

log = get_logger(__name__)


@dataclass(slots=True)
class KillSwitch:
    """File-presence kill switch with edge-triggered logging.

    State is read from disk on every call — never cached — so an operator's
    `touch` takes effect on the next decision without a restart.
    """

    path: Path
    _last_state: bool | None = None

    def is_engaged(self) -> bool:
        try:
            engaged = self.path.exists()
        except OSError:
            # If we cannot determine the state, assume engaged. Failing closed
            # here costs missed entries; failing open costs unwanted trades.
            log.error(
                "kill switch path unreadable — assuming ENGAGED",
                extra={"action": "killswitch_check", "path": str(self.path)},
            )
            engaged = True

        if engaged != self._last_state:
            if engaged:
                log.warning(
                    "KILL SWITCH ENGAGED — no new positions will be opened; "
                    "existing positions remain managed and protected",
                    extra={
                        "event": EventType.KILL_SWITCH_ENGAGED.value,
                        "action": "killswitch_engaged",
                        "path": str(self.path),
                    },
                )
            elif self._last_state is not None:
                log.warning(
                    "kill switch released — new positions permitted again",
                    extra={
                        "event": EventType.KILL_SWITCH_RELEASED.value,
                        "action": "killswitch_released",
                        "path": str(self.path),
                    },
                )
            self._last_state = engaged
        return engaged

    def allows_new_positions(self) -> bool:
        """The single question the execution engine should ask before opening."""
        return not self.is_engaged()

    def allows_position_management(self) -> bool:
        """Always True. Protective actions are never blocked by the kill
        switch — see the module docstring."""
        return True

    def engage(self) -> None:
        """Programmatic engage, for tests and emergency handlers."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()

    def release(self) -> None:
        self.path.unlink(missing_ok=True)
