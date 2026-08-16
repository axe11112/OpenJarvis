"""Automatic merge of a verified JARVIS pull request.

This is the last gate before the default branch, and the default branch is what
a host deploys. Merging is therefore treated as production authority even though
nothing here deploys anything: JARVIS has no deploy call, but a repository with
deploy-on-merge configured turns a merge *into* a deployment, and a gate that
only holds when the repository is configured a particular way is not a gate.

The whole module exists to answer one question — *may this exact commit land on
the default branch right now?* — and to answer it from recorded evidence rather
than from anybody's account of events.

Three properties are load-bearing.

**The coding agent has no vote.** ``RepairAttempt.claim`` is what Claude said it
did. It is never read here. The only success signal that counts is
``VerificationResult.passed``, produced by re-running the probe that opened the
incident against a real preview deployment. A repair where the agent reports
triumph and the verifier reports failure is refused, and there is a test named
after exactly that case.

**Verification is bound to a SHA, not to a branch.** Every gate below asks about
``attempt.commit_sha`` — the commit that was tested, built, previewed and
verified. A branch is a moving pointer; the moment a merge decision is made
about a *branch*, the thing merged and the thing verified are only incidentally
the same commit. So the head SHA is re-read immediately before merging, compared
to the verified SHA, and then handed to GitHub as the ``sha`` merge parameter so
the server itself refuses if it moved in between. Check and use become one
operation, which is the only way to actually close the window rather than narrow
it.

**Every decision is written down, especially the refusals.** A merge that was
refused is the interesting entry in an audit log: it is evidence the gates are
load-bearing. Each decision is appended to the incident's hash-chained history
and attached as structured evidence, whichever way it went.

Nothing in this module can push, force-push, create a branch, deploy, or touch a
database. Its entire write surface is one call to
:meth:`~openjarvis.reliability.sources.github.GitHubSource.merge_pull_request`.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from openjarvis.reliability.types import (
    Evidence,
    EvidenceKind,
    Incident,
    IncidentState,
    RepairAttempt,
    Severity,
    TrustLevel,
    now_iso,
)

logger = logging.getLogger(__name__)

__all__ = [
    "REQUIRED_CHECKS",
    "AutoMerger",
    "GateResult",
    "MergeDecision",
    "MergeRecord",
    "evaluate_merge",
]

#: Local checks that must have *run and passed* for the commit to be mergeable.
#:
#: Stricter than opening a pull request, deliberately. ``CheckSuite.from_config``
#: marks lint advisory because a style violation is a poor reason to leave
#: production broken while a human reviews a fix. Merging without a human is a
#: different bargain: nobody is going to read this before it lands, so the bar
#: is every check green, lint included. A suite that never ran passes vacuously
#: — see ``CheckSuiteResult.passed`` — so ``ran`` is asserted separately.
REQUIRED_CHECKS = ("lint", "typecheck", "tests", "build")

#: Incident states from which no merge is ever attempted.
_REFUSING_STATES = frozenset(
    {
        IncidentState.HUMAN_REQUIRED,
        IncidentState.RECOVERY_REQUIRED,
        IncidentState.FAILED,
        IncidentState.ROLLED_BACK,
    }
)

_PR_URL = re.compile(r"/pull/(\d+)\b")


def pull_request_number(url: str) -> int:
    """Extract a pull request number from its HTML URL. ``0`` when absent."""
    match = _PR_URL.search(url or "")
    return int(match.group(1)) if match else 0


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GateResult:
    """One gate's verdict.

    ``passed=False`` refuses the merge. ``detail`` is written for somebody
    reading the audit log months later who does not have the code in front of
    them, so it names the values compared rather than merely the conclusion.
    """

    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the audit record."""
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(slots=True)
class MergeDecision:
    """Whether a merge may proceed, and every gate that was consulted.

    Gates are evaluated in full rather than short-circuiting at the first
    refusal. A record showing which of eleven gates failed is worth more to the
    person debugging it than one showing that the first one did, and none of
    the gates is expensive once the PR has been read.
    """

    allowed: bool
    reason: str = ""
    rule: str = ""
    gates: List[GateResult] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.allowed

    @property
    def failures(self) -> List[GateResult]:
        """Every gate that refused."""
        return [g for g in self.gates if not g.passed]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the audit record."""
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "rule": self.rule,
            "gates": [g.to_dict() for g in self.gates],
        }


@dataclass(slots=True)
class MergeRecord:
    """The full account of one merge decision, for the audit log.

    Holds identifiers, SHAs and verdicts only. No diff, no log output, no probe
    body — nothing that could carry application data or a credential into the
    audit trail.
    """

    incident_id: str = ""
    pr_number: int = 0
    verified_head_sha: str = ""
    observed_head_sha: str = ""
    base_ref: str = ""
    base_sha_at_verification: str = ""
    base_sha_observed: str = ""
    merge_commit_sha: str = ""
    method: str = ""
    actor: str = "jarvis"
    at: str = field(default_factory=now_iso)
    decision: MergeDecision = field(default_factory=lambda: MergeDecision(False))
    merged: bool = False
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for the audit record."""
        return {
            "incident_id": self.incident_id,
            "pr_number": self.pr_number,
            "verified_head_sha": self.verified_head_sha,
            "observed_head_sha": self.observed_head_sha,
            "base_ref": self.base_ref,
            "base_sha_at_verification": self.base_sha_at_verification,
            "base_sha_observed": self.base_sha_observed,
            "merge_commit_sha": self.merge_commit_sha,
            "method": self.method,
            "actor": self.actor,
            "at": self.at,
            "merged": self.merged,
            "error": self.error,
            "decision": self.decision.to_dict(),
        }

    def summary(self) -> str:
        """One line for the hash-chained history."""
        if self.merged:
            return (
                f"merged PR #{self.pr_number} at {self.verified_head_sha[:12]} "
                f"({self.method}) -> {self.merge_commit_sha[:12]}"
            )
        head = f"PR #{self.pr_number}" if self.pr_number else "merge"
        return f"refused to merge {head}: {self.decision.reason or self.error}"


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


