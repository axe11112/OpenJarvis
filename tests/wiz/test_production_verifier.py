"""Tests for production verification executor."""

from __future__ import annotations

import pytest

from openjarvis.wiz.acceptance_test_executor import SuiteExecutionResult
from openjarvis.wiz.production_verifier import (
    HealthMetrics,
    ProductionVerificationExecutor,
)


class TestHealthMetrics:
    """HealthMetrics dataclass."""

    def test_create_healthy_metrics(self) -> None:
        metrics = HealthMetrics(
            feature_id="WIZE-PILOT-001",
            deployment_sha="abc123def456",
            deployed_at="2026-08-22T15:45:00Z",
            production_url="https://production.example.com",
            error_rate=0.001,
            latency_p99_ms=300.0,
            latency_p95_ms=150.0,
            alerts_triggered=0,
            user_reports_bug=0,
            active_users=5000,
            feature_adoption_rate=0.75,
        )
        assert metrics.is_healthy is True
        assert metrics.should_rollback is False

    def test_create_unhealthy_metrics_high_error_rate(self) -> None:
        metrics = HealthMetrics(
            feature_id="WIZE-001",
            deployment_sha="xyz789",
            deployed_at="2026-08-22T16:00:00Z",
            production_url="https://production.example.com",
            error_rate=0.10,  # 10% error rate
            latency_p99_ms=500.0,
            alerts_triggered=0,
            user_reports_bug=0,
        )
        assert metrics.is_healthy is False
        assert metrics.should_rollback is True

    def test_create_unhealthy_metrics_alerts(self) -> None:
        metrics = HealthMetrics(
            feature_id="WIZE-002",
            deployment_sha="abc123",
            deployed_at="2026-08-22T16:00:00Z",
            production_url="https://production.example.com",
            error_rate=0.005,
            latency_p99_ms=500.0,
            alerts_triggered=3,  # Multiple alerts
            user_reports_bug=0,
        )
        assert metrics.should_rollback is True

    def test_create_unhealthy_metrics_user_reports(self) -> None:
        metrics = HealthMetrics(
            feature_id="WIZE-003",
            deployment_sha="def456",
            deployed_at="2026-08-22T16:00:00Z",
            production_url="https://production.example.com",
            error_rate=0.001,
            latency_p99_ms=500.0,
            alerts_triggered=0,
            user_reports_bug=5,  # User reports
        )
        assert metrics.should_rollback is True

    def test_create_unhealthy_metrics_high_latency(self) -> None:
        metrics = HealthMetrics(
            feature_id="WIZE-004",
            deployment_sha="ghi789",
            deployed_at="2026-08-22T16:00:00Z",
            production_url="https://production.example.com",
            error_rate=0.001,
            latency_p99_ms=15000.0,  # 15 seconds
            alerts_triggered=0,
            user_reports_bug=0,
        )
        assert metrics.should_rollback is True

    def test_health_metrics_to_dict(self) -> None:
        metrics = HealthMetrics(
            feature_id="WIZE-001",
            deployment_sha="abc123",
            deployed_at="2026-08-22T15:45:00Z",
            production_url="https://production.example.com",
            error_rate=0.001,
            latency_p99_ms=300.0,
        )
        d = metrics.to_dict()
        assert d["feature_id"] == "WIZE-001"
        assert d["error_rate"] == 0.001
        assert d["is_healthy"] is True


