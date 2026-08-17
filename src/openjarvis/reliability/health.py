"""Health states — the vocabulary that keeps JARVIS honest.

The single most dangerous thing a monitoring system can do is report green when
it did not actually look. Six states exist so that "we checked and it is fine"
can never be confused with "we could not check":

``HEALTHY``
    The check ran and passed.
``DEGRADED``
    The check ran; some capabilities work and some do not.
``FAILED``
    The check ran and the thing is broken.
``UNKNOWN``
    The check was attempted but could not reach a verdict — a 403, a timeout,
    a permission the token does not have.
``NOT_CONFIGURED``
    No credentials or identifiers were supplied, so nothing was attempted.
``NOT_CHECKED``
    Deliberately skipped this run.

Only ``HEALTHY`` counts as good news, and only ``FAILED`` justifies an incident.
Everything else is a statement about JARVIS's own blind spots, and is reported
as such rather than being rounded to the nearest traffic light.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List

__all__ = ["CheckResult", "HealthState", "aggregate", "worst"]


class HealthState(str, Enum):
    """The result of looking at something."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    #: The host could not be reached at all — DNS, a firewall, or an egress
    #: proxy refusing CONNECT. Distinguished from UNKNOWN because the two need
    #: different actions: UNKNOWN usually means a missing scope or an ambiguous
    #: response, BLOCKED means *this network cannot get there*, and no amount of
    #: fixing credentials will help. Distinguished from FAILED because the
    #: target is very probably fine — JARVIS is the one that cannot see.
    BLOCKED = "BLOCKED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    NOT_CHECKED = "NOT_CHECKED"

    @property
    def icon(self) -> str:
        """Terminal indicator."""
        return {
            HealthState.HEALTHY: "🟢",
            HealthState.DEGRADED: "🟡",
            HealthState.FAILED: "🔴",
            HealthState.UNKNOWN: "⚪",
            HealthState.BLOCKED: "🚫",
            HealthState.NOT_CONFIGURED: "⚫",
            HealthState.NOT_CHECKED: "⚫",
        }[self]

    @property
    def is_good_news(self) -> bool:
        """``True`` only for HEALTHY.

        Deliberately narrow. ``UNKNOWN`` is not good news, and a caller that
        wants to treat it as such has to say so explicitly.
        """
        return self is HealthState.HEALTHY

    @property
    def was_checked(self) -> bool:
        """Whether a verdict was actually reached."""
        return self in (
            HealthState.HEALTHY,
            HealthState.DEGRADED,
            HealthState.FAILED,
        )

    @property
    def justifies_incident(self) -> bool:
        """Only a real observed failure opens an incident.

        A missing credential is JARVIS's problem, not the site's, and must
        never be recorded as a production failure.
        """
        return self is HealthState.FAILED


#: Order used when combining states. A single FAILED dominates; NOT_CONFIGURED
#: ranks above HEALTHY so an unconfigured integration cannot be averaged away
#: into a green overall result.
_SEVERITY_ORDER: List[HealthState] = [
    HealthState.NOT_CHECKED,
    HealthState.HEALTHY,
    HealthState.NOT_CONFIGURED,
    HealthState.UNKNOWN,
    HealthState.BLOCKED,
    HealthState.DEGRADED,
    HealthState.FAILED,
]


def worst(states: List[HealthState]) -> HealthState:
    """Return the most concerning state in *states*."""
    if not states:
        return HealthState.NOT_CHECKED
    return max(states, key=_SEVERITY_ORDER.index)


@dataclass
class CheckResult:
    """The outcome of one check, with room for sub-checks.

    ``capabilities`` is what makes partial permissions expressible: a GitHub
    token that can read commits but not Actions produces a ``DEGRADED`` parent
    with a ``HEALTHY`` commits capability and an ``UNKNOWN`` actions capability,
    rather than an all-or-nothing verdict or an exception.
    """

    name: str
    state: HealthState = HealthState.NOT_CHECKED
    summary: str = ""
    detail: str = ""
    capabilities: Dict[str, "CheckResult"] = field(default_factory=dict)
    facts: Dict[str, Any] = field(default_factory=dict)
    remediation: str = ""
    duration_seconds: float = 0.0

    def add(self, capability: "CheckResult") -> "CheckResult":
        """Attach a sub-check."""
        self.capabilities[capability.name] = capability
        return capability

    def derive_state(self) -> HealthState:
        """Set this check's state from its capabilities and return it.

        A mix of working and non-working capabilities is ``DEGRADED`` — the
        state that exists precisely so a partially-scoped token reads as what
        it is.
        """
        if not self.capabilities:
            return self.state
        states = [c.state for c in self.capabilities.values()]
        checked = [s for s in states if s.was_checked]

        if any(s is HealthState.FAILED for s in states):
            self.state = HealthState.FAILED
        elif all(s is HealthState.NOT_CONFIGURED for s in states):
            self.state = HealthState.NOT_CONFIGURED
        elif checked and any(
            s in (HealthState.UNKNOWN, HealthState.NOT_CONFIGURED) for s in states
        ):
            # Some of it worked and some of it did not: that is degraded, and
            # saying "healthy" here would be the exact lie this module exists
            # to prevent.
            self.state = HealthState.DEGRADED
        elif not checked:
            self.state = worst(states)
        else:
            self.state = worst(checked)
        return self.state

    @property
    def unchecked_capabilities(self) -> List[str]:
        """Names of capabilities that produced no verdict.

        Surfaced in every report so a reader can see the blind spots without
        reading the whole tree.
        """
        return sorted(
            name for name, c in self.capabilities.items() if not c.state.was_checked
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "name": self.name,
            "state": self.state.value,
            "summary": self.summary,
            "detail": self.detail,
            "facts": dict(self.facts),
            "remediation": self.remediation,
            "duration_seconds": round(self.duration_seconds, 3),
            "capabilities": {
                name: c.to_dict() for name, c in self.capabilities.items()
            },
        }

    @classmethod
    def not_configured(
        cls, name: str, *, missing: str, remediation: str = ""
    ) -> "CheckResult":
        """Build a NOT_CONFIGURED result naming what is missing."""
        return cls(
            name=name,
            state=HealthState.NOT_CONFIGURED,
            summary=f"not configured: {missing}",
            remediation=remediation,
        )

    @classmethod
    def unknown(cls, name: str, *, reason: str, remediation: str = "") -> "CheckResult":
        """Build an UNKNOWN result explaining why no verdict was reached."""
        return cls(
            name=name,
            state=HealthState.UNKNOWN,
            summary=f"could not determine: {reason}",
            remediation=remediation,
        )


def aggregate(checks: List[CheckResult]) -> CheckResult:
    """Combine top-level checks into one overall verdict."""
    overall = CheckResult(name="overall")
    for check in checks:
        overall.capabilities[check.name] = check
    overall.derive_state()

    checked = [c.name for c in checks if c.state.was_checked]
    blind = [c.name for c in checks if not c.state.was_checked]
    parts = []
    if checked:
        parts.append(f"checked: {', '.join(sorted(checked))}")
    if blind:
        parts.append(f"NOT checked: {', '.join(sorted(blind))}")
    overall.summary = "; ".join(parts) or "nothing checked"
    return overall
