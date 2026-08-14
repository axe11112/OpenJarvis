"""Tests for the 24/7 supervisor.

These cover the decisions that only matter once nobody is watching: refusing to
start in a dangerous configuration, refusing to resume an interrupted repair,
refusing to run two repairs at once, and refusing to retry immediately after a
failure.

Every one of them is a *refusal*. That is the shape of this module: an
autonomous system earns its autonomy by the things it declines to do.
"""

from __future__ import annotations

import pytest

from openjarvis.core.config import JarvisConfig
from openjarvis.reliability.escalation import EscalationPolicy, EscalationTracker
from openjarvis.reliability.flapping import FlappingDetector
from openjarvis.reliability.report import build_report, format_duration
from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.types import (
    Incident,
    IncidentState,
    RecoveryType,
    RepairAttempt,
    Severity,
    VerificationResult,
)
from openjarvis.reliability.watch import (
    RepairGate,
    UnsafeConfigurationError,
    WatchSupervisor,
    assert_safe_to_start,
    startup_banner,
)


@pytest.fixture
def store(tmp_path):
    s = IncidentStore(tmp_path / "incidents.db")
    yield s
    s.close()


def _incident(store, **overrides) -> Incident:
    defaults = dict(
        fingerprint="fp",
        severity=Severity.HIGH,
        component="dashboard",
        title="Dashboard does not load",
        probe_id="dashboard",
    )
    defaults.update(overrides)
    return store.create(Incident(**defaults))


class _FakeMonitor:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self, **_kwargs):
        self.started = True

    def run_forever(self, **_kwargs):
        self.started = True

    def stop(self, **_kwargs):
        self.stopped = True

    def health(self):
        return {"running": self.started and not self.stopped, "checks": 1}


@pytest.fixture
def supervisor(store):
    return WatchSupervisor(monitor=_FakeMonitor(), store=store)


# ---------------------------------------------------------------------------
# Startup safety (§21)
# ---------------------------------------------------------------------------


class TestStartupSafety:
    def _config(self, **repair) -> JarvisConfig:
        config = JarvisConfig()
        for key, value in repair.items():
            setattr(config.reliability.repair, key, value)
        return config

    def test_defaults_are_safe(self):
        assert_safe_to_start(JarvisConfig())  # must not raise

    def test_repair_without_production_reach_is_safe(self):
        config = self._config(enabled=True, workspace="/tmp/checkout")
        assert_safe_to_start(config)

    def test_repair_plus_default_branch_push_refuses_to_start(self):
        config = self._config(enabled=True, workspace="/tmp/checkout")
        config.reliability.policy.allow_push_to_default_branch = True
        with pytest.raises(UnsafeConfigurationError, match="default branch"):
            assert_safe_to_start(config)

    def test_repair_plus_auto_deploy_refuses_to_start(self):
        config = self._config(enabled=True, workspace="/tmp/checkout")
        config.reliability.policy.deploy_mode = "auto_deploy_allowlisted"
        with pytest.raises(UnsafeConfigurationError, match="production"):
            assert_safe_to_start(config)

    def test_repair_plus_supabase_writes_refuses_to_start(self):
        config = self._config(enabled=True, workspace="/tmp/checkout")
        config.reliability.supabase.allow_production_writes = True
        with pytest.raises(UnsafeConfigurationError, match="Supabase"):
            assert_safe_to_start(config)

    def test_repair_without_a_workspace_refuses_to_start(self):
        with pytest.raises(UnsafeConfigurationError, match="workspace"):
            assert_safe_to_start(self._config(enabled=True))

    def test_dangerous_flags_alone_are_fine_without_repair(self):
        """They only become dangerous in combination with automatic repair."""
        config = JarvisConfig()
        config.reliability.policy.allow_push_to_default_branch = True
        config.reliability.supabase.allow_production_writes = True
        assert_safe_to_start(config)

    def test_every_problem_is_reported_not_just_the_first(self):
        config = self._config(enabled=True, workspace="/tmp/x")
        config.reliability.policy.allow_push_to_default_branch = True
        config.reliability.supabase.allow_production_writes = True
        with pytest.raises(UnsafeConfigurationError) as exc:
            assert_safe_to_start(config)
        assert str(exc.value).count("  - ") >= 2

    def test_the_banner_states_every_interlock(self):
        banner = startup_banner(JarvisConfig())
        for label in (
            "Monitoring",
            "Automatic repair",
            "Production deployment",
            "Default branch push",
            "Automatic PR merge",
            "Supabase writes",
        ):
            assert label in banner

    def test_the_banner_never_claims_production_deployment_is_on(self):
        config = JarvisConfig()
        config.reliability.repair.enabled = True
        config.reliability.policy.deploy_mode = "auto_deploy_allowlisted"
        line = next(
            row
            for row in startup_banner(config).splitlines()
            if "Production deployment" in row
        )
        assert line.split()[-1] == "OFF"


