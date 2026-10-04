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


# ==========================================================================
# Suspend detection (2026-10-04: a 12h host suspend left a position unwatched)
# ==========================================================================


def test_suspend_gap_alerts_and_reconciles_immediately(monkeypatch, caplog):
    import logging
    sent = []
    monkeypatch.setattr("bot.runner.send_alert",
                        lambda token, chat, text: sent.append(text) or True)
    monkeypatch.setattr("bot.runner.send_unresolved_digest", lambda *a, **k: False)
    monkeypatch.setattr("bot.runner.send_daily_summary", lambda *a, **k: False)
    r = BotRunner(manager=FakeManager(), reconciler=FakeReconciler(),
                  intervals=LoopIntervals(), store=object(), config=FakeConfig())

    r.tick(now=100, wall=1_000)
    reconciles = r.reconciler.runs
    sweeps = r.manager.sweeps
    # Monotonic time barely moves across a suspend; the wall clock jumps 12h.
    with caplog.at_level(logging.WARNING):
        r.tick(now=102, wall=1_000 + 12 * 3600)

    assert r.reconciler.runs == reconciles + 1, "reconciled despite the cadence"
    assert r.manager.sweeps == sweeps + 1
    assert sent == ["bot was suspended for 12h 0m, reconciling now"]
    assert r.stats.suspends == 1
    assert any(getattr(rec, "action", None) == "suspend_detected"
               for rec in caplog.records)


def test_ordinary_tick_gap_is_not_a_suspend(monkeypatch):
    sent = []
    monkeypatch.setattr("bot.runner.send_alert",
                        lambda token, chat, text: sent.append(text) or True)
    r = runner()
    r.tick(now=100, wall=1_000)
    r.tick(now=102, wall=1_002)
    r.tick(now=222, wall=1_120)  # exactly the threshold: not exceeded
    assert sent == [] and r.stats.suspends == 0


def test_first_tick_is_never_a_suspend():
    r = runner()
    r.tick(now=0, wall=10**9)
    assert r.stats.suspends == 0


def test_suspend_inside_a_tick_is_detected(monkeypatch):
    """2026-10-04: the host slept during a CLI call, so the frozen time sat
    INSIDE a tick and the idle gap afterwards was ordinary. The wall clock
    still jumped against the monotonic one."""
    sent = []
    monkeypatch.setattr("bot.runner.send_alert",
                        lambda token, chat, text: sent.append(text) or True)
    r = BotRunner(manager=FakeManager(), reconciler=FakeReconciler(),
                  intervals=LoopIntervals(), config=FakeConfig())
    r.tick(now=100, wall=1_000)
    r._last_wall = 1_000 + 3600   # that tick ended an hour of wall time later
    r.tick(now=102, wall=1_000 + 3602)  # idle gap 2s, clocks diverged by 1h
    assert sent == ["bot was suspended for 1h 0m, reconciling now"]


def test_slow_tick_is_not_a_suspend(monkeypatch):
    """A tick stuck in CLI timeouts advances both clocks together."""
    sent = []
    monkeypatch.setattr("bot.runner.send_alert",
                        lambda token, chat, text: sent.append(text) or True)
    r = runner()
    r.tick(now=100, wall=1_000)
    r._last_wall = 1_300           # 300s tick
    r.tick(now=402, wall=1_302)
    assert sent == []

