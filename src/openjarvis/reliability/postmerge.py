"""Production verification of a merge that has already happened.

Everything before this module answers *may this land?* This one answers the
only question that matters afterwards: *did production actually get better?*

The distinction is not academic. Every gate in :mod:`openjarvis.reliability.merge`
reasons about a preview deployment of a commit that no longer exists once the
merge lands — a squash produces a new SHA, built from a different base, served
by a different deployment. Treating the pre-merge verdict as evidence about
production is the single easiest way for an automated repair loop to leave a
site broken while reporting success, because every signal it consulted was true
and none of them was about production.

So the evidence here is re-gathered from scratch, against the real site:

1. the production deployment carrying the *merge commit* is identified by SHA,
   never by recency — "the newest production deployment" is whatever else
   happened to ship, and mistaking it for this one is how a green light gets
   borrowed from someone else's change;
2. it must reach ``READY`` inside a bounded wait, with ``ERROR``/``CANCELED``
   failing immediately rather than burning the timeout;
3. the probe that opened the incident is re-run against production — the
   original reproduction, not a proxy for it;
4. every enabled production probe is then run, because a fix that repairs one
   page and breaks another is not a fix.

Only all four produce ``RESOLVED``. Anything else produces ``HUMAN_REQUIRED``,
a CRITICAL notification, and a durable marker that stops the loop trying again:
after a bad merge the safe move is to stop, and a repair loop that responds to
a broken production by merging harder is the failure mode this exists to
prevent.

Nothing here writes to a database, deploys, pushes, or rolls back. It reads
Vercel, runs probes, and records what it saw.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from openjarvis.reliability.types import (
    Evidence,
    EvidenceKind,
    Incident,
    IncidentState,
    Severity,
    TrustLevel,
    now_iso,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DeploymentObservation",
    "PostMergeResult",
    "PostMergeVerifier",
    "ProbeOutcome",
    "post_merge_failure_for",
]

#: Vercel states that mean the deployment will never become ``READY``. Waiting
#: out the full timeout on one of these would turn a known answer into a slow
#: one.
_DEAD_STATES = frozenset({"ERROR", "CANCELED", "CANCELLED", "DELETED"})

#: The state that permits verification to begin.
_READY = "READY"

#: Incident metadata key holding the durable post-merge failure marker.
POST_MERGE_FAILURE_KEY = "post_merge_failure"


def post_merge_failure_for(store: Any, fingerprint: str) -> Optional[Dict[str, Any]]:
    """Return the post-merge failure recorded for *fingerprint*, if any.

    The retry-storm guard, and deliberately fingerprint-scoped rather than
    incident-scoped. The incident that failed is already ``HUMAN_REQUIRED`` and
    blocked by that alone; the real risk is the *next* incident — same probe,
    same fingerprint, opened minutes later because production is still broken —
    arriving clean and being merged on top of the merge that broke it.

    Resolved incidents are excluded, which is how the block is *cleared*: a
    human who resolves the escalated incident has said the production problem is
    dealt with, and the guard lifts with it. Without that, the marker would
    block the fingerprint forever and the only remedy would be editing the
    database.

    Returns ``None`` when the store cannot answer. A guard that crashes the
    caller is worse than one that abstains, and every consumer treats ``None``
    as "no marker found" while the merge gates keep every other check.
    """
    if not fingerprint or store is None:
        return None
    try:
        lookup = getattr(store, "list_by_fingerprint", None)
        if callable(lookup):
            incidents = list(lookup(fingerprint) or [])
        else:
            # A store that predates the fingerprint history query — every test
            # double, among others. Its answer is narrower but never wrong in
            # the dangerous direction: a marker it misses leaves every other
            # gate standing.
            one = store.find_by_fingerprint(fingerprint)
            incidents = [one] if one is not None else []
    except Exception:  # noqa: BLE001 - a guard must not break the caller
        logger.exception("could not read incidents for fingerprint %s", fingerprint)
        return None
    for incident in incidents:
        marker = (getattr(incident, "metadata", None) or {}).get(POST_MERGE_FAILURE_KEY)
        if marker:
            found = dict(marker)
            found.setdefault("incident_id", getattr(incident, "id", ""))
            return found
    return None


@dataclass(slots=True)
class DeploymentObservation:
    """What was seen while waiting for the production deployment of a merge."""

    matched: bool = False
    ready: bool = False
    deployment_id: str = ""
    state: str = ""
    url: str = ""
    commit_sha: str = ""
    target: str = ""
    created_at: str = ""
    waited_seconds: float = 0.0
    polls: int = 0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the audit record."""
        return {
            "matched": self.matched,
            "ready": self.ready,
            "deployment_id": self.deployment_id,
            "state": self.state,
            "url": self.url,
            "commit_sha": self.commit_sha,
            "target": self.target,
            "created_at": self.created_at,
            "waited_seconds": round(self.waited_seconds, 1),
            "polls": self.polls,
            "reason": self.reason,
        }


