"""Walking a feature from "build X" to "it's ready".

This is the orchestrator. Everything it needs already exists — the store, the
queue, the worktree, the Claude adapter, the gates, the preview observer, the
verifier — and what this module contributes is the *order*, the decisions
between the steps, and the discipline that stops any of them being skipped.

The shape is deliberately one step at a time. :meth:`FeaturePipeline.advance`
performs exactly one transition and returns; :meth:`FeaturePipeline.run` calls it
until the feature reaches a state where nothing further can happen without
somebody else — an approval, an authority, a person. Two reasons. It makes every
transition testable in isolation, and it gives the queue somewhere to interrupt:
if reliability picks up an incident while a feature is building, the check
happens between steps and the feature stops with its worktree intact.

Four rules that are properties, not conventions:

**Claude writes the code.** Every state that changes a file goes through
:class:`~openjarvis.wiz.features.engineer.ClaudeCodeEngineeringAgent`. There is
no branch of this module that edits a source file, and no fallback if the CLI is
missing — the feature stops at ``HUMAN_REQUIRED`` saying the CLI is unavailable.

**The diff is read from git.** What the agent says it did is stored on the
attempt as a claim. What it actually changed is read from the worktree, and the
risk classification runs on *that*.

**Risk is re-decided after the build.** A LOW request that turns out to touch
``auth/session.ts`` becomes HIGH the moment the diff exists, and a HIGH feature
cannot proceed without an approval bound to that specific plan. The agent's own
opinion may only raise the level.

**A failure is evidence, not an ending.** Gates, previews and verification all
feed the exact text of what went wrong back into the next attempt, up to a
bounded number of attempts. Only when they run out does a person get involved.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

from openjarvis.reliability.briefing import has_critical_secret
from openjarvis.reliability.checks import CheckResult
from openjarvis.wiz.approvals import ApprovalError, ApprovalStore
from openjarvis.wiz.authority import Actor, Authority, AuthorityPolicy
from openjarvis.wiz.capabilities import Risk
from openjarvis.wiz.features.acceptance import (
    AcceptanceContract,
    Criterion,
    contract_for,
    criteria_from_mapping,
    extract_proposed_criteria,
)
from openjarvis.wiz.features.diskspace import has_enough_disk
from openjarvis.wiz.features.engineer import (
    ClaudeCodeEngineeringAgent,
    CodingEngineUnavailable,
    ContextPack,
)
from openjarvis.wiz.features.model import (
    FeatureAttempt,
    FeatureRequest,
    FeatureState,
    Priority,
)
from openjarvis.wiz.features.postship import complete as _complete_postship
from openjarvis.wiz.features.preview import PreviewObserver
from openjarvis.wiz.features.profile import EngineeringProfile
from openjarvis.wiz.features.provision import PROVISION_CHECK_NAME, provision_check
from openjarvis.wiz.features.risk import classify, classify_paths
from openjarvis.wiz.proclock import LeaseTimeout, ProcessLease
from openjarvis.wiz.features.verification import (
    CriterionOutcome,
    FeatureVerifier,
    gate_outcome,
)

logger = logging.getLogger(__name__)

__all__ = [
    "FeaturePipeline",
    "PipelineStep",
    "StepResult",
    "DEFAULT_MAX_ATTEMPTS",
]

#: How many times Claude may try before a person is asked. Three is enough for
#: "misread the requirement", "fixed it but broke a test" and "fixed both"; a
#: fourth attempt on this hardware costs an hour and has, in practice, never
#: been the one that worked.
DEFAULT_MAX_ATTEMPTS = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class StepResult:
    """What one transition did."""

    feature: FeatureRequest

    #: Whether the pipeline can usefully take another step immediately.
    progressed: bool = True

    #: What to tell the operator, if anything. Written in their language.
    message: str = ""

    def __bool__(self) -> bool:
        return self.progressed


PipelineStep = Callable[[FeatureRequest], StepResult]


@dataclass
class FeaturePipeline:
    """Drives feature requests through their lifecycle.

    Every collaborator is injected. That is not test convenience — it is how a
    test proving "voice cannot merge" or "planning writes nothing" proves it
    about the same object the operator runs.
    """

    store: Any
    profile: EngineeringProfile
    engineer: ClaudeCodeEngineeringAgent
    workspace: Any

    #: Builds the local gate suite for a workspace. Injected because the
    #: commands come from the profile and the runner comes from reliability, and
    #: this module should know neither.
    check_suite_factory: Callable[[EngineeringProfile], Any]

    #: Provisions dependencies before the check suite runs — see
    #: openjarvis.wiz.features.provision. Injected for the same reason
    #: check_suite_factory is: a test proving the gate/retry loop should not
    #: need a real npm install to do it. Defaults to the real, deterministic
    #: provisioner.
    provision_factory: Callable[[EngineeringProfile, str], Any] = None  # type: ignore[assignment]

    preview: Optional[PreviewObserver] = None
    verifier: Optional[FeatureVerifier] = None

    queue: Any = None
    journal: Any = None

    #: Tells the owner exactly two things — a feature shipped, or a feature
    #: needs them — and stays silent about every step in between. See
    #: :mod:`openjarvis.wiz.features.notify`.
    owner_notifier: Any = None
    approvals: Optional[ApprovalStore] = None
    policy: Optional[AuthorityPolicy] = None

    #: Advisory only. Deterministic gates remain authoritative.
    reviewer: Any = None

    #: Opens the pull request when a feature reaches READY, and — only
    #: through the explicit :meth:`ship` verb, never from :meth:`run` — merges
    #: it. Opening is routine; :meth:`ship` is a separate authority this
    #: pipeline's ordinary advancement never exercises on its own.
    shipper: Any = None

    #: Proves a merged feature in production, or hands the failure to
    #: reliability. Only used from :meth:`ship`, after ``shipper`` reports a
    #: successful merge — see :mod:`openjarvis.wiz.features.postship`.
    postship: Any = None

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    clock: Callable[[], str] = _now

    #: Set when the pipeline pushed a branch and a preview is expected.
    push_remote: str = "origin"

    #: Cross-process guard for :meth:`ship`, in addition to ``_ship_lock``.
    #: ``None`` in tests that construct a bare pipeline (they run alone, so a
    #: cross-process guard has nothing to protect against); the real Wiz
    #: always wires one in — see :func:`~openjarvis.wiz.assemble.assemble`.
    #: See :mod:`openjarvis.wiz.proclock` for why this is a kernel ``flock``
    #: lease rather than a second, hand-rolled PID/TTL scheme.
    ship_lease: Optional[ProcessLease] = None
    ship_lease_timeout: float = 30.0

    def __post_init__(self) -> None:
        if self.provision_factory is None:
            self.provision_factory = _default_provision
        self._worktrees: Dict[str, Any] = {}
        # Guards the whole of ship(): merge, production observation and
        # production verification are one critical section, process-wide.
        # See ship()'s own docstring for why one lock is the right shape.
        self._ship_lock = threading.Lock()

    # -- intake ------------------------------------------------------------

    def submit(
        self,
        text: str,
        *,
        actor: Actor,
        title: str = "",
        priority: Priority = Priority.P3,
        target: str = "",
    ) -> FeatureRequest:
        """Record a request. Does no work; that is what :meth:`run` is for.

        Every channel arrives here — the Control Center, the CLI, Telegram,
        voice. §5 of the brief asks for one pipeline rather than one per input,
        and the reason is that a second intake path is a second place for the
        risk and authority rules to be slightly different.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            raise ValueError("a feature request needs to say what to build")

        now = self.clock()
        feature = FeatureRequest(
            id=self.store.next_id(),
            title=title.strip() or _title_from(cleaned),
            operator_request=cleaned,
            desired_outcome="",
            source=actor.channel.value,
            actor_id=actor.actor_id,
            target=target or self.profile.name,
            repository=self.profile.repository,
            priority=priority,
            state=FeatureState.RECEIVED,
            risk=classify(text=cleaned).risk.value,
            created_at=now,
            updated_at=now,
        )
        self.store.create(feature)
        self._record(feature, "feature.received", cleaned[:400])
        if self.queue is not None:
            self.queue.submit(feature)
        return feature

    # -- driving -----------------------------------------------------------

    def run(self, feature_id: str, *, max_steps: int = 30) -> FeatureRequest:
        """Advance *feature_id* until it stops making progress on its own."""
        feature = self._load(feature_id)
        for _ in range(max_steps):
            if feature.terminal or feature.state is FeatureState.READY:
                break
            result = self.advance(feature)
            feature = result.feature
            if not result.progressed:
                break
        return feature

    def cancel(self, feature_id: str, *, reason: str = "") -> FeatureRequest:
        """Stop work on a request, on the operator's instruction.

        Always available, and deliberately the simplest verb in the pipeline.
        Stopping is the one instruction that can never make Wiz more powerful
        than it already was, so it needs no approval, no risk check and no
        authority beyond being the operator — and an operator who wants work to
        stop should not discover that the stop button is the part that needed
        configuring.

        What it does *not* do is undo anything. A worktree that exists stays on
        disk, a pull request that was opened stays open, and a change that
        reached production stays there. Cancelling is Wiz agreeing to take no
        further steps; unwinding a step already taken is a separate decision,
        with different risks, that belongs to a person.

        Raises ``KeyError`` for an unknown id, and returns an already-terminal
        feature unchanged — asking twice is not an error, it is an operator who
        did not see the first answer.
        """
        feature = self._load(feature_id)
        if feature.terminal:
            return feature
        feature.transition(
            FeatureState.CANCELLED,
            at=self.clock(),
            reason=(reason or "the operator asked me to stop")[:300],
        )
        self.store.save(feature)
        self._record(
            feature, "feature.cancelled", reason or "operator asked me to stop"
        )
        # The queue slot goes back immediately: a cancelled feature must not
        # hold the one concurrency slot that reliability might need.
        self._release(feature)
        if self.queue is not None:
            try:
                self.queue.cancel(feature.id)
            except Exception:  # noqa: BLE001 - a queue that cannot forget it
                logger.exception("could not remove %s from the queue", feature.id)
        return feature

    def reopen_for_planning(
        self, feature_id: str, *, reason: str = ""
    ) -> FeatureRequest:
        """Give a feature that crashed before writing any code another run at planning.

        Deliberately narrow, and deliberately not the crash-recovery path in
        :mod:`openjarvis.wiz.features.recovery` — that one is for a feature
        that reached ``BUILDING`` and re-verifies real evidence (a branch, a
        pull request) before trusting it. A feature with zero attempts and no
        pull request never got that far: there is nothing to re-verify, only
        the original request, worth trying again now that whatever stopped
        it — a transient Claude Code capacity limit, a classifier bug since
        fixed — no longer applies. See
        :meth:`FeatureRequest.resume_planning_from_human_required` for the
        guard this relies on; ``InvalidFeatureTransition`` propagates as this
        method's refusal for anything that does not qualify.

        An explicit operator action, not something the pipeline reaches on
        its own — the same reason :meth:`cancel` is its own verb rather than
        a state :meth:`run` can enter unprompted.
        """
        feature = self._load(feature_id)
        feature.resume_planning_from_human_required(
            at=self.clock(), reason=(reason or "reopened by the operator")[:300]
        )
        self.store.save(feature)
        self._record(
            feature, "feature.reopened_for_planning", reason or "reopened by the operator"
        )
        if self.queue is not None:
            self.queue.submit(feature)
        return feature

    def reopen_for_deploy(
        self, feature_id: str, *, reason: str = ""
    ) -> FeatureRequest:
        """Give a feature whose attempts were exhausted by infrastructure,
        not by its own diff, another run at the check suite — without a new
        Claude session.

        Deliberately not :meth:`reopen_for_planning` (that one is for zero
        attempts and nothing built yet) and not the crash-recovery path in
        :mod:`openjarvis.wiz.features.recovery` (that one requires a real
        pull request to re-verify against; a feature stopped here never
        reached one). Found on FEAT-00031: three attempts failed identically
        on ``node_modules`` never existing — an infrastructure gap, fixed in
        :mod:`openjarvis.wiz.features.provision` — not a defect in what
        Claude wrote. The worktree still has that diff, uncommitted, exactly
        as the last attempt left it; this re-enters the pipeline at
        ``TESTING``, the same step :meth:`~.pipeline.FeaturePipeline._deploy_preview`
        already owns, so provisioning and every gate run for real against
        it, through the same code every ordinary feature goes through from
        that point on — including, if they now pass, the commit, push,
        preview and verification steps that never happened before.

        If a gate genuinely fails this time — a real defect, not
        infrastructure — :meth:`~.pipeline.FeaturePipeline._retry_or_stop`
        finds attempts already exhausted and stops again, the same as any
        other feature that runs out of attempts. This method does not, and
        cannot, grant a fourth Claude session; it only proves whether the
        third one's actual output was ever the problem.

        An explicit operator action, not something the pipeline reaches on
        its own — the same reason :meth:`reopen_for_planning` and
        :meth:`cancel` are their own verbs rather than states :meth:`run`
        enters unprompted.
        """
        feature = self._load(feature_id)
        feature.resume_deploy_from_human_required(
            at=self.clock(),
            reason=(reason or "reopened for deploy by the operator")[:300],
        )
        self.store.save(feature)
        self._record(
            feature,
            "feature.reopened_for_deploy",
            reason or "reopened for deploy by the operator",
        )
        if self.queue is not None:
            self.queue.submit(feature)
        return feature

    def reopen_for_owner_authorized_rebuild(
        self, feature_id: str, *, reason: str
    ) -> FeatureRequest:
        """Grant one genuinely fresh Claude Code session to a feature whose
        attempts are exhausted, on the owner's explicit, one-off say-so.

        The most privileged of the three ``reopen_for_*`` operator verbs, and
        the only one that spends a new attempt past an exhausted
        ``max_attempts`` — see
        :meth:`~openjarvis.wiz.features.model.FeatureRequest.resume_for_owner_authorized_rebuild_from_human_required`
        for when this is (and is not) the right call, and for the one-use-
        per-feature guard that makes this a specific decision rather than a
        standing retry button. ``reason`` has no default, deliberately: unlike
        :meth:`reopen_for_planning` and :meth:`reopen_for_deploy`, there is no
        generic phrase honest enough to stand in for "the owner looked at
        this specific feature and said so" — the caller must actually say why.

        An explicit operator action, not something the pipeline reaches on
        its own — the same reason :meth:`reopen_for_planning` and
        :meth:`reopen_for_deploy` are their own verbs rather than states
        :meth:`run` enters unprompted.
        """
        feature = self._load(feature_id)
        feature.resume_for_owner_authorized_rebuild_from_human_required(
            at=self.clock(), reason=reason[:300]
        )
        self.store.save(feature)
        self._record(feature, "feature.owner_authorized_rebuild", reason[:1000])
        if self.queue is not None:
            self.queue.submit(feature)
        return feature

    def approve_manual_acceptance(
        self, feature_id: str, *, reason: str
    ) -> FeatureRequest:
        """Issue a one-off, fingerprint-bound approval for exactly this
        feature's current outstanding ``awaiting_a_person`` items, at
        exactly its current verified head SHA.

        Closes a real lifecycle gap: a feature whose only remaining gap is
        one or more structurally-unmeasurable criteria (a layout check with
        no selector to compile, typically) could never reach ``READY``
        through :meth:`_finish` — which stops unconditionally whenever
        ``verification.complete`` is false — and could not reach it through
        :class:`~openjarvis.wiz.features.recovery.FeatureRecovery` either,
        since that path requires an existing pull request, and a pull
        request is only ever opened *after* reaching ``READY``. Found on
        FEAT-00031: every criterion this system could check, checked out
        clean, and the feature was still permanently stuck.

        This method only *issues* the approval (via the same
        :class:`~openjarvis.wiz.approvals.ApprovalStore` every other
        fingerprint-bound yes in this codebase uses) and stores its token
        where :meth:`_finish` already knows to look
        (``feature.metadata["ship_manual_approval_token"]``, redeemed by
        :meth:`_awaiting_items_approved`). It grants nothing by itself: the
        stored fingerprint names this feature, this exact head SHA, and the
        exact sorted set of items currently outstanding, and redemption
        recomputes that same fingerprint fresh from whatever the feature's
        real state is when :meth:`_finish` next runs — so this approval
        silently fails to redeem the moment any of those three things
        change (a new attempt, a changed contract, a different set of
        outstanding items), rather than being honoured as a standing yes.

        Grants nothing about *automated* criteria: :meth:`_finish` checks
        ``verification.passed`` — a real Playwright/gate failure — before
        it ever looks at this approval, unconditionally, so this can never
        mask one.

        ``reason`` has no default, matching
        :meth:`reopen_for_owner_authorized_rebuild`: there is no generic
        phrase honest enough to stand in for a person having actually
        looked at this feature's evidence and said yes.
        """
        feature = self._load(feature_id)
        if self.approvals is None:
            raise ApprovalError("no approval store is configured")
        if not feature.attempts or not feature.attempts[-1].commit_sha:
            raise ApprovalError(
                f"{feature_id} has no verified commit to bind this approval to"
            )
        head_sha = feature.attempts[-1].commit_sha
        outstanding = sorted(
            (feature.metadata.get("verification") or {}).get("awaiting_a_person")
            or []
        )
        if not outstanding:
            raise ApprovalError(
                f"{feature_id} has no outstanding manual-verification items "
                "to approve"
            )
        approval = self.approvals.issue(
            capability="feature.ship_manual_items",
            subject=feature.id,
            parameters={"head_sha": head_sha, "outstanding": outstanding},
            actor_id=feature.actor_id,
            channel=feature.source,
            summary=reason[:1000],
        )
        feature.metadata["ship_manual_approval_token"] = approval.token
        self.store.save(feature)
        self._record(feature, "feature.manual_acceptance_approved", reason[:1000])
        return feature

    def ship(
        self, feature_id: str, *, operator_approved: bool = False
    ) -> FeatureRequest:
        """Merge a READY feature and prove it in production, or say why not.

        Deliberately not a step ``run`` reaches on its own. ``run`` advances a
        feature up to the point a person becomes visible to it and stops at
        ``READY`` even when a shipper and a post-ship verifier are both
        configured, because opening a pull request is where its ordinary
        autonomy ends by design — see :meth:`_open_pull_request`. ``ship`` is
        the one call that can take a feature past that point, and every gate
        that matters is re-asked inside it rather than trusted from whatever
        ``READY`` last observed: the pull request and CI status are read fresh,
        :meth:`FeatureShipper.merge_feature` re-checks the shipping gates, the
        authority model and the token's actual write permission, and GitHub's
        own merge call refuses server-side if the branch moved since any of
        that was read.

        A refusal leaves the feature exactly ``READY``. Nothing about calling
        this and being told no — CI still running, the operator has not
        approved a HIGH-risk feature, a scoped-down token — makes the feature
        itself worse off; the same call can simply be made again once whatever
        blocked it has changed.

        Serialised on ``self._ship_lock``: merging one feature and observing
        its production deployment is a critical section a second call — for
        this feature or any other — must never interleave with. Two features
        both reaching READY and both being shipped at the same moment is not
        hypothetical; without this, the second call's production observation
        could race the first's and attribute the wrong deployment to the
        wrong feature. The lock is a plain ``threading.Lock``, in-memory and
        process-wide, the same shape every other single-flight guard in this
        codebase uses (:class:`~openjarvis.wiz.features.queue.DevelopmentQueue`,
        :class:`~openjarvis.wiz.features.store.FeatureStore`); it does not
        need to survive a crash to be crash-safe, because a crash also ends
        the process holding it — the next process starts unlocked, and a
        feature left mid-ship is exactly the ``MERGING``/``DEPLOYING`` restart
        case :meth:`_load` and the state machine already have to answer.

        ``_ship_lock`` alone only protects against a second call *in this
        process*. Wiz is meant to run as more than one process against the
        same state directory, and two of those each holding their own
        ``_ship_lock`` would not stop each other from merging at the same
        moment — the exact race the paragraph above describes, just between
        processes instead of threads. When ``self.ship_lease`` is configured
        (the real deployment always sets one; see
        :mod:`openjarvis.wiz.proclock`), it is acquired *outside*
        ``_ship_lock`` and held for the same critical section, so a second
        process blocks — and eventually refuses with a named holder, never
        silently proceeds — while this one is shipping anything at all. A
        refusal here behaves exactly like any other ``ship_refused``: the
        feature is left ``READY``, to be tried again.
        """
        with self._ship_lock:
            if self.ship_lease is None:
                return self._ship_locked(feature_id, operator_approved=operator_approved)
            try:
                with self.ship_lease.acquire(
                    timeout=self.ship_lease_timeout,
                    reason=f"shipping {feature_id}",
                ):
                    return self._ship_locked(
                        feature_id, operator_approved=operator_approved
                    )
            except LeaseTimeout as exc:
                feature = self._load(feature_id)
                self._record(feature, "feature.ship_refused", str(exc))
                return feature

    def _ship_locked(
        self, feature_id: str, *, operator_approved: bool = False
    ) -> FeatureRequest:
        """The body of :meth:`ship`, for a caller already holding the lock."""
        feature = self._load(feature_id)
        if feature.state is not FeatureState.READY:
            self._record(
                feature,
                "feature.ship_refused",
                f"the feature is {feature.state.value}, not READY",
            )
            return feature
        if self.shipper is None:
            self._record(feature, "feature.ship_refused", "no shipper is configured")
            return feature
        if not feature.pr_number:
            self._record(
                feature, "feature.ship_refused", "the feature has no pull request"
            )
            return feature

        try:
            pr = self.shipper.github.get_pull_request(feature.pr_number)
        except Exception as exc:
            self._record(
                feature,
                "feature.ship_refused",
                f"could not read the pull request: {exc}",
            )
            return feature

        required_contexts = list(
            getattr(self.shipper.policy, "required_status_contexts", ()) or ()
        )
        status: Optional[Dict[str, Any]] = None
        if required_contexts and pr.get("head_sha"):
            try:
                status = self.shipper.github.combined_status(
                    pr["head_sha"], required_contexts=required_contexts
                )
            except Exception as exc:
                logger.warning("could not read CI status for %s: %s", feature.id, exc)
                status = {"state": "unreadable", "contexts": {}}

        head_sha = str(pr.get("head_sha", ""))
        medium_risk_approved = self._medium_ship_approved(feature, head_sha=head_sha)
        awaiting_items_approved = self._awaiting_items_approved(
            feature, head_sha=head_sha
        )

        merge_result = self.shipper.merge_feature(
            feature,
            pull_request=pr,
            status=status,
            base_sha_at_verification=feature.base_sha,
            observed_base_sha=pr.get("base_sha", ""),
            operator_approved=operator_approved,
            medium_risk_approved=medium_risk_approved,
            awaiting_items_approved=awaiting_items_approved,
        )
        if not merge_result.get("merged"):
            if pr.get("merged"):
                # Not a refusal: the PR is already merged. Either this is a
                # retry after a crash between GitHub confirming the merge and
                # this process recording it, or a person merged it by hand
                # while Wiz waited. Either way "ship_refused" would be false
                # — nothing here refused anything — and leaving the feature
                # sitting at READY forever would hide a merge that already
                # happened. This is not a state ship() can safely continue
                # from on its own: which of the two it was changes whether
                # production has already been checked, and guessing wrong in
                # either direction is exactly the "fake continuation" this
                # pipeline elsewhere refuses to do.
                feature.transition(
                    FeatureState.HUMAN_REQUIRED,
                    at=self.clock(),
                    reason=(
                        "the pull request is already merged, but I do not "
                        "know whether I was the one who merged it or "
                        "whether production has been verified — please check"
                    ),
                )
                self.store.save(feature)
                self._record(
                    feature,
                    "feature.merge_already_done",
                    f"PR #{feature.pr_number} was already merged",
                )
                self._release(feature)
                return feature
            self._record(
                feature,
                "feature.ship_refused",
                merge_result.get("reason", "the merge was refused"),
            )
            return feature

        merge_sha = merge_result.get("sha", "")
        self._transition(
            feature, FeatureState.MERGING, f"merged pull request #{feature.pr_number}"
        )
        self._transition(
            feature, FeatureState.DEPLOYING, "waiting for the production deployment"
        )

        if self.postship is None:
            # Merged, but nothing here can prove production. Handed to a
            # person rather than called COMPLETE on the strength of a merge
            # alone — see the module docstring on why that word means what it
            # says.
            self._transition(
                feature,
                FeatureState.PRODUCTION_VERIFYING,
                "no post-ship verifier is configured",
            )
            feature.transition(
                FeatureState.HUMAN_REQUIRED,
                at=self.clock(),
                reason=(
                    "merged, but I have no way to verify production — please "
                    "check it yourself"
                ),
            )
            self.store.save(feature)
            self._record(feature, "feature.merged_unverified", f"merged at {merge_sha}")
            self._release(feature)
            return feature

        self._transition(
            feature,
            FeatureState.PRODUCTION_VERIFYING,
            f"verifying commit {merge_sha[:12]}",
        )
        result = self.postship.verify(feature, merge_commit_sha=merge_sha)
        _complete_postship(feature, result, at=self.clock())
        self.store.save(feature)
        self._record(
            feature,
            "feature.shipped" if result.verified else "feature.production_unverified",
            result.summary(),
        )
        self._release(feature)
        return feature

    def auto_ship_if_eligible(
        self,
        feature_id: str,
        *,
        emergency_stop_engaged: Callable[[], bool] = lambda: False,
        reliability_busy: Callable[[], bool] = lambda: False,
        audit_healthy: Callable[[], bool] = lambda: True,
    ) -> FeatureRequest:
        """Ship *feature_id* on its own, iff it never needed a person to say so.

        Not a step :meth:`run` reaches — ``run`` stops at ``READY`` by design
        and stays that way; see :meth:`ship`'s docstring and
        ``TestReadyOpensAPullRequestAndNothingMore.test_run_never_merges_anything``.
        This is a second, deliberately narrow caller sitting above both: the
        thing an operator wires in after ``run`` returns, when they have
        decided in advance — in :class:`~openjarvis.wiz.features.shipping.
        FeatureShippingPolicy`, in configuration, not here — that genuinely
        LOW-risk work may go all the way on its own.

        Every check below is a reason *not to call* :meth:`ship` at all, not a
        looser version of what :meth:`ship` already checks — the pull request
        state, the base branch, the required status contexts, the authority
        model and the token's write permission are re-verified fresh inside
        :meth:`ship` regardless of how it was invoked, exactly as they are for
        the operator's own Ship button. What this method adds is the three
        things ``evaluate_shipping`` has no way to see because they are not
        about this feature: whether the operator has pulled the emergency
        stop, whether reliability is busy with production, and whether Wiz's
        own audit trail still checks out. A machine that cannot prove the last
        merge it recorded is the last merge that happened has no business
        deciding to make another one by itself.

        MEDIUM, HIGH and UNKNOWN risk are refused here, before ``ship`` is
        even called — not just left to ``evaluate_shipping``'s own risk gate —
        because the brief is explicit that this trigger must never *attempt*
        an autonomous merge outside LOW, not merely fail to complete one.

        Idempotent: a feature not sitting at exactly ``READY`` with
        ``merge_low_risk`` on is left untouched, so calling this twice, or
        after a restart finds the feature already ``MERGING``, ``COMPLETE`` or
        back at ``READY`` from a refusal, never risks a second merge attempt.
        """
        feature = self._load(feature_id)
        if feature.state is not FeatureState.READY:
            return feature
        if (feature.risk or "").strip().upper() != Risk.LOW.value:
            return feature
        if self.shipper is None or not getattr(
            self.shipper.policy, "merge_low_risk", False
        ):
            return feature

        if emergency_stop_engaged():
            self._record(
                feature,
                "feature.auto_ship_skipped",
                "the emergency stop is engaged; not shipping automatically",
            )
            return feature
        if reliability_busy():
            self._record(
                feature,
                "feature.auto_ship_skipped",
                "reliability is busy with production; deferring automatic shipping",
            )
            return feature
        if not audit_healthy():
            self._record(
                feature,
                "feature.auto_ship_skipped",
                "the audit trail is not healthy; refusing to ship automatically",
            )
            return feature

        if not feature.pr_number:
            # A feature can reach READY with no pull request — a shipping
            # integration failure, a permission error, a transient outage —
            # and `run()` never revisits READY to try again. Self-heal that
            # one gap here, under the same three checks just re-asked above,
            # rather than leaving every such feature stranded until someone
            # notices and intervenes by hand.
            feature = self.recover_missing_pull_request(feature_id)
            if not feature.pr_number:
                return feature

        return self.ship(feature_id)

    def advance(self, feature: FeatureRequest) -> StepResult:
        """Perform exactly one transition."""
        if feature.terminal:
            return StepResult(feature, progressed=False, message="nothing left to do")

        yielded = self._check_preemption(feature)
        if yielded is not None:
            return yielded

        root = getattr(self.workspace, "root", None)
        if root and not has_enough_disk(root):
            # Checked before the step runs, not after it fails: FEAT-00007
            # crashed a coding session with a bare "JavaScript heap out of
            # memory" when the real problem was disk, and FEAT-00008 died
            # too abruptly to even record a failed attempt. Both left a
            # feature stuck rather than a clear, actionable stop.
            return self._stop(
                feature,
                "I'm stopping before doing more work: this machine is "
                "nearly out of disk space, and a coding session, a build "
                "or a preview is more likely to crash than finish. Free "
                "some space and try again.",
                kind="feature.disk_exhausted",
            )

        step = self._steps().get(feature.state)
        if step is None:
            return StepResult(
                feature,
                progressed=False,
                message=f"I have no next step from {feature.state.value}",
            )

        try:
            return step(feature)
        except CodingEngineUnavailable as exc:
            # The one failure that must never be worked around. No second
            # coding model, no API key: the work stops and says why.
            return self._stop(
                feature,
                f"I cannot write code right now: {exc}",
                kind="feature.engine_unavailable",
            )
        except Exception as exc:
            logger.exception("feature %s failed during %s", feature.id, feature.state)
            return self._stop(
                feature,
                _plain_failure(exc),
                kind="feature.failed",
            )

    def _steps(self) -> Dict[FeatureState, PipelineStep]:
        return {
            FeatureState.RECEIVED: self._understand,
            FeatureState.UNDERSTANDING: self._plan,
            FeatureState.PLANNING: self._decide_to_build,
            FeatureState.APPROVED_FOR_BUILD: self._start_building,
            # Retries re-enter here: _retry_or_stop puts the feature back into
            # BUILDING, and BUILDING is the state that runs a Claude session.
            FeatureState.BUILDING: self._build,
            FeatureState.TESTING: self._deploy_preview,
            FeatureState.PREVIEWING: self._verify,
            FeatureState.VERIFYING: self._finish,
        }

    # -- steps -------------------------------------------------------------

    def _understand(self, feature: FeatureRequest) -> StepResult:
        """Work out what was asked for, without asking Claude yet.

        Cheap and deterministic. The expensive read-only session is worth
        spending only once there is a brief for it to answer.
        """
        feature.desired_outcome = feature.desired_outcome or feature.operator_request
        feature.brief = _brief_for(feature)
        assessment = classify(text=feature.operator_request)
        feature.risk = assessment.risk.value
        feature.metadata["risk_reasons"] = list(assessment.reasons)
        self._transition(feature, FeatureState.UNDERSTANDING, "read the request")
        return StepResult(feature, message="Understanding...")

    def _plan(self, feature: FeatureRequest) -> StepResult:
        """A read-only Claude session investigates the repository.

        Run inside a worktree rather than the live checkout. The session's tool
        list already makes writing impossible, but "impossible because the tools
        are absent" and "impossible because it is not pointed at anything that
        matters" are two defences and the cost of the second is one worktree.
        """
        worktree = self._worktree_for(feature)
        pack = ContextPack(
            goal=feature.brief or feature.operator_request,
            repository=feature.repository,
            base_sha=feature.base_sha,
            branch=feature.branch,
            gates=list(self.profile.configured_gates),
            protected_paths=list(self.profile.protected_paths),
        )
        session = self.engineer.plan(pack, workspace=worktree.path)

        if not session.succeeded:
            if session.metadata.get("transient_reason") == "usage_limit":
                return self._retry_transient_plan_failure(feature, session)
            return self._stop(
                feature,
                f"I could not work out how to build this: {session.error}",
                kind="feature.plan_failed",
            )

        feature.plan = session.claim
        feature.affected_components = list(session.changed_files)
        # Additive only — see contract_for()'s docstring on why the plan
        # never writes the contract, only ever adds to it.
        proposed = extract_proposed_criteria(session.claim)
        if proposed:
            feature.metadata["proposed_criteria"] = proposed
        self._transition(feature, FeatureState.PLANNING, "produced a plan")
        return StepResult(feature, message="Planning...")

    def _decide_to_build(self, feature: FeatureRequest) -> StepResult:
        """Risk, authority and approval — before anything is written."""
        planned_paths = _paths_mentioned(feature.plan)
        assessment = classify(
            text=feature.operator_request,
            paths=planned_paths,
            agent_opinion=_agent_risk_opinion(feature.plan),
        )
        feature.risk = assessment.risk.value
        feature.metadata["risk_reasons"] = list(assessment.reasons)

        contract = contract_for(
            feature_id=feature.id,
            request=feature.operator_request,
            plan=feature.plan,
            gates=list(self.profile.configured_gates),
            extra=self._proposed_criteria(feature),
        )
        feature.acceptance = contract.describe()
        feature.metadata["contract"] = contract.to_dict()

        if contract.empty:
            return self._stop(
                feature,
                "I could not turn this into anything I would be able to check "
                "afterwards, so I have not built it.",
                kind="feature.no_contract",
            )

        denial = self._authority_denial(feature)
        if denial:
            return self._stop(feature, denial, kind="feature.authority_refused")

        if assessment.risk is Risk.HIGH and not self._approved_for(feature, contract):
            # Not `_transition`: PLANNING -> PLANNING is not a legal forward
            # transition (see LEGAL_TRANSITIONS), and the state itself is not
            # changing here — only the risk assessment and its reasons are.
            # But `feature.risk` was already reassigned above (line ~650) as
            # part of computing `assessment`, and without an explicit save
            # here that escalation lived only in this call's memory: `jarvis
            # wiz show` and Control Center kept reporting whatever risk the
            # feature had *before* planning, and an approval request created
            # against the true (HIGH) risk could not be redeemed against a
            # feature record that still claimed a lower one.
            message = (
                "Sir, I need your approval before I build this, because "
                + (assessment.reasons[0] if assessment.reasons else "it is high risk")
                + "."
            )
            self.store.save(feature)
            self._record(feature, "feature.needs_approval", message)
            return StepResult(feature, progressed=False, message=message)

        self._transition(
            feature, FeatureState.APPROVED_FOR_BUILD, f"risk {feature.risk}"
        )
        return StepResult(feature, message="Ready to build...")

    def _start_building(self, feature: FeatureRequest) -> StepResult:
        """Enter the build state.

        Its own step so that the transition into ``BUILDING`` is recorded before
        a session that may take half an hour starts, rather than after it. An
        operator looking at the Control Center during that half hour should see
        "building", not "approved and apparently idle".
        """
        self._transition(feature, FeatureState.BUILDING, "starting the build")
        return StepResult(feature, message="Building with Claude Code...")

    def _build(self, feature: FeatureRequest) -> StepResult:
        """Claude Code implements the change in the feature's worktree."""
        worktree = self._worktree_for(feature)
        attempt = feature.next_attempt(
            at=self.clock(), hypothesis=self._hypothesis_for(feature)
        )
        attempt.branch = feature.branch
        attempt.base_sha = feature.base_sha

        pack = ContextPack(
            goal=feature.brief or feature.operator_request,
            repository=feature.repository,
            base_sha=feature.base_sha,
            branch=feature.branch,
            architecture_notes=feature.plan,
            gates=list(self.profile.configured_gates),
            acceptance=list(feature.acceptance),
            protected_paths=list(self.profile.protected_paths),
            previous_attempts=[a.to_dict() for a in feature.attempts[:-1]],
        )
        session = self.engineer.build(pack, workspace=worktree.path)
        attempt.session_id = str(session.metadata.get("session_id", ""))
        attempt.claim = session.claim

        # Read from git, never from the agent's account of itself.
        attempt.changed_files = list(self.workspace.changed_files(worktree))
        added, removed = self.workspace.line_counts(worktree)
        attempt.lines_changed = added + removed

        if not session.succeeded:
            attempt.failure = session.error or "the coding session did not finish"
            self.store.save(feature)
            return self._retry_or_stop(
                feature, attempt, "the coding session did not finish"
            )

        if not attempt.changed_files:
            # The most common silent failure: a session that finished cleanly
            # and changed nothing. Reported as a failed attempt rather than
            # carried forward into gates that would pass against no change.
            attempt.failure = "no files were changed"
            self.store.save(feature)
            return self._retry_or_stop(
                feature, attempt, "the coding session changed no files"
            )

        # Risk is re-decided on the real diff. A request that read as harmless
        # and a change that touches authentication is exactly the case this
        # catches, and it catches it before the branch is pushed anywhere.
        after = classify_paths(attempt.changed_files)
        if after.risk is Risk.HIGH and feature.risk != Risk.HIGH.value:
            feature.risk = Risk.HIGH.value
            feature.metadata["risk_reasons"] = list(after.reasons)
            self.store.save(feature)
            return self._stop(
                feature,
                "Sir, this turned out to change something sensitive — "
                + (after.reasons[0] if after.reasons else "a protected path")
                + " — so I have stopped and left it for you to look at.",
                kind="feature.risk_raised_by_diff",
            )

        self._transition(feature, FeatureState.TESTING, "implementation written")
        return StepResult(feature, message="Building with Claude Code...")

    def _deploy_preview(self, feature: FeatureRequest) -> StepResult:
        """Provision dependencies, run the local gates, then push the branch
        so a preview is built.

        FEAT-00031: three attempts failed identically on "tsc: command not
        found" because node_modules did not exist in the worktree and
        nothing installed it — dependency installation had always been a
        Bash-enabled coding session's own implicit job, and BUILDING has had
        no Bash since the FEAT-00030 fix (see
        openjarvis.wiz.features.engineer's module docstring). Provisioning
        runs here, first, deterministically, using the exact same
        path_prepend the check suite itself was built with — a project that
        pins a Node version gets the same one for installing as for
        checking, never two different runtimes silently disagreeing about
        what "the dependencies" even are.
        """
        worktree = self._worktree_for(feature)
        attempt = feature.attempts[-1]

        # Its own gate, checked and recorded before the suite runs at all —
        # not merged into the suite's own result object, deliberately: the
        # suite's shape (CheckSuiteResult, or a test double standing in for
        # one) is owned by check_suite_factory, and reconstructing one from
        # pieces here would mean silently assuming what that shape is.
        provision_result = self.provision_factory(self.profile, worktree.path)
        if not provision_result.passed:
            attempt.checks = {
                "passed": False,
                "ran_any": provision_result.ran,
                "results": [provision_result.to_dict()],
                "summary": provision_result.summary,
            }
            feature.metadata["gates"] = attempt.checks
            self.store.save(feature)
            evidence = (
                provision_result.output.strip()
                if provision_result.output
                else provision_result.summary
            )
            return self._retry_or_stop(feature, attempt, f"### {PROVISION_CHECK_NAME} failed\n\n{evidence}")

        suite = self.check_suite_factory(self.profile)
        result = suite.run(workspace=worktree.path)
        checks = _checks_record(result)
        # provision_result is recorded first — same list, same shape as
        # every other gate, not a separate top-level key a reader would
        # have to know to look for.
        checks["results"] = [provision_result.to_dict(), *checks.get("results", [])]
        attempt.checks = checks
        feature.metadata["gates"] = attempt.checks

        if not getattr(result, "passed", False):
            # ``feedback()`` is the command's actual output — the type error,
            # the failing assertion — and ``summary`` is one line per check.
            # The next attempt needs the first; the pull request body and the
            # operator need the second. Sending the summary to Claude is how a
            # second attempt ends up being told "tests: failed" and guessing.
            evidence = _gate_feedback(result)
            self.store.save(feature)
            return self._retry_or_stop(feature, attempt, evidence)

        # A gate the engineering profile has no way to configure: none of
        # lint, typecheck, test or build is a secret scanner, and a repository
        # without one configured as a custom lint step would otherwise commit
        # and push whatever the diff contains. Checked here — on the diff,
        # before it is committed — rather than only redacted later when it is
        # rendered into a pull request or a notification; those catch a
        # secret from reaching an operator's screen, this catches one from
        # reaching git history at all.
        if has_critical_secret(self.workspace.diff(worktree)):
            evidence = (
                "the diff contains what looks like a real credential (an API "
                "key, token, or password assigned directly rather than read "
                "from configuration); I have not committed it. Use an "
                "environment variable or the project's existing secret "
                "handling instead of a literal value."
            )
            self.store.save(feature)
            return self._retry_or_stop(feature, attempt, evidence)

        # Usually there is something uncommitted here — the session that just
        # ran BUILDING left it. Not always: reopen_for_deploy re-enters this
        # exact step for an attempt a *previous* run of this method already
        # committed and pushed (found re-verifying FEAT-00031 against a fixed
        # acceptance contract — the diff was already on its branch, and
        # commit_all() raised "nothing to commit" rather than being asked to
        # commit nothing). The worktree's current HEAD is that same commit
        # either way, so this reuses it instead of trying to create a new one.
        if self.workspace.has_changes(worktree):
            message = _commit_message(feature)
            attempt.commit_sha = self.workspace.commit_all(worktree, message)
        else:
            attempt.commit_sha = self.workspace.head_sha(worktree)
        attempt.succeeded = False  # not yet: the preview has to agree
        self.store.save(feature)

        if self.preview is None:
            # Nothing would ever look at the pushed branch, and pushing is not
            # free: it makes the work visible to CI and to anybody watching the
            # repository. A machine with no preview provider stops here, with
            # the change committed locally and an honest account of why.
            return self._stop(
                feature,
                "I have no way to see a preview of this, so I cannot prove it "
                f"works. The change is committed on {feature.branch} and the "
                "checks passed.",
                kind="feature.no_preview_provider",
            )

        try:
            self.workspace.push(worktree, remote=self.push_remote)
        except Exception as exc:
            # Not a retry: pushing again would fail the same way, and another
            # coding session cannot fix a credential.
            return self._stop(
                feature,
                f"I built this and the checks passed, but I could not push the "
                f"branch: {exc}",
                kind="feature.push_failed",
            )

        self._transition(feature, FeatureState.PREVIEWING, "pushed for a preview")
        return StepResult(feature, message="Creating Preview...")

    def _verify(self, feature: FeatureRequest) -> StepResult:
        """Wait for this commit's preview, then check the contract against it."""
        attempt = feature.attempts[-1]

        if self.preview is None:
            return self._stop(
                feature,
                "I have no way to see a preview of this, so I cannot prove it "
                "works. The branch is pushed and waiting for you.",
                kind="feature.no_preview_provider",
            )

        observation = self.preview.observe(
            commit_sha=attempt.commit_sha, branch=feature.branch
        )
        attempt.verification["preview"] = observation.to_dict()

        if not observation.usable:
            attempt.failure = observation.reason
            self.store.save(feature)
            return self._retry_or_stop(feature, attempt, observation.evidence())

        feature.preview_url = observation.url
        self.store.save(feature)
        self._transition(feature, FeatureState.VERIFYING, "preview is ready")
        return StepResult(feature, message="Checking Preview...")

    def _finish(self, feature: FeatureRequest) -> StepResult:
        """Check the acceptance contract, and decide."""
        attempt = feature.attempts[-1]
        contract = AcceptanceContract.from_dict(feature.metadata.get("contract") or {})

        if self.verifier is None:
            return self._stop(
                feature,
                "I have no browser here, so I cannot check that this works. "
                "The preview is ready for you to look at.",
                kind="feature.no_verifier",
            )

        # Exact SHA gate: fail closed if Preview doesn't match expected commit
        sha_gate_result = self._verify_preview_sha(feature, attempt)
        if sha_gate_result is not None:
            return sha_gate_result

        # Get deployment_id from preview observation
        preview_obs = attempt.verification.get("preview") or {}
        deployment_id = preview_obs.get("deployment_id") or ""

        verification = self.verifier.verify(
            contract,
            preview_url=feature.preview_url,
            commit_sha=attempt.commit_sha,
            deployment_id=deployment_id,
            attempt=attempt.number,
            gate_outcomes=self._gate_outcomes(attempt),
        )
        attempt.verification["acceptance"] = verification.to_dict()
        feature.metadata["verification"] = verification.to_dict()

        if not verification.passed:
            attempt.failure = verification.summary()
            self.store.save(feature)
            return self._retry_or_stop(feature, attempt, verification.evidence())

        attempt.succeeded = True
        attempt.finished_at = self.clock()

        if self.reviewer is not None:
            # Advisory. A review that dislikes the change is recorded and shown
            # to the operator; it does not overrule a contract that passed, and
            # it cannot pass one that failed.
            worktree = self._worktree_for(feature)
            try:
                feature.metadata["review"] = self.reviewer.review(
                    feature, workspace=worktree.path, worktree=worktree
                )
            except Exception as exc:  # pragma: no cover - advisory
                logger.warning("the review session failed: %s", exc)

        if not verification.complete:
            # verification.passed is already true here (checked above,
            # unconditionally) — every awaiting_a_person item left is a
            # structural gap (no selector for a layout check, typically),
            # never a failure. A redeemed, fingerprint-bound approval can
            # close exactly that gap; it is asked for fresh every time,
            # against whatever this feature's outstanding items and head
            # SHA actually are right now, so it can never stand in for a
            # different set of items or a different commit.
            manual_approved = self._awaiting_items_approved(
                feature, head_sha=attempt.commit_sha
            )
            if not manual_approved:
                self.store.save(feature)
                return self._stop(
                    feature,
                    "Sir, everything I can check passed. "
                    + "; ".join(verification.awaiting_a_person)
                    + " — that part needs you.",
                    kind="feature.needs_a_person",
                    message_is_success=True,
                )
            # Recorded distinctly from verification.outcomes, never folded
            # into it: an owner looking at a preview and saying "this is
            # fine" is real evidence, but it is a different *kind* of
            # evidence than a Playwright assertion, and conflating the two
            # would let a future reader mistake a human's yes for a
            # measurement nothing here ever took. verification.complete
            # itself is never touched — it stays exactly what was actually
            # measured, permanently; this is a second, independent reason
            # the pipeline may still proceed despite it.
            feature.metadata["manual_acceptance"] = {
                "head_sha": attempt.commit_sha,
                "items": list(verification.awaiting_a_person),
                "owner_confirmed": True,
                "confirmed_at": self.clock(),
            }
            self.store.save(feature)
            self._record(
                feature,
                "feature.manual_items_confirmed",
                "; ".join(verification.awaiting_a_person),
            )

        self._transition(feature, FeatureState.READY, verification.summary())
        pr = self._open_pull_request(feature)
        self._cleanup_worktree(feature)
        self._release(feature)

        message = f"Sir, {feature.title} is ready. Preview: {feature.preview_url}"
        if pr.get("url"):
            message += f" — pull request: {pr['url']}"
        return StepResult(feature, progressed=False, message=message)

    def _open_pull_request(self, feature: FeatureRequest) -> Dict[str, Any]:
        """Open the pull request, if there is a shipper and it is permitted.

        Opening one is where Wiz's autonomy over a feature ends by default.
        Merging is a different authority, deliberately not exercised here — see
        :mod:`openjarvis.wiz.features.shipping`.
        """
        if self.shipper is None:
            return {}
        try:
            result = self.shipper.open_pull_request(feature)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("could not open a pull request: %s", exc)
            return {}
        self.store.save(feature)
        if result.get("created"):
            self._record(feature, "feature.pr_created", str(result.get("url", "")))
        return result

    def recover_missing_pull_request(self, feature_id: str) -> FeatureRequest:
        """Retry opening a pull request for a READY feature that never got one.

        The gap ``run()`` cannot self-heal: :meth:`_open_pull_request` runs
        exactly once, at the ``VERIFYING -> READY`` transition inside
        :meth:`_finish`, and ``run()`` on an already-READY feature is
        deliberately a no-op — see its own docstring. A feature that reached
        READY while opening the pull request itself failed (a stale
        integration, a permission error, a transient GitHub outage) had no
        way back before this. This is that one missing step, run once
        evidence proves it is still safe to — not a second ``run()``, and not
        a bypass of anything ``ship()`` itself still checks fresh before
        merging.

        Idempotent: a feature that already has a pull request is returned
        unchanged, so calling this by mistake, twice, or after a restart
        never risks a duplicate. A refusal — wrong state, elevated risk,
        evidence that has gone stale — leaves the feature exactly READY, the
        same contract ``ship()``'s own refusals keep; nothing here fabricates
        a state transition.
        """
        feature = self._load(feature_id)

        if feature.state is not FeatureState.READY:
            self._record(
                feature,
                "feature.pr_recovery_refused",
                f"the feature is {feature.state.value}, not READY",
            )
            return feature

        if feature.pr_number:
            # Already has one — not a refusal, just nothing to recover.
            return feature

        if self.shipper is None:
            self._record(
                feature, "feature.pr_recovery_refused", "no shipper is configured"
            )
            return feature

        if (feature.risk or "").strip().upper() == Risk.HIGH.value:
            self._record(
                feature,
                "feature.pr_recovery_refused",
                "risk is HIGH; recovery does not open pull requests unattended "
                "for HIGH risk",
            )
            return feature

        attempt = feature.attempts[-1] if feature.attempts else None
        if attempt is None or not attempt.commit_sha:
            self._record(
                feature,
                "feature.pr_recovery_refused",
                "no verified commit is on record",
            )
            return feature

        verification = feature.metadata.get("verification") or {}
        if (
            not verification.get("passed")
            or verification.get("commit_sha") != attempt.commit_sha
        ):
            self._record(
                feature,
                "feature.pr_recovery_refused",
                "the recorded acceptance evidence does not match the verified commit",
            )
            return feature

        try:
            current_head = self.shipper.github.branch_head_sha(feature.branch)
        except Exception as exc:
            self._record(
                feature,
                "feature.pr_recovery_refused",
                f"could not read the branch: {exc}",
            )
            return feature
        if not current_head or current_head[:12] != attempt.commit_sha[:12]:
            self._record(
                feature,
                "feature.pr_recovery_refused",
                "the branch has moved since verification; this is not a simple retry",
            )
            return feature

        if self.preview is not None:
            observation = self.preview.observe(
                commit_sha=attempt.commit_sha, branch=feature.branch
            )
            if not observation.usable:
                self._record(
                    feature,
                    "feature.pr_recovery_refused",
                    observation.reason or "the preview is no longer usable",
                )
                return feature

        pr = self._open_pull_request(feature)
        if pr.get("created") or pr.get("reconciled"):
            self._cleanup_worktree(feature)
        return feature

    # -- the iterative loop -------------------------------------------------

    def _retry_transient_plan_failure(
        self, feature: FeatureRequest, session: Any
    ) -> StepResult:
        """Claude Code was out of capacity, not unable to understand the task.

        A usage-limit exit is not evidence the request is hard to build — it
        is evidence the machine ran out of runway. Treating it like an
        ordinary plan failure sent a fine, unread request to a human with a
        message ("could not work out how to build this: exit code 1") that
        did not even say what actually happened, which is exactly what
        FEAT-00017 hit.

        Reuses the same bounded-attempts policy and the same shape
        :meth:`_retry_or_stop` already applies to build failures: state stays
        at ``UNDERSTANDING`` — ``UNDERSTANDING -> UNDERSTANDING`` is not a
        transition, and pretending it is would put a step in the history that
        never happened — and :meth:`StepResult.progressed` defaults to
        ``True``, so :meth:`run`'s own loop is what retries, immediately,
        with no new scheduler and no worktree torn down. Only once the bound
        is spent does this give up, and even then with the real reason.
        """
        attempts = int(feature.metadata.get("plan_transient_attempts", 0)) + 1
        feature.metadata["plan_transient_attempts"] = attempts
        feature.updated_at = self.clock()
        self.store.save(feature)
        reason = f"Claude Code usage/session limit: {session.error}"
        self._record(feature, "feature.plan_transient_capacity", reason)

        if attempts > self.max_attempts:
            return self._stop(feature, reason, kind="feature.plan_capacity_exhausted")

        return StepResult(feature, message="Claude Code is at its usage limit; retrying...")

    def _retry_or_stop(
        self, feature: FeatureRequest, attempt: FeatureAttempt, evidence: str
    ) -> StepResult:
        """Feed the failure back to Claude, or give up and say so.

        §16: do not notify the operator on the first normal failure. A first
        attempt that fails a type check is not news; it is what the loop is for.
        """
        attempt.finished_at = self.clock()
        # The evidence, not the one-line reason, is what goes on the attempt.
        # The next context pack is built from the attempts, so anything kept
        # only in metadata never reaches Claude — which is how a second attempt
        # ends up being told "the preview failed" and nothing more.
        attempt.failure = evidence.strip() or attempt.failure
        feature.metadata["last_evidence"] = evidence[:8000]

        if feature.attempts_used >= self.max_attempts:
            return self._stop(
                feature,
                f"Sir, I tried {feature.attempts_used} times and could not get "
                f"{feature.title} working. The last problem was: "
                f"{_first_line(attempt.failure)}",
                kind="feature.attempts_exhausted",
            )

        reason = f"attempt {attempt.number} failed: {_first_line(attempt.failure)}"
        if feature.state is FeatureState.BUILDING:
            # Already there: the coding session itself is what failed. Recording
            # without transitioning, because BUILDING -> BUILDING is not a
            # transition and pretending it is would put a step in the history
            # that never happened.
            feature.updated_at = self.clock()
            self.store.save(feature)
        else:
            self._transition(feature, FeatureState.BUILDING, reason)
        self._record(feature, "feature.retrying", f"{reason}\n{evidence[:2000]}")
        return StepResult(feature, message="Trying a different approach...")

    def _hypothesis_for(self, feature: FeatureRequest) -> str:
        """What Wiz believes is wrong, going into this attempt."""
        if not feature.attempts:
            return "first attempt"
        evidence = str(feature.metadata.get("last_evidence", ""))
        return f"attempt {len(feature.attempts) + 1}, after: {_first_line(evidence)}"

    # -- helpers -----------------------------------------------------------

    def _worktree_for(self, feature: FeatureRequest) -> Any:
        """The feature's isolated worktree, created on first use.

        "First use" is process-relative, and this in-memory cache is not the
        only fact that says so — ``feature.worktree``/``.branch``/``.base_sha``
        are persisted on the feature itself and can point at a worktree this
        process has never seen (a restarted watcher, or a one-off recovery
        run against an existing HUMAN_REQUIRED feature). Reused via
        :meth:`~.workspace.FeatureWorkspace.reuse` when it still checks out —
        never re-created — because ``create()`` unconditionally removes
        whatever already exists at that path, and for a feature with an
        existing attempt that is somebody else's uncommitted diff.
        """
        existing = self._worktrees.get(feature.id)
        if existing is not None:
            return existing

        reused = self.workspace.reuse(
            feature.id,
            path=feature.worktree,
            branch=feature.branch,
            base_sha=feature.base_sha,
        )
        if reused is not None:
            self._worktrees[feature.id] = reused
            return reused

        worktree = self.workspace.create(
            feature.id,
            title=feature.title,
            base_ref=self.profile.base_branch or "HEAD",
        )
        self._worktrees[feature.id] = worktree
        feature.worktree = worktree.path
        feature.branch = worktree.branch
        feature.base_sha = worktree.base_commit
        self.store.save(feature)
        return worktree

    def _cleanup_worktree(self, feature: FeatureRequest) -> None:
        """Tear down a READY feature's worktree — its job is done.

        By the time a feature reaches READY, its commit is already pushed to
        the base branch's remote (that is what the preview was built from),
        so nothing downstream — opening the pull request, shipping, postship
        verification — ever reads the local worktree again. Left in place, it
        is pure disk cost: node_modules and a `next build` typically outweigh
        the source by two orders of magnitude, and every READY feature leaves
        one behind forever. FEAT-00001 through FEAT-00006 did exactly that —
        about 20 GiB of dead build output before this was wired in.

        Best-effort: a feature is READY either way, and a worktree that
        cannot be removed is a disk problem to fix by hand, not a reason to
        fail the feature that already succeeded.
        """
        worktree = self._worktrees.pop(feature.id, None)
        if worktree is None:
            return
        try:
            self.workspace.remove(worktree, succeeded=True)
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "could not clean up the worktree for %s", feature.id, exc_info=True
            )

    def _release(self, feature: FeatureRequest) -> None:
        if self.queue is not None:
            self.queue.finish(feature.id)

    def _check_preemption(self, feature: FeatureRequest) -> Optional[StepResult]:
        """Stop for production, between steps, with the worktree intact."""
        if self.queue is None or not self.queue.must_yield(feature.id):
            return None
        self._record(feature, "feature.yielded", "production needs the machine")
        return StepResult(
            feature,
            progressed=False,
            message=(
                "I have paused this while I deal with something on the live site. "
                "Nothing is lost; I will pick it up again."
            ),
        )

    def _authority_denial(self, feature: FeatureRequest) -> str:
        """Whether the requesting channel may have code written for it at all."""
        if self.policy is None:
            return ""
        actor = Actor(
            actor_id=feature.actor_id,
            channel=_channel_for(feature.source),
            authenticated=True,
        )
        decision = self.policy.decide(
            actor, Authority.CODE_WRITE, capability="feature.build"
        )
        if decision.allowed:
            return ""
        return (
            "I am not allowed to change code for a request that arrived this "
            f"way: {decision.reason}."
        )

    def _approved_for(
        self, feature: FeatureRequest, contract: AcceptanceContract
    ) -> bool:
        """Whether a live approval binds this exact plan.

        Bound to the plan, so a plan that changed after the operator read it has
        no approval — there is nothing to reuse, because the token names
        something that no longer exists.
        """
        token = str(feature.metadata.get("approval_token", ""))
        if not token or self.approvals is None:
            return False
        try:
            self.approvals.redeem(
                token,
                capability="feature.build",
                subject=feature.id,
                parameters={
                    "plan": _digest(feature.plan),
                    "risk": feature.risk,
                    "acceptance": contract.describe(),
                },
            )
        except ApprovalError as exc:
            feature.metadata["approval_error"] = str(exc)
            return False
        feature.approved_plan_hash = _digest(feature.plan)
        feature.metadata.pop("approval_token", None)
        return True

    def _medium_ship_approved(self, feature: FeatureRequest, *, head_sha: str) -> bool:
        """Whether a live, redeemed approval authorises shipping this exact
        MEDIUM-risk feature at this exact head SHA.

        A one-off yes, never a policy change: it never touches
        ``merge_medium_risk`` (that switch is unaffected either way), it
        names this feature and this head SHA specifically — the same
        fingerprint-and-redeem shape :meth:`_approved_for` already uses for a
        HIGH-risk plan, applied here to a MEDIUM-risk merge instead of a
        build — and it is consumed the moment it is used. A feature whose
        head SHA has moved since the approval was issued gets a fingerprint
        mismatch, not a stale yes: see ``approvals.py``'s module docstring.
        """
        if (feature.risk or "").strip().upper() != Risk.MEDIUM.value:
            return False
        token = str(feature.metadata.get("ship_approval_token", ""))
        if not token or self.approvals is None:
            return False
        try:
            self.approvals.redeem(
                token,
                capability="feature.ship",
                subject=feature.id,
                parameters={"risk": feature.risk, "head_sha": head_sha},
            )
        except ApprovalError as exc:
            feature.metadata["ship_approval_error"] = str(exc)
            return False
        feature.metadata["ship_approved_head_sha"] = head_sha
        feature.metadata.pop("ship_approval_token", None)
        return True

    def _awaiting_items_approved(
        self, feature: FeatureRequest, *, head_sha: str
    ) -> bool:
        """Whether a live, redeemed approval confirms this exact feature's
        current outstanding "needs a person" items at this exact head SHA.

        Same shape as :meth:`_medium_ship_approved`, for the
        ``nothing_awaiting_a_person`` gate instead of the risk gate: bound to
        the feature, the head SHA, and the exact *set* of outstanding
        descriptions currently stored — not just their count, and not "any
        approval this feature has ever gotten". A new manual criterion
        appearing, or the diff changing what a criterion could not measure,
        changes the fingerprint and the approval no longer matches, the same
        way a moved head SHA already does not.
        """
        token = str(feature.metadata.get("ship_manual_approval_token", ""))
        if not token or self.approvals is None:
            return False
        outstanding = sorted(
            (feature.metadata.get("verification") or {}).get("awaiting_a_person") or []
        )
        if not outstanding:
            return False
        try:
            self.approvals.redeem(
                token,
                capability="feature.ship_manual_items",
                subject=feature.id,
                parameters={"head_sha": head_sha, "outstanding": outstanding},
            )
        except ApprovalError as exc:
            feature.metadata["ship_manual_approval_error"] = str(exc)
            return False
        feature.metadata["ship_manual_approved_head_sha"] = head_sha
        feature.metadata.pop("ship_manual_approval_token", None)
        return True

    def _proposed_criteria(self, feature: FeatureRequest) -> Sequence[Criterion]:
        """Criteria the planning session suggested. Additive only."""
        proposed = feature.metadata.get("proposed_criteria")
        if not proposed:
            return ()
        return criteria_from_mapping(proposed)

    def _verify_preview_sha(
        self, feature: FeatureRequest, attempt: FeatureAttempt
    ) -> Optional[StepResult]:
        """Exact SHA gate: fail closed if Preview SHA != expected SHA.

        Returns None if SHA matches (proceed with verification).
        Returns StepResult with failure if SHA mismatch or missing (stop).
        """
        expected_sha = attempt.commit_sha or ""
        if not expected_sha:
            return self._retry_or_stop(
                feature,
                attempt,
                "No commit SHA recorded for verification; cannot gate browser acceptance.",
            )

        # Get the deployment SHA from preview observation
        preview_obs = attempt.verification.get("preview") or {}
        actual_sha = preview_obs.get("commit_sha") or ""

        if not actual_sha:
            return self._retry_or_stop(
                feature,
                attempt,
                "Preview deployment Git SHA is not available; cannot verify against feature commit.",
            )

        # Exact SHA match required
        expected_short = expected_sha[:12]
        actual_short = actual_sha[:12]

        if expected_sha != actual_sha:
            message = (
                f"Exact SHA mismatch: expected {expected_short}, "
                f"but Preview deployed {actual_short}. "
                "This prevents browser acceptance against unverified code."
            )
            return self._retry_or_stop(feature, attempt, message)

        # SHA matches — proceed
        return None

    @staticmethod
    def _gate_outcomes(attempt: FeatureAttempt) -> List[CriterionOutcome]:
        checks = attempt.checks or {}
        results = checks.get("results") or checks.get("checks") or []
        outcomes: List[CriterionOutcome] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            if not item.get("ran", True):
                continue
            outcomes.append(
                gate_outcome(
                    str(item.get("name", "check")),
                    bool(item.get("passed", False)),
                    str(item.get("summary", ""))[:500],
                )
            )
        return outcomes

    def _transition(
        self, feature: FeatureRequest, target: FeatureState, reason: str
    ) -> None:
        feature.transition(target, at=self.clock(), reason=reason)
        self.store.save(feature)
        self._record(feature, f"feature.{target.value.lower()}", reason)

    def _stop(
        self,
        feature: FeatureRequest,
        message: str,
        *,
        kind: str,
        message_is_success: bool = False,
    ) -> StepResult:
        """End the feature's autonomous progress and say why."""
        if not feature.terminal:
            feature.transition(
                FeatureState.HUMAN_REQUIRED, at=self.clock(), reason=message[:300]
            )
        self.store.save(feature)
        self._record(feature, kind, message)
        self._release(feature)
        return StepResult(feature, progressed=False, message=message)

    def _load(self, feature_id: str) -> FeatureRequest:
        feature = self.store.get(feature_id)
        if feature is None:
            raise KeyError(f"no feature request {feature_id!r}")
        return feature

    def _record(self, feature: FeatureRequest, kind: str, reason: str) -> None:
        if self.journal is not None:
            try:
                self.journal.record(
                    at=self.clock(),
                    kind=kind,
                    capability="feature.build",
                    actor_id=feature.actor_id,
                    channel=feature.source,
                    reason=reason[:1000],
                    detail={
                        "feature_id": feature.id,
                        "state": feature.state.value,
                        "risk": feature.risk,
                        "attempts": feature.attempts_used,
                    },
                )
            except Exception:
                logger.exception("could not journal a feature event")

        # Every event is journalled; the owner hears about almost none of
        # them. FeatureOwnerNotifier.notify itself decides which `kind`s are
        # an outcome worth a phone buzzing rather than a step — see its own
        # module docstring — so this call is unconditional and cheap when
        # nothing is configured to receive it.
        if self.owner_notifier is not None:
            try:
                self.owner_notifier.notify(feature, kind=kind, reason=reason)
            except Exception:
                logger.exception("owner notification failed for %s", feature.id)


