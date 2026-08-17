"""Tests for probe retries, flake suppression and severity escalation."""

from __future__ import annotations

import pytest

from openjarvis.reliability.probes._stubs import (
    BaseProbeRunner,
    MissingCredentialError,
)
from openjarvis.reliability.probes.executor import (
    ConfirmationTracker,
    ProbeExecutor,
    escalate_severity,
)
from openjarvis.reliability.probes.spec import parse_probe
from openjarvis.reliability.types import ProbeResult, Severity


class _ScriptedRunner(BaseProbeRunner):
    """Returns a pre-scripted sequence of outcomes."""

    runner_id = "scripted"

    def __init__(self, outcomes, *, raises=None):
        self.outcomes = list(outcomes)
        self.calls = 0
        self._raises = raises

    def run(self, spec, *, base_url="", evidence_dir=None, **options):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        success = self.outcomes.pop(0) if self.outcomes else False
        return ProbeResult(
            probe_id=spec.id,
            success=success,
            failure_kind="" if success else "assertion",
            error="" if success else "boom",
        )


def _spec(**overrides):
    probe = {"id": "p", "runner": "http", "url": "/"}
    probe.update(overrides)
    return parse_probe({"probe": probe})


def _executor(runner, **kwargs):
    return ProbeExecutor(runners={"http": runner}, sleep=lambda _: None, **kwargs)


class TestRetries:
    def test_success_first_try(self):
        runner = _ScriptedRunner([True])
        assert _executor(runner).run(_spec()).success
        assert runner.calls == 1

    def test_retries_until_success(self):
        runner = _ScriptedRunner([False, True])
        result = _executor(runner).run(_spec(retry={"attempts": 2}))
        assert result.success
        assert runner.calls == 2

    def test_gives_up_after_attempts(self):
        runner = _ScriptedRunner([False, False, False])
        result = _executor(runner).run(_spec(retry={"attempts": 3}))
        assert not result.success
        assert runner.calls == 3

    def test_no_retry_by_default(self):
        runner = _ScriptedRunner([False, True])
        assert not _executor(runner).run(_spec()).success
        assert runner.calls == 1

    def test_backoff_is_applied_between_attempts(self):
        slept = []
        runner = _ScriptedRunner([False, False])
        executor = ProbeExecutor(runners={"http": runner}, sleep=slept.append)
        executor.run(_spec(retry={"attempts": 2, "backoff_seconds": 7}))
        assert slept == [7.0]

    def test_no_backoff_after_the_last_attempt(self):
        slept = []
        runner = _ScriptedRunner([False])
        executor = ProbeExecutor(runners={"http": runner}, sleep=slept.append)
        executor.run(_spec())
        assert slept == []


class TestErrorHandling:
    def test_missing_credential_is_not_a_site_failure(self):
        """JARVIS being misconfigured must never look like the site is broken."""
        runner = _ScriptedRunner([], raises=MissingCredentialError("no $X"))
        result = _executor(runner).run(_spec())
        assert not result.success
        assert result.failure_kind == "misconfigured"

    def test_missing_credential_is_not_retried(self):
        runner = _ScriptedRunner([], raises=MissingCredentialError("no $X"))
        _executor(runner).run(_spec(retry={"attempts": 3}))
        assert runner.calls == 1

    def test_unexpected_exception_becomes_a_result(self):
        """A runner bug must not take down the monitoring loop."""
        runner = _ScriptedRunner([], raises=RuntimeError("kaboom"))
        result = _executor(runner).run(_spec())
        assert not result.success
        assert result.failure_kind == "runner_error"
        assert "kaboom" in result.error


class TestRunAll:
    def test_skips_disabled_probes(self):
        runner = _ScriptedRunner([True, True])
        executor = _executor(runner)
        specs = [_spec(id="a"), _spec(id="b", enabled=False)]
        assert len(executor.run_all(specs)) == 1


