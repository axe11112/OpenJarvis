"""What, exactly, is being asked of the owner.

"The website needs you" is not a request. Neither is "I could not fix it".
Both are true, both are useless at three in the morning, and both are what a
system produces when the decision to escalate is separated from the question of
what the escalation is *for*.

This module joins them back together. An escalation assembles an
:class:`OwnerAsk` first, and the ask carries one field the rest of the system
treats as a gate: :attr:`OwnerAsk.action`, the specific thing the operator must
do. If that field is empty there is no escalation — not a vaguer one, none.
The problem stays open, the investigation continues, and the Control Center
shows it parked with the reason it could not be handed over.

That inverts the usual failure mode. A system that escalates whenever it is
stuck sends its most alarming message at the moment it has least to say. A
system that escalates only when it has an ask stays quiet through the part
where it is still working, and speaks when there is a decision only a person
can make.

The asks themselves are a closed table, keyed on the reason the loop recorded.
None of them is generated: an operator action invented for the occasion is a
sentence nobody checked, pointing at a control that may not exist. Where the
table has no entry the ask is empty by design, and the outcome is silence
rather than an improvised one.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

from openjarvis.reliability.types import Incident, Severity

logger = logging.getLogger(__name__)

__all__ = [
    "OwnerAsk",
    "build_owner_ask",
    "last_good_deployment",
    "owner_subjects",
]


# ---------------------------------------------------------------------------
# The ask
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnerAsk:
    """A structured escalation, or an explicit statement that there is none.

    The five descriptive fields exist so the Control Center can show the whole
    picture and the phone message can be two lines of it. :attr:`action` is the
    one that decides whether anything is sent at all.
    """

    #: The observable fact, in the owner's words. "Login is unavailable".
    what_failed: str = ""
    #: Established or likely cause, or empty when nothing was established.
    cause: str = ""
    #: Measurements and identifiers. Never prose, never secrets.
    evidence: Tuple[str, ...] = ()
    #: What JARVIS attempted, in order.
    tried: Tuple[str, ...] = ()
    #: Why it cannot safely continue on its own.
    why_blocked: str = ""
    #: **The gate.** The exact operator action required. Empty means: do not
    #: notify. Not "notify with less detail" — do not notify.
    action: str = ""
    #: One identifier line for the message: a PR number or an incident id.
    reference: str = ""
    #: Why no action could be named, when none could. For the Control Center.
    parked_reason: str = ""
    #: The outage this ask is about, when correlation established one.
    outage_key: str = ""
    #: Every component implicated, so one message can name all of them.
    subjects: Tuple[str, ...] = ()

    @property
    def actionable(self) -> bool:
        """Whether there is a specific thing for the owner to do."""
        return bool(self.action.strip())

    def digest(self) -> str:
        """A stable hash of *what is being asked*. The action, and nothing else.

        Everything else was tried and everything else was wrong. Including the
        attempt count re-sent on every retry. Including the internal reason
        re-sent on every state change. Including the affected components —
        which looks right until a fourth probe joins an outage the owner has
        already been asked about, the subject line grows from "login" to
        "login and sign-up", and they get the same request twice for the same
        problem. A probe joining an outage is not a new thing to do.

        The outage identity is carried separately, by the ledger key, so two
        genuinely different problems that happen to need the same action are
        still two messages. What this hash answers is narrower and is the only
        question that matters here: is JARVIS asking for something new?
        """
        material = self.action.strip().lower()
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize, for the Control Center and the audit log."""
        return {
            "what_failed": self.what_failed,
            "cause": self.cause,
            "evidence": list(self.evidence),
            "tried": list(self.tried),
            "why_blocked": self.why_blocked,
            "action": self.action,
            "reference": self.reference,
            "actionable": self.actionable,
            "parked_reason": self.parked_reason,
            "outage_key": self.outage_key,
            "subjects": list(self.subjects),
            "digest": self.digest(),
        }


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------


#: ``(reason substring, builder)``, most specific first.
#:
#: A builder returns the operator action, or ``""`` when this reason does not
#: by itself give the owner anything to do. The second case is not a gap to be
#: filled in later — it is the answer for a class of situations where JARVIS
#: being stuck is JARVIS's problem, not the owner's, until something changes.
def _revert_action(context: "_Context") -> str:
    reference = context.pr_reference or context.deployment_reference
    if reference:
        return (
            f"Revert {reference} or roll production back to the previous "
            "deployment, then tell me to continue."
        )
    return "Roll production back to the previous deployment, then tell me to continue."


