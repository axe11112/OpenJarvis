"""Severity-based notification escalation.

Routine notifications go out once. A CRITICAL incident that is *still* open some
minutes later is a different situation: either nobody saw the first message, or
they saw it and JARVIS has not managed to resolve it. Both deserve a second
message.

What this module deliberately does not do is reach for a different channel.
Escalating from Telegram to SMS or a phone call needs a paid third-party
gateway, and inventing one — or shipping a stub that silently drops messages —
would look like coverage while providing none. The escalation therefore repeats
through the configured providers, and the transport list is the thing an
operator extends when they have a gateway to point it at.

Escalation is bounded: a fixed number of reminders, then silence. An alert that
repeats forever trains its reader to ignore it, which is worse than one that
stops.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from openjarvis.reliability.types import Incident, IncidentState, Severity

logger = logging.getLogger(__name__)

__all__ = ["EscalationPolicy", "EscalationTracker"]


@dataclass(slots=True)
class EscalationPolicy:
    """When an unresolved incident should be raised again."""

    #: Only incidents at or above this severity escalate at all.
    min_severity: Severity = Severity.CRITICAL
    #: How long an incident may stay open before the first reminder.
    after_minutes: float = 5.0
    #: How many reminders to send before falling silent.
    max_reminders: int = 2
    enabled: bool = True

    @property
    def after_seconds(self) -> float:
        """The delay in seconds."""
        return max(0.0, self.after_minutes * 60.0)


@dataclass
class EscalationTracker:
    """Tracks open incidents and emits reminders when they go stale.

    Driven by :meth:`sweep`, which the watcher calls on its cadence. Keeping it
    poll-driven rather than timer-driven means there is no background thread to
    leak, and a process that dies simply stops reminding rather than leaving a
    scheduled callback behind.
    """

    policy: EscalationPolicy = field(default_factory=EscalationPolicy)
    notifier: Any = None
    clock: Callable[[], float] = time.monotonic
    _first_seen: Dict[str, float] = field(default_factory=dict, repr=False)
    _sent: Dict[str, int] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def observe(self, incident: Incident) -> None:
        """Start the clock for an incident, if it qualifies."""
        if not self._qualifies(incident):
            return
        with self._lock:
            self._first_seen.setdefault(incident.id, self.clock())

    def clear(self, incident_id: str) -> None:
        """Stop tracking an incident that has been resolved or handed over."""
        with self._lock:
            self._first_seen.pop(incident_id, None)
            self._sent.pop(incident_id, None)

    def sweep(self, incidents: List[Incident]) -> List[Incident]:
        """Send reminders for incidents that have gone stale.

        Returns the incidents a reminder was sent for. Incidents no longer in
        the open list are forgotten, so a resolved incident cannot keep nagging.
        """
        if not self.policy.enabled:
            return []

        open_ids = {incident.id for incident in incidents}
        with self._lock:
            for stale in list(self._first_seen):
                if stale not in open_ids:
                    self._first_seen.pop(stale, None)
                    self._sent.pop(stale, None)

        escalated: List[Incident] = []
        now = self.clock()
        for incident in incidents:
            if not self._qualifies(incident):
                continue
            with self._lock:
                first = self._first_seen.setdefault(incident.id, now)
                sent = self._sent.get(incident.id, 0)
                if sent >= max(0, self.policy.max_reminders):
                    continue
                # Reminders are spaced by the same interval, so the second one
                # lands at 2x, the third at 3x. Backing off would risk a long
                # silence on the thing that matters most.
                due_at = first + self.policy.after_seconds * (sent + 1)
                if now < due_at:
                    continue
                self._sent[incident.id] = sent + 1
                attempt_number = sent + 1

            if self._send(incident, attempt_number):
                escalated.append(incident)
        return escalated

    # -- internals --------------------------------------------------------

    def _qualifies(self, incident: Incident) -> bool:
        if not self.policy.enabled:
            return False
        if incident.state in (
            IncidentState.RESOLVED,
            IncidentState.FAILED,
            IncidentState.ROLLED_BACK,
        ):
            return False
        return incident.severity.at_least(self.policy.min_severity)

    def _send(self, incident: Incident, reminder: int) -> bool:
        if self.notifier is None:
            return False
        minutes = int(self.policy.after_minutes * reminder)
        reason = (
            f"still unresolved {minutes} minute(s) after detection "
            f"(reminder {reminder} of {self.policy.max_reminders})"
        )
        try:
            return bool(
                self.notifier.human_required(
                    incident,
                    reason=reason,
                    attempts=incident.attempts_used,
                    max_attempts=0,
                )
            )
        except Exception:
            logger.exception("could not escalate %s", incident.id)
            return False

    def snapshot(self) -> Dict[str, Any]:
        """State for the dashboard."""
        with self._lock:
            return {
                "tracking": sorted(self._first_seen),
                "reminders_sent": dict(self._sent),
                "after_minutes": self.policy.after_minutes,
                "max_reminders": self.policy.max_reminders,
                "enabled": self.policy.enabled,
            }


def build_policy(config: Any) -> Optional[EscalationPolicy]:
    """Build an escalation policy from ``[reliability.notification]``."""
    section = getattr(config.reliability, "notification", None)
    if section is None:
        return None
    minutes = float(getattr(section, "critical_escalation_minutes", 0) or 0)
    return EscalationPolicy(
        min_severity=Severity.CRITICAL,
        after_minutes=minutes,
        enabled=bool(getattr(section, "enabled", False)) and minutes > 0,
    )