# ---------------------------------------------------------------------------
# Small deterministic helpers
# ---------------------------------------------------------------------------


def _title_from(text: str, *, max_words: int = 8) -> str:
    words = text.replace("\n", " ").split()
    title = " ".join(words[:max_words])
    return title[:120].rstrip(" .,")


def _brief_for(feature: FeatureRequest) -> str:
    """What Claude is told the goal is.

    The operator's words come first and verbatim. Wiz's restatement follows,
    marked as a restatement, so that a wrong restatement is visible to the agent
    rather than replacing what was actually asked for.
    """
    lines = [
        "The operator asked for this, in their words:",
        f"    {feature.operator_request}",
        "",
        f"Target: {feature.target or 'the application'}",
    ]
    if feature.desired_outcome and feature.desired_outcome != feature.operator_request:
        lines.append(f"What should be true afterwards: {feature.desired_outcome}")
    return "\n".join(lines)


def _plain_failure(exc: Exception) -> str:
    """What the operator is told when something unexpected went wrong.

    The full exception is logged and journalled; what reaches the operator is a
    sentence they can act on. A dump of `git worktree add -b ... failed (255)`
    is precise and useless: it tells somebody who is not looking at the code
    nothing about what to do, and §34 asks for simple English.
    """
    from openjarvis.reliability.workspace import WorkspaceError

    if isinstance(exc, WorkspaceError):
        return (
            "I could not set up a clean copy of the repository to work in, so "
            "I have not changed anything. It usually means a previous attempt "
            "left something behind."
        )
    if isinstance(exc, (OSError, PermissionError)):
        return (
            "I could not read or write something on disk, so I have stopped "
            "before changing anything."
        )
    # Anything genuinely unforeseen. One line, and the detail is in the log.
    first = str(exc).strip().split("\n", 1)[0]
    return f"Something went wrong and I have stopped: {first[:160]}"


