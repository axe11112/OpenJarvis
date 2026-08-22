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

from openjarvis.reliability.events import (
    RELIABILITY_INCIDENT_DEDUPED,
    RELIABILITY_INCIDENT_RECURRENCE,
    RELIABILITY_RECOVERED_EXTERNALLY,
)
from openjarvis.reliability.fingerprint import fingerprint
from openjarvis.reliability.probes.executor import ConfirmationTracker
from openjarvis.reliability.probes.spec import ProbeSpec
from openjarvis.reliability.severity import classify
from openjarvis.reliability.types import (
    Evidence,
    EvidenceKind,
    Incident,
    IncidentState,
    ProbeResult,
    RecoveryType,
    Severity,
    Signal,
    TrustLevel,
    now_iso,
)

logger = logging.getLogger(__name__)

__all__ = ["Detection", "Detector"]

#: Probe failure kinds that describe JARVIS being unable to look, rather than
#: the site being broken. None of these may open an incident: an incident that
#: says "production is down" when the truth is "our network refused the
#: connection" is worse than no monitoring at all.
_SELF_FAILURE_KINDS = frozenset({"misconfigured", "runner_error", "blocked"})

#: Failure kinds where the site answered correctly and only took too long.
#: Public, because :mod:`openjarvis.reliability.monitor_health` imports it — two
#: definitions of "slow" in one pipeline is a bug waiting for an evening.
LATENCY_FAILURE_KINDS = frozenset({"slow", "slow_response", "budget_exceeded"})

