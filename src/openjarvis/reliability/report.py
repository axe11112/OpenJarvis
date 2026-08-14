"""Post-incident reports.

Written once an incident reaches a terminal state, from the incident record
alone — no model, no narration, no interpretation. Every line is a fact JARVIS
recorded at the time, which is what makes the report usable as evidence rather
than as a summary.

Two things it is careful about:

* **Attribution.** "Recovered externally" and "verified repair" are different
  outcomes and are always labelled as such. A report that let JARVIS take credit
  for transient failures clearing themselves would make its effectiveness
  impossible to measure.
* **Secrets.** Every free-text field passes through the same redaction the
  briefing applies, because root causes and fix summaries are partly model
  output and partly captured log text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from openjarvis.reliability.briefing import redact_secrets
from openjarvis.reliability.types import (
    Incident,
    IncidentState,
    RecoveryType,
    now_iso,
)

logger = logging.getLogger(__name__)

__all__ = ["IncidentReport", "build_report", "format_duration"]


def _parse(timestamp: str) -> Optional[datetime]:
    """Parse an ISO timestamp, tolerating the ones we did not write."""
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp)
    except ValueError:
        return None


def format_duration(seconds: Optional[float]) -> str:
    """Render a duration as ``8m 42s``, or ``unknown`` when it cannot be known."""
    if seconds is None or seconds < 0:
        return "unknown"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


@dataclass(slots=True)
class IncidentReport:
    """A finished incident, as facts."""

    incident_id: str
    title: str
    component: str
    severity: str
    state: str
    detected_at: str = ""
    resolved_at: str = ""
    duration_seconds: Optional[float] = None
    acknowledged_after_seconds: Optional[float] = None
    repair_started_after_seconds: Optional[float] = None
    occurrences: int = 0
    attempts: int = 0
    recovery_type: str = RecoveryType.UNKNOWN.value
    root_cause: str = ""
    fix_summary: str = ""
    changed_files: List[str] = field(default_factory=list)
    regression_tests: List[str] = field(default_factory=list)
    checks: List[Dict[str, Any]] = field(default_factory=list)
    preview_url: str = ""
    verified: bool = False
    verification_note: str = ""
    pull_request: str = ""
    base_commit: str = ""
    flapping: bool = False
    timeline: List[Dict[str, str]] = field(default_factory=list)
    #: Always false in this phase; recorded so the report answers the question
    #: rather than leaving the reader to infer it.
    production_deployed: bool = False

    @property
    def duration(self) -> str:
        """Human-readable incident duration."""
        return format_duration(self.duration_seconds)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the dashboard and the API."""
        return {
            "incident_id": self.incident_id,
            "title": self.title,
            "component": self.component,
            "severity": self.severity,
            "state": self.state,
            "detected_at": self.detected_at,
            "resolved_at": self.resolved_at,
            "duration": self.duration,
            "duration_seconds": self.duration_seconds,
            "acknowledged_after_seconds": self.acknowledged_after_seconds,
            "repair_started_after_seconds": self.repair_started_after_seconds,
            "occurrences": self.occurrences,
            "attempts": self.attempts,
            "recovery_type": self.recovery_type,
            "root_cause": self.root_cause,
            "fix_summary": self.fix_summary,
            "changed_files": list(self.changed_files),
            "regression_tests": list(self.regression_tests),
            "checks": list(self.checks),
            "preview_url": self.preview_url,
            "verified": self.verified,
            "verification_note": self.verification_note,
            "pull_request": self.pull_request,
            "base_commit": self.base_commit,
            "flapping": self.flapping,
            "timeline": list(self.timeline),
            "production_deployed": self.production_deployed,
        }

    def render(self) -> str:
        """Render the report as plain text."""
        lines = [
            "INCIDENT REPORT",
            "",
            self.incident_id,
            "",
            f"Title:        {self.title}",
            f"Component:    {self.component}",
            f"Severity:     {self.severity}",
            f"Final state:  {self.state}",
            f"Detected:     {self.detected_at or 'unknown'}",
            f"Resolved:     {self.resolved_at or 'not resolved'}",
            f"Duration:     {self.duration}",
            f"Occurrences:  {self.occurrences}",
            f"Recovery:     {self.recovery_type}",
            "",
        ]
        if self.flapping:
            lines += ["Flapping:     yes (escalated rather than repaired)", ""]

        lines += ["Root cause:", self.root_cause or "  not determined", ""]

        if self.recovery_type == RecoveryType.RECOVERED_EXTERNALLY.value:
            lines += [
                "Repair:",
                "  None. The failure stopped reproducing without JARVIS "
                "changing anything.",
                "",
            ]
        else:
            lines += [
                "Repair:",
                f"  {self.fix_summary or 'no repair was made'}",
                f"  Attempts: {self.attempts}",
                f"  Base commit: {self.base_commit[:12] or 'n/a'}",
                "",
            ]

        if self.changed_files:
            lines += ["Files changed:"]
            lines += [f"  - {path}" for path in self.changed_files]
            lines.append("")

        lines += ["Regression test:"]
        if self.regression_tests:
            lines += [f"  - {path}" for path in self.regression_tests]
        else:
            lines.append("  none added — the fix is not covered by a new test")
        lines.append("")

        if self.checks:
            lines += ["Checks:"]
            for check in self.checks:
                if not check.get("ran"):
                    verdict = "NOT RUN"
                else:
                    verdict = "PASS" if check.get("passed") else "FAIL"
                lines.append(f"  {check.get('name')}: {verdict}")
            lines.append("")

        lines += [
            "Verification:",
            f"  Browser/probe: {'PASS' if self.verified else 'not verified'}",
            f"  Preview: {self.preview_url or 'none'}",
        ]
        if self.verification_note:
            lines.append(f"  {self.verification_note}")
        lines += [
            "",
            f"Pull request: {self.pull_request or 'none'}",
            "",
            "Production deployment:",
            "  Not performed"
            if not self.production_deployed
            else "  PERFORMED — investigate, this phase must not deploy",
            "",
        ]

        if self.timeline:
            lines.append("Timeline:")
            for entry in self.timeline:
                lines.append(
                    f"  {entry.get('at', '')}  {entry.get('state', '')}"
                    + (f"  — {entry['reason']}" if entry.get("reason") else "")
                )
        return "\n".join(lines)


