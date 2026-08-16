"""Whether an event is worth ringing a phone about.

A notification the operator can read later and a call that makes their phone
ring in the night are not the same instrument, and the mistake this module
exists to prevent is treating them as one. The Telegram policy in
:mod:`~openjarvis.reliability.notify` is already strict — successes and serious
failures only. This is stricter still: of the things worth *telling* the
operator, only the ones that cannot wait until morning are worth *calling* about.

A successful repair is never a call. It is good news, it is already a Telegram
message, and waking someone to deliver good news is how the phone gets silenced
before the night it matters.

Repeats are suppressed per incident rather than per message. Production being
broken tends to produce a run of related events — the deployment failed, then
the probe failed, then the retry failed — and each one is a fair reason to call
exactly once.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from openjarvis.reliability.types import Incident, IncidentState, Severity

logger = logging.getLogger(__name__)

__all__ = ["CallDecision", "CallTrigger", "REASONS"]

#: Why a call may be placed. Each is a situation where JARVIS has stopped and
#: production is, or may be, broken.
REASONS = {
    "post_merge_failure": "a fix went live and production did not come good",
    "production_deployment_failed": "a production deployment failed",
    "human_required_production": "a production problem needs a human",
    "critical_unhandled": "a critical fault JARVIS cannot safely handle",
    "attempts_exhausted": "repair attempts ran out on a production outage",
    "security_event": "a security control refused something",
}

#: How long the same incident stays suppressed after a call.
DEFAULT_COOLDOWN_SECONDS = 3600.0


@dataclass
class CallDecision:
    """Whether to ring, and why."""

    call: bool
    reason: str = ""
    detail: str = ""
    incident_id: str = ""

    def __bool__(self) -> bool:
        return self.call


@dataclass
class CallTrigger:
    """Decides, and remembers what it already decided.

    Parameters
    ----------
    cooldown_seconds:
        Minimum gap between calls about the same incident.
    """

    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    clock: Callable[[], float] = time.monotonic
    _called: Dict[str, float] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- the policy -------------------------------------------------------

    def evaluate(
        self,
        incident: Optional[Incident],
        *,
        event: str = "",
        production_authority_used: bool = False,
    ) -> CallDecision:
        """Whether *event* on *incident* is worth a call.

        ``event`` names what just happened, using the keys of :data:`REASONS`
        where one fits. An event nobody listed is not a call: this policy is an
        allowlist for the same reason the command set is.
        """
        if incident is None:
            return CallDecision(False, detail="no incident")

        reason = self._reason(incident, event, production_authority_used)
        if not reason:
            return CallDecision(
                False,
                detail="handled automatically",
                incident_id=incident.id,
            )
        if not self._claim(incident.id):
            return CallDecision(
                False,
                reason=reason,
                detail="already called about this incident",
                incident_id=incident.id,
            )
        return CallDecision(
            True, reason=reason, detail=REASONS[reason], incident_id=incident.id
        )

    def _reason(
        self, incident: Incident, event: str, production_authority_used: bool
    ) -> str:
        """The reason to call, or ``""``. Order is significance, not chance."""
        if event in ("post_merge_failure", "production_deployment_failed"):
            return event
        if incident.metadata.get("post_merge_failure"):
            # The durable marker outlives the event that wrote it, so an
            # incident carrying one is always call-worthy on its own facts.
            return "post_merge_failure"
        if event == "security_event":
            return "security_event"

        if incident.state is IncidentState.HUMAN_REQUIRED:
            # A repair that gave up on something that never reached production
            # is a message, not a call: nothing is live and nothing is worse
            # than it was before JARVIS started.
            if production_authority_used:
                return "human_required_production"
            if incident.severity is Severity.CRITICAL:
                return "critical_unhandled"
            if event == "attempts_exhausted":
                return "attempts_exhausted" if production_authority_used else ""
            return ""

        if (
            incident.severity is Severity.CRITICAL
            and incident.state not in _JARVIS_IS_HANDLING_IT
        ):
            return "critical_unhandled"
        return ""

    # -- repeat suppression ----------------------------------------------

    def _claim(self, incident_id: str) -> bool:
        """Whether this incident may be called about now."""
        with self._lock:
            now = self.clock()
            last = self._called.get(incident_id)
            if last is not None and now - last < self.cooldown_seconds:
                return False
            self._called[incident_id] = now
            return True

    def forget(self, incident_id: str) -> None:
        """Drop the suppression for one incident, so it may ring again."""
        with self._lock:
            self._called.pop(incident_id, None)


#: States where JARVIS is actively working the problem. A CRITICAL fault it is
#: already fixing is not a reason to wake anybody — the call comes if and when
#: it stops.
_JARVIS_IS_HANDLING_IT = frozenset(
    {
        IncidentState.DETECTED,
        IncidentState.INVESTIGATING,
        IncidentState.REPRODUCING,
        IncidentState.FIXING,
        IncidentState.TESTING,
        IncidentState.VERIFYING,
        IncidentState.RESOLVED,
    }
)
