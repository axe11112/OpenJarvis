"""How many messages would this have sent?

The only honest way to answer that about a notification policy is to run it
over history that actually happened, and the only safe way is to run it with
nothing attached to a phone. This module does both: it reads the incident
store, reconstructs what each incident asked of the owner, and drives the
*real* :class:`~openjarvis.reliability.notify.NotificationRouter` — the same
class the watcher uses, not a model of it — through a
:class:`~openjarvis.reliability.notify.RecordingNotifier`.

Two runs, from the same history:

* **before** — one message per fingerprint, no correlation, detection alerts on,
  every escalation sent whether or not it named an action. That is what the
  code did on the morning that produced this work.
* **after** — the current policy, with correlation and the actionable-ask gate.

The difference between the two counts is the claim this change makes, measured
rather than asserted. Running it against a real ``incidents.db`` is the
acceptance test; running it against synthetic incidents is how the regression
suite checks that the reduction is caused by the rules and not by the data.

Deliberately reconstructive rather than exact. The store records incidents,
transitions and evidence; it does not record "a Telegram message was sent at
10:04". So the *before* count is derived by replaying the old rules over the
recorded lifecycle, and it is a lower bound on what was actually delivered: an
incident that flapped between states during a watcher cycle the store never
observed produced messages this cannot see. Stated plainly because a number
that quietly understates the problem it is used to justify fixing is worse than
no number.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from openjarvis.reliability.notify import (
    NotificationRouter,
    RecordingNotifier,
    render_alert,
    render_human_required,
    render_resolved,
)
from openjarvis.reliability.notify_ledger import NotificationLedger
from openjarvis.reliability.outage import OutageRegistry
from openjarvis.reliability.types import Incident, IncidentState, Severity

logger = logging.getLogger(__name__)

__all__ = ["ReplayResult", "replay", "replay_old", "replay_new", "load_incidents"]


#: Internal states that, in the old code, produced an escalation.
_ESCALATING = (
    IncidentState.HUMAN_REQUIRED,
    IncidentState.FAILED,
    IncidentState.RECOVERY_REQUIRED,
    IncidentState.ROLLED_BACK,
)


@dataclass
class ReplayResult:
    """What a replay produced."""

    label: str
    messages: List[Dict[str, Any]] = field(default_factory=list)
    incidents: int = 0
    outages: int = 0

    @property
    def count(self) -> int:
        """How many messages would have reached the owner."""
        return len(self.messages)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize, for ``--json``."""
        return {
            "label": self.label,
            "messages": list(self.messages),
            "count": self.count,
            "incidents": self.incidents,
            "outages": self.outages,
        }


def load_incidents(db_path: str | Path, *, limit: int = 500) -> List[Incident]:
    """Every incident in a store, oldest first.

    Opened read-only through the normal store, so a replay run against the live
    database cannot alter it.
    """
    from openjarvis.reliability.store import IncidentStore

    store = IncidentStore(db_path)
    try:
        incidents = list(store.list(limit=limit))
    finally:
        store.close()
    return sorted(incidents, key=lambda i: str(i.created_at or ""))


def _events(incident: Incident) -> List[Tuple[str, str]]:
    """The owner-facing events this incident's recorded history produced.

    ``(kind, reason)`` in order. Reconstructed from the transition log where
    there is one, and from the final state otherwise — an incident written
    before transitions were recorded still has a state, and dropping it would
    flatter both counts equally but make the comparison less useful.
    """
    events: List[Tuple[str, str]] = []
    transitions = list(getattr(incident, "transitions", []) or [])

    if incident.severity is Severity.CRITICAL:
        events.append(("alert", ""))

    seen_states: List[IncidentState] = []
    for transition in transitions:
        try:
            state = IncidentState.parse(getattr(transition, "to_state", ""))
        except Exception:  # noqa: BLE001 - a malformed row is skipped, not fatal
            continue
        seen_states.append(state)
        reason = str(getattr(transition, "reason", "") or "")
        if state in _ESCALATING:
            events.append(("human_required", reason or "the repair loop stopped"))
        elif state is IncidentState.RESOLVED:
            events.append(("resolved", reason))

    if not seen_states:
        if incident.state in _ESCALATING:
            events.append(("human_required", "the repair loop stopped"))
        elif incident.state is IncidentState.RESOLVED:
            events.append(("resolved", ""))
    return events


# ---------------------------------------------------------------------------
# The two policies
# ---------------------------------------------------------------------------