class TestProductionVerificationExecutor:
    """Production verification executor functionality."""

    def test_initialization(self) -> None:
        executor = ProductionVerificationExecutor()
        assert executor is not None

    def test_verify_deployment_lineage_match(self) -> None:
        executor = ProductionVerificationExecutor()

        is_valid, reason = executor.verify_deployment_lineage(
            deployment_sha="abc123def456",
            expected_branch_tip="abc123def456",
        )

        assert is_valid is True
        assert reason is None

    def test_verify_deployment_lineage_mismatch(self) -> None:
        executor = ProductionVerificationExecutor()

        is_valid, reason = executor.verify_deployment_lineage(
            deployment_sha="old_sha_123",
            expected_branch_tip="new_sha_456",
        )

        assert is_valid is False
        assert "mismatch" in reason.lower()

    def test_wait_for_stabilization(self) -> None:
        executor = ProductionVerificationExecutor()

        is_stable, error = executor.wait_for_stabilization(timeout_seconds=60)

        # Simulation returns stable
        assert is_stable is True
        assert error is None

    def test_execute_production_tests(self) -> None:
        executor = ProductionVerificationExecutor()

        result = executor.execute_production_tests(
            feature_id="WIZE-PILOT-001",
            production_url="https://production.example.com",
        )

        assert isinstance(result, SuiteExecutionResult)
        assert result.total_tests == 3
        assert result.passed_tests == 3
        assert result.success_rate == 1.0

    def test_monitor_metrics_healthy(self) -> None:
        executor = ProductionVerificationExecutor()

        metrics = executor.monitor_metrics(
            feature_id="WIZE-PILOT-001",
            deployment_sha="abc123",
            production_url="https://production.example.com",
        )

        assert metrics.is_healthy is True
        assert metrics.should_rollback is False
        assert metrics.error_rate < 0.01

    def test_build_verification_passed(self) -> None:
        executor = ProductionVerificationExecutor()

        metrics = HealthMetrics(
            feature_id="WIZE-PILOT-001",
            deployment_sha="abc123",
            deployed_at="2026-08-22T15:45:00Z",
            production_url="https://production.example.com",
            error_rate=0.001,
            latency_p99_ms=300.0,
            alerts_triggered=0,
            user_reports_bug=0,
        )

        verification = executor.build_verification(
            feature_id="WIZE-PILOT-001",
            deployment_sha="abc123",
            metrics=metrics,
            acceptance_tests_passed=True,
        )

        assert verification.status.value == "passed"
        assert verification.production_ready is True

    def test_build_verification_failed_tests(self) -> None:
        executor = ProductionVerificationExecutor()

        metrics = HealthMetrics(
            feature_id="WIZE-001",
            deployment_sha="abc123",
            deployed_at="2026-08-22T15:45:00Z",
            production_url="https://production.example.com",
            error_rate=0.001,
            latency_p99_ms=300.0,
            alerts_triggered=0,
            user_reports_bug=0,
        )

        verification = executor.build_verification(
            feature_id="WIZE-001",
            deployment_sha="abc123",
            metrics=metrics,
            acceptance_tests_passed=False,
        )

        assert verification.status.value == "failed"
        assert verification.production_ready is False

    def test_build_verification_failed_metrics(self) -> None:
        executor = ProductionVerificationExecutor()

        metrics = HealthMetrics(
            feature_id="WIZE-001",
            deployment_sha="abc123",
            deployed_at="2026-08-22T15:45:00Z",
            production_url="https://production.example.com",
            error_rate=0.10,  # High error rate
            latency_p99_ms=300.0,
            alerts_triggered=0,
            user_reports_bug=0,
        )

        verification = executor.build_verification(
            feature_id="WIZE-001",
            deployment_sha="abc123",
            metrics=metrics,
            acceptance_tests_passed=True,
        )

        assert verification.status.value == "failed"
        assert verification.production_ready is False
        assert verification.needs_rollback() is True

    def test_decide_rollback_no_rollback(self) -> None:
        executor = ProductionVerificationExecutor()

        metrics = HealthMetrics(
            feature_id="WIZE-PILOT-001",
            deployment_sha="abc123",
            deployed_at="2026-08-22T15:45:00Z",
            production_url="https://production.example.com",
            error_rate=0.001,
            latency_p99_ms=300.0,
            alerts_triggered=0,
            user_reports_bug=0,
        )

        verification = executor.build_verification(
            feature_id="WIZE-PILOT-001",
            deployment_sha="abc123",
            metrics=metrics,
            acceptance_tests_passed=True,
        )

        should_rollback, reason = executor.decide_rollback(verification, metrics)

        assert should_rollback is False
        assert reason is None

    def test_decide_rollback_high_error_rate(self) -> None:
        executor = ProductionVerificationExecutor()

        metrics = HealthMetrics(
            feature_id="WIZE-001",
            deployment_sha="abc123",
            deployed_at="2026-08-22T15:45:00Z",
            production_url="https://production.example.com",
            error_rate=0.10,  # 10% error rate
            latency_p99_ms=300.0,
            alerts_triggered=0,
            user_reports_bug=0,
        )

        verification = executor.build_verification(
            feature_id="WIZE-001",
            deployment_sha="abc123",
            metrics=metrics,
            acceptance_tests_passed=True,
        )

        should_rollback, reason = executor.decide_rollback(verification, metrics)

        assert should_rollback is True
        assert "rollback" in reason.lower()
        assert "error rate" in reason.lower()

    def test_decide_rollback_user_reports(self) -> None:
        executor = ProductionVerificationExecutor()

        metrics = HealthMetrics(
            feature_id="WIZE-001",
            deployment_sha="abc123",
            deployed_at="2026-08-22T15:45:00Z",
            production_url="https://production.example.com",
            error_rate=0.001,
            latency_p99_ms=300.0,
            alerts_triggered=0,
            user_reports_bug=3,  # User reports
        )

        verification = executor.build_verification(
            feature_id="WIZE-001",
            deployment_sha="abc123",
            metrics=metrics,
            acceptance_tests_passed=True,
        )

        should_rollback, reason = executor.decide_rollback(verification, metrics)

        assert should_rollback is True
        assert "user" in reason.lower()

    def test_build_verification_summary_healthy(self) -> None:
        executor = ProductionVerificationExecutor()

        metrics = HealthMetrics(
            feature_id="WIZE-PILOT-001",
            deployment_sha="abc123",
            deployed_at="2026-08-22T15:45:00Z",
            production_url="https://production.example.com",
            error_rate=0.001,
            latency_p99_ms=300.0,
            alerts_triggered=0,
            user_reports_bug=0,
            active_users=5000,
        )

        verification = executor.build_verification(
            feature_id="WIZE-PILOT-001",
            deployment_sha="abc123",
            metrics=metrics,
            acceptance_tests_passed=True,
        )

        summary = executor.build_verification_summary(verification, metrics)

        assert "passed" in summary.lower()
        assert "healthy" in summary.lower()

    def test_build_verification_summary_rollback(self) -> None:
        executor = ProductionVerificationExecutor()

        metrics = HealthMetrics(
            feature_id="WIZE-001",
            deployment_sha="abc123",
            deployed_at="2026-08-22T15:45:00Z",
            production_url="https://production.example.com",
            error_rate=0.10,
            latency_p99_ms=300.0,
            alerts_triggered=3,
            user_reports_bug=2,
            alert_descriptions=["Database connection pool exhausted"],
        )

        verification = executor.build_verification(
            feature_id="WIZE-001",
            deployment_sha="abc123",
            metrics=metrics,
            acceptance_tests_passed=True,
        )

        summary = executor.build_verification_summary(verification, metrics)

        assert "rollback" in summary.lower()


