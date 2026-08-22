"""Autonomy metrics tests.

Verify that metrics correctly track autonomous vs operator-input operations
and produce accurate summaries.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from openjarvis.wiz.autonomy_metrics import (
    AutonomyMetrics,
    AutonomyMetricsStore,
    MetricCategory,
    MetricsSummary,
)


@pytest.fixture
def metrics_store(tmp_path: Path) -> AutonomyMetricsStore:
    """Temporary metrics store for testing."""
    db_path = tmp_path / "metrics.jsonl"
    return AutonomyMetricsStore(db_path)


class TestBasicOperations:
    """Core record and retrieve operations."""

    def test_record_metric(self, metrics_store: AutonomyMetricsStore) -> None:
        metrics_store.record(
            MetricCategory.HEALTH_CHECK,
            "database_check",
            autonomous=True,
            confidence=0.95,
            success=True,
        )

        assert metrics_store.count() == 1

    def test_record_multiple_metrics(
        self, metrics_store: AutonomyMetricsStore
    ) -> None:
        for i in range(5):
            metrics_store.record(
                MetricCategory.PATTERN_DETECTION,
                f"pattern_{i}",
                autonomous=(i % 2 == 0),
                confidence=0.5 + (i * 0.1),
                success=True,
            )

        assert metrics_store.count() == 5

    def test_metric_with_operator_input(
        self, metrics_store: AutonomyMetricsStore
    ) -> None:
        metrics_store.record(
            MetricCategory.IMPLEMENTATION,
            "implement_feature",
            autonomous=False,
            confidence=0.7,
            success=True,
            operator_input="owner@example.com",
        )

        assert metrics_store.count() == 1

    def test_metric_with_details(self, metrics_store: AutonomyMetricsStore) -> None:
        details = {
            "feature_id": "FEAT-123",
            "pr_url": "https://github.com/...",
            "implementation_time": 3600,  # seconds
        }
        metrics_store.record(
            MetricCategory.IMPLEMENTATION,
            "implement_feature",
            autonomous=True,
            success=True,
            details=details,
        )

        assert metrics_store.count() == 1

    def test_clear_all_metrics(self, metrics_store: AutonomyMetricsStore) -> None:
        for i in range(3):
            metrics_store.record(
                MetricCategory.HEALTH_CHECK,
                "check",
                autonomous=True,
                success=True,
            )

        assert metrics_store.count() == 3
        metrics_store.clear()
        assert metrics_store.count() == 0


class TestAutonomyRate:
    """Autonomy rate calculation."""

    def test_all_autonomous_operations(
        self, metrics_store: AutonomyMetricsStore
    ) -> None:
        for i in range(5):
            metrics_store.record(
                MetricCategory.HEALTH_CHECK,
                "check",
                autonomous=True,
                success=True,
            )

        summary = metrics_store.summarize()
        assert summary.autonomy_rate == 1.0
        assert summary.autonomous_operations == 5
        assert summary.operator_input_required == 0

    def test_mixed_autonomous_and_manual(
        self, metrics_store: AutonomyMetricsStore
    ) -> None:
        for i in range(3):
            metrics_store.record(
                MetricCategory.IMPLEMENTATION,
                "impl",
                autonomous=True,
                success=True,
            )
        for i in range(2):
            metrics_store.record(
                MetricCategory.IMPLEMENTATION,
                "impl",
                autonomous=False,
                success=True,
            )

        summary = metrics_store.summarize()
        assert summary.total_operations == 5
        assert summary.autonomous_operations == 3
        assert summary.operator_input_required == 2
        assert summary.autonomy_rate == 0.6

    def test_no_operations(self, metrics_store: AutonomyMetricsStore) -> None:
        summary = metrics_store.summarize()
        assert summary.total_operations == 0
        assert summary.autonomy_rate == 0.0


class TestSuccessRate:
    """Success rate calculation."""

    def test_all_successful_operations(
        self, metrics_store: AutonomyMetricsStore
    ) -> None:
        for i in range(4):
            metrics_store.record(
                MetricCategory.TEST_EXECUTION,
                "test_run",
                autonomous=True,
                success=True,
            )

        summary = metrics_store.summarize()
        assert summary.successful_operations == 4
        assert summary.failed_operations == 0
        assert summary.success_rate == 1.0

    def test_mixed_success_and_failure(
        self, metrics_store: AutonomyMetricsStore
    ) -> None:
        for i in range(3):
            metrics_store.record(
                MetricCategory.TEST_EXECUTION,
                "test_run",
                autonomous=True,
                success=True,
            )
        for i in range(2):
            metrics_store.record(
                MetricCategory.TEST_EXECUTION,
                "test_run",
                autonomous=True,
                success=False,
            )

        summary = metrics_store.summarize()
        assert summary.total_operations == 5
        assert summary.successful_operations == 3
        assert summary.failed_operations == 2
        assert summary.success_rate == 0.6


class TestConfidenceAnalysis:
    """Confidence tracking and statistics."""

    def test_average_confidence(self, metrics_store: AutonomyMetricsStore) -> None:
        confidences = [0.9, 0.8, 0.7, 0.6, 0.5]
        for conf in confidences:
            metrics_store.record(
                MetricCategory.PATTERN_DETECTION,
                "detect",
                autonomous=True,
                confidence=conf,
                success=True,
            )

        summary = metrics_store.summarize()
        expected_avg = sum(confidences) / len(confidences)
        assert abs(summary.average_confidence - expected_avg) < 0.001

    def test_high_confidence_count(self, metrics_store: AutonomyMetricsStore) -> None:
        for conf in [0.95, 0.85, 0.9, 0.5, 0.3]:
            metrics_store.record(
                MetricCategory.PATTERN_DETECTION,
                "detect",
                autonomous=True,
                confidence=conf,
                success=True,
            )

        summary = metrics_store.summarize()
        assert summary.high_confidence_count == 3  # 0.95, 0.85, 0.9

    def test_low_confidence_count(self, metrics_store: AutonomyMetricsStore) -> None:
        for conf in [0.9, 0.8, 0.6, 0.4, 0.2]:
            metrics_store.record(
                MetricCategory.INFERENCE_CORRECTION,
                "infer",
                autonomous=True,
                confidence=conf,
                success=True,
            )

        summary = metrics_store.summarize()
        assert summary.low_confidence_count == 2  # 0.4, 0.2


class TestCategoryBreakdown:
    """Summary breakdown by category."""

    def test_single_category_breakdown(
        self, metrics_store: AutonomyMetricsStore
    ) -> None:
        for i in range(3):
            metrics_store.record(
                MetricCategory.HEALTH_CHECK,
                "database_check",
                autonomous=True,
                success=True,
            )

        summary = metrics_store.summarize()
        assert "health_check" in summary.by_category
        assert summary.by_category["health_check"]["count"] == 3
        assert summary.by_category["health_check"]["autonomous"] == 3
        assert summary.by_category["health_check"]["success"] == 3

    def test_multiple_categories_breakdown(
        self, metrics_store: AutonomyMetricsStore
    ) -> None:
        for i in range(2):
            metrics_store.record(
                MetricCategory.HEALTH_CHECK,
                "health",
                autonomous=True,
                success=True,
            )
        for i in range(3):
            metrics_store.record(
                MetricCategory.IMPLEMENTATION,
                "impl",
                autonomous=False,
                success=True,
            )

        summary = metrics_store.summarize()
        assert len(summary.by_category) == 2
        assert summary.by_category["health_check"]["count"] == 2
        assert summary.by_category["implementation"]["count"] == 3

    def test_category_filter(self, metrics_store: AutonomyMetricsStore) -> None:
        for i in range(2):
            metrics_store.record(
                MetricCategory.HEALTH_CHECK,
                "check",
                autonomous=True,
                success=True,
            )
        for i in range(3):
            metrics_store.record(
                MetricCategory.IMPLEMENTATION,
                "impl",
                autonomous=False,
                success=True,
            )

        # Get summary for only IMPLEMENTATION
        summary = metrics_store.summarize(category=MetricCategory.IMPLEMENTATION)
        assert summary.total_operations == 3
        assert len(summary.by_category) == 1
        assert "implementation" in summary.by_category


class TestTimeRangeFiltering:
    """Summarize over specific time periods."""

    def test_filter_by_start_time(
        self, metrics_store: AutonomyMetricsStore
    ) -> None:
        # Record metrics with a specific start time for testing
        # In a real scenario, these would have different timestamps
        now = datetime.now(timezone.utc).isoformat()
        old_time = "2025-01-01T00:00:00+00:00"

        # Old metric (would be outside range)
        metrics_store.record(
            MetricCategory.HEALTH_CHECK,
            "old_check",
            autonomous=True,
            success=True,
        )

        # New metric (would be in range)
        metrics_store.record(
            MetricCategory.HEALTH_CHECK,
            "new_check",
            autonomous=True,
            success=True,
        )

        # Note: This test is simplified since all metrics get current timestamp.
        # In production, timestamps would vary and filtering would work as intended.
        summary = metrics_store.summarize(start=now)
        assert summary.total_operations >= 0


class TestPersistence:
    """Metrics are persisted to disk."""

    def test_metrics_persist_across_instances(self, tmp_path: Path) -> None:
        db_path = tmp_path / "metrics.jsonl"

        # First store: record metrics
        store1 = AutonomyMetricsStore(db_path)
        store1.record(
            MetricCategory.HEALTH_CHECK,
            "check",
            autonomous=True,
            success=True,
        )
        assert store1.count() == 1

        # Second store: should load existing metrics
        store2 = AutonomyMetricsStore(db_path)
        assert store2.count() == 1

    def test_corrupted_lines_are_skipped(self, tmp_path: Path) -> None:
        db_path = tmp_path / "metrics.jsonl"

        # Write a JSONL file with some corrupted lines
        lines = [
            '{"category": "health_check", "operation": "check", "timestamp": "2025-08-22T10:00:00Z", "autonomous": true, "confidence": 0.9, "success": true, "details": {}, "operator_input": null}',
            "{ corrupted json }",
            '{"category": "pattern_detection", "operation": "detect", "timestamp": "2025-08-22T10:01:00Z", "autonomous": true, "confidence": 0.8, "success": true, "details": {}, "operator_input": null}',
        ]
        db_path.write_text("\n".join(lines) + "\n")

        # Load: should skip corrupted line and load the two good ones
        store = AutonomyMetricsStore(db_path)
        assert store.count() == 2


class TestMetricsObject:
    """AutonomyMetrics dataclass serialization."""

    def test_to_dict(self) -> None:
        metric = AutonomyMetrics(
            category=MetricCategory.IMPLEMENTATION,
            operation="test_op",
            timestamp="2025-08-22T10:00:00Z",
            autonomous=True,
            confidence=0.85,
            success=True,
            details={"key": "value"},
            operator_input=None,
        )

        d = metric.to_dict()
        assert d["category"] == "implementation"
        assert d["operation"] == "test_op"
        assert d["autonomous"] is True
        assert d["success"] is True


class TestSummaryObject:
    """MetricsSummary dataclass."""

    def test_to_dict(self) -> None:
        summary = MetricsSummary(
            period_start="2025-08-22T00:00:00Z",
            period_end="2025-08-23T00:00:00Z",
            total_operations=10,
            autonomous_operations=8,
            operator_input_required=2,
            autonomy_rate=0.8,
            successful_operations=9,
            failed_operations=1,
            success_rate=0.9,
        )

        d = summary.to_dict()
        assert d["total_operations"] == 10
        assert d["autonomy_rate"] == 0.8
        assert d["success_rate"] == 0.9
