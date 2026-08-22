"""Tests for incident detection, diagnosis, and repair system."""

from __future__ import annotations

import pytest

from openjarvis.wiz.repair import (
    Incident,
    IncidentDetector,
    IncidentDiagnoser,
    IncidentManager,
    IncidentRepair,
    IncidentSeverity,
    IncidentType,
)


class TestIncidentDetector:
    """Test incident detection from logs and metrics."""

    @pytest.mark.asyncio
    async def test_detect_database_error_from_logs(self):
        """Test detecting database connection errors in logs."""
        detector = IncidentDetector()
        logs = "ERROR: Unable to connect to database - connection refused"

        incidents = await detector.detect_from_logs(logs)

        assert len(incidents) > 0
        db_incidents = [i for i in incidents if i.type == IncidentType.DATABASE_ERROR]
        assert len(db_incidents) > 0
        assert db_incidents[0].severity == IncidentSeverity.HIGH

    @pytest.mark.asyncio
    async def test_detect_test_failure_from_logs(self):
        """Test detecting test failures in logs."""
        detector = IncidentDetector()
        logs = "Running test suite...\nTest_user_login FAILED\n3 passed, 1 failed"

        incidents = await detector.detect_from_logs(logs)

        test_incidents = [i for i in incidents if i.type == IncidentType.TESTS_FAILING]
        assert len(test_incidents) > 0
        assert test_incidents[0].severity == IncidentSeverity.MEDIUM

    @pytest.mark.asyncio
    async def test_detect_no_incidents(self):
        """Test that no incidents detected when logs are clean."""
        detector = IncidentDetector()
        logs = "All services running normally. Tests passed. No errors."

        incidents = await detector.detect_from_logs(logs)

        assert len(incidents) == 0

    @pytest.mark.asyncio
    async def test_detect_performance_degradation_from_metrics(self):
        """Test detecting performance degradation from metrics."""
        detector = IncidentDetector()
        metrics = {"p99_latency_ms": 8000, "error_rate": 0.02}

        incidents = await detector.detect_from_metrics(metrics)

        perf_incidents = [
            i for i in incidents if i.type == IncidentType.PERFORMANCE_DEGRADED
        ]
        assert len(perf_incidents) > 0
        assert perf_incidents[0].severity == IncidentSeverity.MEDIUM
        assert perf_incidents[0].evidence["p99_latency_ms"] == 8000

    @pytest.mark.asyncio
    async def test_detect_high_error_rate_from_metrics(self):
        """Test detecting high error rate from metrics."""
        detector = IncidentDetector()
        metrics = {"p99_latency_ms": 1000, "error_rate": 0.15}

        incidents = await detector.detect_from_metrics(metrics)

        error_incidents = [
            i for i in incidents if i.type == IncidentType.UNKNOWN and i.affected_component == "services"
        ]
        assert len(error_incidents) > 0
        assert error_incidents[0].severity == IncidentSeverity.HIGH

    @pytest.mark.asyncio
    async def test_detect_metrics_within_threshold(self):
        """Test that no incidents detected when metrics are healthy."""
        detector = IncidentDetector()
        metrics = {"p99_latency_ms": 100, "error_rate": 0.01}

        incidents = await detector.detect_from_metrics(metrics)

        assert len(incidents) == 0

    @pytest.mark.asyncio
    async def test_detect_multiple_incidents_same_logs(self):
        """Test detecting multiple incidents from single logs."""
        detector = IncidentDetector()
        logs = "ERROR: database connection failed\nTest suite failed: 2 tests failing"

        incidents = await detector.detect_from_logs(logs)

        assert len(incidents) >= 1
        types = {i.type for i in incidents}
        assert IncidentType.DATABASE_ERROR in types
        assert IncidentType.TESTS_FAILING in types