# ---------------------------------------------------------------------------
# Crash recovery (§26, §36)
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    @pytest.mark.parametrize(
        "state", [IncidentState.FIXING, IncidentState.TESTING, IncidentState.VERIFYING]
    )
    def test_interrupted_repairs_are_parked(self, store, supervisor, state):
        incident = _incident(store)
        for step in (
            IncidentState.INVESTIGATING,
            IncidentState.REPRODUCING,
            IncidentState.FIXING,
            IncidentState.TESTING,
            IncidentState.VERIFYING,
        ):
            store.transition(incident, step)
            if step is state:
                break

        parked = supervisor.recover_interrupted_repairs()

        assert [i.id for i in parked] == [incident.id]
        assert store.get(incident.id).state is IncidentState.RECOVERY_REQUIRED

    def test_a_restart_never_resumes_a_repair(self, store, supervisor):
        """The whole point: a restart is not evidence the repair is safe."""
        incident = _incident(store)
        for step in (
            IncidentState.INVESTIGATING,
            IncidentState.REPRODUCING,
            IncidentState.FIXING,
        ):
            store.transition(incident, step)

        supervisor.recover_interrupted_repairs()
        parked = store.get(incident.id)

        assert parked.state is IncidentState.RECOVERY_REQUIRED
        assert not parked.can_transition_to(IncidentState.FIXING)

    def test_untouched_incidents_are_left_alone(self, store, supervisor):
        incident = _incident(store)
        assert supervisor.recover_interrupted_repairs() == []
        assert store.get(incident.id).state is IncidentState.DETECTED

    def test_recovery_leaves_an_explanation(self, store, supervisor):
        incident = _incident(store)
        for step in (
            IncidentState.INVESTIGATING,
            IncidentState.REPRODUCING,
            IncidentState.FIXING,
        ):
            store.transition(incident, step)
        supervisor.recover_interrupted_repairs()

        summaries = [e.summary for e in store.get(incident.id).evidence]
        assert any("Interrupted repair" in s for s in summaries)

    def test_the_owner_is_told(self, store):
        notices = []

        class _Notifier:
            def human_required(self, incident, **kwargs):
                notices.append((incident.id, kwargs.get("reason", "")))
                return True

        supervisor = WatchSupervisor(
            monitor=_FakeMonitor(), store=store, notifier=_Notifier()
        )
        incident = _incident(store)
        for step in (
            IncidentState.INVESTIGATING,
            IncidentState.REPRODUCING,
            IncidentState.FIXING,
        ):
            store.transition(incident, step)
        supervisor.recover_interrupted_repairs()

        assert notices and notices[0][0] == incident.id

    def test_the_audit_chain_survives_recovery(self, store, supervisor):
        incident = _incident(store)
        for step in (
            IncidentState.INVESTIGATING,
            IncidentState.REPRODUCING,
            IncidentState.FIXING,
        ):
            store.transition(incident, step)
        supervisor.recover_interrupted_repairs()
        assert store.verify_chain() == (True, None)

    def test_start_runs_recovery_before_monitoring(self, store, supervisor):
        incident = _incident(store)
        for step in (
            IncidentState.INVESTIGATING,
            IncidentState.REPRODUCING,
            IncidentState.FIXING,
        ):
            store.transition(incident, step)

        parked = supervisor.start()

        assert [i.id for i in parked] == [incident.id]
        assert supervisor.monitor.started