class TestConfirmationTracker:
    def test_single_failure_is_not_confirmed(self):
        tracker = ConfirmationTracker()
        tracker.record("p", failed=True)
        assert not tracker.is_confirmed("p", 2)

    def test_two_consecutive_failures_confirm(self):
        tracker = ConfirmationTracker()
        tracker.record("p", failed=True)
        tracker.record("p", failed=True)
        assert tracker.is_confirmed("p", 2)

    def test_success_resets_the_counter(self):
        """An intermittent failure must never accumulate its way to an incident."""
        tracker = ConfirmationTracker()
        tracker.record("p", failed=True)
        tracker.record("p", failed=False)
        tracker.record("p", failed=True)
        assert not tracker.is_confirmed("p", 2)

    def test_probes_are_tracked_independently(self):
        tracker = ConfirmationTracker()
        tracker.record("a", failed=True)
        tracker.record("a", failed=True)
        tracker.record("b", failed=True)
        assert tracker.is_confirmed("a", 2)
        assert not tracker.is_confirmed("b", 2)

    def test_confirm_runs_of_one_confirms_immediately(self):
        tracker = ConfirmationTracker()
        tracker.record("p", failed=True)
        assert tracker.is_confirmed("p", 1)

    def test_zero_is_treated_as_one(self):
        tracker = ConfirmationTracker()
        tracker.record("p", failed=True)
        assert tracker.is_confirmed("p", 0)

    def test_reset(self):
        tracker = ConfirmationTracker()
        tracker.record("p", failed=True)
        tracker.reset("p")
        assert not tracker.is_confirmed("p", 1)


class TestSeverityEscalation:
    def _result(self, **kwargs):
        base = {"probe_id": "p", "success": False}
        base.update(kwargs)
        return ProbeResult(**base)

    def test_site_unreachable_escalates_to_critical(self):
        spec = _spec(severity="medium")
        result = self._result(failure_kind="navigation", steps_completed=0)
        assert escalate_severity(spec, result) is Severity.CRITICAL

    def test_timeout_before_any_step_escalates(self):
        spec = _spec(severity="low")
        result = self._result(failure_kind="timeout", steps_completed=0)
        assert escalate_severity(spec, result) is Severity.CRITICAL

    def test_timeout_mid_workflow_does_not_escalate(self):
        spec = _spec(severity="medium")
        result = self._result(failure_kind="timeout", steps_completed=3)
        assert escalate_severity(spec, result) is Severity.MEDIUM

    def test_server_error_escalates_high_to_critical(self):
        spec = _spec(severity="high")
        result = self._result(failure_kind="http_error", http_status=500)
        assert escalate_severity(spec, result) is Severity.CRITICAL

    def test_server_error_escalates_low_to_high(self):
        spec = _spec(severity="low")
        result = self._result(failure_kind="http_error", http_status=503)
        assert escalate_severity(spec, result) is Severity.HIGH

    def test_severity_is_never_lowered(self):
        """A probe author declaring CRITICAL knows something the runtime does not."""
        spec = _spec(severity="critical")
        result = self._result(failure_kind="assertion", steps_completed=2)
        assert escalate_severity(spec, result) is Severity.CRITICAL

    def test_plain_assertion_keeps_declared_severity(self):
        spec = _spec(severity="medium")
        result = self._result(failure_kind="assertion", steps_completed=4)
        assert escalate_severity(spec, result) is Severity.MEDIUM


class TestRunnerResolution:
    def test_unknown_runner_raises(self):
        executor = ProbeExecutor()
        spec = _spec()
        spec.runner = "carrier-pigeon"
        with pytest.raises(KeyError, match="No probe runner registered"):
            executor.runner_for(spec)

    def test_runner_is_cached(self):
        runner = _ScriptedRunner([True, True])
        executor = _executor(runner)
        assert executor.runner_for(_spec()) is executor.runner_for(_spec())
