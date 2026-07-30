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


# ==========================================================================
# Daily digest — driven by the loop, not just callable
# ==========================================================================


class FakeConfig:
    alert_telegram_bot_token = "TOKEN"
    alert_telegram_chat_id = "CHAT"


def test_digest_is_not_attempted_without_store_and_config(monkeypatch):
    """Omitting either disables the digest — existing callers are unaffected."""
    calls = []
    monkeypatch.setattr("bot.runner.send_unresolved_digest",
                        lambda *a, **kw: calls.append(a) or True)
    r = runner()
    r.tick(now=0)
    r.tick(now=10_000)
    assert calls == []


def test_loop_drives_the_digest_on_its_own_cadence(monkeypatch):
    calls = []
    monkeypatch.setattr("bot.runner.send_unresolved_digest",
                        lambda *a, **kw: calls.append(a) or True)
    r = BotRunner(manager=FakeManager(), reconciler=FakeReconciler(),
                  intervals=LoopIntervals(), store=object(), config=FakeConfig())

    # Same shape as the manage/reconcile cadences: the first check lands one
    # interval in, not on tick zero.
    r.tick(now=0)
    assert calls == []
    r.tick(now=310)               # past digest_check_seconds: checks
    r.tick(now=320)               # inside the interval again: no second call
    assert len(calls) == 1
    r.tick(now=700)               # past it once more

    assert len(calls) == 2
    assert calls[0][1:] == ("TOKEN", "CHAT")
    assert r.stats.digests_sent == 2


def test_a_failing_digest_does_not_stop_protection(monkeypatch):
    """Observability must never be able to break the safety path."""
    def explode(*a, **kw):
        raise RuntimeError("digest blew up")
    monkeypatch.setattr("bot.runner.send_unresolved_digest", explode)
    r = BotRunner(manager=FakeManager(), reconciler=FakeReconciler(),
                  intervals=LoopIntervals(), store=object(), config=FakeConfig())

    r.tick(now=0)
    r.tick(now=1000)              # digest checked here, and explodes
    r.tick(now=2000)              # must still sweep, and check again

    assert r.manager.sweeps == 3, "protection kept running"
    assert r.stats.errors == 2, "both digest failures counted, neither fatal"
    assert r.stats.digests_sent == 0