# ---------------------------------------------------------------------------
# Concurrency and cooldown (§27, §28)
# ---------------------------------------------------------------------------


class TestRepairGate:
    def test_one_repair_at_a_time_by_default(self):
        gate = RepairGate()
        assert gate.start("INC-1")
        allowed, reason = gate.may_start("INC-2")
        assert not allowed
        assert "concurrency limit" in reason

    def test_finishing_frees_the_slot(self):
        gate = RepairGate()
        gate.start("INC-1")
        gate.finish("INC-1", succeeded=True)
        assert gate.start("INC-2")

    def test_the_limit_is_configurable(self):
        gate = RepairGate(max_concurrent=2)
        assert gate.start("INC-1")
        assert gate.start("INC-2")
        assert not gate.start("INC-3")

    def test_the_same_incident_cannot_start_twice(self):
        gate = RepairGate(max_concurrent=5)
        gate.start("INC-1")
        allowed, reason = gate.may_start("INC-1")
        assert not allowed
        assert "already running" in reason

    def test_a_failed_repair_starts_a_cooldown(self):
        clock = {"t": 0.0}
        gate = RepairGate(cooldown_seconds=300, clock=lambda: clock["t"])
        gate.start("INC-1")
        gate.finish("INC-1", succeeded=False)

        allowed, reason = gate.may_start("INC-1")
        assert not allowed
        assert "cooling down" in reason

    def test_the_cooldown_expires(self):
        clock = {"t": 0.0}
        gate = RepairGate(cooldown_seconds=300, clock=lambda: clock["t"])
        gate.start("INC-1")
        gate.finish("INC-1", succeeded=False)
        clock["t"] = 301.0
        assert gate.may_start("INC-1")[0]

    def test_a_successful_repair_has_no_cooldown(self):
        clock = {"t": 0.0}
        gate = RepairGate(cooldown_seconds=300, clock=lambda: clock["t"])
        gate.start("INC-1")
        gate.finish("INC-1", succeeded=True)
        assert gate.may_start("INC-1")[0]

    def test_cooldown_is_per_incident(self):
        clock = {"t": 0.0}
        gate = RepairGate(cooldown_seconds=300, clock=lambda: clock["t"])
        gate.start("INC-1")
        gate.finish("INC-1", succeeded=False)
        assert gate.may_start("INC-2")[0]

    def test_blocking_refuses_everything(self):
        gate = RepairGate()
        gate.block()
        allowed, reason = gate.may_start("INC-1")
        assert not allowed
        assert "emergency stop" in reason

    def test_unblocking_restores_service(self):
        gate = RepairGate()
        gate.block()
        gate.unblock()
        assert gate.may_start("INC-1")[0]

    def test_snapshot_reports_state(self):
        gate = RepairGate()
        gate.start("INC-1")
        snapshot = gate.snapshot()
        assert snapshot["active"] == ["INC-1"]
        assert snapshot["blocked"] is False


class TestEmergencyStop:
    def test_stop_blocks_new_repairs(self, supervisor):
        supervisor.stop()
        assert supervisor.gate.blocked
        assert not supervisor.gate.may_start("INC-1")[0]

    def test_stop_does_not_delete_anything(self, store, supervisor):
        incident = _incident(store)
        supervisor.stop()
        preserved = store.get(incident.id)
        assert preserved is not None
        assert store.verify_chain() == (True, None)

    def test_stop_stops_the_monitor(self, supervisor):
        supervisor.start()
        supervisor.stop()
        assert supervisor.monitor.stopped

    def test_stop_is_safe_when_the_monitor_raises(self, store):
        class _Broken(_FakeMonitor):
            def stop(self, **_kwargs):
                raise RuntimeError("wedged")

        supervisor = WatchSupervisor(monitor=_Broken(), store=store)
        supervisor.stop()  # must not raise
        assert supervisor.gate.blocked


