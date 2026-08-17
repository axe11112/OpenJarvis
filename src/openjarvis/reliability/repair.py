"""The repair loop.

    detect → brief → worktree → agent → scope → checks → commit → push
           → preview → VERIFY → PR or escalate

Each attempt ends in one of a small number of outcomes, and every one of them is
recorded.  The loop stops for exactly three reasons: verification passed, the
attempt budget is exhausted, or the policy refused to continue.  It never stops
because the agent said it was done.

Three properties are load-bearing and should survive any future edit:

* **The agent works in an isolated worktree**, never the operator's checkout.
* **Scope is judged before anything is committed**, so a runaway diff fetches a
  human instead of becoming a pull request.
* **Only independent verification can produce RESOLVED.**  The agent's claim is
  recorded as an assertion and given no authority anywhere in this file.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from openjarvis.reliability.briefing import (
    BriefingRefusedError,
    build_briefing,
    redact_secrets,
)
from openjarvis.reliability.checks import CheckSuite, CheckSuiteResult
from openjarvis.reliability.code_agent import CodeAgent, CodeAgentError
from openjarvis.reliability.events import (
    RELIABILITY_PR_CREATED,
    RELIABILITY_REPAIR_ATTEMPT_END,
    RELIABILITY_REPAIR_ATTEMPT_START,
    RELIABILITY_VERIFICATION,
)
from openjarvis.reliability.playbook import (
    IncidentHistory,
    build_handover,
    classify_cause,
    next_strategy,
)
from openjarvis.reliability.policy import SafetyPolicy
from openjarvis.reliability.probes.spec import ProbeSpec
from openjarvis.reliability.scope import (
    ScopeLimits,
    ScopeVerdict,
    assess_scope,
    find_test_files,
)
from openjarvis.reliability.types import (
    Evidence,
    EvidenceKind,
    Incident,
    IncidentState,
    InvalidTransitionError,
    RepairAttempt,
    TrustLevel,
    VerificationResult,
    now_iso,
    path_between,
)
from openjarvis.reliability.verify import Verifier
from openjarvis.reliability.workspace import RepairWorkspace, WorkspaceError, Worktree

logger = logging.getLogger(__name__)

__all__ = ["RepairLoop", "RepairOutcome", "TestRunResult", "run_tests"]

#: Attempt outcomes.  Recorded on every RepairAttempt.
OUTCOME_VERIFIED = "verified"
OUTCOME_VERIFICATION_FAILED = "verification_failed"
OUTCOME_NO_DIFF = "no_diff"
OUTCOME_TESTS_FAILED = "tests_failed"
OUTCOME_AGENT_ERROR = "agent_error"
OUTCOME_POLICY_DENIED = "policy_denied"
OUTCOME_PROTECTED_PATH = "protected_path"
OUTCOME_NO_PREVIEW = "no_preview"
#: The diff was too large, or touched a category a repair has no business in.
OUTCOME_SCOPE_VIOLATION = "scope_violation"
#: The isolated worktree could not be prepared, or the branch could not be
#: pushed.  JARVIS's own problem, never evidence about the target.
OUTCOME_WORKSPACE_ERROR = "workspace_error"


@dataclass(slots=True)
class TestRunResult:
    """Outcome of running the project's own test suite."""

    ran: bool
    passed: bool
    summary: str = ""
    output: str = ""


@dataclass(slots=True)
class RepairOutcome:
    """The result of the whole loop."""

    resolved: bool
    attempts: int
    final_state: IncidentState
    reason: str = ""
    branch: str = ""
    pull_request_url: str = ""
    verification: Optional[VerificationResult] = None