@dataclass(slots=True)
class ProbeOutcome:
    """One probe's verdict against production."""

    probe_id: str
    passed: bool
    summary: str = ""
    #: The probe's failure kind, so a caller can tell a wrong answer from a
    #: slow one without reading the summary.
    failure_kind: str = ""
    #: How many extra runs it took. Non-zero means the first run failed on the
    #: clock alone and a re-run disagreed — worth recording, because it is the
    #: signature of a busy monitoring machine rather than a sick site.
    confirmations: int = 0

    @property
    def latency_only(self) -> bool:
        """Failed on the duration budget and nothing else."""
        return not self.passed and self.failure_kind == "slow"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the audit record."""
        return {
            "probe_id": self.probe_id,
            "passed": self.passed,
            "summary": self.summary[:300],
            "failure_kind": self.failure_kind,
            "confirmations": self.confirmations,
            "latency_only": self.latency_only,
        }


@dataclass(slots=True)
class PostMergeResult:
    """The full account of one post-merge production verification."""

    verified: bool = False
    reason: str = ""
    rule: str = ""
    deployment: DeploymentObservation = field(default_factory=DeploymentObservation)
    reproduction: Optional[ProbeOutcome] = None
    fleet: List[ProbeOutcome] = field(default_factory=list)
    at: str = field(default_factory=now_iso)

    @property
    def failures(self) -> List[ProbeOutcome]:
        """Every probe that did not pass, reproduction included."""
        failed = (
            []
            if self.reproduction is None or self.reproduction.passed
            else [self.reproduction]
        )
        return failed + [p for p in self.fleet if not p.passed]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the audit record."""
        return {
            "verified": self.verified,
            "reason": self.reason,
            "rule": self.rule,
            "at": self.at,
            "deployment": self.deployment.to_dict(),
            "reproduction": (
                self.reproduction.to_dict() if self.reproduction is not None else None
            ),
            "fleet": [p.to_dict() for p in self.fleet],
        }

    def summary(self) -> str:
        """One line for the hash-chained history."""
        if self.verified:
            return (
                f"production verified on {self.deployment.deployment_id or 'unknown'} "
                f"({len(self.fleet)} probe(s) + reproduction)"
            )
        return f"production verification failed: {self.reason}"


