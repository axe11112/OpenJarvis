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

    def __post_init__(self) -> None:
        if self.checks is None:
            # Preserve the single-command behaviour when no suite is configured.
            self.checks = CheckSuite.from_config(test_command=self.test_command)
        if self.scope_limits is None:
            self.scope_limits = ScopeLimits()

    # -- entry point ------------------------------------------------------

    def run(self, incident: Incident, spec: ProbeSpec) -> RepairOutcome:
        """Attempt to repair *incident*, verifying every attempt independently."""
        gate = self.policy.may_attempt_repair(incident)
        if not gate:
            self._escalate(incident, gate.reason)
            return RepairOutcome(
                resolved=False,
                attempts=incident.attempts_used,
                final_state=incident.state,
                reason=gate.reason,
            )

        previous_failure = ""
        last_verification: Optional[VerificationResult] = None

        while incident.attempts_used < self.policy.max_attempts:
            attempt_number = incident.attempts_used + 1
            attempt, verification, stop_reason = self._attempt(
                incident, spec, attempt_number, previous_failure
            )
            last_verification = verification

            if attempt.outcome == OUTCOME_VERIFIED:
                return self._succeed(incident, attempt, verification)

            if stop_reason:
                self._escalate(incident, stop_reason)
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
        self._escalate(incident, reason)
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
    ) -> tuple[RepairAttempt, Optional[VerificationResult], str]:
        """Run one attempt. Returns (attempt, verification, stop_reason)."""
        branch = self._branch_name(incident)
        attempt = RepairAttempt(number=number, branch=branch, started_at=now_iso())
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

    def _escalate(self, incident: Incident, reason: str) -> None:
        """Stop touching code and hand the incident to a human."""
        logger.warning("Incident %s requires a human: %s", incident.id, reason)
        if incident.state is not IncidentState.HUMAN_REQUIRED:
            try:
                self.store.transition(
                    incident, IncidentState.HUMAN_REQUIRED, reason=reason
                )
            except Exception:
                logger.exception("could not transition %s", incident.id)
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