def _protected_path_action(context: "_Context") -> str:
    paths = ", ".join(context.protected_paths[:3])
    where = f" ({paths})" if paths else ""
    return (
        f"Apply the change to the protected files yourself{where}, or add them "
        "to reliability.policy.protected_paths' allow-list and tell me to "
        "continue."
    )


def _secret_action(context: "_Context") -> str:
    return (
        "Rotate the credential the change would have committed, then tell me "
        "to continue. I have not pushed anything."
    )


def _scope_action(context: "_Context") -> str:
    return (
        "Approve the larger change in Control Center, or make it yourself. It "
        "exceeds the size I am allowed to make unattended."
    )


def _security_action(context: "_Context") -> str:
    return (
        "Review the refused change in Control Center and approve or reject it. "
        "I will not push a change my own safety checks declined."
    )


def _repair_disabled_action(context: "_Context") -> str:
    return (
        'Reply "Fix it" to let me repair this, or fix it yourself. '
        "Automatic repair is switched off, so nothing is happening until one "
        "of those."
    )


def _stopped_action(context: "_Context") -> str:
    """The last-resort ask, and the one that has to be got right.

    An exhausted repair loop is not, on its own, a request. "I could not fix
    it" is a status: it tells the owner something is wrong and gives them
    nothing to do with it, which is the message that trained them to stop
    reading. So this refuses to send one — *unless* it can name a decision.

    Two ways it can. When a previous working deployment is recorded, the
    decision is concrete: put that back, or let JARVIS keep going. When one is
    not, but the outage is genuinely taking the site away from users, the
    decision is still real even without the SHA — roll the current deployment
    back, or take it over — and refusing to ask it would be hiding an outage
    rather than reducing noise, which is the one trade this module must never
    make.

    Everything else — a MEDIUM contract failure on one page, a slow probe, a
    database blip JARVIS is still working through — returns empty and parks.
    """
    if context.last_good_deployment:
        return (
            "Decide whether to roll production back to "
            f"{context.last_good_deployment} or let me keep trying. I have "
            "stopped making changes."
        )
    if context.owner_impacting:
        return (
            "Decide whether to roll the current deployment back or take this "
            "over. I have stopped making changes and nothing else will happen "
            "until you do."
        )
    return ""


_ASKS: Tuple[Tuple[str, Any], ...] = (
    ("post-merge", _revert_action),
    ("production did not verify", _revert_action),
    ("still fails in production", _revert_action),
    ("protected path", _protected_path_action),
    ("security-sensitive", _protected_path_action),
    ("secret", _secret_action),
    ("credential", _secret_action),
    ("security check", _security_action),
    ("refused by", _security_action),
    ("scope", _scope_action),
    ("too large", _scope_action),
    ("repair is disabled", _repair_disabled_action),
    ("not allowed to repair", _repair_disabled_action),
    ("disabled", _repair_disabled_action),
    # Everything below is where a system that narrates would escalate and this
    # one does not. Each returns an ask only when the evidence supplies one.
    ("attempts", _stopped_action),
    ("could not fix", _stopped_action),
    ("exhausted", _stopped_action),
    ("still unresolved", _stopped_action),
)

#: Reasons that never produce an owner ask, whatever else is true.
#:
#: A flapping check is a statement about the monitor. An interrupted repair
#: recovers itself or waits in ``RECOVERY_REQUIRED`` for somebody at a screen.
#: Neither is a thing to wake a person for, and both used to.
_NEVER_ASK = (
    ("flapping", "the check is unreliable, which is a monitoring problem"),
    ("interrupted", "an interrupted repair is parked for review, not escalated"),
    (
        "latency",
        "a latency-only failure is not corroborated as a production outage",
    ),
    (
        "observer",
        "the observing machine looked degraded, so this is not owner-facing",
    ),
)


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------


@dataclass
class _Context:
    """Everything the ask builders are allowed to read. Stored facts only."""

    incident: Incident
    reason: str
    pr_reference: str = ""
    deployment_reference: str = ""
    protected_paths: List[str] = field(default_factory=list)
    last_good_deployment: str = ""
    #: Whether users are currently unable to use the site because of this.
    #: Decides whether an exhausted repair is a parked incident or a decision
    #: the owner has to make. See :func:`_stopped_action`.
    owner_impacting: bool = False