def replay_old(incidents: Sequence[Incident]) -> ReplayResult:
    """What the previous rules would have sent.

    Written out rather than reached for by flipping switches on the current
    router, because the current router no longer *can* behave this way — the
    ask gate and the outage key are not optional. Reproducing the old policy
    explicitly is what makes the comparison a measurement instead of a
    tautology.

    The old rules, exactly:

    * a CRITICAL detection sends "something serious happened";
    * every escalation sends, whatever it can or cannot ask for;
    * every resolution sends, whether or not the owner had heard about it;
    * deduplication is per *fingerprint*, and only suppresses an identical
      owner-state.
    """
    result = ReplayResult(label="before", incidents=len(incidents))
    told: Dict[str, str] = {}

    def _news(key: str, state: str) -> bool:
        """The old ledger's rule, reproduced exactly."""
        previous = told.get(key)
        if previous is None:
            return True
        if previous == state:
            return False
        if state == "fixed":
            return True
        if previous.startswith("working") and state.startswith("needs-you"):
            # The duplicate the owner reported: a CRITICAL detection alerted
            # while the incident was still being worked, and the escalation
            # minutes later counted as new information because the internal
            # state had moved.
            return True
        if previous.startswith("needs-you") and state.startswith("needs-you"):
            try:
                return (
                    Severity.parse(state.split(":")[1]).rank
                    > Severity.parse(previous.split(":")[1]).rank
                )
            except (ValueError, IndexError):
                return False
        return previous == "fixed"

    for incident in incidents:
        key = incident.fingerprint or incident.id
        for kind, reason in _events(incident):
            if kind == "alert":
                # Sent while the incident was still being handled, so the
                # recorded owner-state is "working" — which is precisely why
                # the escalation that followed was not suppressed.
                state = f"working:{incident.severity.value}"
                if not _news(key, state):
                    continue
                told[key] = state
                result.messages.append(
                    {
                        "incident": incident.id,
                        "kind": "alert",
                        "message": render_alert(incident),
                    }
                )
            elif kind == "human_required":
                state = f"needs-you:{incident.severity.value}"
                if not _news(key, state):
                    continue
                told[key] = state
                result.messages.append(
                    {
                        "incident": incident.id,
                        "kind": "human_required",
                        "message": render_human_required(
                            incident, reason=reason, attempts=0, max_attempts=0
                        ),
                    }
                )
            else:
                told.pop(key, None)
                result.messages.append(
                    {
                        "incident": incident.id,
                        "kind": "resolved",
                        "message": render_resolved(incident),
                    }
                )
    return result


def replay_new(
    incidents: Sequence[Incident], *, registry: Optional[OutageRegistry] = None
) -> ReplayResult:
    """What the current rules would send, through the real router.

    Nothing is simulated: this is :class:`NotificationRouter` with an in-memory
    ledger, an in-memory outage registry and a recording transport. If the
    router's behaviour and this count ever disagree, the router is right and
    this is a bug — which is the property a replay tool has to have to be worth
    quoting a number from.
    """
    notifier = RecordingNotifier()
    registry = registry if registry is not None else OutageRegistry()
    router = NotificationRouter(
        notifier=notifier,
        min_severity=Severity.LOW,
        dedup_window_seconds=0.0,
        critical_grace_seconds=0.0,
        redact=False,
        ledger=NotificationLedger(),
        outages=registry,
    )

    for incident in incidents:
        for kind, reason in _events(incident):
            if kind == "alert":
                router.alert(incident)
            elif kind == "human_required":
                router.human_required(
                    incident,
                    reason=reason,
                    attempts=incident.attempts_used,
                    max_attempts=0,
                )
            else:
                previous_state = incident.state
                incident.state = IncidentState.RESOLVED
                try:
                    router.resolved(incident)
                finally:
                    incident.state = previous_state

    return ReplayResult(
        label="after",
        messages=list(notifier.messages),
        incidents=len(incidents),
        outages=len(registry.all_outages()),
    )


def replay(incidents: Sequence[Incident]) -> Dict[str, ReplayResult]:
    """Both policies over the same history.

    The incidents are copied for each run, because both replays write an
    ``outage_key`` and an ``owner_ask`` into incident metadata and neither
    should be able to influence the other — or, more importantly, be written
    back to the live store by a tool that is supposed to only read it.
    """
    before = replay_old([_copy(i) for i in incidents])
    after = replay_new([_copy(i) for i in incidents])
    return {"before": before, "after": after}


def _copy(incident: Incident) -> Incident:
    return Incident.from_dict(incident.to_dict())
