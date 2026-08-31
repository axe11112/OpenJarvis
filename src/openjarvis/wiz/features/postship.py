"""After the merge: proving production, and handing over when it is not proven.

§20. The window this covers has a name in the incident machine for a reason — a
change is live and unproven — and it is the window in which an autonomous system
does its most expensive damage, because the operator has already been told the
feature is done.

The sequence is the same shape as verification against the preview, and that is
deliberate: the same contract, checked by the same browser, against the exact
deployment built from the merge commit. A feature that passed one bar and was
spared the other would make the first bar meaningless.

**The handover boundary is the point of this module.** When production does not
agree, this is no longer a product-development problem — it is a reliability
problem, and reliability owns it. But `reliability` must never import `wiz`, so
the handover cannot be a callback registered in the other direction. It is a
plain event object and a handler protocol: Wiz builds a
:class:`ProductionFailure` describing what it saw, and hands it to whatever
handler it was given. The shipped handler opens an incident through
reliability's own store — which is `wiz` importing `reliability`, the permitted
direction — and any other handler is equally valid, including one that only
writes to the journal on a machine where reliability is switched off.

What this module will never do is roll anything back. Deciding to revert a
change that is live in front of users is a judgement about the product, and it
belongs to a person and to the reliability subsystem's own gates, not to the
thing that just built the change and would very much like it to have worked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from openjarvis.wiz.features.acceptance import AcceptanceContract
from openjarvis.wiz.features.model import FeatureRequest, FeatureState
from openjarvis.wiz.features.preview import PreviewObservation, PreviewObserver
from openjarvis.wiz.features.store import FeatureStore
from openjarvis.wiz.features.verification import FeatureVerification, FeatureVerifier

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids the real cycle
    from openjarvis.wiz.features.recovery import RecoveryResult

logger = logging.getLogger(__name__)

__all__ = [
    "PostShipResult",
    "PostShipVerifier",
    "ProductionFailure",
    "handoff_to_reliability",
    "reconcile_external_merge",
    "reverify_production",
]


@dataclass(slots=True)
class ProductionFailure:
    """What Wiz saw in production, in a form somebody else can act on.

    A plain data object with no behaviour and no imports from the reliability
    package, so that handing it over is a function call rather than a
    dependency. Everything a handler needs to open an incident is on it.
    """

    feature_id: str
    title: str
    merge_commit_sha: str
    reason: str

    deployment_url: str = ""
    deployment_state: str = ""

    #: The criteria that failed, in the operator's language.
    failed_criteria: List[str] = field(default_factory=list)

    #: Console errors and failed requests the browser saw. Untrusted: every
    #: string came from a web page.
    observed: List[str] = field(default_factory=list)

    changed_files: List[str] = field(default_factory=list)
    pr_url: str = ""

    def summary(self) -> str:
        """One line, for an incident title."""
        return f"{self.title} did not work in production after it was merged"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "title": self.title,
            "merge_commit_sha": self.merge_commit_sha,
            "reason": self.reason,
            "deployment_url": self.deployment_url,
            "deployment_state": self.deployment_state,
            "failed_criteria": list(self.failed_criteria),
            "observed": list(self.observed),
            "changed_files": list(self.changed_files),
            "pr_url": self.pr_url,
        }


@dataclass(slots=True)
class PostShipResult:
    """Whether production agreed."""

    verified: bool = False
    reason: str = ""

    deployment: Optional[PreviewObservation] = None
    verification: Optional[FeatureVerification] = None

    #: Set when the failure was handed over. Records *that* it was, and to
    #: what — an unhandled production failure is the worst outcome here and
    #: must not be silently possible.
    handed_over_to: str = ""
    failure: Optional[ProductionFailure] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verified": self.verified,
            "reason": self.reason,
            "deployment": self.deployment.to_dict() if self.deployment else None,
            "verification": (
                self.verification.to_dict() if self.verification else None
            ),
            "handed_over_to": self.handed_over_to,
            "failure": self.failure.to_dict() if self.failure else None,
        }

    def summary(self) -> str:
        if self.verified:
            return "production agrees: the feature works where users are"
        if self.handed_over_to:
            return f"production did not agree; handed to {self.handed_over_to}"
        return f"production did not agree: {self.reason}"


@dataclass
class PostShipVerifier:
    """Proves a merged feature in production, or hands the failure over.

    Parameters
    ----------
    deployments:
        A :class:`~openjarvis.wiz.features.preview.PreviewObserver` pointed at
        ``target="production"``. The same class, because the question is
        identical — the exact commit, and READY means READY — and a second
        implementation would be a second set of answers.
    verifier:
        The same browser verifier the preview stage used. Reusing it is the
        point: a feature that passed one bar and was spared the other would make
        the first bar meaningless.
    handler:
        Called with a :class:`ProductionFailure` when production disagrees.
        Defaults to :func:`handoff_to_reliability`.
    """

    deployments: PreviewObserver
    verifier: Optional[FeatureVerifier] = None
    handler: Optional[Callable[[ProductionFailure], str]] = None
    journal: Any = None
    clock: Callable[[], str] = lambda: ""

    def verify(
        self, feature: FeatureRequest, *, merge_commit_sha: str
    ) -> PostShipResult:
        """Check *feature* against production. Never raises.

        A verification that cannot run is a failed verification. The
        alternative is an exception unwinding somewhere and leaving a feature
        recorded as shipped with nobody looking at it — which is the same
        failure this whole stage exists to prevent, arriving by a different
        route.
        """
        if not merge_commit_sha:
            return self._fail(
                feature,
                "the merge recorded no commit, so no production deployment can "
                "be matched to it",
            )

        try:
            observation = self.deployments.observe(commit_sha=merge_commit_sha)
        except Exception as exc:
            logger.exception("post-ship: could not read deployments")
            return self._fail(feature, f"I could not read the deployments: {exc}")

        if not observation.usable:
            return self._fail(
                feature,
                observation.reason or "no production deployment appeared",
                deployment=observation,
            )

        contract = AcceptanceContract.from_dict(feature.metadata.get("contract") or {})
        if self.verifier is None or not contract.browser_criteria:
            # Nothing to check in a browser. The deployment being READY is the
            # whole of the evidence, and saying so plainly is better than
            # implying a check happened.
            return PostShipResult(
                verified=True,
                reason=(
                    "the production deployment for this commit is ready; there "
                    "was nothing about this feature I could check in a browser"
                ),
                deployment=observation,
            )

        try:
            verification = self.verifier.verify(
                contract,
                preview_url=observation.url,
                commit_sha=merge_commit_sha,
                # A distinct attempt number so production screenshots never
                # overwrite the ones taken against the preview: comparing the
                # two is how somebody sees what changed between them.
                attempt=1000 + feature.attempts_used,
            )
        except Exception as exc:
            logger.exception("post-ship: verification could not run")
            return self._fail(
                feature,
                f"I could not check production in a browser: {exc}",
                deployment=observation,
            )

        if not verification.passed:
            return self._fail(
                feature,
                verification.summary(),
                deployment=observation,
                verification=verification,
            )

        return PostShipResult(
            verified=True,
            reason=verification.summary(),
            deployment=observation,
            verification=verification,
        )

    # -- failure -----------------------------------------------------------

    def _fail(
        self,
        feature: FeatureRequest,
        reason: str,
        *,
        deployment: Optional[PreviewObservation] = None,
        verification: Optional[FeatureVerification] = None,
    ) -> PostShipResult:
        failure = ProductionFailure(
            feature_id=feature.id,
            title=feature.title,
            merge_commit_sha=(
                feature.attempts[-1].commit_sha if feature.attempts else ""
            ),
            reason=reason,
            deployment_url=deployment.url if deployment else "",
            deployment_state=deployment.state if deployment else "",
            failed_criteria=[
                outcome.criterion.description
                for outcome in (verification.failed if verification else [])
            ],
            observed=(
                verification._observed_evidence() if verification is not None else []
            ),
            changed_files=(
                list(feature.attempts[-1].changed_files) if feature.attempts else []
            ),
            pr_url=feature.pr_url,
        )

        handler = self.handler or handoff_to_reliability
        handed_to = ""
        try:
            handed_to = handler(failure) or ""
        except Exception:
            # A handler that fails must not swallow the failure. Recorded and
            # reported as unhandled, which is louder than a silent success.
            logger.exception("post-ship: the failure handler itself failed")

        self._record(feature, failure, handed_to)
        return PostShipResult(
            verified=False,
            reason=reason,
            deployment=deployment,
            verification=verification,
            handed_over_to=handed_to,
            failure=failure,
        )

    def _record(
        self, feature: FeatureRequest, failure: ProductionFailure, handed_to: str
    ) -> None:
        if self.journal is None:
            return
        try:
            self.journal.record(
                at=self.clock(),
                kind="feature.production_failed",
                capability="feature.ship",
                actor_id=feature.actor_id,
                channel=feature.source,
                reason=failure.reason[:1000],
                detail={
                    "feature_id": feature.id,
                    "handed_over_to": handed_to or "nobody",
                },
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("could not journal a production failure")


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


def handoff_to_reliability(
    failure: ProductionFailure, *, store_factory: Optional[Callable[[], Any]] = None
) -> str:
    """Open an incident for a feature that did not work in production.

    This is the whole boundary between the two subsystems, and it points one
    way. Wiz calls into reliability's store; reliability knows nothing about
    features, feature requests or this function. A handler registered in the
    other direction would be a reliability import of wiz, which is the thing the
    architecture forbids.

    Returns the name of what took ownership, or ``""`` if nothing could.
    """
    try:
        from openjarvis.reliability.fingerprint import fingerprint
        from openjarvis.reliability.store import IncidentStore
        from openjarvis.reliability.types import (
            Evidence,
            EvidenceKind,
            Incident,
            Severity,
            TrustLevel,
        )
    except ImportError as exc:  # pragma: no cover - reliability always present
        logger.warning("reliability is not available to hand over to: %s", exc)
        return ""

    if store_factory is None:

        def store_factory() -> Any:  # type: ignore[misc]
            from pathlib import Path

            from openjarvis.core.paths import get_config_dir

            path = Path(get_config_dir()) / "reliability" / "incidents.db"
            if not path.exists():
                raise FileNotFoundError("no incident database")
            return IncidentStore(path)

    try:
        store = store_factory()
    except Exception as exc:
        logger.warning("could not open the incident store to hand over: %s", exc)
        return ""

    try:
        evidence = [
            Evidence(
                kind=EvidenceKind.NOTE,
                summary=f"{failure.feature_id} was merged and did not work",
                content=failure.reason[:4000],
                source="wiz",
                trust=TrustLevel.TRUSTED,
            )
        ]
        evidence.extend(
            Evidence(
                kind=EvidenceKind.NOTE,
                summary=line[:200],
                source="browser",
                # Every one of these strings came from a web page.
                trust=TrustLevel.EXTERNAL,
            )
            for line in failure.observed[:10]
        )

        incident = Incident(
            # Fingerprinted on the feature rather than on the error text, so a
            # feature that keeps failing after a merge groups into one incident
            # instead of opening a new one per check. The reliability side
            # already refuses a second automatic merge for a fingerprint whose
            # last one left production unverified, and that protection only
            # works if the fingerprint is stable across attempts.
            fingerprint=fingerprint(
                component="feature",
                failure_kind="post_merge",
                extra=[failure.feature_id],
            ),
            title=failure.summary(),
            # MEDIUM rather than HIGH: something Wiz built is misbehaving, which
            # is not the same as the site being down, and an incident that
            # over-states its severity is one that wakes somebody for a button.
            # If it *is* an outage, the production probes will say so
            # independently and open their own.
            severity=Severity.MEDIUM,
            component="feature",
            source="wiz",
            evidence=evidence,
            metadata={
                "source": "wiz",
                "feature_id": failure.feature_id,
                "merge_commit_sha": failure.merge_commit_sha,
                "pr_url": failure.pr_url,
                "changed_files": list(failure.changed_files)[:50],
                "failed_criteria": list(failure.failed_criteria)[:20],
                # Named so that a person reading the incident knows a person
                # decides what happens next. Wiz does not revert its own work.
                "rollback": "a human decides",
            },
        )
        created = store.create(incident)
        logger.warning(
            "handed %s to reliability as %s",
            failure.feature_id,
            getattr(created, "id", "?"),
        )
        return f"reliability ({getattr(created, 'id', 'incident')})"
    except Exception as exc:
        logger.exception("could not open an incident: %s", exc)
        return ""
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover - defensive
                logger.debug("closing the incident store failed", exc_info=True)


def complete(feature: FeatureRequest, result: PostShipResult, *, at: str) -> None:
    """Move a feature to its final state after production has answered.

    ``COMPLETE`` only when production agreed. Otherwise ``HUMAN_REQUIRED``,
    because the change is live, unproven and somebody else now owns it — and
    ``COMPLETE`` is a word that should mean what it says.
    """
    if feature.state is not FeatureState.PRODUCTION_VERIFYING:
        return
    feature.production_result = result.summary()
    if result.verification is not None:
        # Bound to the exact deployment/commit result.verification carries —
        # not persisted anywhere before this, so a feature that shipped and
        # a feature whose production check nobody could ever look at again
        # were indistinguishable a moment after the journal line scrolled by.
        feature.metadata["production_verification"] = result.verification.to_dict()
    if result.verified:
        feature.transition(FeatureState.COMPLETE, at=at, reason=result.summary())
    else:
        feature.transition(FeatureState.HUMAN_REQUIRED, at=at, reason=result.summary())


def reverify_production(
    feature_id: str,
    *,
    store: FeatureStore,
    github: Any,
    postship: "PostShipVerifier",
    journal: Any = None,
    clock: Callable[[], str] = lambda: "",
    reason: str = "",
    incident_store: Any = None,
    incident_resolution_reason: str = "",
    owner_notifier: Any = None,
) -> "RecoveryResult":
    """Re-check an already-merged feature's production state, and complete
    it if production now agrees.

    The narrow gap :class:`~.recovery.FeatureRecovery` deliberately does not
    cover: recovery's furthest reach is ``READY``, and merging is a
    different authority it never exercises — see its own module docstring.
    A feature whose merge already happened and whose post-ship check did
    not agree (a real regression, or a flake that has since cleared) is
    stuck past that boundary, with no way back in. This is that way back in,
    and it is exactly as narrow: it never merges anything, never touches
    application code, and never trusts a cached SHA — the pull request is
    re-read fresh, and only a ``merged: true`` answer carrying a real
    ``merge_commit_sha`` is treated as evidence there is a production
    deployment to compare against at all. The check itself is not special-
    cased: it is the same :meth:`PostShipVerifier.verify` an ordinary
    :meth:`~.pipeline.FeaturePipeline.ship` call already runs, against
    whatever the contract and the (possibly since-fixed) verifier currently
    say — so a confirmed benign flake clearing on retry, and a genuine
    regression continuing to fail, are told apart by rerunning the real
    check, not by asserting the answer.

    Idempotent: an already-``COMPLETE`` feature is reported as recovered
    without touching anything, the same way :meth:`FeatureRecovery.recover`
    treats an already-``READY`` one. Every other terminal or in-flight state
    refuses outright — this narrows the *very* wide "any state" a generic
    override would accept down to the one shape a real, stalled post-ship
    check can leave a feature in.
    """
    # Imported here, not at module level: recovery.py imports pipeline.py,
    # which imports this module for `complete` — a real cycle at import
    # time, not just an inconvenient one. By the time this function actually
    # runs, every module involved has finished loading.
    from openjarvis.wiz.features.recovery import RecoveryRefusal, RecoveryResult

    feature = store.get(feature_id)
    if feature is None:
        return RecoveryResult(
            feature_id, False, "UNKNOWN", [RecoveryRefusal("not_found", f"no feature {feature_id!r}")]
        )

    if feature.state is FeatureState.COMPLETE:
        return RecoveryResult(feature.id, True, feature.state.value, [])

    if feature.state is not FeatureState.HUMAN_REQUIRED:
        return RecoveryResult(
            feature.id,
            False,
            feature.state.value,
            [
                RecoveryRefusal(
                    "wrong_state",
                    f"{feature.state.value} is not a state a post-ship "
                    "reverification can act on",
                )
            ],
        )

    if not feature.pr_number:
        return RecoveryResult(
            feature.id,
            False,
            feature.state.value,
            [RecoveryRefusal("no_pull_request", "feature has no recorded pull request")],
        )

    try:
        pr = github.get_pull_request(feature.pr_number)
    except Exception as exc:
        return RecoveryResult(
            feature.id,
            False,
            feature.state.value,
            [RecoveryRefusal("pull_request_unreadable", str(exc))],
        )

    if not pr.get("merged"):
        return RecoveryResult(
            feature.id,
            False,
            feature.state.value,
            [
                RecoveryRefusal(
                    "pull_request_not_merged",
                    f"PR #{feature.pr_number} is not merged "
                    f"(state={pr.get('state', 'unknown')!r}); nothing to "
                    "re-verify in production",
                )
            ],
        )

    merge_commit_sha = str(pr.get("merge_commit_sha") or "")
    if not merge_commit_sha:
        return RecoveryResult(
            feature.id,
            False,
            feature.state.value,
            [
                RecoveryRefusal(
                    "no_merge_commit_sha",
                    f"PR #{feature.pr_number} is merged but GitHub reports "
                    "no merge commit SHA",
                )
            ],
        )

    feature.resume_production_reverification_from_human_required(
        at=clock(), reason=reason or "re-verifying production after a merge"
    )
    store.save(feature)
    _record_journal(
        journal,
        feature,
        clock,
        kind="feature.production_reverify_started",
        reason=f"re-checking production at merge commit {merge_commit_sha[:12]}",
    )

    result = postship.verify(feature, merge_commit_sha=merge_commit_sha)
    complete(feature, result, at=clock())
    store.save(feature)
    outcome_kind = "feature.shipped" if result.verified else "feature.production_unverified"
    _record_journal(journal, feature, clock, kind=outcome_kind, reason=result.summary())

    # Same call the canonical ship() path makes from FeaturePipeline._record:
    # unconditional, cheap when nothing is configured, and it is
    # FeatureOwnerNotifier's own job (and its on-disk ledger) to decide
    # whether this kind is worth a message and to never say the same one
    # twice.
    if owner_notifier is not None:
        try:
            owner_notifier.notify(feature, kind=outcome_kind, reason=result.summary())
        except Exception:
            logger.exception("owner notification failed for %s", feature.id)

    if result.verified and incident_store is not None:
        _resolve_associated_incident(
            feature,
            incident_store,
            reason=incident_resolution_reason
            or f"Production verification passed on reverify: {result.summary()}",
        )

    return RecoveryResult(feature.id, bool(result.verified), feature.state.value, [])


def reconcile_external_merge(
    feature_id: str,
    *,
    pr_number: int,
    owner_acknowledged: bool,
    store: FeatureStore,
    github: Any,
    postship: "PostShipVerifier",
    journal: Any = None,
    clock: Callable[[], str] = lambda: "",
    reason: str = "",
    incident_store: Any = None,
    incident_resolution_reason: str = "",
    owner_notifier: Any = None,
) -> "RecoveryResult":
    """Reconcile a feature whose merge happened *outside* canonical ship().

    Deliberately not a variant of :func:`reverify_production`. That function
    covers a merge the pipeline itself performed, where only the post-ship
    check flaked — the merge's legitimacy was never in question. This covers
    the opposite: a merge this package never authorized, through a channel
    it does not control (found on FEAT-00030 / INC-00107 — a coding
    session's own shell running ``gh pr merge`` directly; see
    :mod:`~openjarvis.reliability.engineer_guard` for the structural fix).
    Reusing ``reverify_production`` for this would let an unauthorized merge
    complete through the same quiet path as an ordinary flaky retry, which
    is exactly the outcome this function exists to refuse by default.

    Two things distinguish it, both required, neither implicit:

    ``pr_number`` is a parameter, not read from ``feature.pr_number``. A
    feature reconciled here typically has no pull request on record at all —
    the pipeline never opened one — so there is nothing on the feature to
    trust; the caller (a person who has looked at what actually happened)
    supplies it.

    ``owner_acknowledged`` must be ``True`` explicitly. There is no default
    that lets this succeed unattended: completing a feature whose real
    shipping mechanism was an unauthorized merge is a judgement call about
    an already-live production change, not a fact a fresh Playwright run can
    establish on its own.

    Even given both, this never pretends ``ship()`` performed the merge: on
    success the feature's ``shipping_path`` metadata is stamped
    ``"external_bypass_reconciled"`` — never the ordinary ``"feature.shipped"``
    journal kind — and the associated incident (see
    :func:`_resolve_associated_incident`) is resolved with that history
    preserved, not erased.
    """
    from openjarvis.wiz.features.recovery import RecoveryRefusal, RecoveryResult

    feature = store.get(feature_id)
    if feature is None:
        return RecoveryResult(
            feature_id, False, "UNKNOWN", [RecoveryRefusal("not_found", f"no feature {feature_id!r}")]
        )

    if feature.state is FeatureState.COMPLETE:
        return RecoveryResult(feature.id, True, feature.state.value, [])

    if feature.state is not FeatureState.HUMAN_REQUIRED:
        return RecoveryResult(
            feature.id,
            False,
            feature.state.value,
            [
                RecoveryRefusal(
                    "wrong_state",
                    f"{feature.state.value} is not a state an external-merge "
                    "reconciliation can act on",
                )
            ],
        )

    if not owner_acknowledged:
        return RecoveryResult(
            feature.id,
            False,
            feature.state.value,
            [
                RecoveryRefusal(
                    "owner_acknowledgement_required",
                    "reconciling a merge that happened outside canonical ship() "
                    "requires explicit, informed owner acknowledgement — refusing "
                    "to complete this silently",
                )
            ],
        )

    if not pr_number:
        return RecoveryResult(
            feature.id,
            False,
            feature.state.value,
            [RecoveryRefusal("no_pull_request", "no pull request number was supplied")],
        )

    try:
        pr = github.get_pull_request(pr_number)
    except Exception as exc:
        return RecoveryResult(
            feature.id,
            False,
            feature.state.value,
            [RecoveryRefusal("pull_request_unreadable", str(exc))],
        )

    if not pr.get("merged"):
        return RecoveryResult(
            feature.id,
            False,
            feature.state.value,
            [
                RecoveryRefusal(
                    "pull_request_not_merged",
                    f"PR #{pr_number} is not merged (state={pr.get('state', 'unknown')!r})",
                )
            ],
        )

    merge_commit_sha = str(pr.get("merge_commit_sha") or "")
    if not merge_commit_sha:
        return RecoveryResult(
            feature.id,
            False,
            feature.state.value,
            [
                RecoveryRefusal(
                    "no_merge_commit_sha",
                    f"PR #{pr_number} is merged but GitHub reports no merge commit SHA",
                )
            ],
        )

    # Recorded now, honestly: a PR exists and this is its number — a fact —
    # distinct from feature.metadata["shipping_path"] below, which records
    # *how* it got merged. One documents what happened; the other documents
    # that ship() is not what did it.
    feature.pr_number = pr_number
    feature.pr_url = str(pr.get("url") or feature.pr_url)

    feature.resume_production_reverification_from_human_required(
        at=clock(),
        reason=reason
        or f"owner-acknowledged reconciliation of PR #{pr_number}, merged outside ship()",
    )
    store.save(feature)
    _record_journal(
        journal,
        feature,
        clock,
        kind="feature.external_bypass_reconciliation_started",
        reason=f"reconciling PR #{pr_number} (merge commit {merge_commit_sha[:12]}), owner acknowledged",
    )

    result = postship.verify(feature, merge_commit_sha=merge_commit_sha)

    if result.verified:
        feature.metadata["shipping_path"] = "external_bypass_reconciled"
        feature.metadata["external_bypass_pr_number"] = pr_number
        feature.metadata["external_bypass_merge_commit_sha"] = merge_commit_sha

    complete(feature, result, at=clock())
    store.save(feature)
    outcome_kind = (
        "feature.external_bypass_reconciled" if result.verified else "feature.production_unverified"
    )
    _record_journal(journal, feature, clock, kind=outcome_kind, reason=result.summary())

    if owner_notifier is not None:
        try:
            owner_notifier.notify(feature, kind=outcome_kind, reason=result.summary())
        except Exception:
            logger.exception("owner notification failed for %s", feature.id)

    if result.verified and incident_store is not None:
        _resolve_associated_incident(
            feature,
            incident_store,
            reason=incident_resolution_reason
            or (
                f"Reconciled: production verification passed against PR #{pr_number}'s "
                f"merge commit, with explicit owner acknowledgement that this merge "
                f"happened outside canonical ship()."
            ),
        )

    return RecoveryResult(feature.id, bool(result.verified), feature.state.value, [])


def _resolve_associated_incident(
    feature: FeatureRequest, incident_store: Any, *, reason: str
) -> None:
    """Close the incident a failed post-ship check opened for *feature*, now
    that a fresh check agrees with production.

    Only ever called after :func:`reverify_production` has just proven
    ``result.verified`` against fresh evidence — this never resolves an
    incident on the strength of anything else. The incident's own evidence
    and transition history are untouched; this appends one more transition
    to them, the same as any other resolution the reliability system
    records. A failed reverification never reaches here at all: the
    incident stays open, and a genuinely recurring failure re-uses it
    rather than opening a duplicate (see :func:`handoff_to_reliability`'s
    fingerprint).
    """
    try:
        from openjarvis.reliability.fingerprint import fingerprint
        from openjarvis.reliability.types import IncidentState
    except ImportError:  # pragma: no cover - reliability always present
        return

    fp = fingerprint(component="feature", failure_kind="post_merge", extra=[feature.id])
    try:
        incident = incident_store.find_by_fingerprint(fp)
        if incident is None:
            return
        if not incident.can_transition_to(IncidentState.RESOLVED):
            logger.warning(
                "incident %s for %s cannot move to RESOLVED from %s",
                incident.id,
                feature.id,
                incident.state.value,
            )
            return
        # store.transition(), not incident.transition_to() + save(): the
        # latter would update the state column but skip appending to the
        # hash-chained, tamper-evident transition log — the whole reason
        # this is "the audited incident lifecycle" and not a bare field
        # update.
        incident_store.transition(incident, IncidentState.RESOLVED, actor="wiz", reason=reason)
    except Exception:  # noqa: BLE001 - resolving is best-effort
        logger.exception("could not resolve the incident for %s", feature.id)


def _record_journal(
    journal: Any, feature: FeatureRequest, clock: Callable[[], str], *, kind: str, reason: str
) -> None:
    if journal is None:
        return
    try:
        journal.record(
            at=clock(),
            kind=kind,
            capability="feature.ship",
            actor_id=feature.actor_id,
            channel=feature.source,
            reason=reason[:1000],
            detail={"feature_id": feature.id, "state": feature.state.value},
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("could not journal a post-ship reverification event for %s", feature.id)
