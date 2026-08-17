"""The capability registry — what Wiz can do, and what each thing costs.

A capability is a named, declared ability: ``feature.plan``, ``incident.read``,
``feature.merge``. Each one states the authority it requires and the product
risk it carries, and each one can say whether it is actually *configured* on
this machine right now.

Three separate questions, deliberately kept apart:

===================  ==========================================================
Does it exist?       Is the capability registered at all?
Is it configured?    Are the credentials, binaries and services it needs
                     present? A capability that needs the ``claude`` CLI is not
                     available on a machine without it.
Is it authorised?    Does :mod:`openjarvis.wiz.authority` permit this actor to
                     exercise it? Answered elsewhere, on purpose.
===================  ==========================================================

The brief is blunt about the first two: *"Never claim a capability that isn't
actually configured"* and *"Never pretend."* So availability is a live check
against the machine rather than a boolean somebody set once. When Wiz says it
can build a feature, that sentence is backed by a ``shutil.which``.

The registry is also the reason a model cannot invent an action. Dispatch
happens by looking a name up in this table; a name that is not registered has
no handler, no authority and no path to execution. Being able to *say*
``feature.merge`` is not the same as there being such a thing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Iterable, List, Optional

from openjarvis.wiz.authority import Authority

logger = logging.getLogger(__name__)

__all__ = [
    "Availability",
    "CapabilityRegistry",
    "CapabilitySpec",
    "Risk",
    "UnknownCapability",
]


class Risk(str, Enum):
    """How much damage getting this wrong could do to the product.

    Risk is about the *change*, authority is about the *requester*. A copy edit
    requested from the dashboard and a copy edit requested by voice carry the
    same risk and different authority; an authentication change and a copy edit
    requested from the same place carry the same authority and different risk.
    Both have to be satisfied.
    """

    #: Copy, styling, presentational and additive UI that touches no data path.
    LOW = "LOW"

    #: New application behaviour, new components, endpoints that write nothing
    #: sensitive.
    MEDIUM = "MEDIUM"

    #: Authentication, authorisation, payments, health and biometric data,
    #: destructive actions, schema, RLS, migrations, secrets, security policy.
    #: Never autonomous, whatever the authority says.
    HIGH = "HIGH"


#: Risk levels that may proceed without an explicit human approval, subject to
#: authority. ``HIGH`` is absent and is meant to stay absent: §11 of the brief
#: requires explicit operator approval for it, and the way to guarantee that is
#: to leave it out of the set rather than to remember to check for it.
AUTONOMOUS_RISK = frozenset({Risk.LOW, Risk.MEDIUM})


@dataclass(frozen=True, slots=True)
class Availability:
    """Whether a capability can actually run here, and why not if it cannot."""

    configured: bool
    detail: str = ""

    def __bool__(self) -> bool:
        return self.configured

    @classmethod
    def ready(cls, detail: str = "") -> "Availability":
        return cls(configured=True, detail=detail)

    @classmethod
    def missing(cls, detail: str) -> "Availability":
        return cls(configured=False, detail=detail)


class UnknownCapability(LookupError):
    """Raised when something asks for a capability that does not exist.

    Deliberately an error rather than a falsy return. An unknown capability is
    not a "no" to be handled; it means a caller — possibly a model — named
    something that was never built, and the caller should stop.
    """


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """One declared ability of Wiz's."""

    name: str
    summary: str
    authority: Authority
    risk: Risk = Risk.LOW

    #: Called to decide whether the capability is usable on this machine.
    #: Absent means "nothing external is required", which is itself a claim and
    #: should only be used when it is true.
    probe: Optional[Callable[[], Availability]] = None

    def availability(self) -> Availability:
        """Ask the machine, not the configuration file."""
        if self.probe is None:
            return Availability.ready()
        try:
            return self.probe()
        except Exception as exc:  # a broken probe is a missing capability
            logger.warning(
                "availability probe for capability '%s' raised %s; "
                "treating the capability as unavailable",
                self.name,
                exc,
            )
            return Availability.missing(f"availability check failed: {exc}")

    def to_dict(self) -> Dict[str, object]:
        available = self.availability()
        return {
            "name": self.name,
            "summary": self.summary,
            "authority": self.authority.value,
            "risk": self.risk.value,
            "configured": available.configured,
            "detail": available.detail,
        }


class CapabilityRegistry:
    """The complete set of things Wiz is able to be asked to do."""

    def __init__(self, specs: Iterable[CapabilitySpec] = ()) -> None:
        self._specs: Dict[str, CapabilitySpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: CapabilitySpec) -> None:
        """Add *spec*, refusing to shadow an existing name.

        Silent replacement would let a later-imported module redefine a
        capability's authority or risk, which is exactly the kind of quiet
        privilege change this package exists to prevent.
        """
        if spec.name in self._specs:
            raise ValueError(f"capability '{spec.name}' is already registered")
        self._specs[spec.name] = spec

    def get(self, name: str) -> CapabilitySpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise UnknownCapability(f"no capability named '{name}'") from exc

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __len__(self) -> int:
        return len(self._specs)

    def names(self) -> List[str]:
        return sorted(self._specs)

    def all(self) -> List[CapabilitySpec]:
        return [self._specs[name] for name in self.names()]

    def configured(self) -> List[CapabilitySpec]:
        """Only what genuinely works here — the honest answer to "what can you do?"."""
        return [spec for spec in self.all() if spec.availability().configured]

    def describe(self) -> List[Dict[str, object]]:
        """A JSON-safe inventory for the dashboard and the CLI."""
        return [spec.to_dict() for spec in self.all()]
