"""What "done" means, written down before anything is built.

The rule this module exists to enforce is §8 of the brief: *the contract, not
Claude's claim, defines completion.* A coding agent finishes every task. It says
so at the end of every session, in the same confident register whether it wrote
the feature or wrote a comment saying the feature would go here. Treating that
sentence as evidence is the single easiest way to build a machine that reports
success forever.

So a feature carries a contract: a list of criteria, each one a thing a program
can check. Criteria come in kinds, and the kind decides who checks it:

``GATE``
    A local command must pass — the target's own tests, types, lint, build.
    Checked by :class:`~openjarvis.reliability.checks.CheckSuite`.

``CONTENT``, ``INTERACTION``, ``VIEWPORT``, ``CONSOLE``, ``NETWORK``
    Something must be true in a real browser on the deployed preview. These
    compile down to :class:`~openjarvis.reliability.probes.spec.ProbeSpec`
    workflows, one per viewport, and are checked by the browser runner.

``ENDPOINT``, ``UNAUTHORIZED``
    An HTTP request must answer a particular way. The http runner.

``MANUAL``
    Something a person has to look at. Recorded honestly and *never* counted as
    passing — a contract with a manual criterion cannot self-verify, which is the
    correct outcome rather than an inconvenience to design around.

The last one matters more than it looks. The temptation, whenever a criterion
turns out to be awkward to automate, is to write it as prose and let the
pipeline treat prose as satisfied. Then the contract still reads impressively
and verifies nothing. Here, a criterion nobody can check keeps the feature out
of ``READY``.

Contracts are *derived*, not invented: :func:`contract_for` builds one from the
operator's request and the plan, using the same deterministic reading the risk
classifier uses. A model may propose extra criteria — it knows the codebase —
but only ever additively, and every proposal must still name a checkable kind.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from openjarvis.reliability.probes.spec import (
    ProbeAssertions,
    ProbeExpectation,
    ProbeRetry,
    ProbeSpec,
    ProbeStep,
)
from openjarvis.reliability.types import Severity

logger = logging.getLogger(__name__)

__all__ = [
    "AcceptanceContract",
    "Criterion",
    "KINDS",
    "Viewport",
    "DESKTOP",
    "MOBILE",
    "contract_for",
    "criteria_from_mapping",
]


#: The kinds, as plain strings. Strings rather than an ``Enum`` because these
#: travel through JSON in the feature document and come back, and because the
#: set is checked at construction anyway — an unknown kind raises rather than
#: quietly becoming a criterion nobody checks.
GATE = "GATE"
CONTENT = "CONTENT"
INTERACTION = "INTERACTION"
VIEWPORT = "VIEWPORT"
CONSOLE = "CONSOLE"
NETWORK = "NETWORK"
ENDPOINT = "ENDPOINT"
UNAUTHORIZED = "UNAUTHORIZED"
PERFORMANCE = "PERFORMANCE"
MANUAL = "MANUAL"

#: Every kind that exists. A criterion outside this set is a bug in whatever
#: produced it, not a criterion to be tolerated.
KINDS = frozenset(
    {
        GATE,
        CONTENT,
        INTERACTION,
        VIEWPORT,
        CONSOLE,
        NETWORK,
        ENDPOINT,
        UNAUTHORIZED,
        PERFORMANCE,
        MANUAL,
    }
)

#: Kinds checked in a browser against the preview deployment.
BROWSER_KINDS = frozenset({CONTENT, INTERACTION, VIEWPORT, CONSOLE, NETWORK})

#: Kinds no program in this system checks. Present so they can be *reported*,
#: never so they can be assumed.
UNCHECKABLE_KINDS = frozenset({MANUAL})


@dataclass(frozen=True, slots=True)
class Viewport:
    """A screen size the feature is verified at."""

    name: str
    width: int
    height: int

    @property
    def size(self) -> Tuple[int, int]:
        return (self.width, self.height)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "width": self.width, "height": self.height}


#: The two sizes every user-interface feature is checked at. Mobile is not
#: optional: the brief asks for it explicitly, and a layout that overflows on a
#: phone is the most common way a "finished" feature turns out not to be.
DESKTOP = Viewport("desktop", 1280, 800)
MOBILE = Viewport("mobile", 390, 844)


@dataclass(frozen=True, slots=True)
class Criterion:
    """One machine-checkable statement about a finished feature."""

    kind: str

    #: What this means, in the operator's language. Shown in the Control Center
    #: and put in the pull request; never parsed.
    description: str

    #: Route the criterion applies to, for browser kinds. Relative to the
    #: preview's origin, so the same contract verifies any deployment.
    route: str = ""

    #: CSS selector, for criteria about a specific element.
    selector: str = ""

    #: Text that must be present (``CONTENT``) or an element clicked
    #: (``INTERACTION``).
    text: str = ""

    #: For ``INTERACTION``: what must become true after the interaction.
    then_selector: str = ""
    then_text: str = ""

    #: For ``ENDPOINT``/``UNAUTHORIZED``: the request and the status expected.
    method: str = "GET"
    expected_status: int = 0

    #: For ``GATE``: which gate. For ``PERFORMANCE``: the metric.
    name: str = ""

    #: For ``PERFORMANCE``: the number that must not be exceeded, and what the
    #: measurement was before the change. A target with no baseline is a wish.
    budget: float = 0.0
    baseline: float = 0.0

    #: Which viewports this applies to. Empty means both.
    viewports: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(
                f"unknown acceptance criterion kind {self.kind!r}; "
                f"valid kinds: {', '.join(sorted(KINDS))}"
            )
        if not self.description.strip():
            raise ValueError("an acceptance criterion needs a description")
        if self.kind == PERFORMANCE and self.budget <= 0:
            raise ValueError(
                "a performance criterion needs a measurable budget; "
                "'should feel faster' is not a criterion"
            )

    @property
    def checkable(self) -> bool:
        """Whether anything in this system can decide this criterion."""
        return self.kind not in UNCHECKABLE_KINDS

    def applies_to(self, viewport: Viewport) -> bool:
        return not self.viewports or viewport.name in self.viewports

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "description": self.description,
            "route": self.route,
            "selector": self.selector,
            "text": self.text,
            "then_selector": self.then_selector,
            "then_text": self.then_text,
            "method": self.method,
            "expected_status": self.expected_status,
            "name": self.name,
            "budget": self.budget,
            "baseline": self.baseline,
            "viewports": list(self.viewports),
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Criterion":
        return cls(
            kind=str(raw.get("kind", "")).upper(),
            description=str(raw.get("description", "")),
            route=str(raw.get("route", "")),
            selector=str(raw.get("selector", "")),
            text=str(raw.get("text", "")),
            then_selector=str(raw.get("then_selector", "")),
            then_text=str(raw.get("then_text", "")),
            method=str(raw.get("method", "GET")).upper() or "GET",
            expected_status=int(raw.get("expected_status", 0) or 0),
            name=str(raw.get("name", "")),
            budget=float(raw.get("budget", 0.0) or 0.0),
            baseline=float(raw.get("baseline", 0.0) or 0.0),
            viewports=tuple(str(v) for v in (raw.get("viewports") or ())),
        )


@dataclass(frozen=True, slots=True)
class AcceptanceContract:
    """Everything that must be true before a feature may be called done."""

    feature_id: str
    criteria: Tuple[Criterion, ...] = ()

    #: Viewports the browser criteria are checked at.
    viewports: Tuple[Viewport, ...] = (DESKTOP, MOBILE)

    #: Routes touched, collected for convenience. Derived from the criteria so
    #: it cannot drift away from them.
    @property
    def routes(self) -> Tuple[str, ...]:
        seen = [c.route for c in self.criteria if c.route]
        return tuple(dict.fromkeys(seen))

    @property
    def gates(self) -> Tuple[str, ...]:
        return tuple(c.name for c in self.criteria if c.kind == GATE and c.name)

    @property
    def browser_criteria(self) -> Tuple[Criterion, ...]:
        return tuple(c for c in self.criteria if c.kind in BROWSER_KINDS)

    @property
    def endpoint_criteria(self) -> Tuple[Criterion, ...]:
        return tuple(c for c in self.criteria if c.kind in (ENDPOINT, UNAUTHORIZED))

    @property
    def manual_criteria(self) -> Tuple[Criterion, ...]:
        return tuple(c for c in self.criteria if not c.checkable)

    @property
    def self_verifiable(self) -> bool:
        """Whether passing everything checkable means the contract is met.

        ``False`` when any criterion needs a person. A feature whose contract is
        not self-verifiable can pass every check there is and still must not
        reach ``READY`` on its own — see :meth:`unmet_without_a_person`.
        """
        return not self.manual_criteria

    def unmet_without_a_person(self) -> Tuple[str, ...]:
        """The criteria that will still be open after every check has passed."""
        return tuple(c.description for c in self.manual_criteria)

    @property
    def empty(self) -> bool:
        return not self.criteria

    def describe(self) -> List[str]:
        """The contract as the operator reads it in the Control Center."""
        return [f"{c.kind}: {c.description}" for c in self.criteria]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "criteria": [c.to_dict() for c in self.criteria],
            "viewports": [v.to_dict() for v in self.viewports],
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "AcceptanceContract":
        viewports = tuple(
            Viewport(
                name=str(v.get("name", "")),
                width=int(v.get("width", 0)),
                height=int(v.get("height", 0)),
            )
            for v in (raw.get("viewports") or ())
        )
        return cls(
            feature_id=str(raw.get("feature_id", "")),
            criteria=tuple(
                Criterion.from_dict(c) for c in (raw.get("criteria") or ())
            ),
            viewports=viewports or (DESKTOP, MOBILE),
        )

    # -- compilation -------------------------------------------------------

    def probe_specs(
        self, *, severity: Severity = Severity.MEDIUM
    ) -> List[Tuple[Viewport, ProbeSpec]]:
        """Compile the browser half of this contract into runnable probes.

        One probe per viewport per route, because a probe is a single browser
        session and the whole point of the mobile pass is that it is a different
        session at a different size. The specs are built in memory and handed
        straight to the runner: they are deliberately *not* written into the
        probe directory, because a feature check is a question asked once about
        one deployment, and a production probe is a promise to keep asking. §15
        of the brief says so, and the failure mode if it were otherwise is a
        probe suite that grows by one flaky entry per feature until nobody reads
        it.
        """
        specs: List[Tuple[Viewport, ProbeSpec]] = []
        browser = self.browser_criteria
        if not browser:
            return specs

        for viewport in self.viewports:
            applicable = [c for c in browser if c.applies_to(viewport)]
            if not applicable:
                continue
            for route in self._routes_for(applicable):
                on_route = [c for c in applicable if (c.route or "/") == route]
                spec = self._spec_for_route(
                    route, on_route, viewport, severity=severity
                )
                if spec is not None:
                    specs.append((viewport, spec))
        return specs

    @staticmethod
    def _routes_for(criteria: Sequence[Criterion]) -> List[str]:
        return list(dict.fromkeys((c.route or "/") for c in criteria))

    def _spec_for_route(
        self,
        route: str,
        criteria: Sequence[Criterion],
        viewport: Viewport,
        *,
        severity: Severity,
    ) -> Optional[ProbeSpec]:
        steps: List[ProbeStep] = [
            ProbeStep(action="goto", url=route, label=f"Open {route}")
        ]
        expectations: List[ProbeExpectation] = []
        assertions = ProbeAssertions()

        for criterion in criteria:
            if criterion.kind == CONTENT:
                if criterion.selector and not criterion.text:
                    expectations.append(
                        ProbeExpectation(kind="visible", selector=criterion.selector)
                    )
                elif criterion.text:
                    expectations.append(
                        ProbeExpectation(
                            kind="text",
                            selector=criterion.selector,
                            value=criterion.text,
                        )
                    )
            elif criterion.kind == INTERACTION:
                if criterion.selector:
                    steps.append(
                        ProbeStep(
                            action="click",
                            selector=criterion.selector,
                            label=criterion.description,
                        )
                    )
                if criterion.then_selector and not criterion.then_text:
                    expectations.append(
                        ProbeExpectation(
                            kind="visible", selector=criterion.then_selector
                        )
                    )
                elif criterion.then_text:
                    expectations.append(
                        ProbeExpectation(
                            kind="text",
                            selector=criterion.then_selector,
                            value=criterion.then_text,
                        )
                    )
            elif criterion.kind == VIEWPORT:
                # A layout criterion has nothing to assert beyond the page
                # rendering at this size without error; the overflow check is
                # done by the verifier, which can measure the document.
                if criterion.selector:
                    expectations.append(
                        ProbeExpectation(kind="visible", selector=criterion.selector)
                    )
            elif criterion.kind == CONSOLE:
                assertions.no_console_errors = True
            elif criterion.kind == NETWORK:
                assertions.no_failed_requests = True
                assertions.max_http_status = 499

        if not expectations and not (
            assertions.no_console_errors or assertions.no_failed_requests
        ):
            # Nothing to check on this route at this size. Returning None rather
            # than an empty probe keeps "we ran a check that asserted nothing"
            # from appearing in the evidence as a pass.
            return None

        slug = re.sub(r"[^a-z0-9]+", "-", route.lower()).strip("-") or "root"
        return ProbeSpec(
            id=f"feature-{self.feature_id.lower()}-{viewport.name}-{slug}",
            name=f"{self.feature_id} on {viewport.name} at {route}",
            component="feature-verification",
            severity=severity,
            runner="browser",
            description=(
                f"Temporary acceptance check for {self.feature_id}. "
                "Not a production probe."
            ),
            # Never mutating: a feature check runs against a preview, but a
            # preview usually shares the production database, so a check that
            # creates data is a check that writes to production.
            mutating=False,
            steps=steps,
            expect=expectations,
            assertions=assertions,
            # One attempt, no confirmation runs: this is not incident detection,
            # where a second opinion prevents a false alarm. Here a failure is
            # evidence handed straight back to Claude, and repeating it only
            # costs time.
            retry=ProbeRetry(attempts=1, confirm_runs=1, backoff_seconds=0.0),
            metadata={
                "feature_id": self.feature_id,
                "viewport": viewport.name,
                "temporary": True,
            },
        )


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------

#: Route mentioned explicitly in a request or plan: ``/coach/summary``.
_ROUTE = re.compile(r"(?<![\w/])(/[a-z0-9][a-z0-9/_-]*)")

#: Phrases that mean the change has a user interface. Deliberately broad: a
#: false positive costs a browser check on a page that was going to be checked
#: anyway; a false negative ships an unverified UI.
_UI_WORDS = re.compile(
    r"\b(button|page|screen|view|dashboard|panel|modal|dialog|form|menu|nav|"
    r"navigation|banner|card|chart|graph|table|list|tab|tooltip|badge|layout|"
    r"header|heading|footer|sidebar|ui|interface|display|show|render|colour|"
    r"color|style|css|responsive|mobile|desktop|onboarding|empty state|"
    r"title|label|link|icon|toggle|dropdown|checkbox|input|field|section|"
    r"widget|avatar|breadcrumb|placeholder|spinner|skeleton|image|text)\b",
    re.IGNORECASE,
)

_API_WORDS = re.compile(
    r"\b(endpoint|api|route handler|webhook|server action|rest|graphql)\b",
    re.IGNORECASE,
)

#: Quoted text in a request is almost always the literal string the operator
#: wants to see: *add a "Download report" button*.
_QUOTED = re.compile(r"[\"“']([^\"”']{2,60})[\"”']")


def _first_route(*sources: str) -> str:
    for source in sources:
        for match in _ROUTE.finditer(source or ""):
            candidate = match.group(1)
            # Filter out things that look like paths but are files.
            if "." in candidate.rsplit("/", 1)[-1]:
                continue
            return candidate
    return ""


def _quoted_labels(text: str) -> List[str]:
    return [m.group(1).strip() for m in _QUOTED.finditer(text or "")]


def contract_for(
    *,
    feature_id: str,
    request: str,
    plan: str = "",
    gates: Sequence[str] = (),
    extra: Sequence[Criterion] = (),
    touches_ui: Optional[bool] = None,
    route: str = "",
) -> AcceptanceContract:
    """Derive the contract for a feature from what was asked and what was planned.

    Deterministic on purpose. The temptation is to have the planning session
    write the contract, since it has just read the repository and knows the
    selectors — but then the same agent writes the exam and sits it, and the
    exam gets easier every time it is failed. A model may contribute through
    *extra*, which is additive: it can add criteria, never remove or relax one.

    *gates* comes from the target's engineering profile, so a repository with no
    typecheck script gets no typecheck criterion rather than one that fails
    because the script does not exist.
    """
    criteria: List[Criterion] = []
    corpus = f"{request}\n{plan}"

    for gate in gates:
        name = str(gate).strip()
        if not name:
            continue
        criteria.append(
            Criterion(
                kind=GATE,
                name=name,
                description=f"the project's {name} check passes",
            )
        )

    is_ui = touches_ui if touches_ui is not None else bool(_UI_WORDS.search(corpus))
    resolved_route = route or _first_route(request, plan) or ("/" if is_ui else "")

    if is_ui:
        labels = _quoted_labels(request) or _quoted_labels(plan)
        for label in labels[:3]:
            criteria.append(
                Criterion(
                    kind=CONTENT,
                    route=resolved_route,
                    text=label,
                    description=f"{resolved_route} shows “{label}”",
                )
            )
        if not labels:
            # Nothing quoted to look for. The page must still load and render
            # cleanly, which is weak but true and checkable — and the planning
            # session is asked to add a real content criterion on top.
            criteria.append(
                Criterion(
                    kind=VIEWPORT,
                    route=resolved_route,
                    description=f"{resolved_route} renders without layout overflow",
                )
            )
        criteria.append(
            Criterion(
                kind=CONSOLE,
                route=resolved_route,
                description=f"{resolved_route} logs no JavaScript errors",
            )
        )
        criteria.append(
            Criterion(
                kind=NETWORK,
                route=resolved_route,
                description=f"{resolved_route} makes no failed requests",
            )
        )
        criteria.append(
            Criterion(
                kind=VIEWPORT,
                route=resolved_route,
                viewports=(MOBILE.name,),
                description=f"{resolved_route} works on a phone-sized screen",
            )
        )

    if _API_WORDS.search(corpus) and not is_ui:
        # An API change with no route named cannot be turned into a check
        # automatically. Say so as a MANUAL criterion rather than inventing an
        # endpoint: an invented endpoint check passes against a 404 handler.
        criteria.append(
            Criterion(
                kind=MANUAL,
                description=(
                    "the new endpoint answers as intended, and refuses an "
                    "unauthenticated caller — no route was named in the request, "
                    "so I could not turn this into an automatic check"
                ),
            )
        )

    criteria.extend(extra)

    return AcceptanceContract(
        feature_id=feature_id, criteria=tuple(_dedupe(criteria))
    )


def _dedupe(criteria: Iterable[Criterion]) -> List[Criterion]:
    seen: Dict[Tuple[Any, ...], Criterion] = {}
    for criterion in criteria:
        key = (
            criterion.kind,
            criterion.route,
            criterion.selector,
            criterion.text,
            criterion.name,
            criterion.viewports,
        )
        seen.setdefault(key, criterion)
    return list(seen.values())


def criteria_from_mapping(raw: Iterable[Any]) -> List[Criterion]:
    """Parse criteria proposed by something outside this module.

    Used for the planning session's suggestions. Anything malformed is dropped
    with a warning rather than raising: a plan that proposes one unusable
    criterion should still contribute its usable ones, and the contract's
    deterministic core is already in place regardless.
    """
    parsed: List[Criterion] = []
    for item in raw or ():
        if not isinstance(item, dict):
            logger.warning("ignoring a proposed criterion that is not a table")
            continue
        try:
            parsed.append(Criterion.from_dict(item))
        except (ValueError, TypeError) as exc:
            logger.warning("ignoring an unusable proposed criterion: %s", exc)
    return parsed
