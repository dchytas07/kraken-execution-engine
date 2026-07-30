"""Production loop: protection must never queue behind slower work."""

import pytest

from bot.execution.manager import PROTECTION_SLA_SECONDS
from bot.runner import BotRunner, LoopIntervals


class FakeManager:
    def __init__(self):
        self.sweeps = 0
        self.steps = 0
        self.sweep_raises = False

    def protection_sweep(self):
        self.sweeps += 1
        if self.sweep_raises:
            raise RuntimeError("boom")
        return []

    def step_all(self):
        self.steps += 1
        return []


class FakeReconciler:
    def __init__(self):
        self.runs = 0

    def run_once(self):
        self.runs += 1
        return None


def runner(**kw):
    return BotRunner(manager=FakeManager(), reconciler=FakeReconciler(),
                     intervals=LoopIntervals(**kw))


def test_protection_sweeps_every_tick():
    r = runner()
    for i in range(5):
        r.tick(now=i * 2.0)
    assert r.manager.sweeps == 5


def test_strategy_runs_less_often_than_protection():
    r = runner(sweep_seconds=2, manage_seconds=15, reconcile_seconds=60)
    for i in range(10):
        r.tick(now=i * 2.0)
    assert r.manager.sweeps == 10
    assert r.manager.steps < r.manager.sweeps


def test_reconciliation_is_the_slowest_cadence():
    r = runner(sweep_seconds=2, manage_seconds=15, reconcile_seconds=60)
    for i in range(40):
        r.tick(now=i * 2.0)
    assert r.reconciler.runs < r.manager.steps < r.manager.sweeps


def test_a_failing_sweep_does_not_stop_later_ticks():
    r = runner()
    r.manager.sweep_raises = True
    r.tick(now=0)
    r.manager.sweep_raises = False
    r.tick(now=2.0)
    assert r.manager.sweeps == 2
    assert r.stats.errors == 1


def test_intervals_must_be_able_to_meet_the_sla():
    """Configuration that cannot meet the SLA is rejected, not silently slow."""
    with pytest.raises(ValueError, match="protection SLA"):
        LoopIntervals(sweep_seconds=PROTECTION_SLA_SECONDS)


def test_default_intervals_meet_the_sla_with_retries():
    i = LoopIntervals()
    assert i.sweep_seconds * 3 <= PROTECTION_SLA_SECONDS
    assert i.manage_seconds < i.reconcile_seconds
