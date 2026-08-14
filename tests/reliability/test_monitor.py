"""Tests for the monitoring loop: scheduling, tick isolation, and wiring."""

from __future__ import annotations

import pytest

from openjarvis.reliability.detector import Detector
from openjarvis.reliability.monitor import ReliabilityMonitor
from openjarvis.reliability.probes.spec import parse_probe
from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.types import IncidentState, ProbeResult, Severity, Signal


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _Executor:
    """Returns scripted probe outcomes and counts calls per probe."""

    def __init__(self, outcomes=None, *, raises=False):
        self.outcomes = dict(outcomes or {})
        self.calls: list[str] = []
        self.raises = raises

    def run(self, spec):
        self.calls.append(spec.id)
        if self.raises:
            raise RuntimeError("probe exploded")
        success = self.outcomes.get(spec.id, True)
        if callable(success):
            success = success(self.calls.count(spec.id))
        return ProbeResult(
            probe_id=spec.id,
            success=success,
            failure_kind="" if success else "assertion",
            error="" if success else "expected /dashboard, got /login",
        )


class _Source:
    source_id = "fake"

    def __init__(self, signals=None, *, raises=False):
        self.signals = list(signals or [])
        self.polls = 0
        self.raises = raises

    def poll(self, **kwargs):
        self.polls += 1
        if self.raises:
            raise RuntimeError("source exploded")
        return list(self.signals)


@pytest.fixture
def store(tmp_path):
    s = IncidentStore(tmp_path / "incidents.db")
    yield s
    s.close()


def _spec(probe_id="p", interval="60", enabled=True, confirm=1):
    return parse_probe(
        {
            "probe": {
                "id": probe_id,
                "component": "web",
                "enabled": enabled,
                "schedule": {"type": "interval", "value": interval},
                "retry": {"confirm_runs": confirm},
                "steps": [{"action": "goto", "url": "/"}],
            }
        }
    )


def _monitor(store, executor, clock, **kwargs):
    return ReliabilityMonitor(
        detector=kwargs.pop("detector", Detector(store)),
        executor=executor,
        clock=clock,
        jitter=lambda: 0.0,
        **kwargs,
    )


class TestScheduling:
    def test_probes_run_when_due(self, store):
        clock = _Clock()
        executor = _Executor()
        monitor = _monitor(store, executor, clock, specs=[_spec()])
        assert monitor.tick() == 1
        assert executor.calls == ["p"]

    def test_probes_do_not_run_before_they_are_due(self, store):
        clock = _Clock()
        executor = _Executor()
        monitor = _monitor(store, executor, clock, specs=[_spec(interval="60")])
        monitor.tick()
        assert monitor.tick() == 0
        clock.advance(61)
        assert monitor.tick() == 1

    def test_disabled_probes_are_not_scheduled(self, store):
        clock = _Clock()
        executor = _Executor()
        monitor = _monitor(store, executor, clock, specs=[_spec(enabled=False)])
        assert monitor.checks == []
        assert monitor.tick() == 0

    def test_first_runs_are_staggered(self, store):
        """N probes must not hit the site in lockstep."""
        clock = _Clock()
        offsets = iter([0.0, 0.5, 1.0])
        monitor = ReliabilityMonitor(
            detector=Detector(store),
            executor=_Executor(),
            clock=clock,
            jitter=lambda: next(offsets),
            specs=[_spec("a"), _spec("b"), _spec("c")],
        )
        due = [check.next_due for check in monitor.checks]
        assert len(set(due)) == 3

    def test_minimum_interval_is_enforced(self, store):
        """A probe cannot be configured to hammer the site every second."""
        clock = _Clock()
        monitor = _monitor(store, _Executor(), clock, specs=[_spec(interval="1")])
        assert monitor.checks[0].interval_seconds >= 30.0

    def test_malformed_interval_falls_back(self, store):
        clock = _Clock()
        monitor = _monitor(store, _Executor(), clock, specs=[_spec(interval="soon")])
        assert monitor.checks[0].interval_seconds == 300.0

    def test_sources_are_polled(self, store):
        clock = _Clock()
        source = _Source()
        monitor = _monitor(store, _Executor(), clock, sources=[source])
        monitor.tick()
        assert source.polls == 1


class TestTickIsolation:
    def test_one_broken_probe_does_not_stop_the_others(self, store):
        """The whole point of the loop is that it keeps going."""
        clock = _Clock()

        class _Mixed(_Executor):
            def run(self, spec):
                self.calls.append(spec.id)
                if spec.id == "bad":
                    raise RuntimeError("boom")
                return ProbeResult(probe_id=spec.id, success=True)

        executor = _Mixed()
        monitor = _monitor(store, executor, clock, specs=[_spec("bad"), _spec("good")])
        monitor.tick()
        assert set(executor.calls) == {"bad", "good"}
        assert monitor.stats.failures == 1

    def test_a_failing_check_is_still_rescheduled(self, store):
        clock = _Clock()
        executor = _Executor(raises=True)
        monitor = _monitor(store, executor, clock, specs=[_spec(interval="60")])
        monitor.tick()
        assert monitor.checks[0].next_due > 0

    def test_broken_source_does_not_stop_the_loop(self, store):
        clock = _Clock()
        monitor = _monitor(store, _Executor(), clock, sources=[_Source(raises=True)])
        monitor.tick()
        assert monitor.stats.failures == 1


