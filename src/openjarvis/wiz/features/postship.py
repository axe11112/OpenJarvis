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
from typing import Any, Callable, Dict, List, Optional

from openjarvis.wiz.features.acceptance import AcceptanceContract
from openjarvis.wiz.features.model import FeatureRequest, FeatureState
from openjarvis.wiz.features.preview import PreviewObservation, PreviewObserver
from openjarvis.wiz.features.verification import FeatureVerification, FeatureVerifier

logger = logging.getLogger(__name__)

__all__ = [
    "PostShipResult",
    "PostShipVerifier",
    "ProductionFailure",
    "handoff_to_reliability",
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
    if result.verified:
        feature.transition(FeatureState.COMPLETE, at=at, reason=result.summary())
    else:
        feature.transition(FeatureState.HUMAN_REQUIRED, at=at, reason=result.summary())