#: Extra consecutive sightings required before a latency-only failure is
#: believed, on top of the probe's own ``confirm_runs``.
#:
#: Measured rather than guessed. On this machine every active probe runs 6-40x
#: inside its duration budget, including at load average 27: browser probes
#: p95 4.6-5.5s against a 30s budget, HTTP probes p95 0.14-0.46s against 5-8s.
#: Yet nine of the first twenty-five incidents opened on a duration overrun, at
#: 33-140s and 9.65s respectively — 7-48x the loaded p95. Those are stalls on a
#: four-core, eight-gigabyte laptop, not a latency distribution a budget should
#: be widened to accommodate; widening it is how a genuine tenfold regression
#: stops being visible.
#:
#: So the budget stays where the evidence puts it and the *confirmation* is what
#: changes, and only for the kind of failure that was wrong. A missing element,
#: a bad status or an unreachable page is still believed at the probe's own
#: ``confirm_runs``: a site that is down does not become healthy by being asked
#: again.
LATENCY_EXTRA_CONFIRMATIONS = 2


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
        monitor_health: Any = None,
    ) -> None:
        self._store = store
        self._tracker = tracker or ConfirmationTracker()
        # Optional. Absent means every latency failure is judged on its own,
        # which is the previous behaviour and is never less safe — only noisier.
        self._monitor_health = monitor_health
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

        latency_only = result.failure_kind in LATENCY_FAILURE_KINDS
        if self._monitor_health is not None:
            # Recorded before the verdict is asked for, so this probe's own
            # observation is in the window for *other* probes to corroborate
            # with. It is excluded from its own verdict below.
            self._monitor_health.record(spec.id, latency_only=latency_only)
            if latency_only:
                monitor = self._monitor_health.verdict(exclude=spec.id)
                if monitor.degraded:
                    # Not an incident. Two unrelated pages cannot both slow down
                    # while still serving correct content because the site is
                    # unwell; that shape is the observer, and opening a CRITICAL
                    # for it is how a busy laptop woke the owner three times in
                    # one night.
                    logger.warning(
                        "probe %s exceeded its time budget but %s",
                        spec.id,
                        monitor.reason,
                    )
                    return Detection(
                        suppressed=True,
                        reason=(
                            "the page was correct but over its time budget, and "
                            f"{monitor.reason}"
                        ),
                    )

        count = self._tracker.record(spec.id, failed=True)
        required = max(1, spec.retry.confirm_runs)
        if latency_only:
            required += LATENCY_EXTRA_CONFIRMATIONS
        if count < required:
            logger.info(
                "probe %s failed (%d/%d confirmations%s); not opening an incident yet",
                spec.id,
                count,
                required,
                ", latency only" if latency_only else "",
            )
            return Detection(
                suppressed=True,
                reason=(
                    f"awaiting confirmation ({count}/{required})"
                    + (
                        "; the page answered correctly and only exceeded its time "
                        "budget, which needs more than one sighting to be believed"
                        if latency_only
                        else ""
                    )
                ),
            )

        # Severity is decided by deterministic rules over what was observed,
        # never by a model, and never below what the operator declared.
        classification = classify(
            component=spec.component, result=result, declared=spec.severity
        )
        severity = classification.severity
        finger = fingerprint(
            component=spec.component,
            failure_kind=result.failure_kind,
            probe_id=spec.id,
            error=result.error,
        )

        existing = self._store.find_by_fingerprint(finger)
        if existing is not None:
            # One active incident per fingerprint. A homepage that fails every
            # minute for an hour is one problem, not sixty.
            self._store.record_occurrence(existing)
            for item in result.evidence:
                self._store.add_evidence(existing, item)
            self._publish(RELIABILITY_INCIDENT_RECURRENCE, existing)
            self._publish(RELIABILITY_INCIDENT_DEDUPED, existing)
            return Detection(
                incident=existing,
                recurred=True,
                reason=f"occurrence {existing.occurrences} of a known failure",
            )

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
                # Recorded so correlation can tell "the site did not answer"
                # from "the site answered and said no". A 5xx groups with other
                # probes failing at the same moment; a 401 does not.
                "http_status": result.http_status,
                "final_url": result.final_url,
                "declared_severity": spec.severity.value,
                "severity_rule": classification.to_dict(),
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
                IncidentState.MERGED,
            ):
                # A repair is mid-flight; let the repair loop finish rather than
                # racing it to a conclusion.
                #
                # ``MERGED`` especially: one probe going green is a far weaker
                # claim than the post-merge verification currently running, and
                # letting it close the incident would discard the fleet result
                # that is about to arrive.
                continue
            if not incident.can_transition_to(IncidentState.RESOLVED):
                continue
            # Recovering without a JARVIS repair is a different fact from
            # JARVIS having fixed it, and is recorded as such: otherwise JARVIS
            # takes credit for every transient failure that cleared itself, and
            # its real effectiveness becomes impossible to measure.
            repaired = any(a.verified for a in incident.attempts)
            recovery = (
                RecoveryType.VERIFIED_REPAIR
                if repaired
                else RecoveryType.RECOVERED_EXTERNALLY
            )
            note = "Probe passed again; the failure no longer reproduces " + (
                "after a verified JARVIS repair."
                if repaired
                else (
                    "and no JARVIS repair was involved: it was fixed "
                    "elsewhere, or was transient."
                )
            )
            self._store.add_evidence(
                incident,
                Evidence(
                    kind=EvidenceKind.NOTE,
                    summary=note,
                    source="detector",
                    trust=TrustLevel.TRUSTED,
                ),
            )
            incident.resolution.recovery_type = recovery
            incident.resolution.resolved_at = now_iso()
            self._store.save(incident)
            self._store.transition(
                incident,
                IncidentState.RESOLVED,
                reason=(
                    "the probe passed again; "
                    + (
                        "the repair holds"
                        if repaired
                        else "recovered externally, no repair was required"
                    )
                ),
            )
            logger.info("Incident %s resolved (%s)", incident.id, recovery.value)
            if not repaired:
                self._publish(RELIABILITY_RECOVERED_EXTERNALLY, incident)
            self._notify_recovered(incident, recovery)
            return Detection(incident=incident, recovered=True, reason=recovery.value)
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

    def _notify_recovered(self, incident: Incident, recovery: "RecoveryType") -> None:
        """Tell the owner an incident cleared, and whether JARVIS did it."""
        if self._notifier is None:
            return
        try:
            self._notifier.recovered(incident, recovery_type=recovery)
        except AttributeError:
            pass
        except Exception:
            logger.exception("could not send a recovery notice for %s", incident.id)

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