def run_tests(
    command: str,
    *,
    workspace: str,
    timeout: int = 1800,
    max_output: int = 8000,
) -> TestRunResult:
    """Run the project's own test command in *workspace*.

    A missing command is reported as "did not run", never as "passed" — the
    difference matters when deciding whether a repair is trustworthy.
    """
    if not command.strip():
        return TestRunResult(
            ran=False, passed=False, summary="no test command configured"
        )
    try:
        proc = subprocess.run(
            command,
            shell=True,  # noqa: S602 - operator-configured command, not user input
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return TestRunResult(
            ran=True, passed=False, summary=f"tests timed out after {timeout}s"
        )
    except OSError as exc:
        return TestRunResult(
            ran=False, passed=False, summary=f"could not run tests: {exc}"
        )

    output = ((proc.stdout or "") + (proc.stderr or ""))[-max_output:]
    passed = proc.returncode == 0
    return TestRunResult(
        ran=True,
        passed=passed,
        summary="tests passed" if passed else f"tests failed (exit {proc.returncode})",
        output=output,
    )


@dataclass
class RepairLoop:
    """Drives repair attempts for one incident.

    Parameters
    ----------
    agent:
        The coding engine.
    policy:
        Safety gates.
    verifier:
        Independent verification.
    store:
        Incident store, so every attempt and transition is persisted.
    github:
        Optional GitHub source for branches and pull requests.
    preview_lookup:
        ``(branch) -> preview URL or ""``.  Injected so the loop does not
        depend on Vercel directly.
    preview_logs:
        ``(branch) -> build log text``.  Consulted only when no preview
        appeared, so the agent's next attempt sees why the build failed rather
        than merely that it did.
    workspace_manager:
        Creates an isolated git worktree per attempt.  When absent the loop
        falls back to running in :attr:`workspace`, which is useful for unit
        tests but is **not** how the loop is wired in production — see
        ``docs/JARVIS_REPAIR_LOOP.md``.
    checks:
        Local gates (lint, typecheck, tests, build).  When absent, one is built
        from :attr:`test_command`.
    notifier:
        Optional :class:`~openjarvis.reliability.notify.NotificationRouter`.
    """

    agent: CodeAgent
    policy: SafetyPolicy
    verifier: Verifier
    store: Any
    workspace: str = ""
    test_command: str = ""
    github: Any = None
    preview_lookup: Optional[Callable[[str], str]] = None
    preview_logs: Optional[Callable[[str], str]] = None
    bus: Any = None
    sleep: Callable[[float], None] = time.sleep
    preview_wait_seconds: float = 0.0
    protected_paths: List[str] = field(default_factory=list)
    workspace_manager: Optional[RepairWorkspace] = None
    checks: Optional[CheckSuite] = None
    notifier: Any = None
    scope_limits: Optional[ScopeLimits] = None
    base_ref: str = "HEAD"
    base_branch: str = "main"
    push_branch: bool = True
    #: Optional :class:`~openjarvis.reliability.merge.AutoMerger`. ``None`` — the
    #: default — means the pull request is the end of the line, which is what
    #: this loop did for its whole existence before merging was implemented.
    #: Consulted only after a pull request has actually been opened, and it
    #: re-derives every gate itself rather than trusting this loop's word for it.
    auto_merger: Any = None
    #: Optional :class:`~openjarvis.reliability.postmerge.PostMergeVerifier`.
    #: Required once ``auto_merger`` can actually merge: without it a merged
    #: incident has no way to reach RESOLVED and escalates instead, because
    #: "merged" is not "production works" and nothing else here can tell the
    #: difference.
    post_merge_verifier: Any = None
    #: Production URL to re-check against before handing an incident to a human.
    #: Empty disables the re-check, which is the previous behaviour.
    production_url: str = ""

    def __post_init__(self) -> None:
        if self.checks is None:
            # Preserve the single-command behaviour when no suite is configured.
            self.checks = CheckSuite.from_config(test_command=self.test_command)
        if self.scope_limits is None:
            self.scope_limits = ScopeLimits()

    # -- entry point ------------------------------------------------------

    def run(self, incident: Incident, spec: ProbeSpec) -> RepairOutcome:
        """Attempt to repair *incident*, verifying every attempt independently."""
        # Checked before the policy, because it outranks it. A merge that broke
        # production keeps the probe failing, which opens a *fresh* incident
        # with a clean attempt count and a clean state — one that every other
        # gate would happily wave through. Repairing it would stack a second
        # unreviewed change on a live one already known to be bad.
        storm = self._post_merge_block(incident)
        if storm:
            self._escalate(incident, storm)
            return RepairOutcome(
                resolved=False,
                attempts=incident.attempts_used,
                final_state=incident.state,
                reason=storm,
            )

        gate = self.policy.may_attempt_repair(incident)
        if not gate and not gate.needs_human:
            # Refused, but nobody is needed: repair is switched off, the
            # severity is outside what JARVIS may write code for, or a human
            # already owns it. The incident stays open and watched — a probe
            # that starts passing will close it — and no one is woken.
            #
            # This branch is the fix for the specific behaviour the owner saw:
            # a login page that answered correctly in 41 seconds became
            # HUMAN_REQUIRED one second after it was detected, having never been
            # looked at, because "JARVIS may not repair this" was being read as
            # "a person must deal with this".
            logger.info(
                "Incident %s: no repair (%s); leaving it open and watched",
                incident.id,
                gate.reason,
            )
            self._add_note(
                incident,
                "No automatic repair",
                content=(
                    f"{gate.reason}\n\n"
                    "The incident stays open and monitored. It will close by "
                    "itself if the check starts passing again."
                ),
            )
            return RepairOutcome(
                resolved=False,
                attempts=incident.attempts_used,
                final_state=incident.state,
                reason=gate.reason,
            )
        if not gate:
            self._escalate(incident, gate.reason, spec=spec)
            return RepairOutcome(
                resolved=False,
                attempts=incident.attempts_used,
                final_state=incident.state,
                reason=gate.reason,
            )

        previous_failure = ""
        last_verification: Optional[VerificationResult] = None
        history = IncidentHistory(store=self.store)
        cause = classify_cause(incident)

        while incident.attempts_used < self.policy.max_attempts:
            attempt_number = incident.attempts_used + 1
            # A different hypothesis every time. Three passes at one idea is one
            # attempt with extra steps, and it was the shape of "Sir gives up
            # too easily" from the inside: the loop ran out of permitted
            # attempts without ever running out of ideas.
            strategy = next_strategy(history.strategies_tried(incident), cause=cause)
            if strategy is None:
                reason = (
                    "every hypothesis I have was tried and none of them held: "
                    + ", ".join(history.strategies_tried(incident))
                )
                self._escalate(incident, reason, spec=spec)
                return RepairOutcome(
                    resolved=False,
                    attempts=incident.attempts_used,
                    final_state=incident.state,
                    reason=reason,
                    verification=last_verification,
                )
            logger.info(
                "Incident %s attempt %d: working from the hypothesis that %s",
                incident.id,
                attempt_number,
                strategy.hypothesis,
            )
            attempt, verification, stop_reason = self._attempt(
                incident, spec, attempt_number, previous_failure, strategy
            )
            last_verification = verification

            if attempt.outcome == OUTCOME_VERIFIED:
                return self._succeed(incident, attempt, verification, spec)

            if stop_reason:
                self._escalate(incident, stop_reason, spec=spec)
                return RepairOutcome(
                    resolved=False,
                    attempts=incident.attempts_used,
                    final_state=incident.state,
                    reason=stop_reason,
                    branch=attempt.branch,
                    verification=verification,
                )

            previous_failure = (
                Verifier.summarize_for_retry(verification)
                or attempt.test_summary
                or attempt.outcome
            )

        reason = (
            f"{self.policy.max_attempts} repair attempts did not produce a verified fix"
        )
        self._escalate(incident, reason, spec=spec)
        return RepairOutcome(
            resolved=False,
            attempts=incident.attempts_used,
            final_state=incident.state,
            reason=reason,
            verification=last_verification,
        )

    # -- one attempt ------------------------------------------------------

    def _attempt(
        self,
        incident: Incident,
        spec: ProbeSpec,
        number: int,
        previous_failure: str,
        strategy: Any = None,
    ) -> tuple[RepairAttempt, Optional[VerificationResult], str]:
        """Run one attempt. Returns (attempt, verification, stop_reason)."""
        branch = self._branch_name(incident)
        attempt = RepairAttempt(number=number, branch=branch, started_at=now_iso())
        attempt.strategy = getattr(strategy, "key", "") or ""
        self._publish(RELIABILITY_REPAIR_ATTEMPT_START, incident, attempt=number)
        self._notify_progress(incident, number)
        worktree: Optional[Worktree] = None

        try:
            # --- brief ---------------------------------------------------
            try:
                briefing = build_briefing(
                    incident,
                    attempt=number,
                    max_attempts=self.policy.max_attempts,
                    previous_failure=previous_failure,
                    strategy=strategy,
                    protected_paths=self.protected_paths,
                    test_command=self.test_command,
                )
            except BriefingRefusedError as exc:
                attempt.outcome = OUTCOME_POLICY_DENIED
                return self._finish(incident, attempt), None, str(exc)

            attempt.briefing_hash = briefing.hash
            self._transition(incident, IncidentState.FIXING, f"repair attempt {number}")

            # --- isolated workspace --------------------------------------
            try:
                worktree = self._prepare_workspace(incident, attempt, branch)
            except WorkspaceError as exc:
                attempt.outcome = OUTCOME_WORKSPACE_ERROR
                return (
                    self._finish(incident, attempt),
                    None,
                    f"could not prepare an isolated workspace: {exc}",
                )

            workspace_path = worktree.path if worktree else (self.workspace or ".")

            # --- remote branch -------------------------------------------
            if self.github is not None and worktree is None and number == 1:
                # Without a local worktree there is nothing to push, so the
                # branch has to be created through the API instead.
                try:
                    self.github.create_branch(branch)
                except Exception as exc:
                    logger.warning("could not create branch %s: %s", branch, exc)

            # --- agent ----------------------------------------------------
            try:
                result = self.agent.run(
                    briefing.text, workspace=workspace_path, timeout=1800
                )
            except CodeAgentError as exc:
                attempt.outcome = OUTCOME_AGENT_ERROR
                return (
                    self._finish(incident, attempt),
                    None,
                    f"the coding agent could not run: {exc}",
                )

            # The agent's summary is model output that has just read the
            # application's source; redact it before it is persisted, rendered
            # into a pull request, or sent to the owner.
            attempt.claim = redact_secrets(result.claim)[:4000]
            self._record_diff(attempt, result, worktree)

            if not result.succeeded:
                attempt.outcome = OUTCOME_AGENT_ERROR
                attempt.test_summary = redact_secrets(result.error)[:2000]
                return self._finish(incident, attempt), None, ""

            if not attempt.produced_changes:
                # A confident claim with no diff is a failed attempt, not a success.
                attempt.outcome = OUTCOME_NO_DIFF
                self._add_note(
                    incident,
                    "The coding agent produced no changes despite reporting a fix.",
                )
                return self._finish(incident, attempt), None, ""

            # --- protected paths ------------------------------------------
            paths_ok = self.policy.may_modify_paths(attempt.changed_files)
            if not paths_ok:
                attempt.outcome = OUTCOME_PROTECTED_PATH
                return self._finish(incident, attempt), None, paths_ok.reason

            # --- change scope ---------------------------------------------
            # Judged before anything is committed or pushed: a diff nobody asked
            # for should never become a branch on the remote.
            verdict = assess_scope(
                attempt.changed_files,
                limits=self.scope_limits,
                lines_changed=attempt.lines_changed_total,
                protected_paths=self.protected_paths,
            )
            attempt.scope = verdict.to_dict()
            attempt.regression_tests = find_test_files(attempt.changed_files)
            if not verdict:
                attempt.outcome = OUTCOME_SCOPE_VIOLATION
                self._add_note(
                    incident,
                    f"Repair stopped: the change is outside the permitted scope "
                    f"({verdict.reason}).",
                )
                return (
                    self._finish(incident, attempt),
                    None,
                    f"the change is outside the permitted scope: {verdict.reason}",
                )

            # --- local checks ---------------------------------------------
            self._transition(
                incident, IncidentState.TESTING, f"attempt {number}: checks"
            )
            suite = self.checks.run(workspace=workspace_path)
            self._record_checks(incident, attempt, suite, number)
            if not suite.passed:
                attempt.outcome = OUTCOME_TESTS_FAILED
                return self._finish(incident, attempt), None, ""

            # --- commit and publish the branch ----------------------------
            try:
                self._publish_branch(incident, attempt, worktree)
            except WorkspaceError as exc:
                attempt.outcome = OUTCOME_WORKSPACE_ERROR
                return (
                    self._finish(incident, attempt),
                    None,
                    f"could not publish the repair branch: {exc}",
                )

            # --- preview + verification -----------------------------------
            self._transition(
                incident, IncidentState.VERIFYING, f"attempt {number}: verification"
            )
            target_url = self._await_preview(branch)
            attempt.preview_url = target_url
            if not target_url:
                self._record_preview_failure(incident, attempt, branch, number)

            verification = self.verifier.verify(
                spec, target_url=target_url, incident_id=incident.id
            )
            attempt.verification = verification
            attempt.outcome = (
                OUTCOME_VERIFIED
                if verification.passed
                else (
                    OUTCOME_NO_PREVIEW
                    if not target_url
                    else OUTCOME_VERIFICATION_FAILED
                )
            )
            self._finish(incident, attempt)
            self._publish(
                RELIABILITY_VERIFICATION,
                incident,
                attempt=number,
                passed=verification.passed,
            )
            self._publish(
                RELIABILITY_REPAIR_ATTEMPT_END,
                incident,
                attempt=number,
                outcome=attempt.outcome,
            )
            return attempt, verification, ""
        finally:
            if worktree is not None:
                self._release_workspace(worktree, attempt)

    # -- terminal paths ---------------------------------------------------

    def _succeed(
        self,
        incident: Incident,
        attempt: RepairAttempt,
        verification: Optional[VerificationResult],
        spec: Any = None,
    ) -> RepairOutcome:
        """Handle a verified repair: PR by default, deploy only if permitted.

        A final security sweep runs before the pull request is opened. It
        re-checks what was already checked earlier in the attempt, deliberately:
        the earlier check ran before the commit, and this is the last moment
        before the change becomes visible outside this machine. Re-checking a
        few globs is cheap; publishing a branch containing a credential is not.
        """
        sweep = self._security_sweep(incident, attempt)
        if not sweep.allowed:
            self._add_note(
                incident,
                "Pre-PR security check failed",
                content=sweep.reason,
            )
            self._escalate(
                incident, f"the pre-pull-request security check failed: {sweep.reason}"
            )
            return RepairOutcome(
                resolved=False,
                attempts=incident.attempts_used,
                final_state=incident.state,
                reason=f"the pre-pull-request security check failed: {sweep.reason}",
                branch=attempt.branch,
                verification=verification,
            )

        deploy = self.policy.may_deploy(
            incident,
            verification,
            fix_class=str(incident.metadata.get("fix_class", "")),
            changed_paths=attempt.changed_files,
        )

        pull_request_url = ""
        if self.github is not None:
            try:
                created = self.github.create_pull_request(
                    head=attempt.branch,
                    title=f"JARVIS {incident.id}: {incident.title}"[:120],
                    body=self._pull_request_body(incident, attempt, verification),
                    labels=["jarvis"],
                )
                pull_request_url = created.get("url", "")
                self._publish(
                    RELIABILITY_PR_CREATED,
                    incident,
                    url=pull_request_url,
                    branch=attempt.branch,
                )
            except Exception as exc:
                logger.warning("could not open a pull request: %s", exc)

        incident.resolution.root_cause = incident.metadata.get("root_cause", "")
        incident.resolution.fix_summary = attempt.claim[:1000]
        incident.resolution.pr_url = pull_request_url
        incident.resolution.attempts_used = incident.attempts_used
        self.store.save(incident)

        # The merge is attempted *before* any resolution is recorded. Resolving
        # first and merging afterwards — which this loop did until post-merge
        # verification existed — meant a merge could land on the default branch,
        # trigger a production deployment, and find the incident already marked
        # RESOLVED on the strength of a preview of a commit that no longer
        # exists. Nothing downstream could then tell a verified production from
        # an unexamined one.
        merge_record = self._maybe_merge(incident, pull_request_url)
        merged = bool(getattr(merge_record, "merged", False))

        if merged:
            # Live on the default branch, unproven in production. The incident
            # is neither resolved nor failed until production says so.
            self._transition(
                incident,
                IncidentState.MERGED,
                f"merged at {str(getattr(merge_record, 'merge_commit_sha', ''))[:12]}; "
                "production not yet verified",
            )
            return self._verify_production(
                incident, attempt, verification, merge_record, pull_request_url, spec
            )

        # Not merged — refused by the gates, not configured, or no pull request.
        # The pull request is the deliverable, exactly as before.
        if deploy:
            self._transition(
                incident,
                IncidentState.RESOLVED,
                "verified and deployed under the auto-deploy allowlist",
            )
        else:
            # Verified but not deployable: a pull request is the deliverable.
            self._transition(
                incident,
                IncidentState.RESOLVED,
                f"verified; {deploy.reason}",
            )

        self._notify("resolved", incident, attempt=attempt, verification=verification)

        return RepairOutcome(
            resolved=True,
            attempts=incident.attempts_used,
            final_state=incident.state,
            reason=deploy.reason if not deploy else "verified and deployed",
            branch=attempt.branch,
            pull_request_url=pull_request_url,
            verification=verification,
        )

    def _verify_production(
        self,
        incident: Incident,
        attempt: RepairAttempt,
        verification: Any,
        merge_record: Any,
        pull_request_url: str,
        spec: Any = None,
    ) -> RepairOutcome:
        """Prove production after a merge, or hand the incident to a human.

        The one path out of ``MERGED``. With no verifier configured there is no
        honest way to claim production is good, so the incident escalates rather
        than resolving — an operator who enabled automatic merge without
        automatic production verification has asked for something JARVIS should
        refuse to fake.
        """
        if self.post_merge_verifier is None:
            reason = (
                "the merge landed but no post-merge production verifier is "
                "configured, so production cannot be proved"
            )
            self._fail_after_merge(incident, merge_record, result=None, reason=reason)
            return RepairOutcome(
                resolved=False,
                attempts=incident.attempts_used,
                final_state=incident.state,
                reason=reason,
                branch=attempt.branch,
                pull_request_url=pull_request_url,
                verification=verification,
            )

        try:
            result = self.post_merge_verifier.verify(
                incident, merge_record=merge_record, spec=spec
            )
        except Exception as exc:  # noqa: BLE001 - unproven, never assumed good
            logger.exception("post-merge verification raised for %s", incident.id)
            reason = f"post-merge production verification could not run: {exc}"
            self._fail_after_merge(incident, merge_record, result=None, reason=reason)
            return RepairOutcome(
                resolved=False,
                attempts=incident.attempts_used,
                final_state=incident.state,
                reason=reason,
                branch=attempt.branch,
                pull_request_url=pull_request_url,
                verification=verification,
            )

        if not result.verified:
            self._fail_after_merge(
                incident, merge_record, result=result, reason=result.reason
            )
            return RepairOutcome(
                resolved=False,
                attempts=incident.attempts_used,
                final_state=incident.state,
                reason=result.reason,
                branch=attempt.branch,
                pull_request_url=pull_request_url,
                verification=verification,
            )

        # Production itself proved it. This is the only automatic route from
        # MERGED to RESOLVED.
        self._transition(incident, IncidentState.RESOLVED, result.reason)
        if self.notifier is not None:
            try:
                self.notifier.production_verified(
                    incident, record=merge_record, result=result
                )
            except Exception:  # noqa: BLE001
                logger.exception("could not send the production-verified notification")
        return RepairOutcome(
            resolved=True,
            attempts=incident.attempts_used,
            final_state=incident.state,
            reason=result.reason,
            branch=attempt.branch,
            pull_request_url=pull_request_url,
            verification=verification,
        )

    def _fail_after_merge(
        self,
        incident: Incident,
        merge_record: Any,
        *,
        result: Any,
        reason: str,
    ) -> None:
        """Escalate a merge that landed and did not come good in production.

        Four things happen here and the order matters. The durable marker is
        written *first*: if the process dies immediately afterwards, the guard
        that stops a second merge for this fingerprint must already be on disk.
        Only then is the incident moved, the owner told, and the evidence
        recorded.

        No rollback is attempted. Reverting a merge is a write to the default
        branch, and there is no tested rollback mechanism here to invoke — an
        untested one, run automatically against a production already known to be
        unhealthy, is how a bad deployment becomes an outage.
        """
        from openjarvis.reliability.postmerge import (
            POST_MERGE_FAILURE_KEY,
            PostMergeResult,
            failure_marker,
        )

        verdict = result if result is not None else PostMergeResult(reason=reason)
        try:
            incident.metadata[POST_MERGE_FAILURE_KEY] = failure_marker(
                merge_record=merge_record, result=verdict
            )
            self.store.save(incident)
        except Exception:  # noqa: BLE001
            logger.exception("could not write the post-merge guard for %s", incident.id)

        self._add_note(
            incident,
            "Production verification failed after merge",
            content=(
                f"{reason}\n\n"
                f"Merge commit: {getattr(merge_record, 'merge_commit_sha', '')}\n"
                f"Pull request: #{getattr(merge_record, 'pr_number', 0)}\n"
                "No rollback was attempted and no database was written."
            ),
        )
        # notify=False: the CRITICAL message below is this event's notification,
        # and it says more than the generic escalation would.
        self._escalate(incident, reason, notify=False)

        if self.notifier is not None:
            try:
                self.notifier.post_merge_failed(
                    incident, record=merge_record, result=verdict
                )
            except Exception:  # noqa: BLE001
                logger.exception("could not send the CRITICAL post-merge notification")

    def _maybe_merge(self, incident: Incident, pull_request_url: str) -> Any:
        """Hand a freshly opened pull request to the merge gates, if configured.

        Only reached when a pull request actually exists: with no PR there is
        nothing to merge, and calling the merger anyway would record a refusal
        for a decision nobody asked it to make.

        Nothing is passed to the merger about *why* this repair succeeded. It
        re-reads the incident, re-reads the pull request from GitHub, and
        re-derives every gate from the recorded attempt. This loop having just
        concluded the repair is good is not evidence, for the same reason the
        coding agent's claim is not: the value of an independent check is
        exactly that it does not inherit the caller's conclusion.

        Returns the merge record, or ``None`` when no merge was attempted. A
        raised merge still returns ``None``, which reads downstream as "not
        merged" — the safe direction, since the alternative is treating an
        unknown outcome as a landed one.
        """
        if self.auto_merger is None or not pull_request_url:
            return None
        try:
            return self.auto_merger.merge_for(incident)
        except Exception:  # pragma: no cover - a merge must never break a repair
            logger.exception("the merge gate raised for %s", incident.id)
            return None

    def _post_merge_block(self, incident: Incident) -> str:
        """Why this incident may not be repaired, given past post-merge failures.

        Empty string means no block. Fingerprint-scoped: the incident that broke
        production is already ``HUMAN_REQUIRED``, so guarding only that one would
        guard the only case that needs no guarding.
        """
        from openjarvis.reliability.postmerge import post_merge_failure_for

        marker = post_merge_failure_for(self.store, incident.fingerprint)
        if not marker:
            return ""
        return (
            "a previous repair for this fingerprint was merged and production "
            f"did not verify ({marker.get('incident_id', 'unknown')}, merge "
            f"{str(marker.get('merge_commit_sha', ''))[:12]}). Automatic repair "
            "is blocked until a human clears it: "
            f"{str(marker.get('reason', ''))[:200]}"
        )

    def _still_failing(self, incident: Incident, spec: Any) -> bool:
        """Whether the original problem is still there, checked just now.

        Called immediately before handing an incident to a human, and it is the
        cheapest autonomy available: every one of the first nine escalations in
        this system's history was for a fault that had already cleared by the
        time anybody could have looked at it. Waking somebody for a problem that
        no longer exists is the most expensive kind of false alarm, because it
        teaches them the next one is probably nothing too.

        Fails *closed*. No production URL, no spec, an unreadable verdict or a
        crash all mean "assume it is still broken and escalate": the alternative
        is silently dropping a real outage because a check could not run.

        Only consulted when the incident's state already permits RESOLVED, which
        in practice means after a verification attempt. That is deliberate — see
        the caller.
        """
        if not self.production_url or spec is None:
            return True
        try:
            verdict = self.verifier.verify(
                spec, target_url=self.production_url, incident_id=incident.id
            )
        except Exception:  # noqa: BLE001 - unverifiable means still broken
            logger.exception(
                "incident %s: could not re-check before escalating", incident.id
            )
            return True
        if not getattr(verdict, "passed", False):
            return True
        logger.info(
            "incident %s: the problem no longer reproduces against production; "
            "closing instead of escalating",
            incident.id,
        )
        self._add_note(
            incident,
            "Recovered before escalation",
            content=(
                "Re-checked against production immediately before handing this "
                "over, and the original probe now passes:\n\n"
                f"{getattr(verdict, 'actual', '') or 'the probe passed'}\n\n"
                "Nobody was woken. The fault is recorded here in full."
            ),
        )
        return False

    def _recovered_instead(self, incident: Incident, reason: str) -> None:
        """Close an incident that fixed itself while JARVIS was working on it."""
        try:
            self.store.transition(
                incident,
                IncidentState.RESOLVED,
                reason=(
                    "the problem stopped reproducing against production before "
                    f"escalation ({reason})"
                ),
            )
        except Exception:
            logger.exception("could not resolve %s after recovery", incident.id)

    def _escalate(
        self,
        incident: Incident,
        reason: str,
        *,
        notify: bool = True,
        result: Any = None,
        spec: Any = None,
    ) -> None:
        """Stop touching code and hand the incident to a human — with a handover.

        ``notify=False`` is for callers that send their own, more specific
        message. The post-merge failure path is the only one: it has a CRITICAL
        notification naming the live deployment, and sending the generic
        escalation alongside it would tell the owner the same bad news twice.

        The handover is assembled before anything is sent, and if it cannot be
        filled in that fact is logged loudly. "3 repair attempts did not produce
        a verified fix" is a true sentence that helps nobody at 3am; what is
        owed is what failed, what was believed to be causing it, the evidence,
        what was tried, why it did not work, and what is actually being asked.
        """
        # Evidence before escalation, not instead of it. If the fault has
        # cleared there is nothing to hand over and nobody to wake.
        #
        # Conditional on the state machine already permitting RESOLVED, and that
        # condition is load-bearing rather than defensive. Reaching RESOLVED only
        # through VERIFYING is the structural guarantee behind "never trust the
        # coding agent's claim that it fixed something" — a test asserts it —
        # and widening the table so this path could always close an incident
        # would trade that guarantee for a convenience. So where closing is not
        # legal, this escalates exactly as before: a human hearing about a fault
        # that has cleared is a much smaller problem than a state machine that
        # can be talked into RESOLVED.
        if (
            spec is not None
            and incident.can_transition_to(IncidentState.RESOLVED)
            and not self._still_failing(incident, spec)
        ):
            self._recovered_instead(incident, reason)
            return

        logger.warning("Incident %s requires a human: %s", incident.id, reason)

        handover = build_handover(
            incident,
            reason=reason,
            max_attempts=self.policy.max_attempts,
            history=IncidentHistory(store=self.store),
            result=result,
        )
        if not handover.is_complete():
            # Not fatal — refusing to escalate would be worse than escalating
            # thinly — but it is a defect in this path and it is recorded as one
            # rather than quietly shipped.
            logger.error(
                "Incident %s: the handover is missing %s; escalating anyway",
                incident.id,
                ", ".join(handover.missing()),
            )
        try:
            incident.metadata["handover"] = handover.to_dict()
            self.store.save(incident)
        except Exception:  # noqa: BLE001
            logger.exception("could not record the handover for %s", incident.id)
        self._add_note(incident, "Handing over", content=handover.render())

        if incident.state is not IncidentState.HUMAN_REQUIRED:
            try:
                self.store.transition(
                    incident, IncidentState.HUMAN_REQUIRED, reason=reason
                )
            except Exception:
                logger.exception("could not transition %s", incident.id)
        if notify:
            self._notify(
                "human_required",
                incident,
                reason=reason,
                attempts=incident.attempts_used,
                max_attempts=self.policy.max_attempts,
            )

    def _security_sweep(
        self, incident: Incident, attempt: RepairAttempt
    ) -> ScopeVerdict:
        """Last check before anything leaves the machine.

        Four questions, all of which must be answerable from what JARVIS already
        recorded rather than from the agent's account: does the diff touch
        anything forbidden, does the branch name identify this incident, does
        any recorded text still contain a credential, and is the branch
        something other than the default branch.
        """
        verdict = assess_scope(
            attempt.changed_files,
            limits=self.scope_limits,
            lines_changed=attempt.lines_changed_total,
            protected_paths=self.protected_paths,
        )

        extra: List[str] = []

        expected_branch = self._branch_name(incident)
        if attempt.branch != expected_branch:
            extra.append(
                f"the branch is '{attempt.branch}', expected '{expected_branch}'"
            )
        if attempt.branch == self.base_branch:
            extra.append("the repair branch is the default branch")

        # The claim is redacted on the way in; if a secret is still visible here
        # the redaction failed and the pull request must not be opened.
        for label, text in (
            ("the agent's summary", attempt.claim),
            ("the check summary", attempt.test_summary),
        ):
            if text and text != redact_secrets(text):
                extra.append(f"a credential is still present in {label}")

        if extra:
            verdict.allowed = False
            verdict.reasons.extend(extra)
        return verdict

    # -- attempt helpers --------------------------------------------------

    def _branch_name(self, incident: Incident) -> str:
        """The isolated branch for this incident, never the default branch."""
        if self.workspace_manager is not None:
            return self.workspace_manager.branch_name_for(incident.id)
        if self.github is not None:
            return self.github.branch_name_for(incident.id)
        return f"jarvis/incident-{incident.id}"

    def _finish(self, incident: Incident, attempt: RepairAttempt) -> RepairAttempt:
        """Stamp and persist an attempt. Every exit path goes through here."""
        attempt.finished_at = now_iso()
        self.store.add_attempt(incident, attempt)
        return attempt

    def _prepare_workspace(
        self, incident: Incident, attempt: RepairAttempt, branch: str
    ) -> Optional[Worktree]:
        """Cut an isolated worktree, recording what it was based on.

        Returns ``None`` when no manager is configured, in which case the loop
        runs in :attr:`workspace` — supported for unit tests, but never how the
        loop is wired for a real repair.
        """
        if self.workspace_manager is None:
            return None
        push_ok = self.policy.may_push_to(branch, self.base_branch)
        if not push_ok:
            raise WorkspaceError(push_ok.reason)
        worktree = self.workspace_manager.create(incident.id, base_ref=self.base_ref)
        attempt.base_commit = worktree.base_commit
        attempt.worktree_path = worktree.path
        self._add_note(
            incident,
            f"Isolated repair workspace (attempt {attempt.number})",
            content=(
                f"branch: {worktree.branch}\n"
                f"base commit: {worktree.base_commit}\n"
                f"base ref: {worktree.base_ref}\n"
                f"path: {worktree.path}\n"
                f"created: {worktree.created_at}"
            ),
        )
        return worktree

    def _release_workspace(self, worktree: Worktree, attempt: RepairAttempt) -> None:
        """Tear the worktree down, keeping failures for inspection."""
        if self.workspace_manager is None:
            return
        try:
            self.workspace_manager.remove(
                worktree, succeeded=attempt.outcome == OUTCOME_VERIFIED
            )
        except Exception:  # pragma: no cover - cleanup must never mask a result
            logger.exception("could not clean up worktree %s", worktree.path)

    def _record_diff(
        self, attempt: RepairAttempt, result: Any, worktree: Optional[Worktree]
    ) -> None:
        """Record what changed, preferring git over the agent's own account."""
        if worktree is not None and self.workspace_manager is not None:
            try:
                attempt.changed_files = self.workspace_manager.changed_files(worktree)
                attempt.diff_stat = self.workspace_manager.diff_stat(worktree)
                added, removed = self.workspace_manager.line_counts(worktree)
                attempt.lines_changed_total = added + removed
                return
            except WorkspaceError:
                logger.exception("could not read the diff from %s", worktree.path)
        attempt.changed_files = list(result.changed_files)
        attempt.diff_stat = result.diff_stat

    def _record_checks(
        self,
        incident: Incident,
        attempt: RepairAttempt,
        suite: CheckSuiteResult,
        number: int,
    ) -> None:
        """Persist check results and attach failing output as evidence."""
        attempt.checks = suite.to_dict()
        tests = next((r for r in suite.results if r.name == "tests"), None)
        if tests is not None:
            attempt.tests_passed = tests.passed if tests.ran else None
            attempt.test_summary = tests.summary
        if not attempt.test_summary:
            attempt.test_summary = suite.summary[:2000]
        for result in suite.results:
            if result.output:
                self._add_note(
                    incident,
                    f"{result.name} output (attempt {number})",
                    content=result.output,
                    kind=EvidenceKind.TEST_OUTPUT,
                )

    def _publish_branch(
        self,
        incident: Incident,
        attempt: RepairAttempt,
        worktree: Optional[Worktree],
    ) -> None:
        """Commit the agent's work and push the incident branch.

        Pushing is what makes a preview deployment possible, so it happens only
        after the local checks passed — a branch that fails its own tests should
        never consume a build.
        """
        if worktree is None or self.workspace_manager is None:
            return
        message = (
            f"JARVIS {incident.id}: {incident.title}"[:72]
            + f"\n\nAutomated repair attempt {attempt.number}.\n"
            f"Based on {attempt.base_commit}.\n"
            "Verified independently before any pull request is opened."
        )
        attempt.commit_sha = self.workspace_manager.commit_all(worktree, message)
        if not self.push_branch:
            return
        push_ok = self.policy.may_push_to(worktree.branch, self.base_branch)
        if not push_ok:
            raise WorkspaceError(push_ok.reason)
        self.workspace_manager.push(worktree)

    def _record_preview_failure(
        self,
        incident: Incident,
        attempt: RepairAttempt,
        branch: str,
        number: int,
    ) -> None:
        """Attach deployment logs when no preview appeared.

        Without this the agent's next attempt learns only that verification did
        not happen, which is not enough to do anything differently.
        """
        if self.preview_logs is None:
            return
        try:
            logs = self.preview_logs(branch) or ""
        except Exception:  # pragma: no cover - defensive
            logger.exception("could not fetch preview build logs for %s", branch)
            return
        if not logs:
            return
        self._add_note(
            incident,
            f"Preview deployment did not become available (attempt {number})",
            content=logs[:8000],
            kind=EvidenceKind.BUILD_LOG,
            trust=TrustLevel.EXTERNAL,
        )

    # -- notifications ----------------------------------------------------

    def _notify_progress(self, incident: Incident, number: int) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.progress(
                incident, attempt=number, max_attempts=self.policy.max_attempts
            )
        except Exception:  # pragma: no cover - a notifier must never break a repair
            logger.exception("could not send a progress notification")

    def _notify(self, method: str, incident: Incident, **kwargs: Any) -> None:
        if self.notifier is None:
            return
        try:
            getattr(self.notifier, method)(incident, **kwargs)
        except Exception:  # pragma: no cover
            logger.exception("could not send a %s notification", method)

    # -- helpers ----------------------------------------------------------

    def _await_preview(self, branch: str) -> str:
        """Return a preview URL for *branch*, waiting briefly if configured."""
        if self.preview_lookup is None:
            return ""
        deadline = time.monotonic() + self.preview_wait_seconds
        while True:
            url = self.preview_lookup(branch) or ""
            if url or time.monotonic() >= deadline:
                return url
            self.sleep(min(5.0, max(0.1, self.preview_wait_seconds / 10)))

    def _transition(
        self, incident: Incident, state: IncidentState, reason: str
    ) -> None:
        """Advance the incident to *state*, walking the legal path to get there.

        A freshly-DETECTED incident cannot jump straight to FIXING, so the
        intermediate states are walked explicitly.  Doing otherwise (and
        swallowing the resulting error) would leave the incident sitting in
        DETECTED while repairs ran, making the transition log a lie.
        """
        if incident.state is state:
            return
        try:
            route = path_between(incident.state, state)
        except InvalidTransitionError:
            logger.error(
                "no legal path from %s to %s for %s",
                incident.state.value,
                state.value,
                incident.id,
            )
            return
        for index, step in enumerate(route):
            step_reason = reason if step is state else f"{reason} (via {step.value})"
            try:
                self.store.transition(incident, step, reason=step_reason)
            except Exception:
                logger.exception(
                    "could not move %s to %s (step %d of %d)",
                    incident.id,
                    step.value,
                    index + 1,
                    len(route),
                )
                return

    def _add_note(
        self,
        incident: Incident,
        summary: str,
        *,
        content: str = "",
        kind: EvidenceKind = EvidenceKind.NOTE,
        trust: TrustLevel = TrustLevel.TRUSTED,
    ) -> None:
        try:
            self.store.add_evidence(
                incident,
                Evidence(
                    kind=kind,
                    summary=summary,
                    content=content,
                    source="repair_loop",
                    trust=trust,
                ),
            )
        except Exception:
            logger.exception("could not attach evidence to %s", incident.id)

    def _publish(self, event: str, incident: Incident, **extra: Any) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(event, {"incident_id": incident.id, **extra})
        except Exception:
            logger.exception("could not publish %s", event)

    @staticmethod
    def _pull_request_body(
        incident: Incident,
        attempt: RepairAttempt,
        verification: Optional[VerificationResult],
    ) -> str:
        """Render the PR body a human will review.

        Written to be decidable in about a minute: what broke, what was changed,
        what proved it works, and what the reviewer still has to judge.
        """
        files = "\n".join(f"- `{f}`" for f in attempt.changed_files) or "_none_"
        verified = "yes" if verification and verification.passed else "no"

        checks = attempt.checks.get("results") or []
        checks_block = (
            "\n".join(
                f"- {r.get('name')}: "
                + (
                    "not run"
                    if not r.get("ran")
                    else ("passed" if r.get("passed") else "**failed**")
                )
                for r in checks
            )
            or "_none configured_"
        )

        if attempt.regression_tests:
            listed = ", ".join(f"`{p}`" for p in attempt.regression_tests)
            regression_block = f"Test files added or modified: {listed}"
        else:
            regression_block = (
                "**No test file was added or modified.** The fix may still be "
                "correct, but nothing in this diff would catch the failure "
                "coming back. Worth asking for before merging."
            )

        review = attempt.scope.get("review_required") or []
        review_block = ""
        if review:
            listed = ", ".join(f"`{p}`" for p in review)
            review_block = (
                f"\n> ⚠ This diff touches security-adjacent code: {listed}. "
                "JARVIS is permitted to change it, but it is never deployed "
                "automatically and deserves a careful read.\n"
            )

        provenance = ""
        if attempt.base_commit:
            provenance = (
                f"\n**Base commit:** `{attempt.base_commit[:12]}`"
                f"\n**Repair commit:** `{(attempt.commit_sha or '')[:12] or 'n/a'}`"
            )

        return f"""\
## JARVIS incident {incident.id}

**Severity:** {incident.severity.value}
**Component:** {incident.component}
**Detected:** {incident.created_at}
**Repair attempts:** {attempt.number}{provenance}

### Problem

{incident.summary or incident.title}

### Reproduction

{chr(10).join(f"{i}. {s}" for i, s in enumerate(incident.repro_steps, 1)) or "_n/a_"}

### What the coding agent reported

{attempt.claim[:2000] or "_no summary_"}

### Files changed

{files}
{review_block}
### Independent verification

- Verified: **{verified}**
- Probe: `{verification.probe_id if verification else "n/a"}`
- Preview: {attempt.preview_url or "_none_"}
- Expected: {verification.expected if verification else "n/a"}
- Observed: {verification.actual if verification else "n/a"}

Verification re-ran the probe that detected this failure against a preview
deployment. It does not rely on the coding agent's own account.

### Local checks

{checks_block}

### Regression coverage

{regression_block}

### What a reviewer still has to decide

JARVIS proved the original failure no longer reproduces. It did **not** judge
whether this is the right fix, whether it is maintainable, or whether it has
consequences the probe does not cover. That judgement is why this is a pull
request and not a deployment.

---
_Opened by JARVIS. Not merged, and not deployed — a human decides both._
"""
