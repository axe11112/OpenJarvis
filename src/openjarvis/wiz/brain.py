"""The one place a request turns into an action.

Every request — typed into the dashboard, spoken, sent over Telegram, run from
the CLI — passes through :meth:`Wiz.handle`, and the order of the steps is the
security model:

.. code-block:: text

    request
      → intent classification      (deterministic; picks a *name*, not a callable)
      → capability lookup          (unknown name → refused, no handler exists)
      → availability check         (not configured → refused, and said honestly)
      → risk gate                  (HIGH never proceeds without approval)
      → authority decision         (deny-by-default, per channel)
      → handler                    (the only code that acts)
      → journal                    (what was decided, and why)

The property that matters is that a language model never reaches the handler.
A model may participate in the first step, where its entire influence is to
propose one string; that string must then name a verb that a human registered in
this table, and the verb's authority and risk are the ones declared at
registration rather than any the model suggested. There is no free-form command
surface, no ``exec``, no name-to-function resolution by attribute lookup. A
model that asks for ``feature.merge`` when no such verb is registered gets a
``LookupError``, not a merge.

The gates here are additional to the reliability interlocks, never instead of
them. A verb that reaches its handler still meets ``SafetyPolicy``,
``BoundaryGuard``, the SQL write guard and the merge status contract. This layer
can only ever say no earlier.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from openjarvis.wiz.authority import (
    Actor,
    AuthorityDecision,
    AuthorityPolicy,
    Channel,
)
from openjarvis.wiz.capabilities import (
    AUTONOMOUS_RISK,
    CapabilityRegistry,
    CapabilitySpec,
    Risk,
    UnknownCapability,
)
from openjarvis.wiz.journal import WizJournal

logger = logging.getLogger(__name__)

__all__ = ["Outcome", "Request", "Verb", "Wiz"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class Request:
    """Something the operator asked for."""

    text: str
    actor: Actor

    #: Structured arguments, when the caller already has them (the dashboard
    #: form, a CLI flag). Free text is not parsed into these by a model.
    arguments: Dict[str, Any] = field(default_factory=dict)

    #: Set when the operator has already approved this exact action through the
    #: Control Center. Only the Control Center may set it, and only for the
    #: action it actually showed them.
    approved: bool = False


@dataclass(frozen=True, slots=True)
class Outcome:
    """What Wiz did, or why it did not."""

    handled: bool
    capability: str
    message: str

    #: The handler's return value, when one ran.
    result: Any = None

    #: Present whenever an authority check was reached.
    decision: Optional[AuthorityDecision] = None

    def __bool__(self) -> bool:
        return self.handled


@dataclass(frozen=True, slots=True)
class Verb:
    """A registered action: a capability plus the code that performs it."""

    capability: str
    handler: Callable[[Request], Any]


class Wiz:
    """The assistant's dispatcher.

    Deliberately small. Everything it knows how to do arrives by
    :meth:`register`, which means the complete list of things Wiz can be made to
    do is the list of ``register`` calls in the codebase — greppable, reviewable,
    and impossible to extend at runtime by persuading a model.
    """

    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        policy: AuthorityPolicy,
        journal: Optional[WizJournal] = None,
        classifier: Optional[Callable[[Request], Optional[str]]] = None,
        clock: Callable[[], str] = _now,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._journal = journal
        self._classifier = classifier
        self._clock = clock
        self._verbs: Dict[str, Verb] = {}

    # -- registration ------------------------------------------------------

    def register(self, capability: str, handler: Callable[[Request], Any]) -> None:
        """Bind *handler* to an already-declared capability.

        The capability must exist in the registry first. Registering a handler
        for an undeclared name would create an action with no declared authority
        and no declared risk, which is the one thing this design is for.
        """
        spec = self._registry.get(capability)  # raises UnknownCapability
        if capability in self._verbs:
            raise ValueError(f"capability '{capability}' already has a handler")
        self._verbs[capability] = Verb(capability=spec.name, handler=handler)

    def verbs(self) -> List[str]:
        """Every capability that has a handler — the true action surface."""
        return sorted(self._verbs)

    # -- dispatch ----------------------------------------------------------

    def handle(self, request: Request, *, capability: Optional[str] = None) -> Outcome:
        """Run *request*, or explain why it will not run.

        Pass *capability* when the caller already knows the verb (a dashboard
        button). Leave it out to have the classifier choose one from the text.
        """
        name = capability or self._classify(request)
        if not name:
            return self._refuse(
                request,
                capability="",
                kind="intent.unrecognised",
                message=(
                    "I did not recognise that as something I know how to do. "
                    "Ask me what I can do and I will list it."
                ),
            )

        try:
            spec = self._registry.get(name)
        except UnknownCapability:
            # Reached when a classifier — or a model behind one — names
            # something that does not exist. Refused, and recorded, because a
            # request for a capability nobody built is worth knowing about.
            return self._refuse(
                request,
                capability=name,
                kind="capability.unknown",
                message=f"There is no capability called '{name}'.",
            )

        available = spec.availability()
        if not available.configured:
            return self._refuse(
                request,
                capability=name,
                kind="capability.unavailable",
                message=(
                    "I cannot do that here: "
                    f"{available.detail or 'it is not configured'}."
                ),
            )

        if spec.risk is Risk.HIGH and not request.approved:
            return self._refuse(
                request,
                capability=name,
                kind="risk.approval_required",
                message=(
                    f"That is a high-risk change ({spec.summary}). "
                    "I can prepare it, but I need your approval in Control Center "
                    "before I make it."
                ),
            )

        if spec.risk not in AUTONOMOUS_RISK and not request.approved:
            # Defensive: a risk level added later that nobody classified must
            # fail closed rather than inherit LOW's freedom.
            return self._refuse(
                request,
                capability=name,
                kind="risk.unclassified",
                message="That change is not classified as safe to make on my own.",
            )

        decision = self._policy.decide(request.actor, spec.authority, capability=name)
        if not decision.allowed:
            self._record(
                request,
                kind="authority.refused",
                capability=name,
                reason=decision.reason,
                detail={"required": spec.authority.value, "risk": spec.risk.value},
            )
            return Outcome(
                handled=False,
                capability=name,
                message=self._explain_refusal(spec, decision),
                decision=decision,
            )

        verb = self._verbs.get(name)
        if verb is None:
            # Declared but not wired up. Says so rather than implying refusal,
            # because the operator asked for something real that simply is not
            # finished, and "not built yet" and "not allowed" are different
            # sentences.
            return self._refuse(
                request,
                capability=name,
                kind="capability.unimplemented",
                message=f"'{name}' is declared but not implemented yet.",
            )

        self._record(
            request,
            kind="authority.granted",
            capability=name,
            reason=decision.reason,
            detail={"required": spec.authority.value, "risk": spec.risk.value},
        )

        try:
            result = verb.handler(request)
        except Exception as exc:
            # A failing verb is Wiz's problem, not the operator's, and it must
            # not take the process down — reliability shares this interpreter.
            logger.exception("capability '%s' failed", name)
            self._record(
                request,
                kind="capability.failed",
                capability=name,
                reason=str(exc),
            )
            return Outcome(
                handled=False,
                capability=name,
                message=f"I tried to do that and it failed: {exc}",
                decision=decision,
            )

        return Outcome(
            handled=True,
            capability=name,
            message="",
            result=result,
            decision=decision,
        )

    # -- helpers -----------------------------------------------------------

    def _classify(self, request: Request) -> Optional[str]:
        if self._classifier is None:
            return None
        name = self._classifier(request)
        if name is None:
            return None
        # The classifier's only power is to name something. Whether that name
        # means anything is decided by the registry, not by the classifier.
        return str(name)

    def _explain_refusal(
        self, spec: CapabilitySpec, decision: AuthorityDecision
    ) -> str:
        """A refusal the operator can act on, without security jargon."""
        if decision.actor.channel in (Channel.VOICE, Channel.TELEGRAM):
            return (
                f"I am not able to do that from {decision.actor.channel.value}. "
                "It needs Control Center."
            )
        return f"I am not authorised to do that: {decision.reason}."

    def _refuse(
        self,
        request: Request,
        *,
        capability: str,
        kind: str,
        message: str,
    ) -> Outcome:
        self._record(request, kind=kind, capability=capability, reason=message)
        return Outcome(handled=False, capability=capability, message=message)

    def _record(
        self,
        request: Request,
        *,
        kind: str,
        capability: str,
        reason: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self._journal is None:
            return
        try:
            self._journal.record(
                at=self._clock(),
                kind=kind,
                capability=capability,
                actor_id=request.actor.actor_id,
                channel=request.actor.channel.value,
                reason=reason,
                detail=detail or {},
            )
        except Exception:
            # Failing to journal must not fail the request, but it must be
            # visible: an audit trail with silent holes is worse than none.
            logger.exception("could not write to the Wiz journal")
