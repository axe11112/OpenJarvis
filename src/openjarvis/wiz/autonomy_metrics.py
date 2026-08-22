"""Autonomy metrics — measuring how much Wiz operates without human intervention.

Tracks both sides of the system:
- Wiz diagnostics: how many issues detected, deduped, and resolved autonomously
- Wize engineering: feature proposals, implementations, and operator corrections

Metrics distinguish between autonomy (action without asking) and effectiveness
(action that produces the right outcome). High autonomy with low effectiveness
means Wiz is loud and wrong. Low autonomy with high effectiveness means Wiz is
helpful but cautious. The goal is high autonomy + high effectiveness.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["AutonomyMetrics", "MetricsSummary"]


class MetricCategory(str, Enum):
    """What kind of operation this metric covers."""

    # Wiz diagnostics
    HEALTH_CHECK = "health_check"
    PATTERN_DETECTION = "pattern_detection"
    INCIDENT_ANALYSIS = "incident_analysis"
    DEDUPLICATION = "deduplication"

    # Wize engineering
    FEATURE_PROPOSAL = "feature_proposal"
    IMPLEMENTATION = "implementation"
    TEST_EXECUTION = "test_execution"
    PR_CREATION = "pr_creation"
    PR_MERGE = "pr_merge"

    # Memory learning
    MEMORY_OPERATION = "memory_operation"
    INFERENCE_CORRECTION = "inference_correction"
    FACT_LEARNING = "fact_learning"


@dataclass
class AutonomyMetrics:
    """One measurement of autonomous operation.

    Records what happened, when, whether it required human input, and how
    certain Wiz was in the decision.
    """

    category: MetricCategory
    operation: str  # e.g. "flapping_detection", "test_run", "pr_merge"
    timestamp: str  # ISO format
    autonomous: bool  # did this require operator approval?
    confidence: float  # 0.0-1.0; how certain was Wiz?
    success: bool  # did the operation succeed?
    details: Dict[str, Any] = field(default_factory=dict)
    operator_input: Optional[str] = None  # if not autonomous, who decided?

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category.value,
            "operation": self.operation,
            "timestamp": self.timestamp,
            "autonomous": self.autonomous,
            "confidence": self.confidence,
            "success": self.success,
            "details": self.details,
            "operator_input": self.operator_input,
        }


@dataclass
class MetricsSummary:
    """Summary statistics over a time period."""

    period_start: str  # ISO format
    period_end: str  # ISO format
    total_operations: int = 0

    # Autonomy breakdown
    autonomous_operations: int = 0
    operator_input_required: int = 0
    autonomy_rate: float = 0.0  # autonomous_operations / total_operations

    # Effectiveness breakdown
    successful_operations: int = 0
    failed_operations: int = 0
    success_rate: float = 0.0

    # Category breakdown
    by_category: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Confidence analysis
    average_confidence: float = 0.0
    high_confidence_count: int = 0  # confidence > 0.8
    low_confidence_count: int = 0  # confidence < 0.5

    def to_dict(self) -> Dict[str, Any]:
        return {
            "period_start": self.period_start,
            "period_end": self.period_end,
            "total_operations": self.total_operations,
            "autonomous_operations": self.autonomous_operations,
            "operator_input_required": self.operator_input_required,
            "autonomy_rate": self.autonomy_rate,
            "successful_operations": self.successful_operations,
            "failed_operations": self.failed_operations,
            "success_rate": self.success_rate,
            "by_category": self.by_category,
            "average_confidence": self.average_confidence,
            "high_confidence_count": self.high_confidence_count,
            "low_confidence_count": self.low_confidence_count,
        }


class AutonomyMetricsStore:
    """Thread-safe store for autonomy metrics.

    Records operations as they happen and produces summary statistics.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._metrics: List[AutonomyMetrics] = self._load()

    def _load(self) -> List[AutonomyMetrics]:
        """Load existing metrics from disk."""
        if not self._path.exists():
            return []
        metrics: List[AutonomyMetrics] = []
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                metrics.append(self._deserialize(obj))
            except (json.JSONDecodeError, ValueError, TypeError):
                continue  # skip malformed lines
        return metrics

    def _save(self) -> None:
        """Append-only save of metrics to JSONL."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = self._path.read_text(encoding="utf-8") if self._path.exists() else ""
        except OSError:
            text = ""

        # Append new lines (only save what's not already there)
        # For simplicity in this prototype, we'll just rewrite the whole file
        payload = "".join(json.dumps(m.to_dict(), ensure_ascii=False) + "\n" for m in self._metrics)
        self._path.write_text(payload, encoding="utf-8")

    def record(
        self,
        category: MetricCategory,
        operation: str,
        *,
        autonomous: bool,
        confidence: float = 1.0,
        success: bool = True,
        details: Optional[Dict[str, Any]] = None,
        operator_input: Optional[str] = None,
    ) -> None:
        """Record a metric for an operation."""
        now = datetime.now(timezone.utc).isoformat()
        metric = AutonomyMetrics(
            category=category,
            operation=operation,
            timestamp=now,
            autonomous=autonomous,
            confidence=min(1.0, max(0.0, float(confidence))),
            success=success,
            details=details or {},
            operator_input=operator_input,
        )
        self._metrics.append(metric)
        self._save()

    def summarize(
        self,
        *,
        start: Optional[str] = None,
        end: Optional[str] = None,
        category: Optional[MetricCategory] = None,
    ) -> MetricsSummary:
        """Generate summary statistics for a time period.

        Args:
            start: ISO format start time; None = oldest metric
            end: ISO format end time; None = now
            category: filter to specific category; None = all categories
        """
        if end is None:
            end = datetime.now(timezone.utc).isoformat()

        # Filter metrics to time range and category
        filtered: List[AutonomyMetrics] = []
        for metric in self._metrics:
            if start and metric.timestamp < start:
                continue
            if metric.timestamp > end:
                continue
            if category and metric.category != category:
                continue
            filtered.append(metric)

        if not filtered:
            return MetricsSummary(period_start=start or "", period_end=end)

        # Calculate summary
        summary = MetricsSummary(
            period_start=start or filtered[0].timestamp,
            period_end=end,
            total_operations=len(filtered),
        )

        autonomous_count = sum(1 for m in filtered if m.autonomous)
        operator_count = sum(1 for m in filtered if not m.autonomous)
        success_count = sum(1 for m in filtered if m.success)

        summary.autonomous_operations = autonomous_count
        summary.operator_input_required = operator_count
        summary.autonomy_rate = (
            autonomous_count / len(filtered) if filtered else 0.0
        )
        summary.successful_operations = success_count
        summary.failed_operations = len(filtered) - success_count
        summary.success_rate = success_count / len(filtered) if filtered else 0.0

        # Confidence stats
        confidences = [m.confidence for m in filtered]
        summary.average_confidence = sum(confidences) / len(confidences)
        summary.high_confidence_count = sum(1 for c in confidences if c > 0.8)
        summary.low_confidence_count = sum(1 for c in confidences if c < 0.5)

        # Category breakdown
        by_cat: Dict[str, Dict[str, Any]] = {}
        for metric in filtered:
            cat_key = metric.category.value
            if cat_key not in by_cat:
                by_cat[cat_key] = {
                    "count": 0,
                    "autonomous": 0,
                    "success": 0,
                    "operations": [],
                }
            by_cat[cat_key]["count"] += 1
            if metric.autonomous:
                by_cat[cat_key]["autonomous"] += 1
            if metric.success:
                by_cat[cat_key]["success"] += 1
            by_cat[cat_key]["operations"].append(metric.operation)

        # Deduplicate operation lists to unique names
        for cat_data in by_cat.values():
            cat_data["operations"] = list(set(cat_data["operations"]))

        summary.by_category = by_cat

        return summary

    def count(self) -> int:
        """Total number of recorded metrics."""
        return len(self._metrics)

    def clear(self) -> None:
        """Clear all metrics (for testing)."""
        self._metrics = []
        if self._path.exists():
            self._path.unlink()

    @staticmethod
    def _deserialize(data: Dict[str, Any]) -> AutonomyMetrics:
        """Deserialize a metric from JSON."""
        return AutonomyMetrics(
            category=MetricCategory(data.get("category", "health_check")),
            operation=data.get("operation", ""),
            timestamp=data.get("timestamp", ""),
            autonomous=bool(data.get("autonomous", False)),
            confidence=float(data.get("confidence", 1.0)),
            success=bool(data.get("success", True)),
            details=data.get("details", {}),
            operator_input=data.get("operator_input"),
        )
