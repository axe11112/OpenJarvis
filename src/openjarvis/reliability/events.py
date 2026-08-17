"""Event names published by the reliability subsystem.

These are plain module-level string constants rather than new
:class:`~openjarvis.core.events.EventType` members, following the precedent set
by :mod:`openjarvis.scheduler.scheduler` ("avoids editing core EventType enum").

``EventType`` is a ``str`` Enum, so the bus's subscriber dictionary treats a
matching plain string as the same key — ``bus.subscribe(RELIABILITY_TICK_START,
handler)`` and ``bus.publish(RELIABILITY_TICK_START, {...})`` work exactly as
they would for an enum member.
"""

from __future__ import annotations

#: A monitor tick (one probe or one source poll) began.
RELIABILITY_TICK_START = "reliability_tick_start"

#: A monitor tick finished, successfully or otherwise.
RELIABILITY_TICK_END = "reliability_tick_end"

#: A new incident was opened.
RELIABILITY_INCIDENT_OPENED = "reliability_incident_opened"

#: An incident changed state.
RELIABILITY_INCIDENT_TRANSITION = "reliability_incident_transition"

#: A repeat observation was folded into an existing incident.
RELIABILITY_INCIDENT_RECURRENCE = "reliability_incident_recurrence"

#: A repair attempt started.
RELIABILITY_REPAIR_ATTEMPT_START = "reliability_repair_attempt_start"

#: A repair attempt finished.
RELIABILITY_REPAIR_ATTEMPT_END = "reliability_repair_attempt_end"

#: Independent verification produced a verdict.
RELIABILITY_VERIFICATION = "reliability_verification"

#: A safety policy refused an action.
RELIABILITY_POLICY_DENIED = "reliability_policy_denied"

#: The 24/7 watcher started.
RELIABILITY_WATCH_STARTED = "reliability_watch_started"

#: The watcher stopped, whether cleanly or by emergency stop.
RELIABILITY_WATCH_STOPPED = "reliability_watch_stopped"

#: A repeat failure was folded into an existing incident rather than opening a
#: new one.  Recorded so "we saw this 400 times" is auditable.
RELIABILITY_INCIDENT_DEDUPED = "reliability_incident_deduped"

#: A check was found to be alternating between pass and fail.
RELIABILITY_FLAPPING_DETECTED = "reliability_flapping_detected"

#: An incident was found mid-repair after a restart and parked for a human.
RELIABILITY_RECOVERY_REQUIRED = "reliability_recovery_required"

#: A failure stopped reproducing without JARVIS having changed anything.
RELIABILITY_RECOVERED_EXTERNALLY = "reliability_recovered_externally"

#: A pull request was opened for a verified repair.
RELIABILITY_PR_CREATED = "reliability_pr_created"

#: An incident was handed to a human.
RELIABILITY_HUMAN_REQUIRED = "reliability_human_required"

#: A post-incident report was generated.
RELIABILITY_REPORT_GENERATED = "reliability_report_generated"

__all__ = [
    "RELIABILITY_FLAPPING_DETECTED",
    "RELIABILITY_HUMAN_REQUIRED",
    "RELIABILITY_INCIDENT_DEDUPED",
    "RELIABILITY_PR_CREATED",
    "RELIABILITY_RECOVERED_EXTERNALLY",
    "RELIABILITY_RECOVERY_REQUIRED",
    "RELIABILITY_REPORT_GENERATED",
    "RELIABILITY_WATCH_STARTED",
    "RELIABILITY_WATCH_STOPPED",
    "RELIABILITY_INCIDENT_OPENED",
    "RELIABILITY_INCIDENT_RECURRENCE",
    "RELIABILITY_INCIDENT_TRANSITION",
    "RELIABILITY_POLICY_DENIED",
    "RELIABILITY_REPAIR_ATTEMPT_END",
    "RELIABILITY_REPAIR_ATTEMPT_START",
    "RELIABILITY_TICK_END",
    "RELIABILITY_TICK_START",
    "RELIABILITY_VERIFICATION",
]