# ---------------------------------------------------------------------------
# Flapping escalation (§5)
# ---------------------------------------------------------------------------


class TestFlappingEscalation:
    def test_a_flapping_incident_goes_to_a_human(self, store, supervisor):
        incident = _incident(store)
        verdict = None
        for char in "PFPFPF":
            verdict = supervisor.record_result("dashboard", failed=char == "F")

        assert verdict.flapping
        assert supervisor.escalate_flapping(incident, verdict)
        assert store.get(incident.id).state is IncidentState.HUMAN_REQUIRED

    def test_the_verdict_is_recorded_on_the_incident(self, store, supervisor):
        incident = _incident(store)
        for char in "PFPFPF":
            verdict = supervisor.record_result("dashboard", failed=char == "F")
        supervisor.escalate_flapping(incident, verdict)

        assert store.get(incident.id).metadata["flapping"]["flapping"] is True

    def test_escalation_resets_the_window(self, store, supervisor):
        """So the next sample cannot immediately escalate a second time."""
        incident = _incident(store)
        for char in "PFPFPF":
            verdict = supervisor.record_result("dashboard", failed=char == "F")
        supervisor.escalate_flapping(incident, verdict)
        assert not supervisor.flapping.verdict("dashboard").flapping

    def test_an_already_escalated_incident_is_not_escalated_twice(
        self, store, supervisor
    ):
        incident = _incident(store)
        store.transition(incident, IncidentState.HUMAN_REQUIRED)
        verdict = FlappingDetector().record("dashboard", failed=True)
        assert supervisor.escalate_flapping(incident, verdict) is False


# ---------------------------------------------------------------------------
# Escalation timer (§19)
# ---------------------------------------------------------------------------


class _RecordingNotifier:
    def __init__(self):
        self.calls = []

    def human_required(self, incident, **kwargs):
        self.calls.append((incident.id, kwargs.get("reason", "")))
        return True


class TestEscalationTracker:
    def _tracker(self, clock, **policy):
        defaults = dict(after_minutes=5.0, max_reminders=2, enabled=True)
        defaults.update(policy)
        return EscalationTracker(
            policy=EscalationPolicy(**defaults),
            notifier=_RecordingNotifier(),
            clock=lambda: clock["t"],
        )

    def test_a_critical_incident_escalates_after_the_timeout(self, store):
        clock = {"t": 0.0}
        tracker = self._tracker(clock)
        incident = _incident(store, severity=Severity.CRITICAL)

        assert tracker.sweep([incident]) == []
        clock["t"] = 301.0
        assert [i.id for i in tracker.sweep([incident])] == [incident.id]

    def test_a_low_incident_never_escalates(self, store):
        clock = {"t": 0.0}
        tracker = self._tracker(clock)
        incident = _incident(store, severity=Severity.LOW)
        clock["t"] = 100000.0
        assert tracker.sweep([incident]) == []

    def test_reminders_are_bounded(self, store):
        clock = {"t": 0.0}
        tracker = self._tracker(clock, max_reminders=2)
        incident = _incident(store, severity=Severity.CRITICAL)
        tracker.sweep([incident])
        for step in (301.0, 601.0, 901.0, 1201.0):
            clock["t"] = step
            tracker.sweep([incident])
        assert len(tracker.notifier.calls) == 2

    def test_a_resolved_incident_stops_nagging(self, store):
        clock = {"t": 0.0}
        tracker = self._tracker(clock)
        incident = _incident(store, severity=Severity.CRITICAL)
        tracker.sweep([incident])
        clock["t"] = 301.0
        tracker.sweep([])  # no longer open
        clock["t"] = 601.0
        assert tracker.sweep([]) == []
        assert tracker.notifier.calls == []

    def test_disabled_policy_never_escalates(self, store):
        clock = {"t": 100000.0}
        tracker = self._tracker(clock, enabled=False)
        incident = _incident(store, severity=Severity.CRITICAL)
        assert tracker.sweep([incident]) == []

    def test_clearing_forgets_an_incident(self, store):
        clock = {"t": 0.0}
        tracker = self._tracker(clock)
        incident = _incident(store, severity=Severity.CRITICAL)
        tracker.observe(incident)
        tracker.clear(incident.id)
        assert tracker.snapshot()["tracking"] == []


