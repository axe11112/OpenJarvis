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

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from openjarvis.reliability.statefile import write_json_atomic
from openjarvis.reliability.types import Incident, IncidentState, Severity

logger = logging.getLogger(__name__)

__all__ = ["CallDecision", "CallTrigger", "REASONS", "call_record_path"]

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


def call_record_path(config: Any) -> Path:
    """Where the record of what has already been rung about lives.

    Beside the incident database and the notification ledger, for the same
    reason they are beside each other: the watcher and the Control Center must
    not disagree about what the owner has already been told.
    """
    from openjarvis.core.paths import get_config_dir

    configured = getattr(getattr(config, "reliability", None), "db_path", "")
    if configured:
        return Path(configured).expanduser().parent / "called.json"
    return get_config_dir() / "reliability" / "called.json"


#: How long the same incident stays suppressed after a call.
DEFAULT_COOLDOWN_SECONDS = 3600.0

#: How long a problem must persist before it is worth ringing a phone about.
#: Measured against real behaviour on this system: CRITICAL probe timeouts open,
#: escalate, and clear themselves within a couple of minutes — six in one day.
#: Five minutes is long enough that a flap has resolved and short enough that a
#: genuine outage is not sitting unreported.
MINIMUM_AGE_SECONDS = 300.0

#: Calls per rolling hour, across every incident. The per-incident guards do not
#: help when ten different probes flap at once.
MAX_CALLS_PER_HOUR = 3


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
    #: How long a problem must have persisted before it is worth a call.
    #: Post-merge failures ignore this; everything else has to prove it is not
    #: a flap that will clear itself.
    minimum_age_seconds: float = MINIMUM_AGE_SECONDS
    #: A hard ceiling regardless of how many distinct incidents appear. Without
    #: it, ten separate flapping probes are ten calls.
    max_calls_per_hour: int = MAX_CALLS_PER_HOUR
    clock: Callable[[], float] = time.monotonic
    #: Where the record of what has already been rung about is kept.
    #:
    #: Without it both guards below are memory, and memory is exactly what a
    #: restart destroys. ``CallWatchdog`` re-reads every open incident thirty
    #: seconds after start, and ``minimum_age_seconds`` does not hold anything
    #: back — an incident that has been open for an hour passes an age check
    #: immediately. So a watcher that restarts overnight rang the phone again
    #: about a problem the owner had already been woken for, and the supervisor
    #: that restarts a dead watcher could do it repeatedly.
    path: Optional[Path] = None
    #: Wall clock, kept separately from ``clock``. Suppression is measured with
    #: ``time.monotonic`` because it cannot go backwards, but a monotonic value
    #: means nothing in the next process — it is relative to a boot, not an
    #: epoch. What goes on disk is therefore wall-clock, converted back into
    #: this process's monotonic frame on load.
    wall_clock: Callable[[], float] = time.time
    _called: Dict[str, float] = field(default_factory=dict, repr=False)
    _recent_calls: List[float] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self._load()

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

        if reason != "post_merge_failure" and not self._old_enough(incident):
            # A genuinely serious problem is one that is still there a few
            # minutes later. Observed on this system: CRITICAL probe timeouts
            # open, escalate because CRITICAL is not in the auto-repair
            # allowlist, and then clear themselves — six of them in one day.
            # Ringing on sight turns a flapping screenshot into a phone call at
            # two in the morning about something that fixed itself. Waiting
            # costs minutes on a problem that has already lasted minutes.
            #
            # A post-merge failure is exempt: unreviewed code is live and
            # production is unwell, which is not a state that improves by
            # waiting to see.
            return CallDecision(
                False,
                reason=reason,
                detail="too new to be sure it is real",
                incident_id=incident.id,
            )

        if not self._within_hourly_cap():
            # Whatever else is true, a phone that rings more than a few times
            # an hour is a phone that gets silenced — and then the call that
            # mattered arrives on a muted device.
            return CallDecision(
                False,
                reason=reason,
                detail="hourly call limit reached",
                incident_id=incident.id,
            )
        if not self._claim(self._key(incident)):
            return CallDecision(
                False,
                reason=reason,
                detail="already called about this incident",
                incident_id=incident.id,
            )
        with self._lock:
            self._recent_calls.append(self.clock())
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

    def _old_enough(self, incident: Incident) -> bool:
        """Whether the problem has lasted long enough to be worth waking for."""
        if self.minimum_age_seconds <= 0:
            return True
        created = getattr(incident, "created_at", "") or ""
        if not created:
            return True  # cannot tell; do not use uncertainty to stay silent
        try:
            from datetime import datetime, timezone

            opened = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - opened).total_seconds()
        except (ValueError, TypeError):
            return True
        return age >= self.minimum_age_seconds

    def _within_hourly_cap(self) -> bool:
        """Whether another call may be placed this hour."""
        if self.max_calls_per_hour <= 0:
            return True
        with self._lock:
            now = self.clock()
            self._recent_calls = [t for t in self._recent_calls if now - t < 3600.0]
            if len(self._recent_calls) >= self.max_calls_per_hour:
                return False
        return True

    @staticmethod
    def _key(incident: Incident) -> str:
        """What "the same problem" means for suppression.

        The fingerprint, not the incident id. A flapping probe opens a *new*
        incident every time it fails — observed here: one fingerprint produced
        INC-00014 at 05:35 and INC-00021 at 01:15 the next morning, with two
        more in between. Keyed by id, every recurrence is a fresh problem and
        rings again; keyed by fingerprint, it is recognised as the same thing
        going wrong repeatedly, which is exactly what it is.
        """
        return getattr(incident, "fingerprint", "") or incident.id

    def _claim(self, incident_id: str) -> bool:
        """Whether this problem may be called about now."""
        with self._lock:
            now = self.clock()
            last = self._called.get(incident_id)
            if last is not None and now - last < self.cooldown_seconds:
                return False
            self._called[incident_id] = now
            self._save()
            return True

    def forget(self, incident_id: str) -> None:
        """Drop the suppression for one incident, so it may ring again."""
        with self._lock:
            self._called.pop(incident_id, None)
            self._save()

    # -- surviving a restart ----------------------------------------------

    def _load(self) -> None:
        """Restore suppression from disk, in this process's monotonic frame."""
        if self.path is None or not self.path.exists():
            return
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - an unreadable record costs a call,
            logger.warning("voice: could not read the call record at %s", self.path)
            return  # ...never a crash.

        mono_now, wall_now = self.clock(), self.wall_clock()

        def _to_monotonic(then: Any) -> Optional[float]:
            # Ages are clamped at zero. A wall clock that jumped backwards
            # while the machine was asleep would otherwise produce a negative
            # age and read as "called in the future", and the safe reading of a
            # nonsense timestamp is that the owner was just called.
            try:
                age = max(0.0, wall_now - float(then))
            except (TypeError, ValueError):
                return None
            return mono_now - age

        for key, then in dict(saved.get("called") or {}).items():
            restored = _to_monotonic(then)
            if restored is not None and mono_now - restored < self.cooldown_seconds:
                self._called[str(key)] = restored
        for then in list(saved.get("recent_calls") or []):
            restored = _to_monotonic(then)
            if restored is not None and mono_now - restored < 3600.0:
                self._recent_calls.append(restored)

    def _save(self) -> None:
        """Persist as wall-clock. Caller holds the lock."""
        if self.path is None:
            return
        mono_now, wall_now = self.clock(), self.wall_clock()

        def _to_wall(then: float) -> float:
            return wall_now - (mono_now - then)

        write_json_atomic(
            self.path,
            {
                "saved_at": wall_now,
                "called": {k: _to_wall(v) for k, v in self._called.items()},
                "recent_calls": [_to_wall(t) for t in self._recent_calls],
            },
        )


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