@dataclass
class PostMergeVerifier:
    """Waits for the merge's production deployment, then proves production.

    Parameters
    ----------
    vercel:
        A :class:`~openjarvis.reliability.sources.vercel.VercelSource`, or
        anything with ``list_deployments``.
    verifier:
        The same :class:`~openjarvis.reliability.verify.Verifier` the repair
        loop uses. Reusing it is deliberate: production is judged by exactly the
        machinery that judged the preview, so a repair cannot pass one bar and
        be spared the other.
    fleet_provider:
        Returns the production probe specs to run. Called once, after the
        deployment is READY, so a spec edited during the wait is still picked up.
    production_url:
        Base URL probes run against. Empty means the deployment's own URL, which
        is a weaker check — the production domain is what users type.
    """

    vercel: Any
    verifier: Any
    store: Any = None
    fleet_provider: Optional[Callable[[], Sequence[Any]]] = None
    production_url: str = ""
    notifier: Any = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    #: How long to wait for the merge's production deployment to go READY.
    deployment_timeout_seconds: float = 900.0
    poll_interval_seconds: float = 15.0
    #: How many deployments to scan per poll. Generous: a busy repository can
    #: ship several times while this one builds, and the match is by SHA, so a
    #: short window would simply lose the deployment rather than mismatch it.
    scan_limit: int = 30
    #: Extra runs allowed for a probe that failed on its *duration budget alone*.
    #:
    #: This exists because of a specific way auto-merge could go wrong. These
    #: probes run on the operator's laptop, and a laptop that is compiling
    #: something serves a correct page slowly. Without confirmation, a perfectly
    #: good merge becomes "production probe(s) failed", HUMAN_REQUIRED and a
    #: CRITICAL notification — three real incidents in one night were exactly
    #: this, before anything was merging at all.
    #:
    #: Deliberately narrow. Only ``failure_kind == "slow"`` is retried: a wrong
    #: status, a missing element or an unreachable page fails on the first run
    #: and is never given a second chance, because a site that is down does not
    #: become healthy by being asked twice.
    latency_confirmations: int = 2
    #: Seconds to wait before re-running a latency-only failure. Long enough for
    #: a compile to finish a chunk, short enough not to stretch the window in
    #: which unverified code is live.
    latency_confirmation_delay_seconds: float = 10.0

    # -- entry point ------------------------------------------------------

    def verify(
        self,
        incident: Incident,
        *,
        merge_record: Any,
        spec: Any = None,
    ) -> PostMergeResult:
        """Prove production for a merge that already landed.

        Never raises: a verification that cannot run is a failed verification,
        because the alternative is an exception unwinding into the repair loop
        and leaving an incident in ``MERGED`` with nobody looking at it.
        """
        merge_sha = str(getattr(merge_record, "merge_commit_sha", "") or "")
        if not merge_sha:
            # Nothing to match a deployment against. GitHub returned a merge
            # without a SHA, or the record was never filled in; either way there
            # is no lineage to check and no honest way to claim production was
            # verified.
            result = PostMergeResult(
                verified=False,
                reason=(
                    "the merge recorded no commit SHA, so no production "
                    "deployment can be matched to it"
                ),
                rule="no_merge_sha",
            )
            self._record(incident, result)
            return result

        try:
            observation = self._await_deployment(merge_sha)
        except Exception as exc:  # noqa: BLE001 - a failed check, not a crash
            logger.exception("post-merge: could not observe deployments")
            observation = DeploymentObservation(
                reason=f"could not read deployments from Vercel: {exc}"
            )

        if not observation.matched:
            result = PostMergeResult(
                verified=False,
                reason=observation.reason
                or f"no production deployment appeared for {merge_sha[:12]}",
                rule="deployment_missing",
                deployment=observation,
            )
            self._record(incident, result)
            return result

        self._audit(
            incident,
            f"production deployment observed: {observation.deployment_id} "
            f"({observation.state}) for {merge_sha[:12]}",
        )
        self._notify_deployment(incident, observation)

        if not observation.ready:
            result = PostMergeResult(
                verified=False,
                reason=(
                    f"the production deployment for {merge_sha[:12]} is "
                    f"{observation.state or 'in an unknown state'}"
                ),
                rule="deployment_not_ready",
                deployment=observation,
            )
            self._record(incident, result)
            return result

        target_url = self.production_url or observation.url
        self._audit(
            incident,
            f"production verification started against {target_url} "
            f"on deployment {observation.deployment_id}",
        )
        self._notify_verification_started(incident, observation, target_url)

        result = PostMergeResult(deployment=observation)

        # 1. The original reproduction, against production.
        if spec is not None:
            result.reproduction = self._run_probe(
                spec, target_url=target_url, incident_id=incident.id
            )
            if not result.reproduction.passed:
                result.verified = False
                if result.reproduction.latency_only:
                    result.reason = (
                        f"the probe that opened this incident "
                        f"({result.reproduction.probe_id}) served correct "
                        "content against production but over its time budget, "
                        f"after {self.latency_confirmations} confirmation(s): "
                        f"{result.reproduction.summary}"
                    )
                    result.rule = "reproduction_slow_unconfirmed"
                else:
                    result.reason = (
                        f"the probe that opened this incident "
                        f"({result.reproduction.probe_id}) still fails against "
                        f"production: {result.reproduction.summary}"
                    )
                    result.rule = "reproduction_failed"
                self._record(incident, result)
                return result

        # 2. The rest of the fleet. A fix that repairs one page and breaks
        #    another is not a fix, and only the fleet can say so.
        for fleet_spec in self._fleet(exclude=getattr(spec, "id", "")):
            outcome = self._run_probe(
                fleet_spec, target_url=target_url, incident_id=incident.id
            )
            result.fleet.append(outcome)

        failed = [p for p in result.fleet if not p.passed]
        if failed:
            result.verified = False
            named = ", ".join(f"{p.probe_id} ({p.summary[:80]})" for p in failed)
            if all(p.latency_only for p in failed):
                # Still not verified — the rule is that only a pass resolves an
                # incident — but the account has to be accurate. Saying
                # production "failed" when every page served correct content is
                # how an owner is sent to look for an outage that is not there.
                result.reason = (
                    "production could not be verified: every failing check "
                    "served correct content but took longer than its budget, "
                    f"after {self.latency_confirmations} confirmation(s). This "
                    "is usually the monitoring machine being busy rather than "
                    f"the site being unwell. Checks: {named}"
                )
                result.rule = "fleet_slow_unconfirmed"
            else:
                result.reason = (
                    "production probe(s) failed after the merge: " + named
                )
                result.rule = "fleet_failed"
            self._record(incident, result)
            return result

        result.verified = True
        result.reason = (
            f"production verified on deployment {observation.deployment_id} "
            f"for {merge_sha[:12]}: reproduction and "
            f"{len(result.fleet)} production probe(s) all pass"
        )
        result.rule = "verified"
        self._record(incident, result)
        return result

    # -- deployment lineage -----------------------------------------------

    def _await_deployment(self, merge_sha: str) -> DeploymentObservation:
        """Wait for the production deployment built from *merge_sha*.

        Matched by commit SHA and never by recency. The newest production
        deployment is whatever shipped most recently, which on any active
        repository is frequently somebody else's commit; accepting it would let
        an unrelated green deployment vouch for this repair. A deployment that
        does not carry this SHA is not this deployment, however new it is and
        however ready it looks.
        """
        started = self.clock()
        observation = DeploymentObservation(commit_sha=merge_sha)
        deadline = started + max(0.0, self.deployment_timeout_seconds)

        while True:
            observation.polls += 1
            match = self._find(merge_sha)
            if match is not None:
                observation.matched = True
                observation.deployment_id = str(match.get("id") or "")
                observation.state = str(match.get("state") or "").upper()
                observation.url = str(match.get("url") or "")
                observation.target = str(match.get("target") or "")
                observation.created_at = str(match.get("created_at") or "")
                observation.commit_sha = str(match.get("commit_sha") or merge_sha)

                if observation.state == _READY:
                    observation.ready = True
                    observation.reason = "ready"
                    break
                if observation.state in _DEAD_STATES:
                    # A terminal failure state answers the question now. Waiting
                    # out the timeout would only make the same answer later.
                    observation.ready = False
                    observation.reason = (
                        f"the production deployment for {merge_sha[:12]} ended in "
                        f"{observation.state}"
                    )
                    break

            observation.waited_seconds = self.clock() - started
            if self.clock() >= deadline:
                observation.ready = False
                if observation.matched:
                    observation.reason = (
                        f"the production deployment for {merge_sha[:12]} was still "
                        f"{observation.state or 'unknown'} after "
                        f"{observation.waited_seconds:.0f}s"
                    )
                else:
                    observation.reason = (
                        f"no production deployment carrying {merge_sha[:12]} "
                        f"appeared within {observation.waited_seconds:.0f}s"
                    )
                break
            self.sleep(self.poll_interval_seconds)

        observation.waited_seconds = self.clock() - started
        return observation

    def _find(self, merge_sha: str) -> Optional[Dict[str, Any]]:
        """The newest *production* deployment whose commit SHA is *merge_sha*."""
        try:
            deployments = (
                self.vercel.list_deployments(limit=self.scan_limit, target="production")
                or []
            )
        except Exception:  # noqa: BLE001 - one failed poll, not a failed wait
            logger.exception("post-merge: a deployment poll failed")
            return None
        for deployment in deployments:
            if str(deployment.get("commit_sha") or "").strip() != merge_sha:
                continue
            # Belt and braces: the API was asked for production only, but a
            # preview deployment of the same commit must never stand in for the
            # production one — it is a different build on a different domain.
            target = str(deployment.get("target") or "").lower()
            if target and target != "production":
                continue
            return deployment
        return None

    # -- probes -----------------------------------------------------------

    def _fleet(self, *, exclude: str = "") -> List[Any]:
        """Enabled production probe specs, minus the reproduction already run."""
        if self.fleet_provider is None:
            return []
        try:
            specs = list(self.fleet_provider() or [])
        except Exception:  # noqa: BLE001
            logger.exception("post-merge: could not load the production probe fleet")
            return []
        return [
            s
            for s in specs
            if getattr(s, "enabled", True) and getattr(s, "id", "") != exclude
        ]

    def _run_probe(
        self, spec: Any, *, target_url: str, incident_id: str
    ) -> ProbeOutcome:
        """Run one probe against production.

        A crash is a failure — an unverifiable production is not a verified one.
        A *latency-only* failure is re-run up to
        :attr:`latency_confirmations` times before being believed; nothing else
        is retried.
        """
        probe_id = str(getattr(spec, "id", "") or "probe")
        outcome = self._run_probe_once(spec, target_url, incident_id)
        if outcome.passed or not outcome.latency_only:
            return outcome

        for attempt in range(1, max(0, self.latency_confirmations) + 1):
            logger.info(
                "post-merge: %s served correct content over its time budget; "
                "confirming (%d/%d)",
                probe_id,
                attempt,
                self.latency_confirmations,
            )
            self.sleep(self.latency_confirmation_delay_seconds)
            retry = self._run_probe_once(spec, target_url, incident_id)
            retry.confirmations = attempt
            if retry.passed:
                retry.summary = (
                    f"passed on confirmation {attempt}; the first run was over "
                    f"the time budget only ({outcome.summary[:120]})"
                )
                return retry
            if not retry.latency_only:
                # It got worse, or it was never only slow. Believe the worse one.
                return retry
            outcome = retry
        outcome.confirmations = max(0, self.latency_confirmations)
        return outcome

    def _run_probe_once(
        self, spec: Any, target_url: str, incident_id: str
    ) -> ProbeOutcome:
        """One run, with a crash converted into a failure."""
        probe_id = str(getattr(spec, "id", "") or "probe")
        try:
            verdict = self.verifier.verify(
                spec, target_url=target_url, incident_id=incident_id
            )
        except Exception as exc:  # noqa: BLE001 - unverifiable is not verified
            logger.exception("post-merge: probe %s could not run", probe_id)
            return ProbeOutcome(
                probe_id=probe_id,
                passed=False,
                summary=f"the probe could not run: {exc}",
            )
        passed = bool(getattr(verdict, "passed", False))
        summary = (
            getattr(verdict, "actual", "")
            or getattr(verdict, "notes", "")
            or ("passed" if passed else "failed")
        )
        return ProbeOutcome(
            probe_id=probe_id,
            passed=passed,
            summary=str(summary),
            failure_kind=str(getattr(verdict, "failure_kind", "") or ""),
        )

    # -- recording --------------------------------------------------------

    def _audit(self, incident: Incident, reason: str) -> None:
        """Append one tamper-evident entry without moving the incident.

        The window between a merge and its verdict is where an operator most
        needs a timeline: which deployment was picked up, when probing began,
        how long the gap was. Recording only the final verdict would leave that
        window blank in the one log that cannot be edited afterwards.
        """
        if self.store is None:
            return
        try:
            self.store.record_audit(incident, actor="jarvis-postmerge", reason=reason)
        except Exception:  # noqa: BLE001 - an audit gap must not stop the check
            logger.exception("could not audit post-merge progress for %s", incident.id)

    def _record(self, incident: Incident, result: PostMergeResult) -> None:
        """Attach the verdict as evidence and to the audit chain."""
        if self.store is None:
            return
        try:
            self.store.add_evidence(
                incident,
                Evidence(
                    kind=EvidenceKind.NOTE,
                    summary=f"Post-merge production verification: {result.rule}",
                    content=_render_evidence(result),
                    source="postmerge",
                    trust=TrustLevel.TRUSTED,
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not attach post-merge evidence to %s", incident.id)
        try:
            self.store.record_audit(
                incident,
                actor="jarvis-postmerge",
                reason=result.summary(),
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not audit post-merge result for %s", incident.id)

    # -- notification -----------------------------------------------------

    def _notify_deployment(
        self, incident: Incident, observation: DeploymentObservation
    ) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.production_deployment(incident, observation=observation)
        except Exception:  # noqa: BLE001 - notification must never break a check
            logger.exception("could not send the deployment notification")

    def _notify_verification_started(
        self, incident: Incident, observation: DeploymentObservation, target_url: str
    ) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.production_verification_started(
                incident, observation=observation, target_url=target_url
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not send the verification-started notification")


def _render_evidence(result: PostMergeResult) -> str:
    """Human-readable evidence body. Identifiers and verdicts only."""
    lines = [
        f"Verified: {result.verified}",
        f"Rule: {result.rule}",
        f"Reason: {result.reason}",
        "",
        "Deployment:",
        f"  id        {result.deployment.deployment_id or '-'}",
        f"  state     {result.deployment.state or '-'}",
        f"  target    {result.deployment.target or '-'}",
        f"  commit    {result.deployment.commit_sha or '-'}",
        f"  url       {result.deployment.url or '-'}",
        f"  waited    {result.deployment.waited_seconds:.0f}s "
        f"over {result.deployment.polls} poll(s)",
    ]
    if result.reproduction is not None:
        lines += [
            "",
            "Original reproduction against production:",
            f"  {result.reproduction.probe_id}: "
            f"{'PASS' if result.reproduction.passed else 'FAIL'} "
            f"— {result.reproduction.summary[:200]}",
        ]
    if result.fleet:
        lines += ["", "Production probe fleet:"]
        for outcome in result.fleet:
            lines.append(
                f"  {outcome.probe_id}: {'PASS' if outcome.passed else 'FAIL'} "
                f"— {outcome.summary[:200]}"
            )
    return "\n".join(lines)


def failure_marker(*, merge_record: Any, result: PostMergeResult) -> Dict[str, Any]:
    """The durable record that stops this fingerprint being merged again.

    Written into incident metadata rather than kept in memory on purpose: the
    storm this guards against outlives the process that hit it. A watcher
    restarted five minutes later would otherwise meet a fresh incident for the
    same still-broken production and merge a second unreviewed change on top of
    the first.
    """
    return {
        "at": now_iso(),
        "incident_id": str(getattr(merge_record, "incident_id", "") or ""),
        "pr_number": int(getattr(merge_record, "pr_number", 0) or 0),
        "merge_commit_sha": str(getattr(merge_record, "merge_commit_sha", "") or ""),
        "verified_head_sha": str(getattr(merge_record, "verified_head_sha", "") or ""),
        "deployment_id": result.deployment.deployment_id,
        "deployment_state": result.deployment.state,
        "rule": result.rule,
        "reason": result.reason[:500],
    }


def severity_for(result: PostMergeResult) -> Severity:
    """Post-merge failures are CRITICAL regardless of the incident's severity.

    The incident's own severity describes the original fault. This is a
    different fact: unreviewed code is live on the default branch and the site
    did not get better. That is the message a rate limiter must never drop.
    """
    return Severity.CRITICAL if not result.verified else Severity.MEDIUM


#: States a post-merge failure may move an incident to, in preference order.
#: ``HUMAN_REQUIRED`` is the intent; the fallbacks exist only so an incident can
#: never be left sitting in ``MERGED`` because a transition was illegal.
ESCALATION_STATES = (
    IncidentState.HUMAN_REQUIRED,
    IncidentState.FAILED,
)