class TestDetectionWiring:
    def test_failing_probe_opens_an_incident(self, store):
        clock = _Clock()
        executor = _Executor({"p": False})
        monitor = _monitor(store, executor, clock, specs=[_spec(confirm=1)])
        monitor.tick()
        assert store.count() == 1
        assert monitor.stats.incidents_opened == 1

    def test_recovery_is_counted(self, store):
        clock = _Clock()
        # Fail on the first call, pass afterwards.
        executor = _Executor({"p": lambda n: n > 1})
        monitor = _monitor(store, executor, clock, specs=[_spec(confirm=1)])
        monitor.tick()
        clock.advance(61)
        monitor.tick()
        assert monitor.stats.incidents_recovered == 1

    def test_suppressed_failures_are_counted(self, store):
        clock = _Clock()
        executor = _Executor({"p": False})
        monitor = _monitor(store, executor, clock, specs=[_spec(confirm=3)])
        monitor.tick()
        assert monitor.stats.suppressed == 1
        assert store.count() == 0

    def test_signals_open_incidents(self, store):
        clock = _Clock()
        signal = Signal(
            source="vercel",
            kind="deployment_failed",
            title="build failed",
            severity=Severity.HIGH,
        )
        monitor = _monitor(store, _Executor(), clock, sources=[_Source([signal])])
        monitor.tick()
        assert store.count() == 1


class TestRepairWiring:
    class _Loop:
        def __init__(self, outcome):
            self.outcome = outcome
            self.calls = []
            self.policy = type("P", (), {"max_attempts": 3})()

        def run(self, incident, spec):
            self.calls.append(incident.id)
            return self.outcome

    def test_repair_runs_for_a_new_incident(self, store):
        from openjarvis.reliability.repair import RepairOutcome

        clock = _Clock()
        loop = self._Loop(
            RepairOutcome(resolved=True, attempts=1, final_state=IncidentState.RESOLVED)
        )
        monitor = _monitor(
            store,
            _Executor({"p": False}),
            clock,
            specs=[_spec(confirm=1)],
            repair_loop=loop,
        )
        monitor.tick()
        assert len(loop.calls) == 1

    def test_no_repair_loop_means_monitor_only(self, store):
        clock = _Clock()
        monitor = _monitor(
            store, _Executor({"p": False}), clock, specs=[_spec(confirm=1)]
        )
        monitor.tick()
        assert store.count() == 1  # detected and notified, never modified

    def test_repair_failure_does_not_break_the_loop(self, store):
        clock = _Clock()

        class _Exploding:
            policy = type("P", (), {"max_attempts": 3})()

            def run(self, incident, spec):
                raise RuntimeError("repair exploded")

        monitor = _monitor(
            store,
            _Executor({"p": False}),
            clock,
            specs=[_spec(confirm=1)],
            repair_loop=_Exploding(),
        )
        monitor.tick()
        assert store.count() == 1

    def test_resolution_is_notified(self, store):
        from openjarvis.reliability.repair import RepairOutcome

        notified = []

        class _Notifier:
            def resolved(self, incident, **kwargs):
                notified.append(("resolved", incident.id))
                return True

            def human_required(self, incident, **kwargs):
                notified.append(("human", incident.id))
                return True

            def alert(self, incident):
                return True

        clock = _Clock()
        loop = self._Loop(
            RepairOutcome(resolved=True, attempts=1, final_state=IncidentState.RESOLVED)
        )
        monitor = _monitor(
            store,
            _Executor({"p": False}),
            clock,
            specs=[_spec(confirm=1)],
            repair_loop=loop,
            notifier=_Notifier(),
            detector=Detector(store, notifier=_Notifier()),
        )
        monitor.tick()
        assert ("resolved", "INC-00001") in notified


class TestLifecycle:
    def test_health_snapshot(self, store):
        clock = _Clock()
        monitor = _monitor(store, _Executor(), clock, specs=[_spec()])
        health = monitor.health()
        assert health["probes"] == 1
        assert health["checks"] == 1
        assert health["running"] is False

    def test_start_and_stop(self, store):
        monitor = ReliabilityMonitor(
            detector=Detector(store), executor=_Executor(), jitter=lambda: 0.0
        )
        monitor.start(poll_interval=0.01)
        assert monitor.health()["running"]
        monitor.stop(timeout=5)
        assert not monitor.health()["running"]

    def test_start_is_idempotent(self, store):
        monitor = ReliabilityMonitor(
            detector=Detector(store), executor=_Executor(), jitter=lambda: 0.0
        )
        monitor.start(poll_interval=0.01)
        monitor.start(poll_interval=0.01)
        monitor.stop(timeout=5)