def _seconds_between(start: str, end: str) -> Optional[float]:
    first, second = _parse(start), _parse(end)
    if first is None or second is None:
        return None
    return (second - first).total_seconds()


def _first_transition_to(incident: Incident, state: IncidentState) -> str:
    for transition in incident.transitions:
        if transition.to_state is state:
            return transition.at
    return ""


def build_report(incident: Incident) -> IncidentReport:
    """Build a post-incident report from the incident record alone."""
    last = incident.attempts[-1] if incident.attempts else None
    verification = last.verification if last is not None else None

    resolved_at = incident.resolution.resolved_at or _first_transition_to(
        incident, IncidentState.RESOLVED
    )
    if not resolved_at and incident.state in (
        IncidentState.HUMAN_REQUIRED,
        IncidentState.FAILED,
        IncidentState.RECOVERY_REQUIRED,
    ):
        resolved_at = _first_transition_to(incident, incident.state)

    acknowledged = _first_transition_to(incident, IncidentState.INVESTIGATING)
    repair_started = _first_transition_to(incident, IncidentState.FIXING)

    checks = []
    if last is not None:
        checks = list(last.checks.get("results") or [])

    report = IncidentReport(
        incident_id=incident.id,
        title=redact_secrets(incident.title),
        component=incident.component,
        severity=incident.severity.value,
        state=incident.state.value,
        detected_at=incident.created_at,
        resolved_at=resolved_at,
        duration_seconds=_seconds_between(
            incident.created_at, resolved_at or now_iso()
        ),
        acknowledged_after_seconds=_seconds_between(incident.created_at, acknowledged),
        repair_started_after_seconds=_seconds_between(
            incident.created_at, repair_started
        ),
        occurrences=incident.occurrences,
        attempts=len(incident.attempts),
        recovery_type=incident.resolution.recovery_type.value,
        root_cause=redact_secrets(
            incident.resolution.root_cause or incident.summary or ""
        ),
        fix_summary=redact_secrets(incident.resolution.fix_summary or ""),
        changed_files=list(last.changed_files) if last is not None else [],
        regression_tests=list(last.regression_tests) if last is not None else [],
        checks=checks,
        preview_url=last.preview_url if last is not None else "",
        verified=bool(verification and verification.passed),
        verification_note=redact_secrets(verification.notes if verification else ""),
        pull_request=incident.resolution.pr_url,
        base_commit=last.base_commit if last is not None else "",
        flapping=bool(incident.metadata.get("flapping")),
        timeline=[
            {
                "at": transition.at,
                "state": transition.to_state.value,
                "reason": redact_secrets(transition.reason or ""),
            }
            for transition in incident.transitions
        ],
        # There is no code path in this phase that deploys to production. The
        # field is reported rather than assumed so the report answers the
        # question a reader actually has.
        production_deployed=bool(incident.resolution.deployed_at),
    )
    return report