def last_good_deployment(incident: Incident) -> str:
    """The most recent deployment known to have worked, when one is recorded.

    Read from stored facts only. Returns ``""`` when nothing established one,
    and that emptiness is load-bearing: it is what turns "I could not fix it"
    from an escalation into a parked incident.
    """
    metadata = getattr(incident, "metadata", None) or {}
    for key in (
        "last_good_deployment",
        "previous_deployment",
        "last_known_good_sha",
        "rollback_target",
    ):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value[:40]
    return ""


def _pr_reference(incident: Incident) -> str:
    resolution = getattr(incident, "resolution", None)
    url = str(getattr(resolution, "pr_url", "") or "")
    match = re.search(r"/pull/(\d+)", url)
    if match:
        return f"PR #{match.group(1)}"
    number = int(getattr(getattr(incident, "correlation", None), "pr_number", 0) or 0)
    return f"PR #{number}" if number else ""


def _deployment_reference(incident: Incident) -> str:
    from openjarvis.reliability.outage import deployment_identity

    identity = deployment_identity(incident)
    return f"deployment {identity[:12]}" if identity else ""


def _protected_paths(incident: Incident, reason: str) -> List[str]:
    """Paths named by the refusal, when it named any."""
    metadata = getattr(incident, "metadata", None) or {}
    recorded = metadata.get("protected_paths") or metadata.get("refused_paths")
    if isinstance(recorded, (list, tuple)):
        return [str(p) for p in recorded][:5]
    found = re.findall(r"[\w./-]+\.(?:ts|tsx|js|jsx|py|sql|json|toml|ya?ml)", reason)
    return found[:5]


def _handover(incident: Incident) -> Dict[str, Any]:
    handover = (getattr(incident, "metadata", None) or {}).get("handover")
    return handover if isinstance(handover, dict) else {}


def _clean_cause(text: str) -> str:
    cause = str(text or "").strip()
    if not cause or cause.lower().startswith("not established"):
        return ""
    first = cause.split(". ")[0].strip().rstrip(".")
    return first if first and len(first) <= 180 else ""


def _what_failed(incident: Incident, subjects: Sequence[str]) -> str:
    """One sentence naming what the owner cannot use right now."""
    from openjarvis.reliability.notify import plain_subject

    names = [str(s).strip() for s in subjects if str(s).strip()]
    if len(names) > 1:
        # Only the first keeps its capital: "Login, sign-up and the website"
        # is how a person writes a list, and every other rendering looks like
        # a machine reading out a table of components.
        rendered = _join_english(
            [names[0], *(n[:1].lower() + n[1:] for n in names[1:])]
        )
        return f"{rendered} are unavailable"
    return f"{names[0] if names else plain_subject(incident)} is unavailable"


def _join_english(items: Sequence[str]) -> str:
    """``"a, b and c"`` — the way a person would say a list."""
    values = [str(i) for i in items if str(i).strip()]
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    return ", ".join(values[:-1]) + f" and {values[-1]}"


def _evidence_lines(incident: Incident, outage: Any) -> Tuple[str, ...]:
    """Measurements and identifiers, from what was stored."""
    lines: List[str] = []
    metadata = getattr(incident, "metadata", None) or {}
    if incident.component:
        lines.append(f"component: {incident.component}")
    kind = str(metadata.get("failure_kind") or "")
    if kind:
        lines.append(f"failure kind: {kind}")
    status = metadata.get("http_status")
    if status:
        lines.append(f"HTTP status: {status}")
    if incident.occurrences:
        lines.append(f"seen {incident.occurrences} time(s) since it opened")
    if outage is not None:
        probes = list(getattr(outage, "probes", []) or [])
        if len(probes) > 1:
            lines.append(f"failing probes: {', '.join(probes[:6])}")
        deployment = str(getattr(outage, "deployment", "") or "")
        if deployment:
            lines.append(f"deployment: {deployment[:20]}")
    handover = _handover(incident)
    for line in list(handover.get("evidence") or [])[:4]:
        text = str(line).strip()
        if text and text not in lines:
            lines.append(text[:180])
    return tuple(lines[:10])


def _tried_lines(incident: Incident) -> Tuple[str, ...]:
    handover = _handover(incident)
    recorded = [str(t)[:180] for t in (handover.get("tried") or []) if str(t).strip()]
    if recorded:
        return tuple(recorded[:6])
    attempts = list(getattr(incident, "attempts", []) or [])
    return tuple(
        f"attempt {getattr(a, 'number', i + 1)}: "
        f"{str(getattr(a, 'outcome', '') or 'no outcome recorded')[:120]}"
        for i, a in enumerate(attempts[:6])
    )


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


