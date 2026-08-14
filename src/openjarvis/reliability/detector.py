"""Detection — turning probe results and signals into incidents.

This is where restraint lives. Every design choice here exists to stop JARVIS
producing noise:

* A failing probe is not an incident until it has failed ``confirm_runs`` times
  consecutively.
* A repeat of a known failure increments an occurrence counter rather than
  opening a duplicate.
* A recovery closes the incident automatically rather than leaving a stale one
  for a human to notice.
* Misconfiguration (a missing credential) never masquerades as a site failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional

from openjarvis.reliability.events import RELIABILITY_INCIDENT_RECURRENCE
from openjarvis.reliability.fingerprint import fingerprint
from openjarvis.reliability.probes.executor import (
    ConfirmationTracker,
    escalate_severity,
)
from openjarvis.reliability.probes.spec import ProbeSpec
from openjarvis.reliability.types import (
    Evidence,
    EvidenceKind,
    Incident,
    IncidentState,
    ProbeResult,
    Severity,
    Signal,
    TrustLevel,
)

logger = logging.getLogger(__name__)

__all__ = ["Detection", "Detector"]

#: Probe failure kinds that describe JARVIS being unable to look, rather than
#: the site being broken. None of these may open an incident: an incident that
#: says "production is down" when the truth is "our network refused the
#: connection" is worse than no monitoring at all.
_SELF_FAILURE_KINDS = frozenset({"misconfigured", "runner_error", "blocked"})


@dataclass(slots=True)
class Detection:
    """What a detection pass decided."""

    incident: Optional[Incident] = None
    opened: bool = False
    recurred: bool = False
    recovered: bool = False
    suppressed: bool = False
    reason: str = ""


class Detector:
    """Converts probe results and infrastructure signals into incidents.

    Parameters
    ----------
    store:
        Incident store.
    tracker:
        Shared confirmation tracker (one per monitor process).
    environment:
        Recorded on every incident.
    notifier:
        Optional :class:`NotificationRouter`.
    bus:
        Optional event bus.
    """

    def __init__(
        self,
        store: Any,
        *,
        tracker: Optional[ConfirmationTracker] = None,
        environment: str = "production",
        notifier: Any = None,
        bus: Any = None,
    ) -> None:
        self._store = store
        self._tracker = tracker or ConfirmationTracker()
        self._environment = environment
        self._notifier = notifier
        self._bus = bus

    # -- probes -----------------------------------------------------------

    def from_probe(self, spec: ProbeSpec, result: ProbeResult) -> Detection:
        """Decide what a probe result means."""
        if result.success:
            return self._handle_recovery(spec)

        if result.failure_kind in _SELF_FAILURE_KINDS:
            # JARVIS is misconfigured. Saying "the website is broken" here would
            # be a lie, and an alarming one.
            logger.warning("probe %s could not run: %s", spec.id, result.error)
            return Detection(
                suppressed=True,
                reason=f"probe could not run ({result.failure_kind}): {result.error}",
            )

        count = self._tracker.record(spec.id, failed=True)
        required = max(1, spec.retry.confirm_runs)
        if count < required:
            logger.info(
                "probe %s failed (%d/%d confirmations); not opening an incident yet",
                spec.id,
                count,
                required,
            )
            return Detection(
                suppressed=True,
                reason=f"awaiting confirmation ({count}/{required})",
            )

        severity = escalate_severity(spec, result)
        finger = fingerprint(
            component=spec.component,
            failure_kind=result.failure_kind,
            probe_id=spec.id,
            error=result.error,
        )

        existing = self._store.find_by_fingerprint(finger)
        if existing is not None:
            self._store.record_occurrence(existing)
            for item in result.evidence:
                self._store.add_evidence(existing, item)
            self._publish(RELIABILITY_INCIDENT_RECURRENCE, existing)
            return Detection(incident=existing, recurred=True)

        incident = Incident(
            fingerprint=finger,
            severity=severity,
            component=spec.component,
            title=self._title(spec, result),
            summary=self._summary(spec, result),
            environment=self._environment,
            source="probe",
            probe_id=spec.id,
            repro_steps=spec.repro_steps(),
            metadata={
                "expected": spec.expectation_summary(),
                "actual": result.error,
                "failure_kind": result.failure_kind,
                "final_url": result.final_url,
                "declared_severity": spec.severity.value,
            },
        )
        for item in result.evidence:
            incident.add_evidence(item)
        self._store.create(incident)
        self._tracker.reset(spec.id)
        self._notify_alert(incident)
        return Detection(incident=incident, opened=True)

    def _handle_recovery(self, spec: ProbeSpec) -> Detection:
        """A passing probe closes any incident it previously opened."""
        self._tracker.record(spec.id, failed=False)
        for incident in self._store.list(open_only=True, limit=200):
            if incident.probe_id != spec.id:
                continue
            if incident.state in (
                IncidentState.FIXING,
                IncidentState.TESTING,
                IncidentState.VERIFYING,
            ):
                # A repair is mid-flight; let the repair loop finish rather than
                # racing it to a conclusion.
                continue
            if not incident.can_transition_to(IncidentState.RESOLVED):
                continue
            self._store.add_evidence(
                incident,
                Evidence(
                    kind=EvidenceKind.NOTE,
                    summary="Probe passed again; the failure no longer reproduces.",
                    source="detector",
                    trust=TrustLevel.TRUSTED,
                ),
            )
            self._store.transition(
                incident,
                IncidentState.RESOLVED,
                reason="the probe passed again; the failure no longer reproduces",
            )
            logger.info("Incident %s self-resolved", incident.id)
            return Detection(incident=incident, recovered=True)
        return Detection()

    # -- infrastructure signals -------------------------------------------

    def from_signal(self, signal: Signal) -> Detection:
        """Decide what an infrastructure signal means."""
        finger = fingerprint(
            component=signal.component or signal.source,
            failure_kind=signal.kind,
            probe_id=signal.source,
            error=signal.title,
            extra=[signal.external_id],
        )
        existing = self._store.find_by_fingerprint(finger)
        if existing is not None:
            self._store.record_occurrence(existing)
            self._publish(RELIABILITY_INCIDENT_RECURRENCE, existing)
            return Detection(incident=existing, recurred=True)

        incident = Incident(
            fingerprint=finger,
            severity=signal.severity,
            component=signal.component or signal.source,
            title=signal.title,
            summary=signal.detail or signal.title,
            environment=self._environment,
            source=signal.source,
            metadata={**signal.metadata, "signal_kind": signal.kind},
        )
        if signal.detail:
            incident.add_evidence(
                Evidence(
                    kind=EvidenceKind.LOG,
                    summary=signal.title,
                    content=signal.detail,
                    source=signal.source,
                    trust=signal.trust,
                )
            )
        self._store.create(incident)
        self._notify_alert(incident)
        return Detection(incident=incident, opened=True)

    def from_signals(self, signals: List[Signal]) -> List[Detection]:
        """Process a batch of signals."""
        return [self.from_signal(signal) for signal in signals]

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _title(spec: ProbeSpec, result: ProbeResult) -> str:
        if result.failure_kind == "timeout":
            return f"{spec.display_name} timed out"
        if result.failure_kind in ("navigation", "network_failure"):
            return f"{spec.display_name} is unreachable"
        if result.failure_kind == "http_error":
            return f"{spec.display_name} returned HTTP {result.http_status}"
        if result.failure_kind == "console_error":
            return f"{spec.display_name} has JavaScript errors"
        return f"{spec.display_name} failed"

    @staticmethod
    def _summary(spec: ProbeSpec, result: ProbeResult) -> str:
        """One-line description of what went wrong.

        Deliberately does not restate the expectation: probe errors already
        read "expected X, got Y", so prefixing them with the expectation
        produces "expected X, but expected X, got Y".  The expectation is
        carried in ``metadata["expected"]`` and rendered separately by the
        briefing and the dashboard.
        """
        observed = result.error or "the workflow did not complete"
        return f"Probe '{spec.id}' did not pass. Observed: {observed}"

    def _notify_alert(self, incident: Incident) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier.alert(incident)
        except Exception:
            logger.exception("could not send an alert for %s", incident.id)

    def _publish(self, event: str, incident: Incident) -> None:
        if self._bus is None:
            return
        try:
            self._bus.publish(
                event,
                {
                    "incident_id": incident.id,
                    "state": incident.state.value,
                    "severity": incident.severity.value,
                    "occurrences": incident.occurrences,
                },
            )
        except Exception:
            logger.exception("could not publish %s", event)


def severity_at_least(value: Severity, floor: Severity) -> bool:
    """Small helper mirrored from :class:`Severity` for readability."""
    return value.at_least(floor)
