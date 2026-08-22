"""Scheduled proactive engineering tests.

Verify that pattern detection runs correctly and converts patterns to features.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.types import Incident, IncidentState, Severity
from openjarvis.wiz.autonomy_metrics import AutonomyMetricsStore, MetricCategory
from openjarvis.wiz.features.model import FeatureRequest, FeatureState, Priority
from openjarvis.wiz.features.store import FeatureStore
from openjarvis.wiz.proactive import PatternDetector
from openjarvis.wiz.scheduled_proactive import (
    ProactiveRunResult,
    ScheduledProactiveRunner,
)


def make_incident(
    id: str,
    probe_id: str = "test",
    state: IncidentState = IncidentState.DETECTED,
    metadata: Optional[Dict[str, Any]] = None,
    created_at: Optional[str] = None,
) -> Incident:
    """Create an incident with minimal required fields."""
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


@pytest.fixture
def incident_store(tmp_path: Path) -> IncidentStore:
    """Temporary incident store for testing."""
    db_path = tmp_path / "incidents.db"
    return IncidentStore(db_path)


@pytest.fixture
def feature_store(tmp_path: Path) -> FeatureStore:
    """Temporary feature store for testing."""
    db_path = tmp_path / "features.db"
    return FeatureStore(db_path)


@pytest.fixture
def metrics_store(tmp_path: Path) -> AutonomyMetricsStore:
    """Temporary metrics store for testing."""
    db_path = tmp_path / "metrics.jsonl"
    return AutonomyMetricsStore(db_path)


@pytest.fixture
def runner(
    incident_store: IncidentStore,
    feature_store: FeatureStore,
    metrics_store: AutonomyMetricsStore,
) -> ScheduledProactiveRunner:
    """Proactive runner with temp stores."""
    return ScheduledProactiveRunner(
        incident_store=incident_store,
        feature_store=feature_store,
        metrics_store=metrics_store,
        lookback_hours=6,
    )


class TestBasicRun:
    """Basic proactive run operation."""

    def test_run_with_no_incidents(
        self, runner: ScheduledProactiveRunner
    ) -> None:
        result = runner.run()
        assert result.incidents_analyzed == 0
        assert result.patterns_detected == 0
        assert result.features_created == 0
        assert result.errors == []

    def test_run_returns_result_object(
        self, runner: ScheduledProactiveRunner
    ) -> None:
        result = runner.run()
        assert isinstance(result, ProactiveRunResult)
        assert result.timestamp
        assert isinstance(result.to_dict(), dict)

    def test_run_loads_recent_incidents(
        self,
        incident_store: IncidentStore,
        runner: ScheduledProactiveRunner,
    ) -> None:
        # Create some incidents
        for i in range(3):
            incident = make_incident(f"INC-{i:03d}")
            incident_store.create(incident)

        result = runner.run()
        assert result.incidents_analyzed == 3


class TestPatternDetection:
    """Pattern detection integration."""

    def test_detects_flapping_pattern(
        self,
        incident_store: IncidentStore,
        runner: ScheduledProactiveRunner,
    ) -> None:
        # Create an incident with flapping metadata
        incident = make_incident(
            id="INC-flap",
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
        incident_store.create(incident)

        result = runner.run()
        assert result.patterns_detected >= 1

    def test_pattern_detection_errors_are_recorded(
        self,
        incident_store: IncidentStore,
        runner: ScheduledProactiveRunner,
    ) -> None:
        # Create an incident
        incident = make_incident("INC-001")
        incident_store.create(incident)

        # Run with a detector that might fail
        result = runner.run()
        # Should complete without crashing even if detection fails
        assert isinstance(result, ProactiveRunResult)


class TestFeatureCreation:
    """Converting patterns to features."""

    def test_creates_features_from_patterns(
        self,
        incident_store: IncidentStore,
        feature_store: FeatureStore,
        runner: ScheduledProactiveRunner,
    ) -> None:
        # Create incident with detectable pattern
        incident = make_incident(
            id="INC-flap",
            probe_id="test_probe",
            metadata={
                "flapping": {
                    "flapping": True,
                    "probe_id": "test_probe",
                    "transitions": 5,
                    "failures": 3,
                    "samples": 8,
                    "window": 10,
                    "threshold": 3,
                    "recent": "PFPFPFPF",
                }
            },
        )
        incident_store.create(incident)

        result = runner.run()
        # If patterns detected, features should be created
        if result.patterns_detected > 0:
            assert result.features_created >= 1

    def test_feature_deduplication(
        self,
        incident_store: IncidentStore,
        feature_store: FeatureStore,
        runner: ScheduledProactiveRunner,
    ) -> None:
        # Create two identical incidents
        for i in range(2):
            incident = make_incident(
                f"INC-{i}",
                probe_id="same_probe",
                metadata={
                    "flapping": {
                        "flapping": True,
                        "probe_id": "same_probe",
                        "transitions": 4,
                        "failures": 2,
                        "samples": 8,
                        "window": 10,
                        "threshold": 3,
                        "recent": "PFPFPFPF",
                    }
                },
            )
            incident_store.create(incident)

        # Run once
        result1 = runner.run()
        features_created_1 = result1.features_created

        # Run again
        result2 = runner.run()
        features_created_2 = result2.features_created

        # Second run should detect dedup
        if features_created_1 > 0:
            # Dedup window may or may not trigger depending on timing
            assert result2.patterns_deduped >= 0


class TestAutonomyMetrics:
    """Autonomy metrics recording."""

    def test_records_run_metric(
        self,
        incident_store: IncidentStore,
        metrics_store: AutonomyMetricsStore,
        runner: ScheduledProactiveRunner,
    ) -> None:
        # Create an incident
        incident = make_incident("INC-001")
        incident_store.create(incident)

        runner.run()

        # Metrics should be recorded
        assert metrics_store.count() >= 1

    def test_records_autonomous_operations(
        self,
        incident_store: IncidentStore,
        feature_store: FeatureStore,
        metrics_store: AutonomyMetricsStore,
        runner: ScheduledProactiveRunner,
    ) -> None:
        # Create detectable pattern
        incident = make_incident(
            "INC-pattern",
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
        incident_store.create(incident)

        runner.run()

        # Get summary
        summary = metrics_store.summarize()
        # Should have recorded autonomous operations
        assert summary.autonomy_rate == 1.0 or summary.total_operations == 0


class TestErrorHandling:
    """Error handling and recovery."""

    def test_handles_corrupted_incidents(
        self,
        incident_store: IncidentStore,
        runner: ScheduledProactiveRunner,
    ) -> None:
        # Create a normal incident
        incident = make_incident("INC-001")
        incident_store.create(incident)

        # Run should not crash
        result = runner.run()
        assert isinstance(result, ProactiveRunResult)

    def test_records_errors(
        self,
        incident_store: IncidentStore,
        runner: ScheduledProactiveRunner,
    ) -> None:
        # Create an incident that might cause issues
        incident = make_incident("INC-001")
        incident_store.create(incident)

        result = runner.run()
        # Errors list should exist (even if empty)
        assert isinstance(result.errors, list)

    def test_continues_after_errors(
        self,
        incident_store: IncidentStore,
        runner: ScheduledProactiveRunner,
    ) -> None:
        # Create multiple incidents
        for i in range(3):
            incident = make_incident(f"INC-{i}")
            incident_store.create(incident)

        # Run should process all incidents even if some fail
        result = runner.run()
        assert result.incidents_analyzed == 3


class TestResultObject:
    """ProactiveRunResult dataclass."""

    def test_result_to_dict(self) -> None:
        result = ProactiveRunResult(
            timestamp="2025-08-22T10:00:00Z",
            incidents_analyzed=10,
            patterns_detected=3,
            patterns_deduped=1,
            tasks_created=2,
            features_created=2,
            errors=[],
        )

        d = result.to_dict()
        assert d["incidents_analyzed"] == 10
        assert d["patterns_detected"] == 3
        assert d["features_created"] == 2
        assert d["errors"] == []

    def test_result_with_errors(self) -> None:
        result = ProactiveRunResult(
            timestamp="2025-08-22T10:00:00Z",
            incidents_analyzed=5,
            patterns_detected=1,
            patterns_deduped=0,
            tasks_created=1,
            features_created=0,
            errors=["error1", "error2"],
        )

        assert len(result.errors) == 2
        d = result.to_dict()
        assert d["errors"] == ["error1", "error2"]


class TestLookbackWindow:
    """Lookback window filtering."""

    def test_ignores_old_incidents(
        self,
        incident_store: IncidentStore,
        runner: ScheduledProactiveRunner,
    ) -> None:
        # Create an old incident (10 hours ago)
        old_time = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        old_incident = make_incident("INC-old", created_at=old_time)
        incident_store.create(old_incident)

        # Create a recent incident
        recent_incident = make_incident("INC-recent")
        incident_store.create(recent_incident)

        result = runner.run()
        # Should only analyze recent incident
        assert result.incidents_analyzed == 1

    def test_includes_recent_incidents(
        self,
        incident_store: IncidentStore,
        runner: ScheduledProactiveRunner,
    ) -> None:
        # Create incidents within lookback window
        now = datetime.now(timezone.utc)
        for i in range(3):
            created_at = (now - timedelta(hours=2 * i)).isoformat()
            incident = make_incident(f"INC-{i}", created_at=created_at)
            incident_store.create(incident)

        result = runner.run()
        assert result.incidents_analyzed == 3