def build_owner_ask(
    incident: Incident,
    *,
    reason: str,
    outage: Any = None,
    attempts: int = 0,
    max_attempts: int = 0,
) -> OwnerAsk:
    """Assemble what is being asked of the owner, or establish that nothing is.

    Parameters
    ----------
    incident:
        The incident being escalated. Read-only.
    reason:
        The internal reason the loop recorded. Matched against the ask table.
    outage:
        The correlated :class:`~openjarvis.reliability.outage.Outage`, when one
        exists. Supplies the other affected components so one message can name
        all of them.
    attempts, max_attempts:
        Recorded in ``why_blocked``. Never in the ask itself — how many times
        JARVIS tried is not something the owner does anything with.
    """
    lowered = str(reason or "").lower()
    subjects = owner_subjects(incident, outage)

    context = _Context(
        incident=incident,
        reason=lowered,
        pr_reference=_pr_reference(incident),
        deployment_reference=_deployment_reference(incident),
        protected_paths=_protected_paths(incident, str(reason or "")),
        last_good_deployment=last_good_deployment(incident),
        owner_impacting=_owner_impacting(incident, outage),
    )

    handover = _handover(incident)
    cause = _clean_cause(handover.get("cause") or "") or _clean_cause(
        getattr(getattr(incident, "resolution", None), "root_cause", "") or ""
    )
    if not cause and outage is not None and getattr(outage, "deployment", ""):
        cause = (
            f"the current site deployment ({str(outage.deployment)[:12]}) is failing"
        )

    base = dict(
        what_failed=_what_failed(incident, subjects),
        cause=cause,
        evidence=_evidence_lines(incident, outage),
        tried=_tried_lines(incident),
        why_blocked=_why_blocked(reason, attempts, max_attempts),
        reference=context.pr_reference
        or (f"Incident {incident.id}" if incident.id else ""),
        outage_key=str(getattr(outage, "key", "") or ""),
        subjects=tuple(subjects),
    )

    for needle, why in _NEVER_ASK:
        if needle in lowered:
            return OwnerAsk(**base, action="", parked_reason=why)

    for needle, builder in _ASKS:
        if needle in lowered:
            action = str(builder(context) or "").strip()
            if action:
                return OwnerAsk(**base, action=action)
            return OwnerAsk(
                **base,
                action="",
                parked_reason=(
                    "there is no specific action for you yet: no previous "
                    "working deployment is recorded to roll back to"
                ),
            )

    return OwnerAsk(
        **base,
        action="",
        parked_reason=(
            f"no owner action is defined for this reason ({reason.strip()[:120]})"
        ),
    )


def owner_subjects(incident: Incident, outage: Any) -> List[str]:
    """Plain-English names of everything this outage takes down."""
    from openjarvis.reliability.notify import plain_subject

    components = list(getattr(outage, "components", []) or []) if outage else []
    if not components:
        return [plain_subject(incident)]

    names: List[str] = []
    for component in components:
        stand_in = Incident(
            fingerprint="",
            severity=incident.severity,
            component=str(component),
            title="",
        )
        name = plain_subject(stand_in)
        if name not in names:
            names.append(name)
    return names


def _owner_impacting(incident: Incident, outage: Any) -> bool:
    """Whether this is currently costing the owner's users the site.

    Narrow, and narrow in a specific direction. It takes a severity of HIGH or
    above and a user-facing surface. What it deliberately does *not* require is
    positive proof of unavailability: a failure whose kind was never recorded
    is one nobody has shown to be harmless, and treating "unknown" as "fine"
    is how a genuine outage would end up parked in silence — the one mistake
    this whole change must not make while removing noise.

    A recorded *contract* failure is the exception, because it carries that
    proof the other way: the page served, and the complaint is about what it
    said. That is a bug, and bugs wait for the pull request.
    """
    from openjarvis.reliability.outage import failure_shape

    if not incident.severity.at_least(Severity.HIGH):
        return False
    family = str(getattr(outage, "family", "") or "")
    if family == "site_availability":
        return True
    if family and not family.startswith("site_"):
        return False
    return failure_shape(incident) != "contract"


def _why_blocked(reason: str, attempts: int, max_attempts: int) -> str:
    """Why JARVIS cannot safely continue, in one line."""
    text = str(reason or "").strip()
    if attempts and max_attempts:
        return f"{text} (after {attempts} of {max_attempts} attempts)"
    if attempts:
        return f"{text} (after {attempts} attempt(s))"
    return text
