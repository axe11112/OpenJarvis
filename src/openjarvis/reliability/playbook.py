"""How Sir works an incident, and what it owes a human when it stops.

The complaint this answers is "Sir gives up much too easily". It was accurate,
and the reason was structural rather than a missing retry: the loop had one
strategy, applied it up to three times, and escalated. Three attempts at the
same idea is one attempt with extra steps, and the message it produced said
only "3 repair attempts did not produce a verified fix" — which tells a person
nothing they can act on at two in the morning.

So this module holds three things the loop did not have.

**A shape for the work.** The six stages below are the order a competent
engineer works a page in, written down so the loop cannot skip to the end:
confirm it is real, classify it, look at it, choose a response, do the work,
and — the one that was missing — try a *different idea* when the first fails.

**A shape for giving up.** :class:`Handover` is what HUMAN_REQUIRED must carry:
what failed, what was believed to be causing it, the evidence for that, what was
tried, why each attempt failed, and what specifically is being asked of the
person. An escalation that cannot fill this in is an escalation that has not
done the work, and the loop refuses to send one.

**A memory.** :class:`IncidentHistory` reads what happened the last time this
fingerprint appeared. It is what stops the loop trying the strategy that already
failed twice, and what lets it say "this same failure resolved itself within
four minutes on each of the last three occasions" instead of waking somebody.

None of this decides *whether* JARVIS may act — that is
:class:`~openjarvis.reliability.policy.SafetyPolicy`, and it is untouched here.
Every safety gate applies exactly as before; this only governs how hard Sir
thinks before it reaches one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "STAGES",
    "AutonomyMetrics",
    "CauseClass",
    "Handover",
    "IncidentHistory",
    "Strategy",
    "STRATEGIES",
    "build_handover",
    "classify_cause",
    "next_strategy",
]


# ---------------------------------------------------------------------------
# The shape of the work
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stage:
    """One step of the playbook, in the order it is worked."""

    key: str
    title: str
    question: str


#: Written in order, and in the order they must happen. CONFIRM is first
#: because acting on a fault that has already gone away is how an assistant
#: turns a blip into an incident, a pull request and a phone call.
STAGES = (
    Stage("confirm", "Confirm", "Is this real, and is it still happening?"),
    Stage("classify", "Classify", "What kind of problem is it?"),
    Stage("investigate", "Investigate", "What does the evidence actually say?"),
    Stage("choose", "Choose a response", "What is the best available action?"),
    Stage("repair", "Repair", "Make the change, and verify it independently."),
    Stage(
        "alternatives",
        "Try alternatives",
        "The first idea failed. What is a *different* idea?",
    ),
)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class CauseClass:
    """What kind of problem this is. Strings, so they survive JSON."""

    TRANSIENT = "transient"
    LATENCY = "latency"
    EXTERNAL = "external"
    CONFIGURATION = "configuration"
    CODE = "code"
    INFRASTRUCTURE = "infrastructure"
    UNKNOWN = "unknown"


#: What each class means for autonomy. A class JARVIS cannot fix by writing code
#: is not a failure of the repair loop, and must not be reported as one.
_FIXABLE_BY_CODE = frozenset({CauseClass.CODE, CauseClass.CONFIGURATION})

_STATUS_HINTS = (
    (frozenset({502, 503, 504}), CauseClass.TRANSIENT),
    (frozenset({429}), CauseClass.EXTERNAL),
    (frozenset({401, 403}), CauseClass.CONFIGURATION),
    (frozenset({500}), CauseClass.CODE),
    (frozenset({404}), CauseClass.CODE),
)

_LATENCY_KINDS = frozenset({"slow", "slow_response", "budget_exceeded"})


def classify_cause(incident: Any, result: Any = None) -> str:
    """Name the kind of problem, from measurements rather than impressions.

    Deliberately conservative: anything this cannot place is ``UNKNOWN``, which
    reads correctly in a handover ("I could not classify this") and never lets a
    guess drive a decision.
    """
    kind = str(getattr(result, "failure_kind", "") or "")
    status = getattr(result, "http_status", None)
    steps = getattr(result, "steps_completed", None)

    if kind in _LATENCY_KINDS and steps and (status or 200) < 400:
        # It answered, correctly, slowly. Not an outage, and not something a
        # code change made by an agent at 3am should be attempted for.
        return CauseClass.LATENCY
    if kind in ("dns", "connection", "timeout", "network"):
        return CauseClass.INFRASTRUCTURE
    if isinstance(status, int):
        for codes, cause in _STATUS_HINTS:
            if status in codes:
                return cause
    if getattr(incident, "occurrences", 1) <= 1 and not kind:
        return CauseClass.TRANSIENT
    return CauseClass.UNKNOWN


def fixable_by_code(cause: str) -> bool:
    """Whether writing a diff is even the right kind of response."""
    return cause in _FIXABLE_BY_CODE


# ---------------------------------------------------------------------------
# Alternatives — the stage that was missing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Strategy:
    """A hypothesis, and how to act on it.

    ``guidance`` is appended to the repair brief. It is a *direction to look*,
    never an instruction to make a particular change: the agent still has to
    find and justify the real cause, and the verifier still decides whether it
    worked.
    """

    key: str
    hypothesis: str
    guidance: str


#: Ordered by how often each is the answer, cheapest first. The point is that
#: attempt two is a different *idea* from attempt one — repeating the first with
#: a longer prompt is what "gives up too easily" actually looked like from the
#: inside: three attempts, one hypothesis, no new information.
STRATEGIES = (
    Strategy(
        "recent_change",
        "a recent change broke it",
        "Start from what changed most recently in the correlated commits. If a "
        "commit is implicated, explain how, and fix that rather than working "
        "around its symptom.",
    ),
    Strategy(
        "data_shape",
        "the code is fine for the data it was written for and this input is different",
        "Look at the inputs and the data the failing path actually receives — "
        "an empty list, a null, a missing key, an unexpected type. The previous "
        "attempt assumed the logic was wrong; assume instead that the logic is "
        "right and its input is not what it expects.",
    ),
    Strategy(
        "dependency_or_config",
        "the code did not change and its environment did",
        "Look at configuration, environment variables, dependency versions and "
        "external responses rather than at application logic. Do not weaken "
        "authentication, authorisation or row-level security to make anything "
        "pass; if the fix appears to require that, stop and say so.",
    ),
    Strategy(
        "reproduce_first",
        "the failure is not what the probe says it is",
        "Do not change anything yet. Reproduce the failure locally and report "
        "precisely what you observe, including the case where you cannot "
        "reproduce it at all — that is a finding, not a failure.",
    ),
)


def next_strategy(
    attempted: Sequence[str], *, cause: str = CauseClass.UNKNOWN
) -> Optional[Strategy]:
    """The next untried idea, or ``None`` when they are exhausted.

    Exhausting them is a legitimate reason to stop, and — unlike "3 attempts
    failed" — it is one a person can read: every hypothesis JARVIS has was tried
    and named.
    """
    tried = {str(key) for key in attempted if key}
    if cause == CauseClass.CONFIGURATION:
        # Start where the evidence already points rather than at the top of a
        # list that was ordered for the general case.
        ordered = tuple(
            sorted(STRATEGIES, key=lambda s: s.key != "dependency_or_config")
        )
    else:
        ordered = STRATEGIES
    for strategy in ordered:
        if strategy.key not in tried:
            return strategy
    return None


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


@dataclass
class IncidentHistory:
    """What happened the last time this exact failure appeared.

    Reads through the store rather than caching, because the watcher, the
    dashboard and the CLI are separate processes and a cache would let them
    disagree about what has been tried.
    """

    store: Any

    def previous(self, incident: Any, *, limit: int = 20) -> List[Any]:
        """Earlier incidents with the same fingerprint, newest first."""
        fingerprint = getattr(incident, "fingerprint", "") or ""
        if not fingerprint or self.store is None:
            return []
        try:
            found = self.store.list_by_fingerprint(fingerprint, limit=limit)
        except Exception:  # noqa: BLE001 - history is an improvement, not a
            logger.exception("could not read incident history")  # ...dependency
            return []
        return [other for other in found if other.id != getattr(incident, "id", "")]

    def strategies_tried(self, incident: Any) -> List[str]:
        """Every strategy already attempted, here and in past occurrences.

        The whole point of the list: attempt four must not be attempt one with a
        different date on it.
        """
        keys: List[str] = []
        for source in [incident, *self.previous(incident)]:
            for attempt in getattr(source, "attempts", []) or []:
                key = str(getattr(attempt, "strategy", "") or "")
                if key and key not in keys:
                    keys.append(key)
        return keys

    def self_recovered(self, incident: Any) -> int:
        """How many past occurrences ended without anyone doing anything.

        The number that turns "I have escalated this" into "this has cleared
        itself the last four times; I am watching rather than waking you".
        """
        recovered = 0
        for other in self.previous(incident):
            resolution = getattr(other, "resolution", None)
            if getattr(resolution, "recovery_type", "") in ("self", "self_recovered"):
                recovered += 1
            elif not getattr(other, "attempts", None) and _is_resolved(other):
                recovered += 1
        return recovered

    def summary(self, incident: Any) -> str:
        """One line of history for a handover, or empty when there is none."""
        earlier = self.previous(incident)
        if not earlier:
            return ""
        recovered = self.self_recovered(incident)
        line = f"seen {len(earlier)} time(s) before"
        if recovered:
            line += f", {recovered} of which cleared without intervention"
        tried = self.strategies_tried(incident)
        if tried:
            line += f"; already tried: {', '.join(tried)}"
        return line


def _is_resolved(incident: Any) -> bool:
    state = getattr(incident, "state", None)
    return str(getattr(state, "value", state) or "") == "RESOLVED"


# ---------------------------------------------------------------------------
# Giving up, properly
# ---------------------------------------------------------------------------


@dataclass
class Handover:
    """What JARVIS owes a person when it stops.

    Six fields, because those are the six questions somebody woken at 3am asks
    in order. A handover missing any of them is not sent — see
    :meth:`is_complete`. That refusal is the mechanism: it is not possible to
    escalate vaguely, because the escalation will not assemble.
    """

    #: The observable fact. "Login returns 500", not "the auth incident".
    what_failed: str = ""
    #: The best current explanation, or an explicit statement that there is none.
    cause: str = ""
    #: Measurements and identifiers, one per line. Never prose.
    evidence: List[str] = field(default_factory=list)
    #: Each hypothesis attempted, in order.
    tried: List[str] = field(default_factory=list)
    #: Why each attempt did not work.
    why_failed: str = ""
    #: The specific thing being asked of the person.
    what_is_needed: str = ""
    #: Classification, for the Control Center and the metrics.
    cause_class: str = CauseClass.UNKNOWN
    #: One line of prior history, when there is any.
    history: str = ""

    def is_complete(self) -> bool:
        """Whether this says enough to be worth waking somebody for."""
        return bool(
            self.what_failed.strip()
            and self.cause.strip()
            and self.what_is_needed.strip()
            and (self.evidence or self.tried)
        )

    def missing(self) -> List[str]:
        """Which required parts are absent — for the log, when one is."""
        gaps = []
        if not self.what_failed.strip():
            gaps.append("what failed")
        if not self.cause.strip():
            gaps.append("the cause")
        if not self.what_is_needed.strip():
            gaps.append("what is needed")
        if not (self.evidence or self.tried):
            gaps.append("evidence or attempts")
        return gaps

    def render(self) -> str:
        """The note that goes on the incident, in the order a person reads."""
        lines = [f"**What failed.** {self.what_failed}", ""]
        lines += [f"**Cause.** {self.cause}", ""]
        if self.evidence:
            lines.append("**Evidence.**")
            lines += [f"- {item}" for item in self.evidence]
            lines.append("")
        if self.tried:
            lines.append("**What I tried.**")
            lines += [f"{n}. {item}" for n, item in enumerate(self.tried, 1)]
            lines.append("")
        if self.why_failed:
            lines += [f"**Why that did not work.** {self.why_failed}", ""]
        if self.history:
            lines += [f"**History.** {self.history}", ""]
        lines.append(f"**What I need from you.** {self.what_is_needed}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "what_failed": self.what_failed,
            "cause": self.cause,
            "cause_class": self.cause_class,
            "evidence": list(self.evidence),
            "tried": list(self.tried),
            "why_failed": self.why_failed,
            "what_is_needed": self.what_is_needed,
            "history": self.history,
            "complete": self.is_complete(),
        }


def build_handover(
    incident: Any,
    *,
    reason: str,
    max_attempts: int = 0,
    history: Optional[IncidentHistory] = None,
    result: Any = None,
) -> Handover:
    """Assemble a handover from what JARVIS already recorded.

    Everything here comes from stored facts — the incident, its evidence, its
    attempts and their verifications. Nothing is generated for the occasion,
    which is the same rule the notification copy follows and for the same
    reason: an explanation nobody checked is worse than no explanation, because
    it will be believed.
    """
    attempts = list(getattr(incident, "attempts", []) or [])
    cause_class = classify_cause(incident, result)

    what_failed = (
        getattr(incident, "title", "")
        or getattr(incident, "summary", "")
        or f"{getattr(incident, 'component', 'something')} is failing"
    )

    recorded_cause = str(
        getattr(getattr(incident, "resolution", None), "root_cause", "") or ""
    ).strip()
    if recorded_cause:
        cause = recorded_cause
    elif cause_class == CauseClass.UNKNOWN:
        cause = (
            "Not established. I could not classify this from the evidence "
            "available to me."
        )
    else:
        cause = f"Classified as {cause_class}, from the probe result and status code."

    evidence = _evidence_lines(incident, result)
    tried = [_describe_attempt(attempt) for attempt in attempts]
    why_failed = _why_failed(attempts, reason, max_attempts)
    history_line = history.summary(incident) if history is not None else ""

    return Handover(
        what_failed=what_failed,
        cause=cause,
        evidence=evidence,
        tried=tried,
        why_failed=why_failed,
        what_is_needed=_what_is_needed(reason, cause_class, attempts),
        cause_class=cause_class,
        history=history_line,
    )


def _evidence_lines(incident: Any, result: Any) -> List[str]:
    """Measurements, one per line, capped so a phone can hold it."""
    lines: List[str] = []
    component = getattr(incident, "component", "")
    if component:
        lines.append(f"component: {component}")
    status = getattr(result, "http_status", None)
    if status:
        lines.append(f"HTTP status: {status}")
    kind = getattr(result, "failure_kind", "")
    if kind:
        lines.append(f"failure kind: {kind}")
    occurrences = getattr(incident, "occurrences", 0)
    if occurrences:
        lines.append(f"seen {occurrences} time(s) since it opened")
    for item in list(getattr(incident, "evidence", []) or [])[:4]:
        label = getattr(item, "label", "") or getattr(item, "kind", "evidence")
        detail = str(getattr(item, "summary", "") or getattr(item, "content", ""))
        if detail:
            lines.append(f"{label}: {detail[:160]}")
    return lines[:10]


def _describe_attempt(attempt: Any) -> str:
    """One line naming the hypothesis and what came of it."""
    number = getattr(attempt, "number", 0)
    strategy = str(getattr(attempt, "strategy", "") or "")
    outcome = str(getattr(attempt, "outcome", "") or "no outcome recorded")
    hypothesis = next(
        (s.hypothesis for s in STRATEGIES if s.key == strategy),
        strategy or "no hypothesis recorded",
    )
    return f"attempt {number}: {hypothesis} — {outcome}"


def _why_failed(attempts: Sequence[Any], reason: str, max_attempts: int) -> str:
    """The honest summary of why each attempt did not work."""
    if not attempts:
        return reason
    parts = []
    for attempt in attempts:
        verification = getattr(attempt, "verification", None)
        summary = str(
            getattr(verification, "summary", "")
            or getattr(attempt, "test_summary", "")
            or getattr(attempt, "outcome", "")
        )
        if summary:
            parts.append(f"attempt {getattr(attempt, 'number', 0)}: {summary[:200]}")
    if not parts:
        return reason
    tail = ""
    if max_attempts and len(attempts) >= max_attempts:
        tail = (
            f" I am allowed {max_attempts} attempt(s) and have used them all, "
            "so I stopped rather than keep changing code."
        )
    return "; ".join(parts) + tail


def _what_is_needed(reason: str, cause_class: str, attempts: Sequence[Any]) -> str:
    """The specific ask. Never "please investigate"."""
    if "protected path" in reason or "security-sensitive" in reason:
        return (
            "A decision from you: the fix appears to need a change I am not "
            "allowed to make on my own. Review the branch and decide."
        )
    if cause_class == CauseClass.EXTERNAL:
        return (
            "Confirmation of whether the third-party service is degraded. There "
            "is no code change I can make that would fix this."
        )
    if cause_class == CauseClass.INFRASTRUCTURE:
        return (
            "A look at hosting or networking. This is not something a code "
            "change fixes, so I have not opened a pull request."
        )
    if not attempts:
        return (
            f"A look from you: {reason}. I have not changed any code, and "
            "production is untouched."
        )
    return (
        "A look at the branch from my last attempt, and a decision on whether "
        "my reading of the cause is right. I have stopped changing code."
    )


# ---------------------------------------------------------------------------
# Measuring autonomy
# ---------------------------------------------------------------------------


@dataclass
class AutonomyMetrics:
    """How often Sir actually handled things.

    Worth stating what this measures and what it does not. A high autonomy rate
    is not the goal — escalating a genuine outage is correct behaviour, and a
    system tuned to keep this number up would be a system that hides problems.
    It is here so that "Sir gives up too easily" becomes a number that can be
    watched instead of an impression, and so the opposite failure — Sir carrying
    on when it should have stopped — is equally visible.
    """

    store: Any

    def snapshot(self, *, limit: int = 200) -> Dict[str, Any]:
        try:
            incidents = list(self.store.list(limit=limit))
        except Exception:  # noqa: BLE001
            logger.exception("could not read incidents for autonomy metrics")
            return {"available": False}

        closed = [i for i in incidents if _is_terminal(i)]
        escalated = [i for i in closed if _needs_human(i)]
        repaired = [
            i for i in closed if _is_resolved(i) and getattr(i, "attempts", None)
        ]
        recovered = [
            i for i in closed if _is_resolved(i) and not getattr(i, "attempts", None)
        ]
        handled = len(closed) - len(escalated)
        with_handover = sum(
            1
            for i in escalated
            if (getattr(i, "metadata", {}) or {}).get("handover", {}).get("complete")
        )

        return {
            "available": True,
            "considered": len(incidents),
            "closed": len(closed),
            "handled_without_a_human": handled,
            "escalated": len(escalated),
            "repaired_by_jarvis": len(repaired),
            "recovered_on_their_own": len(recovered),
            "escalations_with_a_full_handover": with_handover,
            "autonomy_rate": round(handled / len(closed), 3) if closed else None,
        }


def _is_terminal(incident: Any) -> bool:
    return str(getattr(getattr(incident, "state", None), "value", "")) in (
        "RESOLVED",
        "HUMAN_REQUIRED",
        "FAILED",
        "ROLLED_BACK",
        "MERGED",
    )


def _needs_human(incident: Any) -> bool:
    return str(getattr(getattr(incident, "state", None), "value", "")) in (
        "HUMAN_REQUIRED",
        "FAILED",
        "ROLLED_BACK",
    )
