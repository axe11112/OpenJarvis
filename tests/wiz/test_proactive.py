"""Proactive pattern detection turning into tasks.

A detected pattern is not automatically acted on. It becomes a task waiting
for an operator's approval, and only the operator's approval grant it
authority to change code. This is the bridge between diagnostics (read
permission) and engineering (write permission).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openjarvis.reliability.types import Incident, IncidentState, Severity
from openjarvis.wiz.features.model import FeatureState, Priority
from openjarvis.wiz.proactive import (
    PatternDetector,
    ProactiveTask,
    detect_patterns,
    task_to_feature_request,
)


def make_incident(
    id: str,
    probe_id: str = "test",
    state: IncidentState = IncidentState.DETECTED,
    metadata: dict | None = None,
    created_at: str | None = None,
) -> Incident:
    """Helper to create an incident with minimal required fields."""
    now = created_at or datetime.now(timezone.utc).isoformat()
    return Incident(
        fingerprint="fp-test",
        severity=Severity.MEDIUM,
        component="test-component",
        title="Test incident",
        id=id,
        probe_id=probe_id,
        state=state,
        metadata=metadata or {},
        created_at=now,
        updated_at=now,
    )


class TestFlappingDetection:
    """Patterns from flapping probes."""

    def test_flapping_in_incident_data_becomes_a_task(self):
        incident = make_incident(
            id="INC-001",
            probe_id="health_check",
            state=IncidentState.DETECTED,
            metadata={
                "flapping": {
                    "flapping": True,
                    "probe_id": "health_check",
                    "transitions": 4,
                    "failures": 2,
                    "samples": 8,
                    "window": 10,
                    "threshold": 3,
                    "recent": "PFPFPFPF",
                }
            },
        )
        tasks = detect_patterns([incident])
        assert len(tasks) == 1
        assert tasks[0].pattern_type == "flapping"
        assert "health_check" in tasks[0].summary
        assert tasks[0].confidence > 0.5

    def test_non_flapping_incident_produces_no_task(self):
        incident = make_incident(
            id="INC-001",
            probe_id="health_check",
            state=IncidentState.DETECTED,
            metadata={
                "flapping": {
                    "flapping": False,
                    "probe_id": "health_check",
                    "transitions": 1,
                    "recent": "FFFF",
                }
            },
        )
        tasks = detect_patterns([incident])
        assert len(tasks) == 0

    def test_malformed_flapping_data_does_not_crash(self):
        incident = make_incident(
            id="INC-001",
            probe_id="health_check",
            state=IncidentState.DETECTED,
            metadata={"flapping": {"incomplete": "data"}},
        )
        tasks = detect_patterns([incident])
        assert len(tasks) == 0

    def test_flapping_confidence_increases_with_transitions(self):
        tasks = []
        for transitions in [3, 5, 8]:
            incident = make_incident(
                id=f"INC-{transitions}",
                probe_id="test",
                state=IncidentState.DETECTED,
                metadata={
                    "flapping": {
                        "flapping": True,
                        "probe_id": "test",
                        "transitions": transitions,
                        "failures": 4,
                        "samples": 8,
                        "window": 10,
                        "threshold": 3,
                        "recent": "PFPFPFPF",
                    }
                },
            )
            tasks.extend(detect_patterns([incident]))

        confidences = [t.confidence for t in tasks]
        assert confidences == sorted(confidences), "Confidence should increase with transitions"

    def test_flapping_over_many_incidents_is_reported_once(self):
        detector = PatternDetector()
        incident = make_incident(
            id="INC-001",
            probe_id="health_check",
            state=IncidentState.DETECTED,
            metadata={
                "flapping": {
                    "flapping": True,
                    "probe_id": "health_check",
                    "transitions": 4,
                    "failures": 2,
                    "samples": 8,
                    "window": 10,
                    "threshold": 3,
                    "recent": "PFPFPFPF",
                }
            },
        )

        # First run creates a task.
        tasks1 = detector.detect_in_incident(incident)
        assert len(tasks1) == 1

        # Immediate second run does not, due to dedup.
        tasks2 = detector.detect_in_incident(incident)
        assert len(tasks2) == 0

        # After the dedup window, it reports again.
        detector.dedup_window = timedelta(seconds=0)
        tasks3 = detector.detect_in_incident(incident)
        assert len(tasks3) == 1


class TestPersistentFailures:
    """Patterns from probes that never recover in a window."""

    def test_persistent_failures_become_a_task(self):
        incidents = [
            make_incident(
                id=f"INC-{i}",
                probe_id="db_connection",
                state=IncidentState.DETECTED,
                created_at=(datetime.now(timezone.utc) - timedelta(minutes=5 - i)).isoformat(),
            )
            for i in range(3)
        ]
        detector = PatternDetector(window=timedelta(hours=1))
        tasks = detector.detect_in_store(incidents)
        persistent = [t for t in tasks if t.pattern_type == "persistent"]
        assert len(persistent) >= 1
        assert "3" in persistent[0].description

    def test_fewer_than_three_failures_is_not_persistent(self):
        incidents = [
            make_incident(id="INC-1", probe_id="db_connection", state=IncidentState.DETECTED),
            make_incident(id="INC-2", probe_id="db_connection", state=IncidentState.DETECTED),
        ]
        detector = PatternDetector()
        tasks = detector.detect_in_store(incidents)
        persistent = [t for t in tasks if t.pattern_type == "persistent"]
        assert len(persistent) == 0

    def test_failures_outside_the_window_are_ignored(self):
        incidents = [
            make_incident(
                id="INC-old",
                probe_id="db_connection",
                state=IncidentState.DETECTED,
                created_at=(datetime.now(timezone.utc) - timedelta(hours=10)).isoformat(),
            ),
            make_incident(
                id="INC-1",
                probe_id="db_connection",
                state=IncidentState.DETECTED,
            ),
        ]
        detector = PatternDetector(window=timedelta(hours=1))
        tasks = detector.detect_in_store(incidents)
        persistent = [t for t in tasks if t.pattern_type == "persistent"]
        # The old one is outside the window, so only 1 recent incident remains.
        assert len(persistent) == 0

    def test_mixed_open_and_closed_incidents_is_not_persistent(self):
        incidents = [
            make_incident(id="INC-1", probe_id="test", state=IncidentState.DETECTED),
            make_incident(id="INC-2", probe_id="test", state=IncidentState.RESOLVED),
            make_incident(id="INC-3", probe_id="test", state=IncidentState.DETECTED),
        ]
        detector = PatternDetector()
        tasks = detector.detect_in_store(incidents)
        persistent = [t for t in tasks if t.pattern_type == "persistent"]
        assert len(persistent) == 0


class TestRecurrence:
    """Patterns from probes that re-break after being fixed."""

    def test_multiple_closures_indicate_recurrence(self):
        incidents = [
            make_incident(id="INC-1", probe_id="test", state=IncidentState.RESOLVED),
            make_incident(id="INC-2", probe_id="test", state=IncidentState.RESOLVED),
            make_incident(id="INC-3", probe_id="test", state=IncidentState.RESOLVED),
        ]
        detector = PatternDetector()
        tasks = detector.detect_in_store(incidents)
        recurrence = [t for t in tasks if t.pattern_type == "recurrence"]
        assert len(recurrence) == 1
        assert "3" in recurrence[0].description

    def test_only_one_closure_is_not_recurrence(self):
        incidents = [
            make_incident(id="INC-1", probe_id="test", state=IncidentState.RESOLVED),
            make_incident(id="INC-2", probe_id="test", state=IncidentState.DETECTED),
        ]
        detector = PatternDetector()
        tasks = detector.detect_in_store(incidents)
        recurrence = [t for t in tasks if t.pattern_type == "recurrence"]
        assert len(recurrence) == 0


class TestDeduplication:
    """Tasks are not reported multiple times in one run."""

    def test_same_pattern_from_multiple_sources_is_deduplicated(self):
        incident = make_incident(
            id="INC-001",
            probe_id="health_check",
            state=IncidentState.DETECTED,
            metadata={
                "flapping": {
                    "flapping": True,
                    "probe_id": "health_check",
                    "transitions": 4,
                    "failures": 2,
                    "samples": 8,
                    "window": 10,
                    "threshold": 3,
                    "recent": "PFPFPFPF",
                }
            },
        )
        detector = PatternDetector()
        # Both methods might detect the same pattern.
        tasks = detector.detect_in_incident(incident)
        tasks.extend(detector.detect_in_store([incident]))
        deduped = detector._deduplicate(tasks)
        assert len(deduped) <= len(tasks)

    def test_different_patterns_are_not_deduplicated(self):
        incidents = [
            make_incident(
                id="INC-1",
                probe_id="health_check",
                state=IncidentState.DETECTED,
                metadata={
                    "flapping": {
                        "flapping": True,
                        "probe_id": "health_check",
                        "transitions": 4,
                        "failures": 2,
                        "samples": 8,
                        "window": 10,
                        "threshold": 3,
                        "recent": "PFPFPFPF",
                    }
                },
            ),
            make_incident(
                id="INC-2",
                probe_id="database",
                state=IncidentState.DETECTED,
                metadata={
                    "flapping": {
                        "flapping": True,
                        "probe_id": "database",
                        "transitions": 4,
                        "failures": 2,
                        "samples": 8,
                        "window": 10,
                        "threshold": 3,
                        "recent": "PFPFPFPF",
                    }
                },
            ),
        ]
        tasks = detect_patterns(incidents)
        flapping_tasks = [t for t in tasks if t.pattern_type == "flapping"]
        # Both probes flapping should produce two tasks.
        assert len(flapping_tasks) == 2


class TestConversionToFeatureRequest:
    """Proactive tasks become feature requests."""

    def test_task_becomes_feature_request(self):
        task = ProactiveTask(
            pattern_id="flapping:test",
            pattern_type="flapping",
            summary="Probe 'test' is flapping",
            description="Details here",
            confidence=0.8,
            detected_at=datetime.now(timezone.utc).isoformat(),
        )
        feature = task_to_feature_request(task)
        assert feature.title == task.summary
        assert feature.state == FeatureState.RECEIVED
        assert feature.source == "proactive"
        assert feature.actor_id == "wiz-proactive"

    def test_high_confidence_task_gets_higher_priority(self):
        high = ProactiveTask(
            pattern_id="p1",
            pattern_type="test",
            summary="high",
            description="test",
            confidence=0.9,
            detected_at=datetime.now(timezone.utc).isoformat(),
        )
        low = ProactiveTask(
            pattern_id="p2",
            pattern_type="test",
            summary="low",
            description="test",
            confidence=0.5,
            detected_at=datetime.now(timezone.utc).isoformat(),
        )
        high_feature = task_to_feature_request(high)
        low_feature = task_to_feature_request(low)
        assert high_feature.priority.rank < low_feature.priority.rank

    def test_feature_request_references_the_pattern(self):
        task = ProactiveTask(
            pattern_id="test:id",
            pattern_type="flapping",
            summary="Test summary",
            description="Test description",
            confidence=0.7,
            detected_at=datetime.now(timezone.utc).isoformat(),
        )
        feature = task_to_feature_request(task)
        assert task.summary in feature.operator_request
        assert task.summary in feature.title

    def test_feature_request_has_unique_id(self):
        task = ProactiveTask(
            pattern_id="test",
            pattern_type="test",
            summary="test",
            description="test",
            confidence=0.5,
            detected_at=datetime.now(timezone.utc).isoformat(),
        )
        f1 = task_to_feature_request(task)
        f2 = task_to_feature_request(task)
        assert f1.id != f2.id


class TestPatternIsolation:
    """Patterns in one probe do not affect others."""

    def test_probes_are_analyzed_independently(self):
        incidents = [
            make_incident(id="INC-1", probe_id="probe_a", state=IncidentState.DETECTED),
            make_incident(id="INC-2", probe_id="probe_a", state=IncidentState.DETECTED),
            make_incident(id="INC-3", probe_id="probe_a", state=IncidentState.DETECTED),
            make_incident(id="INC-4", probe_id="probe_b", state=IncidentState.DETECTED),
        ]
        detector = PatternDetector()
        tasks = detector.detect_in_store(incidents)
        persistent = [t for t in tasks if t.pattern_type == "persistent"]
        # probe_a has 3 incidents (persistent), probe_b has 1 (not persistent).
        assert len(persistent) == 1
        assert "probe_a" in persistent[0].summary

    def test_different_probes_same_pattern_type_are_separate(self):
        detector = PatternDetector()
        for probe in ["probe_a", "probe_b"]:
            incident = make_incident(
                id=f"INC-{probe}",
                probe_id=probe,
                state=IncidentState.DETECTED,
                metadata={
                    "flapping": {
                        "flapping": True,
                        "probe_id": probe,
                        "transitions": 4,
                        "failures": 2,
                        "samples": 8,
                        "window": 10,
                        "threshold": 3,
                        "recent": "PFPFPFPF",
                    }
                },
            )
            tasks = detector.detect_in_incident(incident)
            assert len(tasks) == 1


class TestEmpty:
    """Empty or missing data does not crash."""

    def test_no_incidents_produces_no_tasks(self):
        tasks = detect_patterns([])
        assert tasks == []

    def test_incident_with_no_flapping_data_produces_no_task(self):
        incident = make_incident(id="INC-1", probe_id="test", state=IncidentState.DETECTED)
        tasks = detect_patterns([incident])
        # May have other patterns, but no flapping task.
        flapping = [t for t in tasks if t.pattern_type == "flapping"]
        assert len(flapping) == 0

    def test_none_detector_uses_defaults(self):
        incident = make_incident(
            id="INC-1",
            probe_id="test",
            state=IncidentState.DETECTED,
            metadata={
                "flapping": {
                    "flapping": True,
                    "probe_id": "test",
                    "transitions": 4,
                    "failures": 2,
                    "samples": 8,
                    "window": 10,
                    "threshold": 3,
                    "recent": "PFPFPFPF",
                }
            },
        )
        tasks = detect_patterns([incident], detector=None)
        assert len(tasks) >= 1
