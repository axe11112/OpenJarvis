"""Proactive discovery of problems as engineering tasks.

Reliability diagnostics (flapping, correlation, pattern fingerprints) detect
problems that are unlikely to be fixed by human intervention alone, and Wiz's
feature pipeline is what a human asks for. This module bridges the gap: when
a pattern repeats, it becomes a task.

Patterns never auto-act. Every detected pattern results in a task with
status RECEIVED, waiting for an operator's approval before any engineering
starts. The distinction matters: detecting that a probe is flapping is a read
permission (diagnostic), asking Wiz to fix it is a CODE_WRITE permission.

Deduplication is per-pattern-per-window: the same flapping probe for two
minutes in a row is one task, not two. Dedup state lives in memory during a
run and is not persisted — a detector restart is a system restart, and it is
OK that an old pattern repeats once the system comes back up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from openjarvis.reliability.correlate import correlate
from openjarvis.reliability.flapping import FlappingDetector, FlappingVerdict
from openjarvis.reliability.types import Incident
from openjarvis.wiz.features.model import FeatureRequest, FeatureState, Priority

logger = logging.getLogger(__name__)

__all__ = ["PatternDetector", "ProactiveTask", "detect_patterns"]


@dataclass
class ProactiveTask:
    """A pattern that was detected and turned into a task."""

    pattern_id: str
    pattern_type: str  # "flapping" | "persistent" | "correlation" | etc
    summary: str
    description: str
    confidence: float  # 0.0-1.0
    detected_at: str


@dataclass
class PatternDetector:
    """Finds repeating problems in incidents.

    Each detector runs over the most recent N hours of incidents and
    remembers what patterns it has already created tasks for, so the same
    problem does not generate duplicate task every few minutes.

    Parameters
    ----------
    window:
        How far back to look for incidents (default 6 hours).
    dedup_window:
        How long to remember a pattern before allowing it to generate a
        second task (default 5 minutes).
    """

    window: timedelta = field(default_factory=lambda: timedelta(hours=6))
    dedup_window: timedelta = field(default_factory=lambda: timedelta(minutes=5))
    flapping_detector: FlappingDetector = field(default_factory=FlappingDetector)

    _recent_patterns: Dict[str, datetime] = field(default_factory=dict, repr=False)

    def detect_in_incident(
        self, incident: Incident
    ) -> List[ProactiveTask]:
        """Look for patterns in one incident.

        Returns
        -------
        List[ProactiveTask]
            Every detectable pattern, ready to become a feature request.
        """
        tasks = []

        # Flapping detection: if a probe changes verdict too often, the problem
        # is usually environmental (slow startup, rate limiting) not a bug to fix.
        # But the human should know the probe is flapping and can decide to
        # address the root cause (capacity, timeouts, etc).
        flapping = self._detect_flapping(incident)
        if flapping:
            tasks.append(flapping)

        return tasks

    def detect_in_store(self, incidents: List[Incident]) -> List[ProactiveTask]:
        """Look for patterns across multiple incidents.

        This is the heavy lift: correlation across an incident history.
        Returns
        -------
        List[ProactiveTask]
            Every pattern found, deduplicated against recent runs.
        """
        tasks = []

        # Group incidents by probe to look for repeated patterns.
        by_probe: Dict[str, List[Incident]] = {}
        cutoff = datetime.now(timezone.utc) - self.window
        for inc in incidents:
            if inc.created_at and datetime.fromisoformat(inc.created_at) < cutoff:
                continue
            probe_id = inc.probe_id or "unknown"
            by_probe.setdefault(probe_id, []).append(inc)

        # Persistent failures: same probe failed many times in the window.
        for probe_id, incs in by_probe.items():
            persistent = self._detect_persistent(probe_id, incs)
            if persistent:
                tasks.append(persistent)

        # Recurrence: same probe, same component, failure patterns.
        for probe_id, incs in by_probe.items():
            recurrence = self._detect_recurrence(probe_id, incs)
            if recurrence:
                tasks.append(recurrence)

        return self._deduplicate(tasks)

    def _detect_flapping(self, incident: Incident) -> Optional[ProactiveTask]:
        """Incident shows evidence of flapping."""
        flapping_data = incident.metadata.get("flapping")
        if not flapping_data:
            return None

        try:
            verdict = FlappingVerdict(**flapping_data)
        except (TypeError, ValueError):
            return None

        if not verdict.flapping:
            return None

        pattern_id = f"flapping:{verdict.probe_id}"

        # Dedup check: did we already create a task for this pattern recently?
        if not self._should_report(pattern_id):
            return None

        return ProactiveTask(
            pattern_id=pattern_id,
            pattern_type="flapping",
            summary=f"Probe '{verdict.probe_id}' is flapping (alternating pass/fail)",
            description=(
                f"The '{verdict.probe_id}' check has changed outcome "
                f"{verdict.transitions} times in the last {verdict.samples} checks "
                f"(threshold: {verdict.threshold}). This usually indicates an "
                f"intermittent environmental issue rather than a code bug, but should "
                f"be investigated. Recent pattern: {verdict.recent}"
            ),
            confidence=min(0.9, 0.5 + verdict.transitions * 0.1),
            detected_at=datetime.now(timezone.utc).isoformat(),
        )

    def _detect_persistent(
        self, probe_id: str, incidents: List[Incident]
    ) -> Optional[ProactiveTask]:
        """Same probe has failed persistently (no successes in the window)."""
        if len(incidents) < 3:
            return None

        # All recent incidents for this probe are still open (not resolved).
        if not all(inc.is_open for inc in incidents):
            return None

        pattern_id = f"persistent:{probe_id}"
        if not self._should_report(pattern_id):
            return None

        return ProactiveTask(
            pattern_id=pattern_id,
            pattern_type="persistent",
            summary=f"Probe '{probe_id}' has failed {len(incidents)} times without recovery",
            description=(
                f"The '{probe_id}' check has opened {len(incidents)} incidents "
                f"in the last {self.window.total_seconds() / 3600:.0f} hours without "
                f"a recovery in between. This is a sustained problem, not an intermittent "
                f"one, and likely needs investigation."
            ),
            confidence=min(1.0, 0.6 + len(incidents) * 0.1),
            detected_at=datetime.now(timezone.utc).isoformat(),
        )

    def _detect_recurrence(
        self, probe_id: str, incidents: List[Incident]
    ) -> Optional[ProactiveTask]:
        """Same probe reopened multiple times (fixing and re-breaking)."""
        from openjarvis.reliability.types import IncidentState

        # Count resolved incidents in the window.
        resolved = [
            inc for inc in incidents if inc.state == IncidentState.RESOLVED
        ]
        if len(resolved) < 2:
            return None

        pattern_id = f"recurrence:{probe_id}"
        if not self._should_report(pattern_id):
            return None

        return ProactiveTask(
            pattern_id=pattern_id,
            pattern_type="recurrence",
            summary=f"Probe '{probe_id}' has recurred {len(resolved)} times in this window",
            description=(
                f"The '{probe_id}' check has closed and reopened {len(resolved)} times "
                f"in the last {self.window.total_seconds() / 3600:.0f} hours. This suggests "
                f"the fix is incomplete or a deeper root cause is being masked by workarounds."
            ),
            confidence=min(0.85, 0.5 + len(resolved) * 0.15),
            detected_at=datetime.now(timezone.utc).isoformat(),
        )

    def _should_report(self, pattern_id: str) -> bool:
        """Check dedup window and update state."""
        now = datetime.now(timezone.utc)
        last_seen = self._recent_patterns.get(pattern_id)

        # Never reported, or dedup window has passed.
        if last_seen is None or (now - last_seen) > self.dedup_window:
            self._recent_patterns[pattern_id] = now
            return True

        return False

    def _deduplicate(self, tasks: List[ProactiveTask]) -> List[ProactiveTask]:
        """Remove duplicates from the same run."""
        seen: Dict[str, ProactiveTask] = {}
        for task in tasks:
            if task.pattern_id not in seen:
                seen[task.pattern_id] = task
        return list(seen.values())


def detect_patterns(
    incidents: List[Incident],
    detector: Optional[PatternDetector] = None,
) -> List[ProactiveTask]:
    """Find actionable patterns in recent incidents.

    Every task returned is ready to become a FeatureRequest with state
    RECEIVED, waiting for operator approval.
    """
    if detector is None:
        detector = PatternDetector()

    tasks = []

    # Per-incident checks (flapping in the incident's own data).
    for incident in incidents:
        tasks.extend(detector.detect_in_incident(incident))

    # Cross-incident checks (patterns across the store).
    tasks.extend(detector.detect_in_store(incidents))

    # Flatten and deduplicate across all checks.
    return detector._deduplicate(tasks)


def task_to_feature_request(
    task: ProactiveTask,
    target: str = "wize",
    source: str = "proactive",
) -> FeatureRequest:
    """Convert a proactive task into a feature request.

    The request starts in RECEIVED state, waiting for approval.
    The operator_request is deterministically generated from the pattern.
    """
    import uuid
    from datetime import datetime

    now = datetime.now(timezone.utc).isoformat()

    return FeatureRequest(
        id=f"PROACTIVE-{uuid.uuid4().hex[:8].upper()}",
        title=task.summary,
        operator_request=f"[proactive] {task.summary}",
        desired_outcome=f"Investigate and fix the detected pattern: {task.summary}",
        source=source,
        actor_id="wiz-proactive",
        target=target,
        repository="",  # Will be filled in by the profile
        priority=Priority.P3 if task.confidence < 0.7 else Priority.P2,
        state=FeatureState.RECEIVED,
        risk="LOW",  # Pattern detection is read-only; the fix decides the risk
        created_at=now,
        updated_at=now,
    )