class TestIncidentDiagnoser:
    """Test incident diagnosis."""

    @pytest.mark.asyncio
    async def test_diagnose_test_failure(self):
        """Test diagnosing test failure incidents."""
        diagnoser = IncidentDiagnoser()
        incident = Incident(
            id="INC-test-001",
            type=IncidentType.TESTS_FAILING,
            severity=IncidentSeverity.MEDIUM,
            title="Test failures",
            description="Tests are failing",
            affected_component="tests",
        )

        diagnosis = await diagnoser.diagnose(incident)

        assert diagnosis is not None
        assert incident.diagnosed is True
        assert incident.diagnosis is not None
        assert "code changes" in diagnosis.lower()

    @pytest.mark.asyncio
    async def test_diagnose_performance_issue(self):
        """Test diagnosing performance degradation."""
        diagnoser = IncidentDiagnoser()
        incident = Incident(
            id="INC-perf-001",
            type=IncidentType.PERFORMANCE_DEGRADED,
            severity=IncidentSeverity.MEDIUM,
            title="Performance degradation",
            description="System is slow",
            affected_component="performance",
            evidence={"p99_latency_ms": 7000},
        )

        diagnosis = await diagnoser.diagnose(incident)

        assert diagnosis is not None
        assert incident.diagnosed is True
        assert "optimization" in diagnosis.lower() or "scaling" in diagnosis.lower()

    @pytest.mark.asyncio
    async def test_diagnose_database_error(self):
        """Test diagnosing database errors."""
        diagnoser = IncidentDiagnoser()
        incident = Incident(
            id="INC-db-001",
            type=IncidentType.DATABASE_ERROR,
            severity=IncidentSeverity.HIGH,
            title="Database error",
            description="Cannot connect",
            affected_component="database",
        )

        diagnosis = await diagnoser.diagnose(incident)

        assert diagnosis is not None
        assert incident.diagnosed is True
        assert "database" in diagnosis.lower()

    @pytest.mark.asyncio
    async def test_diagnose_unknown_type(self):
        """Test that diagnosis returns None for unknown incident types."""
        diagnoser = IncidentDiagnoser()
        incident = Incident(
            id="INC-unk-001",
            type=IncidentType.UNKNOWN,
            severity=IncidentSeverity.MEDIUM,
            title="Unknown incident",
            description="Something is wrong",
            affected_component="unknown",
        )

        diagnosis = await diagnoser.diagnose(incident)

        assert diagnosis is None
        assert incident.diagnosed is False


class TestIncidentRepair:
    """Test incident repair."""

    @pytest.mark.asyncio
    async def test_cannot_repair_critical_severity(self):
        """Test that critical incidents cannot be repaired autonomously."""
        repair = IncidentRepair()
        incident = Incident(
            id="INC-crit-001",
            type=IncidentType.TESTS_FAILING,
            severity=IncidentSeverity.CRITICAL,
            title="Critical test failure",
            description="Critical tests failing",
            affected_component="tests",
            diagnosed=True,
        )

        result = await repair.attempt_repair(incident)

        assert result is False
        assert not incident.can_repair_autonomously

    @pytest.mark.asyncio
    async def test_cannot_repair_undiagnosed(self):
        """Test that undiagnosed incidents cannot be repaired."""
        repair = IncidentRepair()
        incident = Incident(
            id="INC-test-001",
            type=IncidentType.TESTS_FAILING,
            severity=IncidentSeverity.MEDIUM,
            title="Test failure",
            description="Tests failing",
            affected_component="tests",
            diagnosed=False,
        )

        result = await repair.attempt_repair(incident)

        assert result is False

    def test_can_repair_low_severity_test_failure(self):
        """Test that low severity test failures can potentially be repaired."""
        incident = Incident(
            id="INC-test-001",
            type=IncidentType.TESTS_FAILING,
            severity=IncidentSeverity.LOW,
            title="Test failure",
            description="Tests failing",
            affected_component="tests",
            diagnosed=True,
        )

        assert incident.can_repair_autonomously is True

    def test_can_repair_medium_severity_feature_regression(self):
        """Test that medium severity regressions can potentially be repaired."""
        incident = Incident(
            id="INC-reg-001",
            type=IncidentType.FEATURE_REGRESSION,
            severity=IncidentSeverity.MEDIUM,
            title="Feature regression",
            description="Feature broken",
            affected_component="feature",
            diagnosed=True,
        )

        assert incident.can_repair_autonomously is True

    def test_cannot_repair_unsafe_incident_type(self):
        """Test that unsafe incident types cannot be repaired."""
        incident = Incident(
            id="INC-db-001",
            type=IncidentType.DATABASE_ERROR,
            severity=IncidentSeverity.MEDIUM,
            title="Database error",
            description="Database issue",
            affected_component="database",
            diagnosed=True,
        )

        assert incident.can_repair_autonomously is False

    @pytest.mark.asyncio
    async def test_repair_test_failure_placeholder(self):
        """Test repair test failure (placeholder implementation)."""
        repair = IncidentRepair()
        incident = Incident(
            id="INC-test-001",
            type=IncidentType.TESTS_FAILING,
            severity=IncidentSeverity.MEDIUM,
            title="Test failure",
            description="Tests failing",
            affected_component="tests",
            diagnosed=True,
        )

        result = await repair.repair_test_failure(incident)

        assert incident.repair_attempted is True
        assert incident.repair_result is not None
        assert result is False  # Placeholder returns False

    @pytest.mark.asyncio
    async def test_repair_performance_issue_placeholder(self):
        """Test repair performance issue (placeholder implementation)."""
        repair = IncidentRepair()
        incident = Incident(
            id="INC-perf-001",
            type=IncidentType.PERFORMANCE_DEGRADED,
            severity=IncidentSeverity.MEDIUM,
            title="Performance issue",
            description="System is slow",
            affected_component="performance",
            diagnosed=True,
        )

        result = await repair.repair_performance_issue(incident)

        assert incident.repair_attempted is True
        assert incident.repair_result is not None
        assert result is False  # Placeholder returns False