def _default_provision(profile: EngineeringProfile, workspace: str) -> CheckResult:
    """The real provisioner: dependencies, under the same pinned Node the
    check suite itself runs under, or whatever this process already has if
    the project pins none.
    """
    node_bin_dir = profile.resolve_node_bin_dir()
    return provision_check(workspace, path_prepend=[node_bin_dir] if node_bin_dir else None)


def _checks_record(result: Any) -> Dict[str, Any]:
    """The gate outcome, as a dict that carries its own summary.

    ``CheckSuiteResult.to_dict`` deliberately does not include ``summary`` —
    it is a property, and the incident record does not need it. The feature
    record does: the pull request body and the Control Center both show "what
    the checks said", and reading a key that is not there had them silently
    showing nothing.
    """
    if not hasattr(result, "to_dict"):
        return {}
    record = dict(result.to_dict())
    record.setdefault("summary", str(getattr(result, "summary", "")))
    return record


def _gate_feedback(result: Any, *, fallback: str = "the local checks failed") -> str:
    """What the next attempt is told about a failed gate."""
    feedback = getattr(result, "feedback", None)
    if callable(feedback):
        rendered = str(feedback() or "").strip()
        if rendered:
            return rendered
    return str(getattr(result, "summary", "") or fallback)


def _commit_message(feature: FeatureRequest) -> str:
    return f"{feature.title}\n\n{feature.operator_request}\n\nFeature: {feature.id}\n"


