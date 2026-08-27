"""Recovering a feature's stored state from real, current, external evidence.

A build session can crash after doing correct, real work and before the
pipeline gets to record it: the coding session finished, the diff is right,
but the process died before ``_deploy_preview`` ran, or between a commit and a
push, or anywhere else in :class:`~openjarvis.wiz.features.pipeline.FeaturePipeline`.
The feature is then stuck short of ``READY`` even though nothing is actually
wrong with it.

The wrong fix is to let a person — or another Claude session — hand-advance
the stored state to ``READY`` because they are confident the work is good.
That is the exact shortcut §8 exists to forbid everywhere else in this system:
a claim is not evidence. Editing ``features.db``/``journal.jsonl`` directly is
the same shortcut wearing a database client instead of a sentence, which is
why the auto-mode classifier refuses it — those files are Wiz's authoritative
state and audit trail, and a raw write bypasses every gate that exists to keep
them honest.

:func:`recover_feature` is the supported alternative: a narrow, evidence-gated
API that reconstructs a feature's progress by *re-checking* everything a
normal pipeline run would have checked, against the pull request's exact
current head SHA — never against what an earlier attempt claimed, however
recent. It reuses :meth:`FeatureRequest.transition`, so every step it records
is still validated against :data:`~openjarvis.wiz.features.model.LEGAL_TRANSITIONS`,
and every step is journalled the same way :class:`FeaturePipeline` journals one.

What it will not do, structurally rather than by configuration:

* advance a feature whose repository, branch or pull-request state does not
  match what is stored (wrong repository, wrong branch, a closed or merged
  pull request all refuse rather than proceed);
* trust a cached check result, preview URL or commit SHA — every gate below
  is re-run against the PR's head SHA as observed right now;
* touch a HIGH-risk feature (a person reviews those, always);
* grant shipping authority. Recovery's furthest reach is ``READY``, the same
  ceiling :meth:`FeaturePipeline.run` has; merging is a different method
  (:meth:`FeaturePipeline.ship`) that recovery never calls.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from openjarvis.reliability.briefing import has_critical_secret
from openjarvis.wiz.features.model import FeatureRequest, FeatureState
from openjarvis.wiz.features.store import FeatureStore

logger = logging.getLogger(__name__)

__all__ = [
    "RecoveryRefusal",
    "RecoveryResult",
    "FeatureRecovery",
    "recover_feature",
]

#: States a crashed attempt can plausibly have left a feature in. Anything
#: else (RECEIVED, UNDERSTANDING, PLANNING, APPROVED_FOR_BUILD, READY,
#: MERGING, DEPLOYING, ...) either has no build to recover or is already past
#: the point recovery exists to bridge.
#:
#: HUMAN_REQUIRED is included on purpose: it is where the ordinary attempt
#: loop lands a feature whose attempts ran out, and a crashed or interrupted
#: session reaching that state through no fault of the work itself is exactly
#: the case recovery exists for. Leaving it is never done through
#: :meth:`FeatureRequest.transition` — see
#: :meth:`FeatureRequest.resume_from_human_required` for why.
_RECOVERABLE_STATES = frozenset(
    {
        FeatureState.BUILDING,
        FeatureState.TESTING,
        FeatureState.PREVIEWING,
        FeatureState.VERIFYING,
        FeatureState.HUMAN_REQUIRED,
    }
)

#: The path from each recoverable state to READY, in order. Every hop here is
#: one already present in LEGAL_TRANSITIONS — this is a subset, not a new
#: rule. HUMAN_REQUIRED has no entry: leaving it is
#: ``resume_from_human_required``, not ``transition`` (see ``_advance``), and
#: that one hop always lands on BUILDING, whose entry below takes it the rest
#: of the way.
_PATH_TO_READY: Dict[FeatureState, List[FeatureState]] = {
    FeatureState.BUILDING: [
        FeatureState.TESTING,
        FeatureState.PREVIEWING,
        FeatureState.VERIFYING,
        FeatureState.READY,
    ],
    FeatureState.TESTING: [
        FeatureState.PREVIEWING,
        FeatureState.VERIFYING,
        FeatureState.READY,
    ],
    FeatureState.PREVIEWING: [FeatureState.VERIFYING, FeatureState.READY],
    FeatureState.VERIFYING: [FeatureState.READY],
}


@dataclass(frozen=True, slots=True)
class RecoveryRefusal:
    """One reason recovery did not proceed."""

    code: str
    detail: str = ""

    def __str__(self) -> str:  # pragma: no cover - convenience
        return f"{self.code}: {self.detail}" if self.detail else self.code


@dataclass
class RecoveryResult:
    """What recovery decided, and every reason it considered."""

    feature_id: str
    recovered: bool
    state: str
    refusals: List[RecoveryRefusal] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.recovered

    def explain(self) -> str:
        if self.recovered:
            return f"{self.feature_id} is genuinely {self.state}"
        return "; ".join(str(r) for r in self.refusals) or "not recovered"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "recovered": self.recovered,
            "state": self.state,
            "refusals": [{"code": r.code, "detail": r.detail} for r in self.refusals],
        }


def _real_git_rev_parse_head(worktree: str) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _real_git_diff(worktree: str, base_sha: str, head_sha: str) -> str:
    if not base_sha or not head_sha:
        return ""
    proc = subprocess.run(
        ["git", "diff", f"{base_sha}..{head_sha}"],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else ""


@dataclass
class FeatureRecovery:
    """Reconciles a feature's stored state with real, current evidence.

    Every collaborator is injected, in the same style as
    :class:`~openjarvis.wiz.features.pipeline.FeaturePipeline` — so a test
    proving a refusal proves it about the same object the operator runs, and
    so this can be built from the real assembled runtime's own ``github`` and
    ``preview`` clients rather than a parallel set that might drift from them.
    """

    store: FeatureStore
    profile: Any  # EngineeringProfile
    github: Any  # a GitHubSource, or anything shaped like one
    preview: Any  # a PreviewObserver, or anything shaped like one
    check_suite_factory: Callable[[Any], Any]
    journal: Any = None
    clock: Callable[[], str] = None  # type: ignore[assignment]
    required_status_contexts: Sequence[str] = ()
    git_rev_parse_head: Callable[[str], str] = _real_git_rev_parse_head
    git_diff: Callable[[str, str, str], str] = _real_git_diff

    def __post_init__(self) -> None:
        if self.clock is None:
            from datetime import datetime, timezone

            self.clock = lambda: datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )

    # -- the public verb -----------------------------------------------

    def recover(
        self, feature_id: str, *, reason: str, pr_number: Optional[int] = None
    ) -> RecoveryResult:
        """Bring *feature_id* to READY only if fresh evidence proves it belongs there.

        ``reason`` is the operator's account of why recovery is being run
        (e.g. "the build session was interrupted by an API error mid-recovery
        of its own WIP commit") — recorded on every journal entry this writes,
        so the audit trail says *why* a feature jumped states outside the
        ordinary attempt loop, not just that it did.

        ``pr_number`` lets an operator name a pull request to *adopt* when the
        feature has none recorded — the case where a person opened the real
        pull request by hand (or another tool did) rather than through
        :meth:`FeatureShipper.open_pull_request`. It is never used to override
        a pull request the feature already has recorded; adoption only fills
        an empty slot, and every check below still applies to whatever number
        is in play — a named PR from the wrong branch or the wrong repository
        refuses exactly as a recorded one would.
        """
        feature = self.store.get(feature_id)
        if feature is None:
            return RecoveryResult(
                feature_id,
                False,
                "UNKNOWN",
                [RecoveryRefusal("not_found", f"no feature {feature_id!r}")],
            )

        refusal = self._structural_refusal(feature)
        if refusal is not None:
            return self._refuse(feature, [refusal])

        effective_pr_number = feature.pr_number or pr_number
        if not effective_pr_number:
            return self._refuse(
                feature,
                [
                    RecoveryRefusal(
                        "no_pull_request",
                        "feature has no recorded pull request, and none was "
                        "given to adopt",
                    )
                ],
            )

        pr = self.github.get_pull_request(effective_pr_number)
        refusals = self._pr_refusals(feature, pr, expected_number=effective_pr_number)
        if refusals:
            return self._refuse(feature, refusals)

        head_sha = str(pr.get("head_sha", ""))

        if feature.state is FeatureState.READY:
            # Idempotent: recovering an already-recovered feature against the
            # same evidence is a no-op, not a second climb through the FSM
            # (READY -> READY is not a legal transition, and re-running it
            # would be pointless even if it were).
            attempt = feature.attempts[-1] if feature.attempts else None
            if attempt is not None and attempt.commit_sha == head_sha:
                return RecoveryResult(feature.id, True, feature.state.value, [])
            return self._refuse(
                feature,
                [
                    RecoveryRefusal(
                        "already_ready_mismatch",
                        "feature is READY but its recorded commit is not the PR's "
                        "current head; this is a new push, not a recovery",
                    )
                ],
            )

        if feature.state not in _RECOVERABLE_STATES:
            return self._refuse(
                feature,
                [
                    RecoveryRefusal(
                        "wrong_state",
                        f"{feature.state.value} is not a state recovery can act on",
                    )
                ],
            )

        evidence_refusals = self._gather_and_check_evidence(feature, pr, head_sha)
        if evidence_refusals:
            return self._refuse(feature, evidence_refusals)

        self._advance(feature, head_sha=head_sha, pr=pr, reason=reason)
        return RecoveryResult(feature.id, True, feature.state.value, [])

    # -- refusal checks, each one independent and named -----------------

    def _structural_refusal(self, feature: FeatureRequest) -> Optional[RecoveryRefusal]:
        if feature.repository != self.profile.repository:
            return RecoveryRefusal(
                "wrong_repository",
                f"feature.repository={feature.repository!r} != the configured "
                f"target {self.profile.repository!r}",
            )
        if not feature.branch:
            return RecoveryRefusal("no_branch", "feature has no recorded branch")
        if feature.risk.strip().upper() == "HIGH":
            return RecoveryRefusal(
                "risk_too_high", "HIGH-risk features are never recovered automatically"
            )
        return None

    def _pr_refusals(
        self, feature: FeatureRequest, pr: Dict[str, Any], *, expected_number: int
    ) -> List[RecoveryRefusal]:
        refusals: List[RecoveryRefusal] = []
        if int(pr.get("number", 0) or 0) != expected_number:
            refusals.append(
                RecoveryRefusal(
                    "pr_number_mismatch",
                    f"asked for #{expected_number}, got #{pr.get('number')}",
                )
            )
            return refusals  # nothing else here is trustworthy
        state = str(pr.get("state", "")).lower()
        if pr.get("merged"):
            refusals.append(
                RecoveryRefusal(
                    "stale_pull_request", f"PR #{feature.pr_number} is already merged"
                )
            )
        elif state != "open":
            refusals.append(
                RecoveryRefusal(
                    "stale_pull_request",
                    f"PR #{feature.pr_number} is {state or 'unknown'}",
                )
            )
        if pr.get("head_ref") != feature.branch:
            refusals.append(
                RecoveryRefusal(
                    "wrong_branch",
                    f"PR head {pr.get('head_ref')!r} != "
                    f"feature.branch {feature.branch!r}",
                )
            )
        if not pr.get("head_sha"):
            refusals.append(
                RecoveryRefusal("missing_sha", "pull request has no head sha")
            )
        if pr.get("mergeable") is False:
            refusals.append(
                RecoveryRefusal(
                    "not_mergeable", "the pull request reports a merge conflict"
                )
            )
        return refusals

    def _gather_and_check_evidence(
        self, feature: FeatureRequest, pr: Dict[str, Any], head_sha: str
    ) -> List[RecoveryRefusal]:
        refusals: List[RecoveryRefusal] = []

        if feature.worktree:
            local_sha = self.git_rev_parse_head(feature.worktree)
            if local_sha and local_sha != head_sha:
                refusals.append(
                    RecoveryRefusal(
                        "sha_mismatch",
                        f"local worktree HEAD {local_sha[:12]} != "
                        f"PR head {head_sha[:12]}",
                    )
                )
                return refusals

        if not feature.worktree:
            refusals.append(
                RecoveryRefusal(
                    "no_worktree", "no local worktree recorded to verify checks against"
                )
            )
            return refusals

        suite = self.check_suite_factory(self.profile)
        result = suite.run(workspace=feature.worktree)
        if not getattr(result, "passed", False):
            feedback = getattr(result, "feedback", None)
            detail = (
                str(feedback())
                if callable(feedback)
                else str(getattr(result, "summary", "checks failed"))
            )
            refusals.append(RecoveryRefusal("checks_failed", detail[:2000]))
            return refusals

        diff_text = self.git_diff(feature.worktree, feature.base_sha, head_sha)
        if has_critical_secret(diff_text):
            refusals.append(
                RecoveryRefusal(
                    "secret_detected",
                    "the diff contains what looks like a real credential",
                )
            )
            return refusals

        if self.required_status_contexts:
            status = self.github.combined_status(
                head_sha, required_contexts=self.required_status_contexts
            )
            missing = status.get("missing_required") or []
            if missing or status.get("state") not in ("success",):
                refusals.append(
                    RecoveryRefusal(
                        "status_not_green",
                        f"required status not green: {status.get('required')}",
                    )
                )
                return refusals

        observation = self.preview.observe(commit_sha=head_sha, branch=feature.branch)
        if not observation.usable:
            refusals.append(
                RecoveryRefusal(
                    "preview_not_usable", observation.reason or "preview is not usable"
                )
            )
            return refusals

        # Stash what the checks stage found so _advance can attach it to the
        # attempt without re-running anything.
        self._last_check_result = result
        self._last_observation = observation
        return refusals

    # -- applying the recovered state, through the real state machine ---

    def _advance(
        self,
        feature: FeatureRequest,
        *,
        head_sha: str,
        pr: Dict[str, Any],
        reason: str,
    ) -> None:
        attempt = (
            feature.attempts[-1]
            if feature.attempts
            else feature.next_attempt(at=self.clock())
        )
        attempt.commit_sha = head_sha
        attempt.succeeded = True
        attempt.failure = ""
        attempt.finished_at = self.clock()
        result = getattr(self, "_last_check_result", None)
        if result is not None and hasattr(result, "to_dict"):
            record = dict(result.to_dict())
            record.setdefault("summary", str(getattr(result, "summary", "")))
            attempt.checks = record
            feature.metadata["gates"] = record

        feature.pr_url = str(pr.get("url", feature.pr_url))
        feature.pr_number = int(
            pr.get("number", feature.pr_number) or feature.pr_number
        )
        observation = getattr(self, "_last_observation", None)
        if observation is not None:
            feature.preview_url = observation.url

        if feature.state is FeatureState.HUMAN_REQUIRED:
            self._resume(feature, reason)

        for target in _PATH_TO_READY[feature.state]:
            self._transition(feature, target, reason)

    def _resume(self, feature: FeatureRequest, reason: str) -> None:
        """The one hop that leaves HUMAN_REQUIRED — see the method it calls."""
        feature.resume_from_human_required(
            FeatureState.BUILDING, at=self.clock(), reason=f"recovery: {reason}"
        )
        self.store.save(feature)
        self._record(feature, "feature.resumed", reason)

    def _transition(
        self, feature: FeatureRequest, target: FeatureState, reason: str
    ) -> None:
        feature.transition(target, at=self.clock(), reason=f"recovery: {reason}")
        self.store.save(feature)
        self._record(feature, f"feature.{target.value.lower()}", reason)

    def _refuse(
        self, feature: FeatureRequest, refusals: List[RecoveryRefusal]
    ) -> RecoveryResult:
        self._record(
            feature,
            "feature.recovery_refused",
            "; ".join(str(r) for r in refusals),
        )
        return RecoveryResult(feature.id, False, feature.state.value, refusals)

    def _record(self, feature: FeatureRequest, kind: str, reason: str) -> None:
        if self.journal is None:
            return
        try:
            self.journal.record(
                at=self.clock(),
                kind=kind,
                capability="feature.recover",
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
        except Exception:  # noqa: BLE001 - journalling must never mask the result
            logger.exception("could not journal a recovery event for %s", feature.id)


def recover_feature(
    feature_id: str,
    *,
    store: FeatureStore,
    profile: Any,
    github: Any,
    preview: Any,
    check_suite_factory: Callable[[Any], Any],
    journal: Any = None,
    reason: str = "",
    required_status_contexts: Sequence[str] = (),
) -> RecoveryResult:
    """Module-level convenience wrapper around :class:`FeatureRecovery`."""
    recovery = FeatureRecovery(
        store=store,
        profile=profile,
        github=github,
        preview=preview,
        check_suite_factory=check_suite_factory,
        journal=journal,
        required_status_contexts=required_status_contexts,
    )
    return recovery.recover(feature_id, reason=reason)
