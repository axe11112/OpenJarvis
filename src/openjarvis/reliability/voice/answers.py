"""What Sir says, built from what JARVIS recorded.

Every sentence here is assembled from stored facts: incident state, attempt
outcomes, the merge record, the watcher's status. Nothing is generated, nothing
is inferred, and where the record is silent the answer says so rather than
filling the gap. "I don't know" is a correct answer that this system is allowed
to give; a plausible sentence about a production outage is not.

The other constraint is that these are *spoken*. A screen can carry a SHA, a
deployment id and a stack trace and the reader skims past them; a voice reading
"deployment d-p-l-underscore-a-N-D-R-9-i-1-G" is unusable and, worse, drowns the
one sentence that mattered. So identifiers are omitted unless a human would say
them aloud — a pull request number, yes; a commit hash, never — and answers are
capped at a couple of sentences. The detail is on the dashboard, and Sir says so.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from openjarvis.reliability.types import Incident, IncidentState

__all__ = ["VoiceFacts", "answer_for"]

#: Plain words for the states an operator might be told about. The enum names
#: are precise and unspeakable; these are what a person would say.
_STATE_WORDS = {
    IncidentState.DETECTED: "I have just spotted it",
    IncidentState.INVESTIGATING: "I am looking into it",
    IncidentState.REPRODUCING: "I am reproducing it",
    IncidentState.FIXING: "I am working on a fix",
    IncidentState.TESTING: "I am testing a fix",
    IncidentState.VERIFYING: "I am checking a fix",
    IncidentState.MERGED: "my fix is live and I am checking production",
    IncidentState.RESOLVED: "it is fixed",
    IncidentState.FAILED: "I could not fix it",
    IncidentState.HUMAN_REQUIRED: "I stopped and I need you",
    IncidentState.ROLLED_BACK: "I put the old version back",
    IncidentState.RECOVERY_REQUIRED: "I was interrupted and stopped",
}

#: The same plain-English vocabulary the notifications use. Shared on purpose:
#: an owner who is told "login" in a message and hears "authentication" on a
#: call has to work out that they are the same thing, mid-outage.
from openjarvis.reliability.notify import plain_subject as _plain_subject  # noqa: E402


class VoiceFacts:
    """Read-only access to the state Sir is allowed to talk about.

    A narrow facade rather than the store itself. Passing the whole incident
    store into an answer renderer invites an answer that reaches for something
    nobody thought about — the point of the seam is that what can be said is
    bounded by what can be read.
    """

    def __init__(
        self,
        *,
        store: Any = None,
        watcher_status: Any = None,
        site_url: str = "",
        merge_enabled: bool = False,
        repair_enabled: bool = False,
    ) -> None:
        self._store = store
        self._watcher_status = watcher_status
        self.site_url = site_url
        self.merge_enabled = merge_enabled
        self.repair_enabled = repair_enabled

    def open_incidents(self, limit: int = 20) -> List[Incident]:
        """Incidents that are not resolved, newest first."""
        if self._store is None:
            return []
        try:
            return list(self._store.list(open_only=True, limit=limit) or [])
        except Exception:  # noqa: BLE001 - a failed read is "I don't know"
            return []

    def latest(self) -> Optional[Incident]:
        """The incident a conversation is most likely about."""
        incidents = self.open_incidents(limit=1)
        return incidents[0] if incidents else None

    def watcher(self) -> Any:
        """The watcher's state, or ``None``."""
        if self._watcher_status is None:
            return None
        try:
            return self._watcher_status()
        except Exception:  # noqa: BLE001
            return None


def _last_attempt(incident: Incident) -> Any:
    attempts = list(getattr(incident, "attempts", []) or [])
    return attempts[-1] if attempts else None


def _sentence(subject: str, incident: Incident) -> str:
    words = _STATE_WORDS.get(incident.state, "I am working on it")
    return f"{subject} is having trouble and {words}."


def answer_for(intent_name: str, facts: VoiceFacts, **kwargs: Any) -> str:
    """The spoken answer for *intent_name*, from recorded state only."""
    handler = _ANSWERS.get(intent_name)
    if handler is None:
        return "Sir, I can't do that."
    try:
        return handler(facts, **kwargs)
    except Exception:  # noqa: BLE001 - never guess when the read failed
        return "Sir, I couldn't read that just now. It's on the dashboard."


# ---------------------------------------------------------------------------
# One function per READ intent
# ---------------------------------------------------------------------------


def _status(facts: VoiceFacts, **_kw: Any) -> str:
    incidents = facts.open_incidents()
    watcher = facts.watcher()
    running = bool(getattr(watcher, "running", False)) if watcher else False
    stopped = bool(getattr(watcher, "stop_engaged", False)) if watcher else False

    if stopped:
        return "Sir, the emergency stop is on. I am watching but not fixing anything."
    if not incidents:
        return (
            "Sir, everything is passing."
            if running
            else "Sir, everything is passing, but I am not running right now."
        )
    if len(incidents) == 1:
        # Lower-case the leading subject only, never the whole sentence: the
        # "I" in "and I am looking into it" has to survive.
        incident = incidents[0]
        sentence = _sentence(_plain_subject(incident), incident)
        return f"Sir, {sentence[:1].lower()}{sentence[1:]}"
    needing = [i for i in incidents if i.state is IncidentState.HUMAN_REQUIRED]
    if needing:
        return (
            f"Sir, there are {len(incidents)} problems open, "
            f"and {len(needing)} need you."
        )
    return f"Sir, there are {len(incidents)} problems open and I am working on them."