def evaluate_merge(
    incident: Incident,
    attempt: Optional[RepairAttempt],
    pull_request: Dict[str, Any],
    *,
    enabled: bool,
    base_branch: str,
    branch_prefix: str,
    expected_pr_number: int,
    status: Optional[Dict[str, Any]] = None,
    require_status_checks: bool = True,
    base_sha_at_verification: str = "",
    observed_base_sha: str = "",
) -> MergeDecision:
    """Decide whether *pull_request* may be merged for *incident*.

    Pure: every input is passed in, nothing is read from the network or the
    clock, so the same inputs always produce the same decision and every branch
    is reachable from a test.

    Parameters
    ----------
    attempt:
        The repair attempt that was verified. ``None`` means there is nothing
        establishing that anything was verified, which refuses.
    pull_request:
        Facts read from GitHub *now*, in the shape
        :meth:`GitHubSource.get_pull_request` returns.
    status:
        CI's verdict on the head commit, from
        :meth:`GitHubSource.combined_status`.
    base_sha_at_verification / observed_base_sha:
        Where the base branch was when the repair was verified against it, and
        where it is now. Different values mean the fix was verified against code
        that is no longer what it would be merged into.
    """
    gates: List[GateResult] = []

    def gate(name: str, passed: bool, detail: str = "") -> None:
        gates.append(GateResult(name=name, passed=bool(passed), detail=detail))

    # -- 1. the switch ----------------------------------------------------
    gate(
        "merge_enabled",
        enabled,
        "[reliability.merge] enabled = true"
        if enabled
        else "[reliability.merge] enabled = false",
    )

    # -- 2. the incident is in a state that permits acting ----------------
    state_ok = incident.state not in _REFUSING_STATES
    gate(
        "incident_state",
        state_ok,
        f"incident is {incident.state.value}"
        + ("" if state_ok else "; a human owns it now"),
    )
    flapping = bool(incident.metadata.get("flapping"))
    gate(
        "not_flapping",
        not flapping,
        "flapping: the check alternates pass/fail, so a green verification "
        "proves nothing"
        if flapping
        else "not flapping",
    )

    # -- 3. something was actually verified -------------------------------
    if attempt is None:
        gate("attempt_recorded", False, "no repair attempt is recorded")
        gate("verified", False, "nothing was verified")
        gate("verified_sha_known", False, "no verified commit SHA")
    else:
        gate("attempt_recorded", True, f"attempt {attempt.number}")
        # The agent's claim is deliberately not consulted here or anywhere else
        # in this function.
        verified = attempt.verification is not None and attempt.verification.passed
        gate(
            "verified",
            verified,
            (
                f"probe '{attempt.verification.probe_id}' passed against "
                f"{attempt.verification.target_url}"
                if verified and attempt.verification is not None
                else "independent verification did not pass"
            ),
        )
        gate(
            "verified_sha_known",
            bool(attempt.commit_sha),
            f"verified commit {attempt.commit_sha[:12]}"
            if attempt.commit_sha
            else "the attempt records no commit SHA",
        )

        # -- 4. the change was in bounds ----------------------------------
        scope = dict(attempt.scope or {})
        scope_ok = bool(scope) and bool(scope.get("allowed"))
        gate(
            "scope",
            scope_ok,
            "scope verdict allowed"
            if scope_ok
            else (
                "scope refused: " + "; ".join(scope.get("reasons") or [])
                if scope
                else "no scope verdict was recorded"
            ),
        )
        protected = list(scope.get("protected") or [])
        secret_like = list(scope.get("secret_like") or [])
        gate(
            "no_protected_paths",
            not protected,
            "touches protected path(s): " + ", ".join(protected)
            if protected
            else "no protected paths touched",
        )
        gate(
            "no_secret_like_paths",
            not secret_like,
            "touches secret-like path(s): " + ", ".join(secret_like)
            if secret_like
            else "no secret-like paths touched",
        )

        # -- 5. the local gates all ran and all passed --------------------
        by_name = {
            str(r.get("name")): r for r in (attempt.checks or {}).get("results") or []
        }
        for name in REQUIRED_CHECKS:
            result = by_name.get(name)
            if result is None:
                gate("check_" + name, False, f"no result recorded for '{name}'")
            elif not result.get("ran"):
                gate("check_" + name, False, f"'{name}' did not run")
            else:
                passed = bool(result.get("passed"))
                said = result.get("summary") or ("passed" if passed else "failed")
                gate("check_" + name, passed, f"{name}: {said}")

        # -- 6. it was verified against a real preview --------------------
        gate(
            "preview_deployment",
            bool(attempt.preview_url),
            f"verified against {attempt.preview_url}"
            if attempt.preview_url
            else "no preview deployment was recorded for this attempt",
        )
        if attempt.verification is not None and incident.probe_id:
            same_probe = attempt.verification.probe_id == incident.probe_id
            gate(
                "original_reproduction",
                same_probe,
                f"re-ran the originating probe '{incident.probe_id}'"
                if same_probe
                else (
                    f"verified probe '{attempt.verification.probe_id}' is not the "
                    f"probe that opened the incident ('{incident.probe_id}')"
                ),
            )

    # -- 7. the pull request is the one JARVIS opened for this incident ---
    number = int(pull_request.get("number") or 0)
    right_pr = bool(expected_pr_number) and number == expected_pr_number
    gate(
        "pr_belongs_to_incident",
        right_pr,
        f"PR #{number} is the one recorded on {incident.id}"
        if right_pr
        else (
            f"PR #{number} is not the pull request recorded for {incident.id} "
            f"(#{expected_pr_number or 'none'})"
        ),
    )
    expected_branch = f"{branch_prefix}{incident.id}"
    head_ref = str(pull_request.get("head_ref") or "")
    gate(
        "pr_head_is_incident_branch",
        head_ref == expected_branch,
        f"head branch is '{head_ref}'"
        + ("" if head_ref == expected_branch else f", expected '{expected_branch}'"),
    )

    # -- 8. it targets the default branch and is open and clean -----------
    base_ref = str(pull_request.get("base_ref") or "")
    gate(
        "pr_base_is_default_branch",
        base_ref == base_branch,
        f"base is '{base_ref}'"
        + ("" if base_ref == base_branch else f", expected '{base_branch}'"),
    )
    state = str(pull_request.get("state") or "")
    already_merged = bool(pull_request.get("merged"))
    gate(
        "pr_open",
        state == "open" and not already_merged,
        "open"
        if state == "open" and not already_merged
        else f"state is '{state}'" + (" and already merged" if already_merged else ""),
    )
    draft = bool(pull_request.get("draft"))
    gate("pr_not_draft", not draft, "draft" if draft else "not a draft")

    mergeable = pull_request.get("mergeable")
    mergeable_state = str(pull_request.get("mergeable_state") or "")
    # None means GitHub is still computing it. Unknown is not yes.
    gate(
        "no_conflicts",
        mergeable is True and mergeable_state not in ("dirty", "blocked"),
        f"mergeable={mergeable!r} mergeable_state='{mergeable_state}'",
    )

    # -- 9. the commit about to be merged is the verified one -------------
    observed_head = str(pull_request.get("head_sha") or "")
    verified_head = attempt.commit_sha if attempt is not None else ""
    same_head = bool(verified_head) and observed_head == verified_head
    gate(
        "head_sha_unchanged",
        same_head,
        f"head {observed_head[:12]} matches the verified commit"
        if same_head
        else (
            f"head is {observed_head[:12] or 'unknown'} but "
            f"{verified_head[:12] or 'nothing'} was verified — new commits "
            "landed on the branch after verification"
        ),
    )

    # -- 10. the branch it would merge into has not moved -----------------
    if base_sha_at_verification or observed_base_sha:
        unmoved = bool(base_sha_at_verification) and (
            observed_base_sha == base_sha_at_verification
        )
        gate(
            "base_unchanged",
            unmoved,
            f"base still at {observed_base_sha[:12]}"
            if unmoved
            else (
                f"'{base_branch}' moved from "
                f"{base_sha_at_verification[:12] or 'unknown'} to "
                f"{observed_base_sha[:12] or 'unknown'} since verification; "
                "the fix was verified against code that is no longer the base"
            ),
        )

    # -- 11. CI agrees, where CI exists -----------------------------------
    if require_status_checks:
        reported = dict(status or {})
        ci_state = str(reported.get("state") or "none")
        if ci_state == "success":
            detail = f"{reported.get('count', 0)} check(s) green: " + ", ".join(
                reported.get("contexts") or []
            )
        elif ci_state == "unreadable":
            # Never advise switching the gate off here. The checks may well be
            # green; JARVIS is simply not allowed to look, and turning off the
            # gate because of a permission error would disable a working control
            # on the strength of a misread.
            missing = ", ".join(reported.get("missing_permissions") or []) or "unknown"
            detail = (
                "JARVIS is not permitted to read CI on the head commit — the "
                f"GitHub token is missing: {missing}. This is a credential "
                "problem, not evidence about CI. Grant the permission; do not "
                "set require_status_checks = false to work around it."
            )
        elif ci_state == "none":
            detail = (
                "no status or check-run reported on the head commit; "
                "set [reliability.merge] require_status_checks = false only "
                "if this repository genuinely runs no CI"
            )
        else:
            contexts = ", ".join(reported.get("contexts") or [])
            detail = f"checks are '{ci_state}'" + (f" ({contexts})" if contexts else "")
        gate("status_checks", ci_state == "success", detail)
    else:
        gate(
            "status_checks",
            True,
            "not required ([reliability.merge] require_status_checks = false); "
            "local checks and preview verification are the only evidence",
        )

    failures = [g for g in gates if not g.passed]
    if failures:
        return MergeDecision(
            allowed=False,
            reason="; ".join(f"{g.name}: {g.detail}" for g in failures),
            rule=failures[0].name,
            gates=gates,
        )
    return MergeDecision(allowed=True, reason="every merge gate passed", gates=gates)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclass
