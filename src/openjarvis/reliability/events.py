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

__all__ = [
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
