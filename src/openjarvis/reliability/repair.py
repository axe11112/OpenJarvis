"""The repair loop.

    detect → brief → branch → agent → tests → preview → VERIFY → PR or escalate

Each attempt ends in one of a small number of outcomes, and every one of them is
recorded.  The loop stops for exactly three reasons: verification passed, the
attempt budget is exhausted, or the policy refused to continue.  It never stops
because the agent said it was done.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from openjarvis.reliability.briefing import BriefingRefusedError, build_briefing
from openjarvis.reliability.code_agent import CodeAgent, CodeAgentError
from openjarvis.reliability.events import (
    RELIABILITY_REPAIR_ATTEMPT_END,
    RELIABILITY_REPAIR_ATTEMPT_START,
    RELIABILITY_VERIFICATION,
)
from openjarvis.reliability.policy import SafetyPolicy
from openjarvis.reliability.probes.spec import ProbeSpec
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
    """

    agent: CodeAgent
    policy: SafetyPolicy
    verifier: Verifier
    store: Any
    workspace: str = ""
    test_command: str = ""
    github: Any = None
    preview_lookup: Optional[Callable[[str], str]] = None
    bus: Any = None
    sleep: Callable[[float], None] = time.sleep
    preview_wait_seconds: float = 0.0
    protected_paths: List[str] = field(default_factory=list)

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
        branch = (
            self.github.branch_name_for(incident.id)
            if self.github is not None
            else f"jarvis/incident-{incident.id}"
        )
        attempt = RepairAttempt(number=number, branch=branch, started_at=now_iso())
        self._publish(RELIABILITY_REPAIR_ATTEMPT_START, incident, attempt=number)

        # --- brief -------------------------------------------------------
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
            attempt.finished_at = now_iso()
            self.store.add_attempt(incident, attempt)
            return attempt, None, str(exc)

        attempt.briefing_hash = briefing.hash

        self._transition(incident, IncidentState.FIXING, f"repair attempt {number}")

        # --- branch ------------------------------------------------------
        if self.github is not None and number == 1:
            try:
                self.github.create_branch(branch)
            except Exception as exc:
                logger.warning("could not create branch %s: %s", branch, exc)

        # --- agent -------------------------------------------------------
        try:
            result = self.agent.run(
                briefing.text, workspace=self.workspace or ".", timeout=1800
            )
        except CodeAgentError as exc:
            attempt.outcome = OUTCOME_AGENT_ERROR
            attempt.finished_at = now_iso()
            self.store.add_attempt(incident, attempt)
            return attempt, None, f"the coding agent could not run: {exc}"

        attempt.claim = result.claim[:4000]
        attempt.changed_files = list(result.changed_files)
        attempt.diff_stat = result.diff_stat

        if not result.succeeded:
            attempt.outcome = OUTCOME_AGENT_ERROR
            attempt.test_summary = result.error[:2000]
            attempt.finished_at = now_iso()
            self.store.add_attempt(incident, attempt)
            return attempt, None, ""

        if not attempt.produced_changes:
            # A confident claim with no diff is a failed attempt, not a success.
            attempt.outcome = OUTCOME_NO_DIFF
            attempt.finished_at = now_iso()
            self.store.add_attempt(incident, attempt)
            self._add_note(
                incident,
                "The coding agent produced no changes despite reporting a fix.",
            )
            return attempt, None, ""

        # --- protected paths ---------------------------------------------
        paths_ok = self.policy.may_modify_paths(attempt.changed_files)
        if not paths_ok:
            attempt.outcome = OUTCOME_PROTECTED_PATH
            attempt.finished_at = now_iso()
            self.store.add_attempt(incident, attempt)
            return attempt, None, paths_ok.reason

        # --- tests -------------------------------------------------------
        self._transition(incident, IncidentState.TESTING, f"attempt {number}: tests")
        tests = run_tests(self.test_command, workspace=self.workspace or ".")
        attempt.tests_passed = tests.passed if tests.ran else None
        attempt.test_summary = tests.summary
        if tests.output:
            self._add_note(
                incident,
                f"Test output (attempt {number})",
                content=tests.output,
                kind=EvidenceKind.TEST_OUTPUT,
            )
        if tests.ran and not tests.passed:
            attempt.outcome = OUTCOME_TESTS_FAILED
            attempt.finished_at = now_iso()
            self.store.add_attempt(incident, attempt)
            return attempt, None, ""

        # --- preview + verification --------------------------------------
        self._transition(
            incident, IncidentState.VERIFYING, f"attempt {number}: verification"
        )
        target_url = self._await_preview(branch)
        verification = self.verifier.verify(
            spec, target_url=target_url, incident_id=incident.id
        )
        attempt.verification = verification
        attempt.outcome = (
            OUTCOME_VERIFIED
            if verification.passed
            else (OUTCOME_NO_PREVIEW if not target_url else OUTCOME_VERIFICATION_FAILED)
        )
        attempt.finished_at = now_iso()
        self.store.add_attempt(incident, attempt)
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

    # -- terminal paths ---------------------------------------------------

    def _succeed(
        self,
        incident: Incident,
        attempt: RepairAttempt,
        verification: Optional[VerificationResult],
    ) -> RepairOutcome:
        """Handle a verified repair: PR by default, deploy only if permitted."""
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
    ) -> None:
        try:
            self.store.add_evidence(
                incident,
                Evidence(
                    kind=kind,
                    summary=summary,
                    content=content,
                    source="repair_loop",
                    trust=TrustLevel.TRUSTED,
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
        """Render the PR body a human will review."""
        files = "\n".join(f"- `{f}`" for f in attempt.changed_files) or "_none_"
        verified = "yes" if verification and verification.passed else "no"
        return f"""\
## JARVIS incident {incident.id}

**Severity:** {incident.severity.value}
**Component:** {incident.component}
**Detected:** {incident.created_at}
**Repair attempts:** {attempt.number}

### Problem

{incident.summary or incident.title}

### Reproduction

{chr(10).join(f"{i}. {s}" for i, s in enumerate(incident.repro_steps, 1)) or "_n/a_"}

### What the coding agent reported

{attempt.claim[:2000] or "_no summary_"}

### Files changed

{files}

### Independent verification

- Verified: **{verified}**
- Probe: `{verification.probe_id if verification else "n/a"}`
- Expected: {verification.expected if verification else "n/a"}
- Observed: {verification.actual if verification else "n/a"}

Verification re-ran the probe that detected this failure against a preview
deployment. It does not rely on the coding agent's own account.

### Tests

{attempt.test_summary or "_not run_"}
"""
