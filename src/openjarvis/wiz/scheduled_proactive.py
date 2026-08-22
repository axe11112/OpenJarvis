"""Scheduled proactive engineering runner.

Periodically runs pattern detection over incidents and converts discovered
patterns into feature requests. Tracks all operations through autonomy metrics
and logs problem detection, deduplication, task creation, and outcomes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from openjarvis.reliability.store import IncidentStore
from openjarvis.reliability.types import Incident
from openjarvis.wiz.autonomy_metrics import (
    AutonomyMetricsStore,
    MetricCategory,
)
from openjarvis.wiz.features.model import FeatureRequest, FeatureState
from openjarvis.wiz.features.store import FeatureStore
from openjarvis.wiz.proactive import (
    PatternDetector,
    ProactiveTask,
    task_to_feature_request,
)

logger = logging.getLogger(__name__)

__all__ = ["ScheduledProactiveRunner", "ProactiveRunResult"]


@dataclass
class ProactiveRunResult:
    """Outcome of a proactive engineering run."""

    timestamp: str
    incidents_analyzed: int
    patterns_detected: int
    patterns_deduped: int
    tasks_created: int
    features_created: int
    errors: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "incidents_analyzed": self.incidents_analyzed,
            "patterns_detected": self.patterns_detected,
            "patterns_deduped": self.patterns_deduped,
            "tasks_created": self.tasks_created,
            "features_created": self.features_created,
            "errors": self.errors,
        }


class ScheduledProactiveRunner:
    """Run pattern detection and convert to feature requests.

    This is the bridge between:
    - Reliability diagnostics (incident store, pattern detector)
    - Wiz feature engineering (feature requests, autonomy metrics)

    Every detected pattern becomes a FeatureRequest in RECEIVED state,
    waiting for operator approval before engineering begins.
    """

    def __init__(
        self,
        incident_store: IncidentStore,
        feature_store: FeatureStore,
        metrics_store: AutonomyMetricsStore,
        *,
        detector: Optional[PatternDetector] = None,
        lookback_hours: int = 6,
    ) -> None:
        self._incident_store = incident_store
        self._feature_store = feature_store
        self._metrics_store = metrics_store
        self._detector = detector or PatternDetector()
        self._lookback = timedelta(hours=lookback_hours)

    def run(self) -> ProactiveRunResult:
        """Run one iteration of proactive detection and feature creation.

        Returns a result object with counts and any errors encountered.
        """
        now = datetime.now(timezone.utc).isoformat()
        result = ProactiveRunResult(
            timestamp=now,
            incidents_analyzed=0,
            patterns_detected=0,
            patterns_deduped=0,
            tasks_created=0,
            features_created=0,
            errors=[],
        )

        try:
            # 1. Load recent incidents
            incidents = self._load_recent_incidents()
            result.incidents_analyzed = len(incidents)
            logger.info(
                "proactive_run: loaded %d incidents for analysis",
                result.incidents_analyzed,
            )

            # 2. Detect patterns
            try:
                tasks = self._detector._deduplicate(
                    self._detect_all_patterns(incidents)
                )
                result.patterns_detected = len(tasks)
                logger.info("proactive_run: detected %d patterns", len(tasks))
            except Exception as exc:
                error_msg = f"pattern_detection_error: {exc}"
                result.errors.append(error_msg)
                logger.exception("proactive_run: pattern detection failed")
                self._metrics_store.record(
                    MetricCategory.PATTERN_DETECTION,
                    "run",
                    autonomous=True,
                    success=False,
                    details={"error": str(exc)},
                )
                return result

            # 3. Convert patterns to features
            features_created = 0
            for task in tasks:
                try:
                    feature = task_to_feature_request(task)
                    # Store the feature
                    self._feature_store.create(feature)
                    features_created += 1
                    logger.info(
                        "proactive_run: created feature %s from pattern %s",
                        feature.id,
                        task.pattern_id,
                    )

                    # Record autonomy metric
                    self._metrics_store.record(
                        MetricCategory.FEATURE_PROPOSAL,
                        f"pattern_to_feature:{task.pattern_type}",
                        autonomous=True,
                        confidence=task.confidence,
                        success=True,
                        details={
                            "pattern_id": task.pattern_id,
                            "pattern_type": task.pattern_type,
                            "feature_id": feature.id,
                            "confidence": task.confidence,
                        },
                    )
                except Exception as exc:
                    error_msg = f"feature_creation_error({task.pattern_id}): {exc}"
                    result.errors.append(error_msg)
                    logger.exception(
                        "proactive_run: failed to create feature from pattern %s",
                        task.pattern_id,
                    )

            result.tasks_created = len(tasks)
            result.features_created = features_created

            # 4. Record overall run metric
            self._metrics_store.record(
                MetricCategory.PATTERN_DETECTION,
                "scheduled_run",
                autonomous=True,
                confidence=1.0,
                success=(len(result.errors) == 0),
                details=result.to_dict(),
            )

            logger.info(
                "proactive_run: completed: %d patterns detected, "
                "%d deduped, %d features created, %d errors",
                result.patterns_detected,
                result.patterns_deduped,
                result.features_created,
                len(result.errors),
            )

        except Exception as exc:
            error_msg = f"run_error: {exc}"
            result.errors.append(error_msg)
            logger.exception("proactive_run: critical error during run")

        return result

    def _load_recent_incidents(self) -> List[Incident]:
        """Load incidents from the last N hours."""
        cutoff = datetime.now(timezone.utc) - self._lookback
        cutoff_str = cutoff.isoformat()

        try:
            # list() returns all incidents; we filter by date
            all_incidents = self._incident_store.list(limit=1000)
            recent = [
                inc
                for inc in all_incidents
                if inc.created_at and inc.created_at >= cutoff_str
            ]
            return recent
        except Exception as exc:
            logger.exception("failed to load incidents: %s", exc)
            return []

    def _detect_all_patterns(self, incidents: List[Incident]) -> List[ProactiveTask]:
        """Run all pattern detection methods over incidents."""
        tasks = []

        # Per-incident detection
        for incident in incidents:
            try:
                tasks.extend(self._detector.detect_in_incident(incident))
            except Exception as exc:
                logger.warning(
                    "pattern detection failed for incident %s: %s",
                    incident.id,
                    exc,
                )

        # Cross-incident detection
        try:
            tasks.extend(self._detector.detect_in_store(incidents))
        except Exception as exc:
            logger.warning("cross-incident pattern detection failed: %s", exc)

        return tasks