def _first_line(text: str, *, limit: int = 200) -> str:
    line = (text or "").strip().split("\n", 1)[0]
    return line[:limit]


def _digest(text: str) -> str:
    import hashlib

    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


_PATH_PATTERN = re.compile(r"[\w./-]+\.(?:ts|tsx|js|jsx|py|sql|json|ya?ml|css|scss)")

#: A line that is *only* a markdown heading — "# Files" or "**Files**" — not a
#: path mention inside an ordinary sentence that merely happens to use bold.
_HEADING_LINE = re.compile(
    r"^\s*(?:#{1,6}\s*(?P<hash>.+?)\s*#*|\*\*(?P<bold>[^*].*?[^*])\*\*)\s*$"
)

#: A heading naming the section that lists files the change will actually
#: touch — the planning prompt asks for exactly this ("the files you expect
#: to change"), so most plans that name a files section title it close to
#: this wording. "unchang..." is filtered separately below, so a "files
#: that stay unchanged" heading is not read as the opposite of what it says.
_FILES_TO_CHANGE_HEADING = re.compile(r"\bfiles?\b.{0,20}\bchang", re.IGNORECASE)


#: Paths a plan mentions, used to classify risk before anything is written.
def _paths_mentioned(plan: str) -> List[str]:
    """Paths named in the plan's own "files expected to change" section.

    Scoped to that section rather than the whole plan: a file cited only as
    existing, unrelated context — "e2e/auth.spec.ts already covers this
    page" — is not a file the change touches, and extracting every
    path-shaped token in the prose read it as though it were, raising risk
    on a plan that never proposed touching it. Falls back to scanning the
    whole plan when no such section can be found, which keeps this
    classifier's own conservative default: a plan that does not name its
    files at all is read as though anything it mentions might be touched,
    not as though nothing is.
    """
    text = plan or ""
    lines = text.splitlines()
    headings: List[tuple] = []
    for i, line in enumerate(lines):
        match = _HEADING_LINE.match(line)
        if match:
            headings.append((i, (match.group("hash") or match.group("bold") or "").strip()))

    sections = [
        "\n".join(lines[line_no + 1 : (headings[idx + 1][0] if idx + 1 < len(headings) else len(lines))])
        for idx, (line_no, heading) in enumerate(headings)
        if _FILES_TO_CHANGE_HEADING.search(heading) and "unchang" not in heading.lower()
    ]

    scope = "\n".join(sections) if sections else text
    return list(dict.fromkeys(_PATH_PATTERN.findall(scope)))[:200]


def _agent_risk_opinion(plan: str) -> Optional[Risk]:
    """A risk level the planning session raised, if it raised one.

    Only ever read as a *raise*: :func:`classify` takes the maximum, so an agent
    saying "this is high risk" is believed and an agent saying "this is safe" is
    not consulted. Being too careful is not a failure mode worth defending
    against.
    """
    lowered = (plan or "").lower()
    if "high risk" in lowered or "high-risk" in lowered:
        return Risk.HIGH
    if "medium risk" in lowered or "medium-risk" in lowered:
        return Risk.MEDIUM
    return None


def _channel_for(source: str) -> Any:
    from openjarvis.wiz.authority import Channel

    try:
        return Channel(source)
    except ValueError:
        return Channel.AUTONOMOUS
