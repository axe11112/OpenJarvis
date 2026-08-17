"""Deterministic severity classification.

Severity decides who gets woken up and whether JARVIS is allowed to touch the
code, so it must not be a model's opinion. Every rule here is a plain predicate
over facts JARVIS observed: which component failed, what kind of failure it was,
what HTTP status came back, whether the site responded at all.

The declared severity on a probe spec is the *floor*. Observed impact can raise
it — a login probe declared HIGH that finds authentication completely
unavailable is CRITICAL — but nothing here lowers what the operator declared.
Raising-only is the safe direction: over-classifying costs a notification,
under-classifying costs an outage nobody was told about.

Rules are evaluated in order and the first match wins, so the most specific and
most severe conditions are listed first. Each carries the reason it fired, which
ends up in the incident record and the owner's notification — "CRITICAL" on its
own is not actionable; "CRITICAL: authentication is completely unavailable" is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence, Tuple

from openjarvis.reliability.types import ProbeResult, Severity

logger = logging.getLogger(__name__)

__all__ = [
    "Classification",
    "classify",
    "AUTHENTICATION_COMPONENTS",
    "CRITICAL_COMPONENTS",
]

#: Components whose total failure means nobody can use the product.
AUTHENTICATION_COMPONENTS = ("auth", "authentication", "login", "signin", "session")

#: Components on the critical path. Failure here is at least HIGH.
CRITICAL_COMPONENTS = (
    *AUTHENTICATION_COMPONENTS,
    "checkout",
    "payment",
    "billing",
    "signup",
    "registration",
    "api",
    "dashboard",
)

#: Failure kinds meaning the site did not respond at all, as opposed to
#: responding with something wrong.
_UNREACHABLE_KINDS = frozenset(
    {"navigation", "network_failure", "timeout", "dns", "connection"}
)

#: Failure kinds that are cosmetic until proven otherwise.
_COSMETIC_KINDS = frozenset({"visual", "layout", "screenshot_diff"})

#: Failures where the site answered correctly and only took too long. These are
#: emphatically not outages, and treating them as such is what produced two
#: CRITICAL incidents overnight for a homepage that returned 200 with the right
#: title in 9.65s against a 5s budget, and a login page that rendered the right
#: form in 41s against a 30s budget. Nothing was broken; the machine running
#: the probes was busy.
_LATENCY_KINDS = frozenset({"slow", "slow_response", "budget_exceeded"})

#: Rules whose verdict is *not* raised to the probe's declared severity.
#:
#: The floor exists because an operator knows things about their product that a
#: generic rule does not — when they declare a probe CRITICAL they mean "if this
#: workflow is broken, that is critical". A latency overrun is a different
#: observation entirely: the workflow ran and produced the right answer. Letting
#: the floor apply to it turns "the login page was slow" into "authentication is
#: down", which is how an owner gets woken for a page that was working.
_FLOOR_EXEMPT_RULES = frozenset({"responded_but_slow"})


@dataclass(slots=True)
class Classification:
    """A severity, and the deterministic rule that produced it."""

    severity: Severity
    rule: str
    reason: str
    declared: Severity = Severity.MEDIUM

    @property
    def escalated(self) -> bool:
        """Whether observed impact raised the probe's declared severity."""
        return self.severity.rank > self.declared.rank

    def to_dict(self) -> dict:
        """Serialize for the incident record."""
        return {
            "severity": self.severity.value,
            "rule": self.rule,
            "reason": self.reason,
            "declared": self.declared.value,
            "escalated": self.escalated,
        }


def _component_matches(component: str, needles: Sequence[str]) -> bool:
    lowered = (component or "").lower()
    return any(needle in lowered for needle in needles)


#: ``(name, predicate, severity, reason)``, most severe first.
#: The predicate takes ``(component, result)``.
_RULES: List[Tuple[str, Callable[[str, ProbeResult], bool], Severity, str]] = [
    (
        # First, because it is the cheapest way to be wrong about an outage.
        # A response that arrived, with the right content, over the budget is a
        # performance observation — never an availability one.
        "responded_but_slow",
        lambda _component, result: (
            result.failure_kind in _LATENCY_KINDS
            and bool(result.steps_completed)
            and (result.http_status or 200) < 400
        ),
        Severity.LOW,
        "the site answered correctly but took longer than its budget",
    ),
    (
        "auth_unavailable",
        lambda component, result: (
            _component_matches(component, AUTHENTICATION_COMPONENTS)
            and (
                result.failure_kind in _UNREACHABLE_KINDS
                or (result.http_status or 0) >= 500
            )
        ),
        Severity.CRITICAL,
        "authentication is completely unavailable",
    ),
    (
        "site_unreachable",
        lambda _component, result: (
            result.failure_kind in _UNREACHABLE_KINDS and not result.steps_completed
        ),
        Severity.CRITICAL,
        "the site did not respond at all",
    ),
    (
        "server_error_on_critical_path",
        lambda component, result: (
            _component_matches(component, CRITICAL_COMPONENTS)
            and (result.http_status or 0) >= 500
        ),
        Severity.CRITICAL,
        "a critical-path endpoint returned a server error",
    ),
    (
        "auth_workflow_broken",
        lambda component, _result: _component_matches(
            component, AUTHENTICATION_COMPONENTS
        ),
        Severity.HIGH,
        "an authentication workflow is broken",
    ),
    (
        "critical_path_broken",
        lambda component, _result: _component_matches(component, CRITICAL_COMPONENTS),
        Severity.HIGH,
        "a critical user workflow is broken",
    ),
    (
        "server_error",
        lambda _component, result: (result.http_status or 0) >= 500,
        Severity.HIGH,
        "an endpoint returned a server error",
    ),
    (
        "client_error",
        lambda _component, result: 400 <= (result.http_status or 0) < 500,
        Severity.MEDIUM,
        "an endpoint returned a client error",
    ),
    (
        "cosmetic",
        lambda _component, result: result.failure_kind in _COSMETIC_KINDS,
        Severity.LOW,
        "a visual or non-blocking issue",
    ),
]


def classify(
    *,
    component: str,
    result: ProbeResult,
    declared: Optional[Severity] = None,
) -> Classification:
    """Classify a probe failure deterministically.

    Parameters
    ----------
    component:
        The failing component, from the probe spec.
    result:
        What the probe observed.
    declared:
        The probe's declared severity, used as a floor.

    Returns
    -------
    Classification
        Never lower than *declared*.
    """
    floor = declared or Severity.MEDIUM

    for name, predicate, severity, reason in _RULES:
        try:
            matched = predicate(component, result)
        except Exception:  # pragma: no cover - a rule must never break detection
            logger.exception("severity rule %s raised; skipping it", name)
            continue
        if not matched:
            continue
        if name in _FLOOR_EXEMPT_RULES:
            # The declaration answers "how bad is it when this workflow breaks".
            # This rule fired because the workflow did *not* break, so the
            # declaration is not an answer to the question being asked.
            return Classification(
                severity=severity,
                rule=name,
                reason=reason,
                declared=floor,
            )
        if severity.rank >= floor.rank:
            return Classification(
                severity=severity, rule=name, reason=reason, declared=floor
            )
        # The rule matched but would lower the declared severity. The operator's
        # declaration wins: they know things about their product that a generic
        # rule does not.
        return Classification(
            severity=floor,
            rule=f"{name}+declared_floor",
            reason=f"{reason}; kept at the probe's declared severity",
            declared=floor,
        )

    return Classification(
        severity=floor,
        rule="declared",
        reason="no rule matched; using the probe's declared severity",
        declared=floor,
    )