# ---------------------------------------------------------------------------
# Post-incident report (§24)
# ---------------------------------------------------------------------------


class TestPostIncidentReport:
    def test_duration_formatting(self):
        assert format_duration(522) == "8m 42s"
        assert format_duration(45) == "45s"
        assert format_duration(3725) == "1h 2m 5s"
        assert format_duration(None) == "unknown"

    def test_a_repaired_incident_reports_its_facts(self, store):
        incident = _incident(store)
        store.add_attempt(
            incident,
            RepairAttempt(
                number=1,
                branch="jarvis/incident-INC-00001",
                changed_files=["app/dashboard.tsx"],
                regression_tests=["tests/test_dashboard.py"],
                base_commit="a" * 40,
                preview_url="https://preview.example",
                checks={"results": [{"name": "tests", "ran": True, "passed": True}]},
                verification=VerificationResult(passed=True, probe_id="dashboard"),
                outcome="verified",
            ),
        )
        incident.resolution.recovery_type = RecoveryType.VERIFIED_REPAIR
        incident.resolution.pr_url = "https://github.com/x/y/pull/194"
        store.save(incident)

        report = build_report(store.get(incident.id))
        rendered = report.render()

        assert report.verified is True
        assert report.changed_files == ["app/dashboard.tsx"]
        assert "tests/test_dashboard.py" in rendered
        assert "pull/194" in rendered
        assert "Not performed" in rendered  # production deployment

    def test_an_externally_recovered_incident_claims_no_credit(self, store):
        incident = _incident(store)
        incident.resolution.recovery_type = RecoveryType.RECOVERED_EXTERNALLY
        store.save(incident)

        rendered = build_report(store.get(incident.id)).render()

        assert "RECOVERED_EXTERNALLY" in rendered
        assert "None. The failure stopped reproducing" in rendered

    def test_a_missing_regression_test_is_stated_not_hidden(self, store):
        incident = _incident(store)
        store.add_attempt(incident, RepairAttempt(number=1, changed_files=["a.ts"]))
        rendered = build_report(store.get(incident.id)).render()
        assert "none added" in rendered

    def test_the_timeline_comes_from_the_transition_log(self, store):
        incident = _incident(store)
        store.transition(incident, IncidentState.INVESTIGATING, reason="triage")
        report = build_report(store.get(incident.id))
        assert any(e["state"] == "INVESTIGATING" for e in report.timeline)
        assert any(e["reason"] == "triage" for e in report.timeline)

    def test_secrets_never_reach_the_report(self, store):
        secret = "ghp_" + "q" * 36
        incident = _incident(store, title=f"boom {secret}")
        incident.resolution.fix_summary = f"removed {secret}"
        incident.resolution.root_cause = f"token {secret} was committed"
        store.save(incident)

        rendered = build_report(store.get(incident.id)).render()

        assert secret not in rendered

    def test_flapping_is_surfaced(self, store):
        incident = _incident(store)
        incident.metadata["flapping"] = {"flapping": True}
        store.save(incident)
        assert "Flapping" in build_report(store.get(incident.id)).render()

    def test_round_trips_to_json(self, store):
        incident = _incident(store)
        payload = build_report(store.get(incident.id)).to_dict()
        assert payload["incident_id"] == incident.id
        assert payload["production_deployed"] is False


class TestSupervisorStatus:
    def test_status_reports_the_safety_posture(self, store, supervisor):
        status = supervisor.status()
        assert status["production_deployment"] == "OFF"
        assert status["automatic_merge"] == "OFF"

    def test_status_lists_incidents_needing_recovery(self, store, supervisor):
        incident = _incident(store)
        for step in (
            IncidentState.INVESTIGATING,
            IncidentState.REPRODUCING,
            IncidentState.FIXING,
        ):
            store.transition(incident, step)
        supervisor.recover_interrupted_repairs()

        assert supervisor.status()["recovery_required"] == [incident.id]
