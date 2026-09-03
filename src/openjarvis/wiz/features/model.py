"""The feature request: what was asked for, and everything that happened to it.

The state machine is the important part. A feature cannot reach ``COMPLETE``
without having passed through verification, and it cannot reach ``MERGING``
without having been ``READY``, because the transition table says so and anything
absent from it raises. That is the same discipline the incident machine uses and
for the same reason: the expensive failure is not a wrong state, it is a state
that was skipped.

``HUMAN_REQUIRED`` is reachable from everywhere. Wiz stopping and saying so is
always a legal outcome, at any point, for any reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, List

__all__ = [
    "FeatureAttempt",
    "FeatureRequest",
    "FeatureState",
    "InvalidFeatureTransition",
    "Priority",
    "TERMINAL_STATES",
    "LEGAL_TRANSITIONS",
]


class FeatureState(str, Enum):
    """Where a feature request has got to."""

    #: Recorded, not yet looked at.
    RECEIVED = "RECEIVED"

    #: Wiz is reading the repository to work out what the request means.
    UNDERSTANDING = "UNDERSTANDING"

    #: A read-only Claude session is producing an implementation plan.
    PLANNING = "PLANNING"

    #: The plan passed the authority and risk checks and may be built. For a
    #: HIGH-risk feature this is where an operator's approval is recorded.
    APPROVED_FOR_BUILD = "APPROVED_FOR_BUILD"

    #: A write-enabled Claude session is working in an isolated worktree.
    BUILDING = "BUILDING"

    #: The target project's own gates are running: lint, types, tests, build.
    TESTING = "TESTING"

    #: A preview deployment is being produced.
    PREVIEWING = "PREVIEWING"

    #: The acceptance contract is being checked against the preview — in a
    #: browser, for anything with a user interface.
    VERIFYING = "VERIFYING"

    #: Everything passed. Waiting for whatever authority the next step needs.
    READY = "READY"

    MERGING = "MERGING"
    DEPLOYING = "DEPLOYING"

    #: Merged and deployed; production has not yet proved it. Named for the
    #: same reason the incident machine names it: this window exists whether or
    #: not it has a name, and it is the one where a change is live and unproven.
    PRODUCTION_VERIFYING = "PRODUCTION_VERIFYING"

    COMPLETE = "COMPLETE"

    #: Wiz stopped. Attempts exhausted, authority missing, the request was
    #: ambiguous, or something happened that no rule covers.
    HUMAN_REQUIRED = "HUMAN_REQUIRED"

    CANCELLED = "CANCELLED"

    @classmethod
    def parse(cls, value: Any) -> "FeatureState":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().upper())
        except ValueError as exc:
            raise ValueError(
                f"Unknown feature state {value!r}; "
                f"valid values: {', '.join(s.value for s in cls)}"
            ) from exc


class Priority(str, Enum):
    """Queue priority. Production always outranks product.

    The ordering is deliberate and is the answer to "what happens if the site
    breaks while a feature is building": the feature yields. An operator waiting
    an extra hour for a dashboard is an inconvenience; an outage continuing
    because the machine was busy building the dashboard is a failure.
    """

    #: A serious production incident. Reliability's own work, never a feature.
    P0 = "P0"

    #: An active reliability repair.
    P1 = "P1"

    #: A feature the operator called urgent.
    P2 = "P2"

    #: A normal feature request.
    P3 = "P3"

    #: Research, maintenance, dependency updates.
    P4 = "P4"

    @property
    def rank(self) -> int:
        """Lower sorts first."""
        return int(self.value[1:])

    @classmethod
    def parse(cls, value: Any) -> "Priority":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().upper())
        except ValueError as exc:
            raise ValueError(f"Unknown priority {value!r}") from exc


#: States where Wiz makes no further progress on its own.
TERMINAL_STATES: FrozenSet[FeatureState] = frozenset(
    {
        FeatureState.COMPLETE,
        FeatureState.HUMAN_REQUIRED,
        FeatureState.CANCELLED,
    }
)


def _from(*states: FeatureState) -> FrozenSet[FeatureState]:
    return frozenset(states)


#: Legal transitions. Anything absent raises.
#:
#: ``HUMAN_REQUIRED`` and ``CANCELLED`` are appended to every non-terminal
#: state below rather than written out fifteen times: stopping and asking for a
#: person is always allowed, and so is the operator changing their mind.
_PROGRESS: Dict[FeatureState, FrozenSet[FeatureState]] = {
    FeatureState.RECEIVED: _from(FeatureState.UNDERSTANDING),
    FeatureState.UNDERSTANDING: _from(FeatureState.PLANNING),
    FeatureState.PLANNING: _from(FeatureState.APPROVED_FOR_BUILD),
    FeatureState.APPROVED_FOR_BUILD: _from(FeatureState.BUILDING),
    FeatureState.BUILDING: _from(FeatureState.TESTING),
    # Testing can send the work back to Claude for another attempt, which is
    # the iterative loop: a failure is evidence, not the end.
    FeatureState.TESTING: _from(FeatureState.PREVIEWING, FeatureState.BUILDING),
    FeatureState.PREVIEWING: _from(FeatureState.VERIFYING, FeatureState.BUILDING),
    FeatureState.VERIFYING: _from(FeatureState.READY, FeatureState.BUILDING),
    FeatureState.READY: _from(FeatureState.MERGING),
    FeatureState.MERGING: _from(FeatureState.DEPLOYING),
    FeatureState.DEPLOYING: _from(FeatureState.PRODUCTION_VERIFYING),
    # Production can refuse the change. Rolling back is a human decision, so it
    # lands in HUMAN_REQUIRED rather than unwinding itself.
    FeatureState.PRODUCTION_VERIFYING: _from(FeatureState.COMPLETE),
}

LEGAL_TRANSITIONS: Dict[FeatureState, FrozenSet[FeatureState]] = {
    state: allowed | _from(FeatureState.HUMAN_REQUIRED, FeatureState.CANCELLED)
    for state, allowed in _PROGRESS.items()
}
# Terminal states progress nowhere on their own — not even HUMAN_REQUIRED,
# and this is checked by test, deliberately: "reachable from everywhere, and
# stopping is always allowed" must not quietly become "escapable by anything
# that calls transition()". A human moving a feature out of HUMAN_REQUIRED
# does so by creating the next step explicitly, not by a transition Wiz (or
# a docile recovery path standing in for it) can make — see
# :meth:`FeatureRequest.resume_from_human_required`, which is a distinct,
# separately-audited method for exactly that reason, not a LEGAL_TRANSITIONS
# entry.
for _terminal in TERMINAL_STATES:
    LEGAL_TRANSITIONS.setdefault(_terminal, frozenset())


class InvalidFeatureTransition(ValueError):
    """Raised when something tries to skip a step."""


def check_transition(current: FeatureState, target: FeatureState) -> None:
    """Raise unless ``current -> target`` is legal."""
    allowed = LEGAL_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidFeatureTransition(
            f"{current.value} -> {target.value} is not a legal feature transition; "
            f"legal: {', '.join(sorted(s.value for s in allowed)) or 'none'}"
        )


@dataclass(slots=True)
class FeatureAttempt:
    """One pass through the build loop.

    Attempts exist so that the second attempt can be told what the first one
    got wrong. ``hypothesis`` is what Wiz believed the problem was going in;
    recording it is what stops the loop repeating an approach that already
    failed.
    """

    number: int
    started_at: str = ""
    finished_at: str = ""

    #: The Claude CLI session this attempt drove, when there was one.
    session_id: str = ""

    #: What Wiz thought was wrong, before trying.
    hypothesis: str = ""

    #: Claude's own account of what it did. Recorded as a claim, never as
    #: evidence — the diff is read from git.
    claim: str = ""

    branch: str = ""
    base_sha: str = ""
    commit_sha: str = ""
    changed_files: List[str] = field(default_factory=list)
    lines_changed: int = 0

    #: What the deterministic gates said.
    checks: Dict[str, Any] = field(default_factory=dict)
    verification: Dict[str, Any] = field(default_factory=dict)

    succeeded: bool = False
    failure: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "number": self.number,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "session_id": self.session_id,
            "hypothesis": self.hypothesis,
            "claim": self.claim,
            "branch": self.branch,
            "base_sha": self.base_sha,
            "commit_sha": self.commit_sha,
            "changed_files": list(self.changed_files),
            "lines_changed": self.lines_changed,
            "checks": dict(self.checks),
            "verification": dict(self.verification),
            "succeeded": self.succeeded,
            "failure": self.failure,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "FeatureAttempt":
        return cls(
            number=int(raw.get("number", 0)),
            started_at=str(raw.get("started_at", "")),
            finished_at=str(raw.get("finished_at", "")),
            session_id=str(raw.get("session_id", "")),
            hypothesis=str(raw.get("hypothesis", "")),
            claim=str(raw.get("claim", "")),
            branch=str(raw.get("branch", "")),
            base_sha=str(raw.get("base_sha", "")),
            commit_sha=str(raw.get("commit_sha", "")),
            changed_files=list(raw.get("changed_files") or []),
            lines_changed=int(raw.get("lines_changed", 0)),
            checks=dict(raw.get("checks") or {}),
            verification=dict(raw.get("verification") or {}),
            succeeded=bool(raw.get("succeeded", False)),
            failure=str(raw.get("failure", "")),
        )


@dataclass
class FeatureRequest:
    """Something the operator asked for, and its whole history."""

    id: str = ""
    title: str = ""

    #: What the operator actually said, preserved verbatim. Wiz's structured
    #: brief is derived from it and stored separately, so that the two can be
    #: compared when a feature turns out not to be what was meant.
    operator_request: str = ""

    #: What the operator wanted to be true afterwards.
    desired_outcome: str = ""

    #: Which channel it arrived on. Not decoration: it caps the authority the
    #: request can ever carry.
    source: str = ""
    actor_id: str = ""

    target: str = ""  # the engineering target profile name, e.g. "wize"
    repository: str = ""

    priority: Priority = Priority.P3
    state: FeatureState = FeatureState.RECEIVED
    risk: str = "LOW"

    created_at: str = ""
    updated_at: str = ""

    #: The implementation brief Wiz derived, and the plan Claude produced.
    brief: str = ""
    plan: str = ""
    affected_components: List[str] = field(default_factory=list)

    #: Machine-checkable acceptance criteria. A feature is not complete because
    #: Claude says it is.
    acceptance: List[str] = field(default_factory=list)

    attempts: List[FeatureAttempt] = field(default_factory=list)

    branch: str = ""
    worktree: str = ""
    base_sha: str = ""

    preview_url: str = ""
    pr_url: str = ""
    pr_number: int = 0

    #: Set when an operator approved this specific feature for build. Bound to
    #: the plan it was shown, so changing the plan invalidates it.
    approved_plan_hash: str = ""

    production_result: str = ""

    #: Every state change, appended.
    history: List[Dict[str, Any]] = field(default_factory=list)

    metadata: Dict[str, Any] = field(default_factory=dict)

    # -- lifecycle ---------------------------------------------------------

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def attempts_used(self) -> int:
        return len(self.attempts)

    def transition(self, target: FeatureState, *, at: str, reason: str = "") -> None:
        """Move to *target*, refusing anything the table does not allow."""
        target = FeatureState.parse(target)
        check_transition(self.state, target)
        self.history.append(
            {
                "at": at,
                "from": self.state.value,
                "to": target.value,
                "reason": reason,
            }
        )
        self.state = target
        self.updated_at = at

    def resume_from_human_required(
        self, target: FeatureState, *, at: str, reason: str
    ) -> None:
        """Leave ``HUMAN_REQUIRED`` — deliberately not via :meth:`transition`.

        This exists for one caller:
        :class:`~openjarvis.wiz.features.recovery.FeatureRecovery`, acting on
        an operator's explicit, evidence-backed decision that a feature the
        ordinary attempt loop gave up on should be looked at again. It is a
        separate method rather than a :data:`LEGAL_TRANSITIONS` entry because
        that table backs a test asserting every terminal state — including
        this one — progresses nowhere *on its own*; adding an entry here would
        make that false for every caller of :meth:`transition`, not just this
        one, deliberate, audited exception.

        ``target`` must be ``BUILDING`` — resume always re-enters the ordinary
        attempt loop at the top, so the same gates (:class:`FeaturePipeline`'s
        or :class:`FeatureRecovery`'s) run again rather than being skipped.
        Every history entry this writes carries ``"resumed": True`` so the
        audit trail can tell a recovery hop apart from an ordinary one at a
        glance, without having to know this method exists.
        """
        if self.state is not FeatureState.HUMAN_REQUIRED:
            raise InvalidFeatureTransition(
                f"resume_from_human_required called from {self.state.value}, "
                "not HUMAN_REQUIRED"
            )
        target = FeatureState.parse(target)
        if target is not FeatureState.BUILDING:
            raise InvalidFeatureTransition(
                f"HUMAN_REQUIRED may only resume into BUILDING, not {target.value}"
            )
        self.history.append(
            {
                "at": at,
                "from": self.state.value,
                "to": target.value,
                "reason": reason,
                "resumed": True,
            }
        )
        self.state = target
        self.updated_at = at

    def resume_planning_from_human_required(self, *, at: str, reason: str) -> None:
        """Reopen a ``HUMAN_REQUIRED`` feature that never left planning.

        Distinct from :meth:`resume_from_human_required`, which is for crash
        recovery of *real build progress* and is gated on external evidence
        (a branch, a PR) to re-check. This is the narrower, strictly safer
        case: a feature that stopped before any code was written at all —
        zero attempts, no pull request — so there is nothing to verify,
        because nothing was ever done. There is only the original request,
        to be planned again from the top exactly as a fresh submission would
        be, through :data:`FeatureState.RECEIVED` so ``_understand`` re-runs
        and re-derives risk rather than carrying forward a stale
        classification from the attempt that crashed.

        A separate, audited method rather than a :data:`LEGAL_TRANSITIONS`
        entry for the same reason :meth:`resume_from_human_required` is: that
        table backs a test asserting every terminal state — including this
        one — progresses nowhere *on its own*. Every history entry this
        writes carries ``"resumed": True``, matching that method's
        convention, so the audit trail can tell a reopened hop apart from an
        ordinary one at a glance.
        """
        if self.state is not FeatureState.HUMAN_REQUIRED:
            raise InvalidFeatureTransition(
                "resume_planning_from_human_required called from "
                f"{self.state.value}, not HUMAN_REQUIRED"
            )
        if self.attempts_used or self.pr_number:
            raise InvalidFeatureTransition(
                "only a feature with zero attempts and no pull request may "
                "reopen into planning; anything further along needs "
                "resume_from_human_required's evidence-gated recovery instead"
            )
        self.history.append(
            {
                "at": at,
                "from": self.state.value,
                "to": FeatureState.RECEIVED.value,
                "reason": reason,
                "resumed": True,
            }
        )
        self.state = FeatureState.RECEIVED
        self.updated_at = at

    def resume_production_reverification_from_human_required(
        self, *, at: str, reason: str
    ) -> None:
        """Reopen a ``HUMAN_REQUIRED`` feature for a fresh post-ship check.

        The third and narrowest of the ``resume_*`` siblings: a feature
        lands here when a real merge happened but production verification
        did not agree (or could not run at all) — see
        :mod:`openjarvis.wiz.features.postship`. There is nothing to
        rebuild and nothing to re-plan; the code is already live. This exists
        only so a fresh :class:`~.postship.PostShipVerifier` run has a
        legal state to land its verdict on, the same way
        :meth:`resume_from_human_required` always lands on ``BUILDING`` so
        the ordinary attempt loop's own gates run again rather than being
        skipped.

        Always targets ``PRODUCTION_VERIFYING``. It is the caller's job —
        see :func:`~.postship.reverify_production` — to have already proven
        this feature actually has a merge to re-check (a real, currently-
        merged pull request; a production deployment that still matches
        that merge's exact commit) before calling this; this method only
        enforces the state machine, exactly like its two siblings.
        """
        if self.state is not FeatureState.HUMAN_REQUIRED:
            raise InvalidFeatureTransition(
                "resume_production_reverification_from_human_required called "
                f"from {self.state.value}, not HUMAN_REQUIRED"
            )
        self.history.append(
            {
                "at": at,
                "from": self.state.value,
                "to": FeatureState.PRODUCTION_VERIFYING.value,
                "reason": reason,
                "resumed": True,
            }
        )
        self.state = FeatureState.PRODUCTION_VERIFYING
        self.updated_at = at

    def resume_deploy_from_human_required(self, *, at: str, reason: str) -> None:
        """Reopen a ``HUMAN_REQUIRED`` feature directly into ``TESTING``,
        to check its most recent attempt's existing diff again rather than
        starting a new build.

        The fourth ``resume_*`` sibling. Found on FEAT-00031: three attempts
        in a row failed identically on infrastructure (a missing
        ``node_modules``, not a code defect — see
        :mod:`openjarvis.wiz.features.provision`), and by the time that was
        fixed, ``max_attempts`` was already spent. Nothing was wrong with
        the diff Claude produced; the worktree still has it, uncommitted,
        exactly as attempt 3 left it. Calling a fourth Claude session to
        redo work that was never the problem would be exactly the kind of
        unverified, unnecessary autonomous action this whole recovery
        family exists to avoid.

        ``TESTING`` is ``_deploy_preview``'s own step, and that method reads
        ``feature.attempts[-1]`` — the same attempt this resume points back
        at, not a new one. Requires that attempt to actually have changed
        files: an attempt that produced nothing has nothing to re-check, and
        this method refuses rather than silently reaching
        :meth:`~.pipeline.FeaturePipeline._deploy_preview` with an empty
        diff. It is the caller's job — see
        :meth:`~.pipeline.FeaturePipeline.reopen_for_deploy` — to have a real
        reason for believing the *previous* failure was infrastructure, not
        code; this method only enforces the state machine and that there is
        something real to test, exactly like its three siblings.
        """
        if self.state is not FeatureState.HUMAN_REQUIRED:
            raise InvalidFeatureTransition(
                f"resume_deploy_from_human_required called from {self.state.value}, "
                "not HUMAN_REQUIRED"
            )
        if not self.attempts or not self.attempts[-1].changed_files:
            raise InvalidFeatureTransition(
                "resume_deploy_from_human_required needs an existing attempt "
                "with real changed files to test — there is nothing to "
                "re-check otherwise"
            )
        self.history.append(
            {
                "at": at,
                "from": self.state.value,
                "to": FeatureState.TESTING.value,
                "reason": reason,
                "resumed": True,
            }
        )
        self.state = FeatureState.TESTING
        self.updated_at = at

    def resume_for_owner_authorized_rebuild_from_human_required(
        self, *, at: str, reason: str
    ) -> None:
        """Reopen a ``HUMAN_REQUIRED`` feature into ``BUILDING`` for one
        genuinely fresh Claude Code session, on the owner's explicit,
        one-off say-so — not an ordinary retry, and not something the
        ordinary attempt loop or any of this method's four siblings can
        reach on their own.

        The narrowest and most privileged of the five ``resume_*`` methods,
        and the only one that grants a *new* attempt past an exhausted
        ``max_attempts``. It exists for exactly one situation: attempts were
        burned entirely by a framework bug that has since been fixed — not
        by the request being hard, and not by anything wrong with a diff —
        so re-running the ordinary attempt loop would just fail the same way
        again for the same non-reason. See FEAT-00031's incident: three
        attempts were spent entirely on ``BUILDING`` having no ``Bash`` to
        provision dependencies with (fixed in
        :mod:`openjarvis.wiz.features.provision`), and the diff that
        infrastructure fix would have unblocked was then lost to a separate
        worktree-reuse bug (fixed in
        :meth:`~openjarvis.wiz.features.workspace.FeatureWorkspace.reuse`) —
        leaving nothing left to re-test the way
        :meth:`resume_deploy_from_human_required` does, and no pull request
        for :class:`~openjarvis.wiz.features.recovery.FeatureRecovery` to
        verify against either.

        Usable exactly once per feature: a second call refuses, because
        "exceptional, owner-authorized" describes a specific decision made
        once, not a standing retry button silently available to whoever
        next calls this. If the fresh attempt this grants also fails, the
        ordinary ``max_attempts`` gate in
        :meth:`~openjarvis.wiz.features.pipeline.FeaturePipeline._retry_or_stop`
        stops it again exactly as it would any other feature — this method
        grants one attempt, never an unbounded loop.
        """
        if self.state is not FeatureState.HUMAN_REQUIRED:
            raise InvalidFeatureTransition(
                "resume_for_owner_authorized_rebuild_from_human_required "
                f"called from {self.state.value}, not HUMAN_REQUIRED"
            )
        if self.risk.strip().upper() == "HIGH":
            raise InvalidFeatureTransition(
                "HIGH-risk features are never reopened without a person "
                "reviewing the new diff themselves"
            )
        marker = "owner-authorized exceptional rebuild"
        if any(
            str(entry.get("reason", "")).startswith(marker) for entry in self.history
        ):
            raise InvalidFeatureTransition(
                f"{self.id} already used its one owner-authorized exceptional "
                "rebuild; this is not an ordinary retry primitive"
            )
        self.history.append(
            {
                "at": at,
                "from": self.state.value,
                "to": FeatureState.BUILDING.value,
                "reason": f"{marker}: {reason}",
                "resumed": True,
            }
        )
        self.state = FeatureState.BUILDING
        self.updated_at = at

    def resume_base_refresh_from_ready(self, *, at: str, reason: str) -> None:
        """Reopen a ``READY`` feature into ``TESTING`` to re-verify its
        existing commit against a base branch that has moved since.

        Not a ``resume_*from_human_required`` sibling: ``ship()``'s
        ``require_base_unmoved`` refusal does not transition state at all
        (see :meth:`~.pipeline.FeaturePipeline._ship_locked` — a refused
        ship leaves the feature exactly ``READY``), so this is the one
        primitive in the family that leaves ``READY`` instead of
        ``HUMAN_REQUIRED``. Found on FEAT-00031: a feature can sit
        genuinely ``READY`` — every automated and manual criterion already
        satisfied — while ``main`` moves underneath it, with no existing
        way back into the pipeline's own re-verification machinery at all.

        Deliberately repeatable, unlike
        :meth:`resume_for_owner_authorized_rebuild_from_human_required`'s
        one-use guard: a base that moves again after one refresh is exactly
        as real a reason to refresh again, not a standing loophole —
        ``ship()``'s own gates, re-evaluated fresh every time, are what
        actually decide whether shipping is safe, not how many times this
        was called.

        ``TESTING`` is the same step :meth:`resume_deploy_from_human_required`
        already re-enters for the same reason (re-run the real, deterministic
        gates rather than trust stale evidence) — the caller's job, not this
        method's, is to have actually produced a new verified commit (a real
        merge or fast-forward against current main, never a discard of the
        existing one) and pointed ``attempts[-1].commit_sha`` /
        :attr:`base_sha` at it *before* calling this, so the re-entered
        ``TESTING`` step verifies the real thing rather than a stale record.
        """
        if self.state is not FeatureState.READY:
            raise InvalidFeatureTransition(
                f"resume_base_refresh_from_ready called from {self.state.value}, "
                "not READY"
            )
        self.history.append(
            {
                "at": at,
                "from": self.state.value,
                "to": FeatureState.TESTING.value,
                "reason": reason,
                "resumed": True,
            }
        )
        self.state = FeatureState.TESTING
        self.updated_at = at

    def next_attempt(self, *, at: str, hypothesis: str = "") -> FeatureAttempt:
        attempt = FeatureAttempt(
            number=len(self.attempts) + 1, started_at=at, hypothesis=hypothesis
        )
        self.attempts.append(attempt)
        return attempt

    def requires_source_change(self) -> bool:
        """Does this feature require modifying application source code?

        True if the feature involves code changes beyond tests/docs.
        False for documentation-only, test-only, or configuration changes.
        """
        # If there are affected_components that include source files, it requires source change
        # By default, most feature requests require source changes
        # Only return False for explicitly non-code features
        request_lower = self.operator_request.lower()

        # Documentation-only features
        if any(x in request_lower for x in ["readme", "docs", "documentation", "wiki"]):
            if not any(
                x in request_lower for x in ["code", "implementation", "feature"]
            ):
                return False

        # Configuration-only (environment variables, config files without code)
        if "config" in request_lower and "code" not in request_lower:
            return False

        # Default: assume source code changes are needed
        return True

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "operator_request": self.operator_request,
            "desired_outcome": self.desired_outcome,
            "source": self.source,
            "actor_id": self.actor_id,
            "target": self.target,
            "repository": self.repository,
            "priority": self.priority.value,
            "state": self.state.value,
            "risk": self.risk,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "brief": self.brief,
            "plan": self.plan,
            "affected_components": list(self.affected_components),
            "acceptance": list(self.acceptance),
            "attempts": [a.to_dict() for a in self.attempts],
            "branch": self.branch,
            "worktree": self.worktree,
            "base_sha": self.base_sha,
            "preview_url": self.preview_url,
            "pr_url": self.pr_url,
            "pr_number": self.pr_number,
            "approved_plan_hash": self.approved_plan_hash,
            "production_result": self.production_result,
            "history": list(self.history),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "FeatureRequest":
        return cls(
            id=str(raw.get("id", "")),
            title=str(raw.get("title", "")),
            operator_request=str(raw.get("operator_request", "")),
            desired_outcome=str(raw.get("desired_outcome", "")),
            source=str(raw.get("source", "")),
            actor_id=str(raw.get("actor_id", "")),
            target=str(raw.get("target", "")),
            repository=str(raw.get("repository", "")),
            priority=Priority.parse(raw.get("priority", "P3")),
            state=FeatureState.parse(raw.get("state", "RECEIVED")),
            risk=str(raw.get("risk", "LOW")),
            created_at=str(raw.get("created_at", "")),
            updated_at=str(raw.get("updated_at", "")),
            brief=str(raw.get("brief", "")),
            plan=str(raw.get("plan", "")),
            affected_components=list(raw.get("affected_components") or []),
            acceptance=list(raw.get("acceptance") or []),
            attempts=[FeatureAttempt.from_dict(a) for a in (raw.get("attempts") or [])],
            branch=str(raw.get("branch", "")),
            worktree=str(raw.get("worktree", "")),
            base_sha=str(raw.get("base_sha", "")),
            preview_url=str(raw.get("preview_url", "")),
            pr_url=str(raw.get("pr_url", "")),
            pr_number=int(raw.get("pr_number", 0) or 0),
            approved_plan_hash=str(raw.get("approved_plan_hash", "")),
            production_result=str(raw.get("production_result", "")),
            history=list(raw.get("history") or []),
            metadata=dict(raw.get("metadata") or {}),
        )
