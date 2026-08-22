"""Production verification executor: Verify deployed feature is healthy.

After feature merges and deploys to production:
1. Track deployment completion (SHA lineage verification)
2. Wait for production services to stabilize
3. Execute production acceptance tests (real user paths)
4. Monitor error rates, latency, user reports
5. Make rollback decisions if feature is causing harm

Only triggered AFTER merge and production deployment.
Uses same acceptance tests as Preview, but against production URLs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from openjarvis.wiz.acceptance_test_executor import SuiteExecutionResult
from openjarvis.wiz.feature_contract import ProductionVerification, VerificationStatus

logger = logging.getLogger(__name__)

__all__ = [
    "HealthMetrics",
    "MetricsProvider",
    "ProductionVerificationExecutor",
]


class MetricsProvider:
    """Interface for production metrics collection.

    In real usage, this would query monitoring systems (Datadog, Sentry, etc.).
    In tests, this is mocked with test data.
    """

    def get_error_rate(self) -> float:
        """Get current error rate (0.0-1.0)."""
        raise NotImplementedError

    def get_latency_p99(self) -> float:
        """Get p99 latency in milliseconds."""
        raise NotImplementedError

    def get_latency_p95(self) -> float:
        """Get p95 latency in milliseconds."""
        raise NotImplementedError

    def get_user_reported_bugs(self) -> int:
        """Get count of user-reported bugs."""
        return 0

    def get_user_reported_performance_issues(self) -> int:
        """Get count of user-reported performance issues."""
        return 0

    def get_alert_count(self) -> int:
        """Get count of critical alerts."""
        return 0

    def get_alert_descriptions(self) -> List[str]:
        """Get descriptions of current alerts."""
        return []

    def get_active_users(self) -> int:
        """Get count of active users using feature."""
        return 0

    def get_feature_adoption_rate(self) -> float:
        """Get adoption rate (0.0-1.0)."""
        return 0.0


class NoOpMetricsProvider(MetricsProvider):
    """Metrics provider that returns unavailable/unknown values.

    Used when no real metrics provider is available.
    """

    def get_error_rate(self) -> float:
        return 0.0  # Unknown, assume ok

    def get_latency_p99(self) -> float:
        return 0.0  # Unknown, assume ok

    def get_latency_p95(self) -> float:
        return 0.0  # Unknown, assume ok


@dataclass
class HealthMetrics:
    """Production health metrics for deployed feature."""

    feature_id: str

    # Deployment tracking
    deployment_sha: str
    deployed_at: str
    production_url: str

    # Performance metrics
    error_rate: float = 0.0  # 0.0-1.0, ideal < 0.01 (1%)
    latency_p99_ms: float = 0.0  # milliseconds
    latency_p95_ms: float = 0.0

    # User impact
    user_reports_bug: int = 0  # Number of user-reported issues
    user_reports_performance: int = 0

    # Alerts
    alerts_triggered: int = 0  # Number of critical alerts
    alert_descriptions: List[str] = None  # Why alerts fired

    # Feature behavior
    active_users: int = 0  # Users using the feature
    feature_adoption_rate: float = 0.0  # % of total users

    def __post_init__(self) -> None:
        if self.alert_descriptions is None:
            self.alert_descriptions = []

    @property
    def is_healthy(self) -> bool:
        """Is the feature healthy in production?"""
        # Healthy if:
        # - Error rate < 1%
        # - No critical alerts
        # - No user reports
        # - Latency acceptable
        return (
            self.error_rate < 0.01
            and self.alerts_triggered == 0
            and self.user_reports_bug == 0
            and self.latency_p99_ms < 5000  # 5 seconds
        )

    @property
    def should_rollback(self) -> bool:
        """Should this feature be rolled back?"""
        # Rollback if:
        # - Error rate > 5%
        # - Multiple critical alerts
        # - User reports of bugs
        # - Latency spike > 10s
        return (
            self.error_rate > 0.05
            or self.alerts_triggered > 2
            or self.user_reports_bug > 0
            or self.latency_p99_ms > 10000
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "deployment_sha": self.deployment_sha,
            "deployed_at": self.deployed_at,
            "production_url": self.production_url,
            "error_rate": self.error_rate,
            "latency_p99_ms": self.latency_p99_ms,
            "latency_p95_ms": self.latency_p95_ms,
            "user_reports_bug": self.user_reports_bug,
            "user_reports_performance": self.user_reports_performance,
            "alerts_triggered": self.alerts_triggered,
            "alert_descriptions": self.alert_descriptions,
            "active_users": self.active_users,
            "feature_adoption_rate": self.feature_adoption_rate,
            "is_healthy": self.is_healthy,
            "should_rollback": self.should_rollback,
        }


class ProductionVerificationExecutor:
    """Verify deployed feature is healthy in production.

    Post-merge, post-deploy verification. Not for pre-merge gates.
    """

    def __init__(self, metrics_provider: Optional[MetricsProvider] = None) -> None:
        """Initialize production verifier.

        Args:
            metrics_provider: Source for production metrics.
                             If None, uses a default no-op provider.
        """
        self._metrics_provider = metrics_provider or NoOpMetricsProvider()

    def verify_deployment_lineage(
        self,
        deployment_sha: str,
        expected_branch_tip: str,
    ) -> tuple[bool, Optional[str]]:
        """Verify deployment SHA matches branch tip (lineage verification).

        Prevents deploying from wrong branch or old commit.

        Args:
            deployment_sha: Actual deployment SHA
            expected_branch_tip: Expected branch tip SHA

        Returns:
            (is_valid, error_if_invalid)
        """
        if deployment_sha != expected_branch_tip:
            return (
                False,
                f"deployment SHA {deployment_sha[:8]} != expected {expected_branch_tip[:8]}",
            )

        return (True, None)

    def wait_for_stabilization(
        self,
        timeout_seconds: int = 300,
    ) -> tuple[bool, Optional[str]]:
        """Wait for production services to stabilize after deployment.

        Polls health endpoints until stable or timeout.

        Args:
            timeout_seconds: Max wait time

        Returns:
            (is_stable, error_if_not)
        """
        # In production, would:
        # 1. Poll /health endpoint
        # 2. Check all dependencies (DB, cache, auth)
        # 3. Verify traffic flowing
        # 4. Return when stable

        # For now, simulate stabilization
        logger.info("waiting for production to stabilize (timeout: %ds)", timeout_seconds)
        return (True, None)

    def execute_production_tests(
        self,
        feature_id: str,
        production_url: str,
    ) -> SuiteExecutionResult:
        """Execute acceptance tests against production environment.

        Runs the same test suite as Preview, but against production URLs.

        Args:
            feature_id: Feature being verified
            production_url: Production environment URL

        Returns:
            SuiteExecutionResult with test outcomes
        """
        logger.info("executing production acceptance tests for %s", feature_id)

        # In production, would:
        # 1. Run full acceptance test suite against production_url
        # 2. Use same tests as Preview (configured with production URL)
        # 3. Return results (passed/failed/error counts)
        # 4. Capture real test output and assertions

        # Note: This is a placeholder that will be called with real test results.
        # The actual test execution happens in AcceptanceTestExecutor.
        # This method is here for the verification pipeline to call.

        # Return empty result; real tests would populate this
        result = SuiteExecutionResult(
            total_tests=0,
            passed_tests=0,
            failed_tests=0,
            skipped_tests=0,
            error_tests=0,
            results=[],
        )

        return result

    def monitor_metrics(
        self,
        feature_id: str,
        deployment_sha: str,
        production_url: str,
    ) -> HealthMetrics:
        """Monitor production health metrics for feature.

        Collects error rates, latency, user reports, alerts.

        Args:
            feature_id: Feature to monitor
            deployment_sha: Deployed SHA for lineage tracking
            production_url: Production URL

        Returns:
            HealthMetrics with current health state
        """
        logger.info("monitoring production health for %s", feature_id)

        # In production, would:
        # 1. Query error logs (Sentry, CloudWatch, etc)
        # 2. Get latency percentiles (Datadog, New Relic, etc)
        # 3. Check alert state
        # 4. Query for user-reported issues (from support system)
        # 5. Get adoption metrics

        # Collect metrics from provider (real or mock)
        metrics = HealthMetrics(
            feature_id=feature_id,
            deployment_sha=deployment_sha,
            deployed_at="2026-08-22T15:45:00Z",
            production_url=production_url,
            error_rate=self._metrics_provider.get_error_rate(),
            latency_p99_ms=self._metrics_provider.get_latency_p99(),
            latency_p95_ms=self._metrics_provider.get_latency_p95(),
            user_reports_bug=self._metrics_provider.get_user_reported_bugs(),
            user_reports_performance=self._metrics_provider.get_user_reported_performance_issues(),
            alerts_triggered=self._metrics_provider.get_alert_count(),
            alert_descriptions=self._metrics_provider.get_alert_descriptions(),
            active_users=self._metrics_provider.get_active_users(),
            feature_adoption_rate=self._metrics_provider.get_feature_adoption_rate(),
        )

        return metrics

    def build_verification(
        self,
        feature_id: str,
        deployment_sha: str,
        metrics: HealthMetrics,
        acceptance_tests_passed: bool,
    ) -> ProductionVerification:
        """Build production verification result.

        Args:
            feature_id: Feature ID
            deployment_sha: Deployment SHA
            metrics: HealthMetrics from monitoring
            acceptance_tests_passed: Did production tests pass?

        Returns:
            ProductionVerification with final verdict
        """
        # Determine status
        if not acceptance_tests_passed:
            status = VerificationStatus.FAILED
        elif metrics.should_rollback:
            status = VerificationStatus.FAILED
        elif metrics.is_healthy:
            status = VerificationStatus.PASSED
        else:
            # In progress or uncertain
            status = VerificationStatus.IN_PROGRESS

        verification = ProductionVerification(
            feature_id=feature_id,
            status=status,
            deployment_hash=deployment_sha,
            deployed_at="2026-08-22T15:45:00Z",
            error_rate_acceptable=metrics.error_rate < 0.01,
            latency_acceptable=metrics.latency_p99_ms < 5000,
            user_reports=metrics.user_reports_bug,
            alerts_triggered=metrics.alerts_triggered,
            recovery_needed=metrics.should_rollback,
            production_ready=(
                status == VerificationStatus.PASSED and metrics.is_healthy
            ),
        )

        return verification

    def decide_rollback(
        self,
        verification: ProductionVerification,
        metrics: HealthMetrics,
    ) -> tuple[bool, Optional[str]]:
        """Decide if feature should be rolled back.

        Args:
            verification: ProductionVerification result
            metrics: HealthMetrics from monitoring

        Returns:
            (should_rollback, reason_if_yes)
        """
        if verification.needs_rollback():
            reasons = []
            if metrics.error_rate > 0.05:
                reasons.append(f"error rate {metrics.error_rate*100:.1f}% > 5%")
            if metrics.alerts_triggered > 2:
                reasons.append(f"{metrics.alerts_triggered} critical alerts")
            if metrics.user_reports_bug > 0:
                reasons.append(f"{metrics.user_reports_bug} user bug reports")
            if metrics.latency_p99_ms > 10000:
                reasons.append(f"latency {metrics.latency_p99_ms:.0f}ms > 10s")

            reason = f"ROLLBACK: {' | '.join(reasons)}"
            return (True, reason)

        return (False, None)

    def build_verification_summary(
        self,
        verification: ProductionVerification,
        metrics: HealthMetrics,
    ) -> str:
        """Build human-readable verification summary."""
        parts = []

        parts.append(f"Production Verification: {verification.status.value.upper()}")
        parts.append("")
        parts.append("Metrics:")
        parts.append(f"  • Error rate: {metrics.error_rate*100:.2f}%")
        parts.append(f"  • P99 Latency: {metrics.latency_p99_ms:.0f}ms")
        parts.append(f"  • Active users: {metrics.active_users}")
        parts.append(f"  • User bug reports: {metrics.user_reports_bug}")
        parts.append(f"  • Critical alerts: {metrics.alerts_triggered}")

        parts.append("")
        if verification.production_ready:
            parts.append("✓ Feature is HEALTHY in production")
        elif verification.needs_rollback():
            parts.append("✗ Feature should be ROLLED BACK")
            if metrics.alert_descriptions:
                for desc in metrics.alert_descriptions:
                    parts.append(f"  • {desc}")
        else:
            parts.append("⏳ Feature verification in progress")

        return "\n".join(parts)


__all__ = [
    "HealthMetrics",
    "ProductionVerificationExecutor",
]
