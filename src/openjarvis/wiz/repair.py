"""Incident detection, diagnosis, and autonomous repair for Wiz."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class IncidentSeverity(Enum):
    """Severity of an incident."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class IncidentType(Enum):
    """Type of incident."""

    DEPLOYMENT_FAILED = "deployment_failed"
    TESTS_FAILING = "tests_failing"
    PERFORMANCE_DEGRADED = "performance_degraded"
    FEATURE_REGRESSION = "feature_regression"
    AUTH_ISSUES = "auth_issues"
    DATABASE_ERROR = "database_error"
    EXTERNAL_SERVICE_DOWN = "external_service_down"
    UNKNOWN = "unknown"


@dataclass
class Incident:
    """Represents a detected incident."""

    id: str
    type: IncidentType
    severity: IncidentSeverity
    title: str
    description: str
    affected_component: str
    evidence: dict = field(default_factory=dict)
    diagnosed: bool = False
    diagnosis: Optional[str] = None
    repair_attempted: bool = False
    repair_result: Optional[str] = None
    resolved: bool = False

    @property
    def can_repair_autonomously(self) -> bool:
        """Determine if incident can be repaired autonomously."""
        # Only LOW/MEDIUM severity, certain types can be repaired autonomously
        if self.severity == IncidentSeverity.CRITICAL:
            return False

        safe_types = {
            IncidentType.TESTS_FAILING,
            IncidentType.FEATURE_REGRESSION,
        }

        return self.type in safe_types and self.diagnosed


class IncidentDetector:
    """Detect incidents from monitoring data."""

    async def detect_from_logs(self, logs: str) -> list[Incident]:
        """Detect incidents from application logs.

        Args:
            logs: Application log output

        Returns:
            List of detected incidents
        """
        incidents = []

        # Pattern detection
        if "error" in logs.lower() and "connection" in logs.lower():
            incidents.append(
                Incident(
                    id="INC-db-001",
                    type=IncidentType.DATABASE_ERROR,
                    severity=IncidentSeverity.HIGH,
                    title="Database connection error",
                    description="Application cannot connect to database",
                    affected_component="database",
                )
            )

        if "test" in logs.lower() and "failed" in logs.lower():
            incidents.append(
                Incident(
                    id="INC-test-001",
                    type=IncidentType.TESTS_FAILING,
                    severity=IncidentSeverity.MEDIUM,
                    title="Test suite failures detected",
                    description="One or more tests are failing",
                    affected_component="tests",
                )
            )

        return incidents

    async def detect_from_metrics(self, metrics: dict) -> list[Incident]:
        """Detect incidents from system metrics.

        Args:
            metrics: System metrics (latency, error_rate, uptime, etc.)

        Returns:
            List of detected incidents
        """
        incidents = []

        # Check latency
        latency = metrics.get("p99_latency_ms", 0)
        if latency > 5000:
            incidents.append(
                Incident(
                    id="INC-perf-001",
                    type=IncidentType.PERFORMANCE_DEGRADED,
                    severity=IncidentSeverity.MEDIUM,
                    title="Performance degradation detected",
                    description=f"P99 latency is {latency}ms (threshold: 5000ms)",
                    affected_component="performance",
                    evidence={"p99_latency_ms": latency},
                )
            )

        # Check error rate
        error_rate = metrics.get("error_rate", 0)
        if error_rate > 0.05:  # >5% errors
            incidents.append(
                Incident(
                    id="INC-err-001",
                    type=IncidentType.UNKNOWN,
                    severity=IncidentSeverity.HIGH,
                    title="High error rate detected",
                    description=f"Error rate is {error_rate * 100}% (threshold: 5%)",
                    affected_component="services",
                    evidence={"error_rate": error_rate},
                )
            )

        return incidents


class IncidentDiagnoser:
    """Diagnose root causes of incidents."""

    async def diagnose(self, incident: Incident) -> Optional[str]:
        """Diagnose root cause of incident.

        Args:
            incident: Incident to diagnose

        Returns:
            Diagnosis description or None if cannot diagnose
        """
        if incident.type == IncidentType.TESTS_FAILING:
            diagnosis = "Recent code changes may have broken tests. Check recent commits."
            incident.diagnosed = True
            incident.diagnosis = diagnosis
            return diagnosis

        if incident.type == IncidentType.PERFORMANCE_DEGRADED:
            latency = incident.evidence.get("p99_latency_ms", 0)
            diagnosis = f"Service is slow (p99={latency}ms). May need optimization or scaling."
            incident.diagnosed = True
            incident.diagnosis = diagnosis
            return diagnosis

        if incident.type == IncidentType.DATABASE_ERROR:
            diagnosis = "Database connection failed. Check database availability and credentials."
            incident.diagnosed = True
            incident.diagnosis = diagnosis
            return diagnosis

        return None


class IncidentRepair:
    """Autonomous repair of incidents."""

    async def repair_test_failure(self, incident: Incident) -> bool:
        """Attempt to repair failing tests.

        Args:
            incident: Test failure incident

        Returns:
            True if repair successful
        """
        # Strategy: Run tests, identify failing test, create PR to fix
        logger.info(f"Attempting to repair test failures in {incident.id}")

        # 1. Collect failure details
        # 2. Identify root cause
        # 3. Implement fix
        # 4. Run tests to verify
        # 5. Create PR if successful

        incident.repair_attempted = True
        incident.repair_result = "Repair attempted but requires validation"
        return False  # Placeholder

    async def repair_performance_issue(self, incident: Incident) -> bool:
        """Attempt to repair performance degradation.

        Args:
            incident: Performance incident

        Returns:
            True if repair successful
        """
        logger.info(f"Attempting to repair performance issue in {incident.id}")

        # Strategy: Identify bottleneck, optimize, test, deploy
        incident.repair_attempted = True
        incident.repair_result = "Performance analysis required"
        return False  # Placeholder

    async def attempt_repair(self, incident: Incident) -> bool:
        """Attempt autonomous repair of incident.

        Args:
            incident: Incident to repair

        Returns:
            True if repair successful
        """
        if not incident.can_repair_autonomously:
            logger.info(
                f"Cannot repair {incident.id} autonomously - severity too high or type unsafe"
            )
            return False

        if incident.type == IncidentType.TESTS_FAILING:
            return await self.repair_test_failure(incident)

        if incident.type == IncidentType.PERFORMANCE_DEGRADED:
            return await self.repair_performance_issue(incident)

        return False


class IncidentManager:
    """Manage complete incident lifecycle."""

    def __init__(self):
        """Initialize incident manager."""
        self.detector = IncidentDetector()
        self.diagnoser = IncidentDiagnoser()
        self.repair = IncidentRepair()
        self.incidents: dict[str, Incident] = {}

    async def handle_incident(self, incident: Incident) -> bool:
        """Handle incident from detection through resolution.

        Args:
            incident: Incident to handle

        Returns:
            True if incident was resolved
        """
        self.incidents[incident.id] = incident

        # Diagnose
        await self.diagnoser.diagnose(incident)
        logger.info(f"Diagnosed {incident.id}: {incident.diagnosis}")

        # Decide on repair
        if not incident.can_repair_autonomously:
            logger.warning(f"{incident.id} requires human intervention")
            return False

        # Attempt repair
        result = await self.repair.attempt_repair(incident)
        incident.resolved = result

        return result


__all__ = [
    "Incident",
    "IncidentDetector",
    "IncidentDiagnoser",
    "IncidentRepair",
    "IncidentManager",
    "IncidentType",
    "IncidentSeverity",
]