def _production_status(facts: VoiceFacts, **_kw: Any) -> str:
    incidents = facts.open_incidents()
    live = [
        i
        for i in incidents
        if i.state in (IncidentState.HUMAN_REQUIRED, IncidentState.MERGED)
    ]
    if not incidents:
        return "Sir, production is passing all its checks."
    if live:
        return (
            f"Sir, {_plain_subject(live[0]).lower()} is failing in production "
            "and I have stopped."
        )
    return (
        f"Sir, {_plain_subject(incidents[0]).lower()} is failing its checks. "
        "I am working on it."
    )


def _incidents(facts: VoiceFacts, **_kw: Any) -> str:
    incidents = facts.open_incidents()
    if not incidents:
        return "Sir, nothing is open."
    if len(incidents) == 1:
        incident = incidents[0]
        return f"Sir, one is open. {_sentence(_plain_subject(incident), incident)}"
    names = ", ".join(_plain_subject(i).lower() for i in incidents[:3])
    return f"Sir, {len(incidents)} are open: {names}."


def _what_happened(facts: VoiceFacts, **_kw: Any) -> str:
    incident = facts.latest()
    if incident is None:
        return "Sir, nothing is wrong at the moment."
    subject = _plain_subject(incident)
    cause = (incident.resolution.root_cause or "").strip()
    if cause:
        first = cause.split(". ")[0].strip().rstrip(".")
        if first and len(first) <= 140:
            reason = f"{first[:1].lower()}{first[1:]}"
            return f"Sir, {subject.lower()} started failing because {reason}."
    return f"Sir, {subject.lower()} started failing. I don't have a cause yet."


def _what_did_you_try(facts: VoiceFacts, **_kw: Any) -> str:
    incident = facts.latest()
    if incident is None:
        return "Sir, I haven't had to try anything."
    attempts = list(getattr(incident, "attempts", []) or [])
    if not attempts:
        return "Sir, I haven't tried a fix yet."
    attempt = attempts[-1]
    outcome = str(getattr(attempt, "outcome", "") or "")
    count = len(attempts)
    tries = "once" if count == 1 else f"{count} times"
    if outcome == "verified":
        return f"Sir, I tried {tries} and the last fix passed its checks."
    if outcome == "verification_failed":
        return f"Sir, I tried {tries}. The fix did not pass the check afterwards."
    if outcome in ("tests_failed", "no_diff"):
        return f"Sir, I tried {tries}. The change did not hold up in testing."
    return f"Sir, I tried {tries} and it did not work."


def _did_you_change_production(facts: VoiceFacts, **_kw: Any) -> str:
    incident = facts.latest()
    if incident is not None and incident.state is IncidentState.MERGED:
        return "Sir, yes. My fix is live and I am still checking production."
    if incident is not None and incident.metadata.get("post_merge_failure"):
        return (
            "Sir, yes. My fix went live and production did not come good, so I stopped."
        )
    if not facts.merge_enabled:
        return (
            "Sir, no. I am not allowed to change production. "
            "I only open pull requests for you."
        )
    return "Sir, no. Nothing of mine has gone live."


def _deployment_failure(facts: VoiceFacts, **_kw: Any) -> str:
    incident = facts.latest()
    marker = (incident.metadata.get("post_merge_failure") or {}) if incident else {}
    if not marker:
        return "Sir, no deployment of mine has failed."
    reason = str(marker.get("rule") or "")
    if reason == "deployment_missing":
        return "Sir, the new version never went live."
    if reason == "deployment_not_ready":
        return "Sir, the deployment itself failed to build."
    return "Sir, the deployment went out but the site still failed its checks."


def _probe_status(facts: VoiceFacts, **_kw: Any) -> str:
    incidents = facts.open_incidents()
    if not incidents:
        return "Sir, every check is passing."
    failing = ", ".join(_plain_subject(i).lower() for i in incidents[:3])
    return f"Sir, the checks failing are: {failing}."


def _diagnostic_result(facts: VoiceFacts, **_kw: Any) -> str:
    """The last diagnostic this session ran, never a remembered older one.

    Deliberately scoped to the call. "The last diagnostic said everything was
    fine" is a dangerous sentence when the diagnostic in question ran yesterday,
    and there is no stored history to date-stamp it against.
    """
    result = (getattr(facts, "last_diagnostic", "") or "").strip()
    if not result:
        return "Sir, I haven't run a diagnostic this call. Ask me to run one."
    return result


def _goodbye(_facts: VoiceFacts, **_kw: Any) -> str:
    return "Goodbye, Sir."


_ANSWERS: Dict[str, Any] = {
    "status": _status,
    "production_status": _production_status,
    "incidents": _incidents,
    "what_happened": _what_happened,
    "what_did_you_try": _what_did_you_try,
    "did_you_change_production": _did_you_change_production,
    "deployment_failure": _deployment_failure,
    "probe_status": _probe_status,
    "diagnostic_result": _diagnostic_result,
    "goodbye": _goodbye,
}