class TestProductionVerificationFlow:
    """End-to-end production verification flow."""

    def test_complete_successful_deployment(self) -> None:
        """Feature deploys, tests pass, metrics healthy, no rollback."""
        executor = ProductionVerificationExecutor()

        # Step 1: Verify lineage
        is_valid, reason = executor.verify_deployment_lineage(
            deployment_sha="abc123def456",
            expected_branch_tip="abc123def456",
        )
        assert is_valid is True

        # Step 2: Wait for stabilization
        is_stable, error = executor.wait_for_stabilization(timeout_seconds=300)
        assert is_stable is True

        # Step 3: Run production tests
        test_result = executor.execute_production_tests(
            feature_id="WIZE-PILOT-001",
            production_url="https://production.example.com",
        )
        assert test_result.success_rate == 1.0

        # Step 4: Monitor metrics
        metrics = executor.monitor_metrics(
            feature_id="WIZE-PILOT-001",
            deployment_sha="abc123def456",
            production_url="https://production.example.com",
        )
        assert metrics.is_healthy is True

        # Step 5: Build verification
        verification = executor.build_verification(
            feature_id="WIZE-PILOT-001",
            deployment_sha="abc123def456",
            metrics=metrics,
            acceptance_tests_passed=True,
        )

        # Step 6: Decide rollback
        should_rollback, reason = executor.decide_rollback(verification, metrics)

        assert verification.production_ready is True
        assert should_rollback is False

    def test_complete_failed_deployment_high_error_rate(self) -> None:
        """Feature deploys but has high error rate, triggers rollback."""
        executor = ProductionVerificationExecutor()

        # Lineage valid
        is_valid, _ = executor.verify_deployment_lineage(
            deployment_sha="abc123",
            expected_branch_tip="abc123",
        )
        assert is_valid is True

        # Tests pass in production
        test_result = executor.execute_production_tests(
            feature_id="WIZE-001",
            production_url="https://production.example.com",
        )

        # But metrics show high error rate
        metrics = HealthMetrics(
            feature_id="WIZE-001",
            deployment_sha="abc123",
            deployed_at="2026-08-22T15:45:00Z",
            production_url="https://production.example.com",
            error_rate=0.15,  # 15% error rate
            latency_p99_ms=300.0,
            alerts_triggered=5,
            user_reports_bug=10,
        )

        # Build verification
        verification = executor.build_verification(
            feature_id="WIZE-001",
            deployment_sha="abc123",
            metrics=metrics,
            acceptance_tests_passed=True,
        )

        # Should rollback
        should_rollback, reason = executor.decide_rollback(verification, metrics)

        assert should_rollback is True
        assert verification.needs_rollback() is True
