"""Tests for flapping detection and deterministic severity.

Both exist to stop JARVIS spending a coding agent on the wrong thing: flapping
detection stops it chasing an intermittent failure, and deterministic severity
stops a model's opinion deciding who gets woken up.
"""

from __future__ import annotations

import pytest

from openjarvis.reliability.flapping import FlappingDetector
from openjarvis.reliability.severity import classify
from openjarvis.reliability.types import ProbeResult, Severity


def _feed(detector: FlappingDetector, pattern: str, probe_id: str = "homepage"):
    """Feed a pattern like ``"FPFPF"`` and return the final verdict."""
    verdict = None
    for char in pattern:
        verdict = detector.record(probe_id, failed=char == "F")
    return verdict


class TestFlappingDetection:
    def test_alternating_results_are_flapping(self):
        detector = FlappingDetector(window=10, failure_threshold=3, min_samples=4)
        verdict = _feed(detector, "PFPFPF")
        assert verdict.flapping
        assert verdict.transitions >= 3

    def test_a_sustained_outage_is_not_flapping(self):
        """Ten failures in a row is an outage, and belongs on the repair path."""
        detector = FlappingDetector(window=10, failure_threshold=3, min_samples=4)
        verdict = _feed(detector, "FFFFFFFFFF")
        assert not verdict.flapping

    def test_a_healthy_check_is_not_flapping(self):
        detector = FlappingDetector()
        verdict = _feed(detector, "PPPPPPPP")
        assert not verdict.flapping

    def test_one_failure_after_passes_is_not_flapping(self):
        detector = FlappingDetector(failure_threshold=3, min_samples=4)
        verdict = _feed(detector, "PPPPF")
        assert not verdict.flapping

    def test_below_min_samples_never_flaps(self):
        """Never call something flapping before there is history to say so."""
        detector = FlappingDetector(failure_threshold=1, min_samples=6)
        verdict = _feed(detector, "PF")
        assert not verdict.flapping

    def test_threshold_is_respected(self):
        low = FlappingDetector(failure_threshold=2, min_samples=4)
        high = FlappingDetector(failure_threshold=5, min_samples=4)
        pattern = "PFPFP"
        assert _feed(low, pattern).flapping
        assert not _feed(high, pattern).flapping

    def test_the_window_slides(self):
        """Old alternation drops out once the check settles."""
        detector = FlappingDetector(window=4, failure_threshold=3, min_samples=4)
        _feed(detector, "PFPF")
        verdict = _feed(detector, "PPPP")
        assert not verdict.flapping

    def test_probes_are_tracked_separately(self):
        detector = FlappingDetector(failure_threshold=3, min_samples=4)
        _feed(detector, "PFPFPF", probe_id="flappy")
        calm = _feed(detector, "PPPPPP", probe_id="calm")
        assert detector.verdict("flappy").flapping
        assert not calm.flapping

    def test_reset_forgets_history(self):
        detector = FlappingDetector(failure_threshold=3, min_samples=4)
        _feed(detector, "PFPFPF")
        detector.reset("homepage")
        assert not detector.verdict("homepage").flapping

    def test_reason_names_the_probe_and_the_pattern(self):
        detector = FlappingDetector(failure_threshold=3, min_samples=4)
        verdict = _feed(detector, "PFPFPF", probe_id="checkout")
        assert "checkout" in verdict.reason
        assert "PFPFPF" in verdict.reason

    def test_snapshot_renders_history(self):
        detector = FlappingDetector()
        _feed(detector, "PFP")
        assert detector.snapshot()["homepage"] == "PFP"

    def test_round_trips(self):
        detector = FlappingDetector(failure_threshold=3, min_samples=4)
        payload = _feed(detector, "PFPFPF").to_dict()
        assert payload["flapping"] is True
        assert payload["recent"] == "PFPFPF"


def _result(**kwargs) -> ProbeResult:
    defaults = dict(probe_id="p", success=False, steps_completed=3)
    defaults.update(kwargs)
    return ProbeResult(**defaults)


class TestDeterministicSeverity:
    def test_auth_unreachable_is_critical(self):
        c = classify(
            component="authentication",
            result=_result(failure_kind="navigation"),
            declared=Severity.HIGH,
        )
        assert c.severity is Severity.CRITICAL
        assert "authentication" in c.reason

    def test_auth_500_is_critical(self):
        c = classify(
            component="login",
            result=_result(failure_kind="http_error", http_status=503),
            declared=Severity.MEDIUM,
        )
        assert c.severity is Severity.CRITICAL

    def test_site_completely_unreachable_is_critical(self):
        c = classify(
            component="marketing",
            result=_result(failure_kind="timeout", steps_completed=0),
            declared=Severity.LOW,
        )
        assert c.severity is Severity.CRITICAL

    def test_critical_path_500_is_critical(self):
        c = classify(
            component="checkout",
            result=_result(failure_kind="http_error", http_status=500),
            declared=Severity.MEDIUM,
        )
        assert c.severity is Severity.CRITICAL

    def test_broken_auth_workflow_is_high(self):
        c = classify(
            component="login",
            result=_result(failure_kind="assertion"),
            declared=Severity.MEDIUM,
        )
        assert c.severity is Severity.HIGH

    def test_broken_dashboard_is_high(self):
        c = classify(
            component="dashboard",
            result=_result(failure_kind="assertion"),
            declared=Severity.MEDIUM,
        )
        assert c.severity is Severity.HIGH

    def test_client_error_is_medium(self):
        c = classify(
            component="reports",
            result=_result(failure_kind="http_error", http_status=404),
            declared=Severity.LOW,
        )
        assert c.severity is Severity.MEDIUM

    def test_visual_issue_is_low(self):
        c = classify(
            component="marketing",
            result=_result(failure_kind="visual"),
            declared=Severity.LOW,
        )
        assert c.severity is Severity.LOW

    def test_declared_severity_is_a_floor_never_a_ceiling(self):
        """The operator knows things a generic rule does not."""
        c = classify(
            component="marketing",
            result=_result(failure_kind="visual"),
            declared=Severity.HIGH,
        )
        assert c.severity is Severity.HIGH

    def test_observed_impact_can_raise_the_declared_severity(self):
        c = classify(
            component="auth",
            result=_result(failure_kind="navigation"),
            declared=Severity.MEDIUM,
        )
        assert c.escalated is True

    def test_unmatched_failure_keeps_the_declared_severity(self):
        c = classify(
            component="widgets",
            result=_result(failure_kind="assertion"),
            declared=Severity.MEDIUM,
        )
        assert c.severity is Severity.MEDIUM
        assert c.rule == "declared"

    def test_classification_is_reproducible(self):
        """Same inputs, same answer — no model, no randomness."""
        kwargs = dict(
            component="checkout",
            result=_result(failure_kind="http_error", http_status=500),
            declared=Severity.LOW,
        )
        assert classify(**kwargs).to_dict() == classify(**kwargs).to_dict()

    @pytest.mark.parametrize("component", ["auth", "AUTH", "user-authentication"])
    def test_component_matching_is_case_insensitive(self, component):
        c = classify(
            component=component,
            result=_result(failure_kind="assertion"),
            declared=Severity.LOW,
        )
        assert c.severity is Severity.HIGH

    def test_every_classification_carries_its_rule(self):
        c = classify(
            component="checkout", result=_result(http_status=500), declared=Severity.LOW
        )
        assert c.rule
        assert c.reason