class AutoMerger:
    """Reads the pull request, applies the gates, and merges at most one commit.

    Parameters
    ----------
    github:
        A :class:`~openjarvis.reliability.sources.github.GitHubSource`.
    store:
        Incident store — every decision is chained into the incident's history.
    enabled:
        Master switch, from ``[reliability.merge] enabled``. Off by default and
        checked as a gate rather than as an early return, so a refusal caused by
        the switch is recorded like any other.
    notifier:
        Optional notification router. Notified before the attempt and after it,
        whichever way it went.
    """

    github: Any
    store: Any
    enabled: bool = False
    method: str = "squash"
    base_branch: str = "main"
    branch_prefix: str = "jarvis/incident-"
    require_status_checks: bool = True
    delete_branch_on_merge: bool = False
    notifier: Any = None
    actor: str = "jarvis-automerge"

    def merge_for(self, incident: Incident) -> MergeRecord:
        """Attempt the merge for *incident*, returning what was decided.

        Never raises for a refusal: a refusal is an outcome, is recorded, and is
        reported. Exceptions from GitHub are caught, recorded and reported as a
        failure to merge rather than allowed to unwind into the watcher.
        """
        record = MergeRecord(
            incident_id=incident.id, method=self.method, actor=self.actor
        )
        attempt = self._last_attempt(incident)
        record.verified_head_sha = attempt.commit_sha if attempt else ""
        record.base_sha_at_verification = (
            attempt.base_commit if attempt is not None else ""
        )

        expected = pull_request_number(incident.resolution.pr_url)
        record.pr_number = expected

        pull_request: Dict[str, Any] = {}
        status: Optional[Dict[str, Any]] = None
        observed_base = ""
        if expected:
            try:
                # Read fresh, immediately before deciding. This is the whole
                # point: a value cached from when the PR was opened would be
                # describing a branch, not a commit.
                pull_request = self.github.get_pull_request(expected)
                record.observed_head_sha = str(pull_request.get("head_sha") or "")
                record.base_ref = str(pull_request.get("base_ref") or "")
                observed_base = self._base_sha()
                record.base_sha_observed = observed_base
            except Exception as exc:  # noqa: BLE001 - refusal, not a crash
                logger.warning("merge: could not read PR #%s: %s", expected, exc)
                record.error = f"could not read the pull request: {exc}"
                record.decision = MergeDecision(
                    allowed=False,
                    reason=record.error,
                    rule="pr_unreadable",
                    gates=[GateResult("pr_readable", False, record.error)],
                )
                self._record(incident, record)
                self._notify_outcome(incident, record)
                return record

            if self.require_status_checks and record.observed_head_sha:
                # Read separately from the pull request, so a CI read that fails
                # produces a *status* verdict rather than aborting the whole
                # evaluation as "could not read the pull request" — which is how
                # a missing token scope used to be reported as a missing PR.
                try:
                    status = self.github.combined_status(record.observed_head_sha)
                except Exception as exc:  # noqa: BLE001 - a gate result, not a crash
                    logger.warning("merge: could not read CI status: %s", exc)
                    status = {
                        "state": "unreadable",
                        "contexts": [],
                        "count": 0,
                        "missing_permissions": [f"unreadable: {exc}"],
                    }

        decision = evaluate_merge(
            incident,
            attempt,
            pull_request,
            enabled=self.enabled,
            base_branch=self.base_branch,
            branch_prefix=self.branch_prefix,
            expected_pr_number=expected,
            status=status,
            require_status_checks=self.require_status_checks,
            base_sha_at_verification=record.base_sha_at_verification,
            observed_base_sha=observed_base,
        )
        record.decision = decision

        if not decision.allowed:
            self._record(incident, record)
            self._notify_outcome(incident, record)
            return record

        self._notify_attempt(incident, record)
        try:
            result = self.github.merge_pull_request(
                number=record.pr_number,
                # The verified SHA, not the observed one. They are equal — a
                # gate just proved it — and passing the verified value means a
                # bug that let the gate through still cannot merge a commit
                # nobody verified.
                expected_head_sha=record.verified_head_sha,
                method=self.method,
                title=f"JARVIS {incident.id}: {incident.title}"[:120],
                message=self._commit_message(incident, record),
            )
            record.merged = bool(result.get("merged"))
            record.merge_commit_sha = str(result.get("sha") or "")
            if not record.merged:
                record.error = str(result.get("message") or "GitHub declined the merge")
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            logger.warning("merge: PR #%s was not merged: %s", record.pr_number, exc)
            record.merged = False
            record.error = str(exc)

        self._record(incident, record)
        self._notify_outcome(incident, record)
        return record

    # -- internals --------------------------------------------------------

    @staticmethod
    def _last_attempt(incident: Incident) -> Optional[RepairAttempt]:
        """The most recent attempt that actually verified.

        Not simply the last attempt: an incident can verify on attempt two and
        then record a later attempt for an unrelated recurrence, and merging the
        commit from an unverified attempt is precisely what this module exists
        to prevent.
        """
        for attempt in reversed(incident.attempts or []):
            if attempt.verification is not None and attempt.verification.passed:
                return attempt
        return incident.attempts[-1] if incident.attempts else None

    def _base_sha(self) -> str:
        """Current tip of the base branch, or ``""`` when it cannot be read."""
        try:
            ref = self.github.client.get_json(
                f"/repos/{self.github.repo}/git/ref/heads/{self.base_branch}",
                default={},
            )
            return str(((ref or {}).get("object") or {}).get("sha", ""))
        except Exception:  # noqa: BLE001 - unknown is handled by the gate
            logger.warning("merge: could not read the base branch tip")
            return ""

    def _commit_message(self, incident: Incident, record: MergeRecord) -> str:
        """Body of the squash commit. Identifiers and verdicts only."""
        lines = [
            f"Incident: {incident.id}",
            f"Verified commit: {record.verified_head_sha}",
            "Verified by: probe re-run against a preview deployment",
            "",
            "Merged by JARVIS after every gate passed. The coding agent's own",
            "claim of success was not consulted.",
        ]
        return "\n".join(lines)

    def _record(self, incident: Incident, record: MergeRecord) -> None:
        """Chain the decision into the incident's history and attach the detail.

        Two writes on purpose. The chained entry is tamper-evident but is one
        line; the evidence carries the full gate-by-gate account, which is what
        somebody actually needs when asking why a merge did not happen.
        """
        try:
            self.store.record_audit(incident, actor=self.actor, reason=record.summary())
        except Exception:  # noqa: BLE001 - never let bookkeeping break the flow
            logger.exception("could not chain the merge decision for %s", incident.id)
        try:
            self.store.add_evidence(
                incident,
                Evidence(
                    kind=EvidenceKind.NOTE,
                    summary=record.summary()[:200],
                    content=json.dumps(record.to_dict(), indent=2, sort_keys=True),
                    source="merge_gate",
                    trust=TrustLevel.TRUSTED,
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not attach the merge record to %s", incident.id)

    def _notify_attempt(self, incident: Incident, record: MergeRecord) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.merge_attempt(
                incident,
                pr_number=record.pr_number,
                head_sha=record.verified_head_sha,
                method=self.method,
            )
        except Exception:  # noqa: BLE001 - a notifier must never block a decision
            logger.exception("could not send a merge-attempt notification")

    def _notify_outcome(self, incident: Incident, record: MergeRecord) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.merge_outcome(incident, record=record)
        except Exception:  # noqa: BLE001
            logger.exception("could not send a merge-outcome notification")


def merge_severity(record: MergeRecord) -> Severity:
    """How loudly a merge outcome should be reported.

    A completed merge is HIGH: something reached the default branch without a
    human, and the owner should see it immediately even if the incident that
    caused it was minor. A refusal is MEDIUM — the system worked.
    """
    return Severity.HIGH if record.merged else Severity.MEDIUM