class TestIncidentManager:
    """Test incident manager orchestration."""

    @pytest.mark.asyncio
    async def test_handle_safe_incident(self):
        """Test handling an incident that can be repaired autonomously."""
        manager = IncidentManager()
        incident = Incident(
            id="INC-test-001",
            type=IncidentType.TESTS_FAILING,
            severity=IncidentSeverity.LOW,
            title="Test failure",
            description="Tests failing",
            affected_component="tests",
        )

        result = await manager.handle_incident(incident)

        assert incident.id in manager.incidents
        assert incident.diagnosed is True
        assert incident.diagnosis is not None

    @pytest.mark.asyncio
    async def test_handle_unsafe_incident(self):
        """Test handling an incident that cannot be repaired."""
        manager = IncidentManager()
        incident = Incident(
            id="INC-crit-001",
            type=IncidentType.AUTH_ISSUES,
            severity=IncidentSeverity.HIGH,
            title="Auth issue",
            description="Authentication broken",
            affected_component="auth",
        )

        result = await manager.handle_incident(incident)

        assert result is False
        assert incident.id in manager.incidents

    @pytest.mark.asyncio
    async def test_manager_stores_multiple_incidents(self):
        """Test that manager can track multiple incidents."""
        manager = IncidentManager()
        incident1 = Incident(
            id="INC-001",
            type=IncidentType.TESTS_FAILING,
            severity=IncidentSeverity.LOW,
            title="Test 1",
            description="Desc 1",
            affected_component="comp1",
        )
        incident2 = Incident(
            id="INC-002",
            type=IncidentType.PERFORMANCE_DEGRADED,
            severity=IncidentSeverity.MEDIUM,
            title="Test 2",
            description="Desc 2",
            affected_component="comp2",
        )

        await manager.handle_incident(incident1)
        await manager.handle_incident(incident2)

        assert len(manager.incidents) == 2
        assert "INC-001" in manager.incidents
        assert "INC-002" in manager.incidents


class TestIncidentProperties:
    """Test incident properties and state."""

    def test_incident_initial_state(self):
        """Test incident initial state."""
        incident = Incident(
            id="INC-001",
            type=IncidentType.TESTS_FAILING,
            severity=IncidentSeverity.MEDIUM,
            title="Test",
            description="Desc",
            affected_component="comp",
        )

        assert incident.diagnosed is False
        assert incident.diagnosis is None
        assert incident.repair_attempted is False
        assert incident.repair_result is None
        assert incident.resolved is False
        assert incident.evidence == {}

    def test_incident_with_evidence(self):
        """Test incident with evidence."""
        evidence = {"latency_ms": 5000, "error_rate": 0.1}
        incident = Incident(
            id="INC-001",
            type=IncidentType.PERFORMANCE_DEGRADED,
            severity=IncidentSeverity.MEDIUM,
            title="Performance",
            description="Slow",
            affected_component="perf",
            evidence=evidence,
        )

        assert incident.evidence == evidence
        assert incident.evidence["latency_ms"] == 5000
