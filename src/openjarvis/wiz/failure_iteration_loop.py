"""Failure → iteration loop: Collect evidence and retry with new approach.

When feature execution fails:
1. Collect failure evidence (error messages, logs, diff, gate failures)
2. Analyze root cause from evidence
3. Spawn new Claude Code session with failure context
4. Claude tries again with different approach
5. Repeat until success or max retries exhausted

Enables autonomous recovery from failures without human intervention.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "FailureType",
    "FailureEvidence",
    "IterationAttempt",
    "FailureIterationLoop",
]


class FailureType(str, Enum):
    """Type of feature execution failure."""

    COMPILATION = "compilation"  # Code doesn't compile/import
    UNIT_TESTS = "unit_tests"  # Local tests fail
    ACCEPTANCE_TESTS = "acceptance_tests"  # Preview/production tests fail
    SECURITY = "security"  # Secrets detected, security issues
    PERFORMANCE = "performance"  # Latency/resource issues
    GATE_FAILURE = "gate_failure"  # Merge gate failure (tests, CI, etc)
    DEPLOYMENT = "deployment"  # Vercel deployment failed
    PRODUCTION = "production"  # Production health check failed
    UNKNOWN = "unknown"


@dataclass
class FailureEvidence:
    """Evidence collected from a failure."""

    feature_id: str
    failure_type: FailureType
    error_message: str
    error_traceback: Optional[str] = None
    diff: Optional[str] = None
    changed_files: Optional[List[str]] = None
    failed_tests: Optional[List[str]] = None
    gate_failures: Optional[Dict[str, str]] = None  # {gate: reason}
    deployment_error: Optional[str] = None
    production_metrics: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "failure_type": self.failure_type.value,
            "error_message": self.error_message,
            "error_traceback": self.error_traceback,
            "diff": self.diff,
            "changed_files": self.changed_files,
            "failed_tests": self.failed_tests,
            "gate_failures": self.gate_failures,
            "deployment_error": self.deployment_error,
            "production_metrics": self.production_metrics,
        }


@dataclass
class IterationAttempt:
    """One iteration attempt for feature."""

    attempt_number: int
    feature_id: str
    session_id: Optional[str] = None
    approach_change: str = ""  # What changed from previous attempt
    failure_evidence: Optional[FailureEvidence] = None
    is_success: bool = False
    is_terminal_failure: bool = False  # Cannot recover (needs human)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempt_number": self.attempt_number,
            "feature_id": self.feature_id,
            "session_id": self.session_id,
            "approach_change": self.approach_change,
            "failure_evidence": (
                self.failure_evidence.to_dict()
                if self.failure_evidence
                else None
            ),
            "is_success": self.is_success,
            "is_terminal_failure": self.is_terminal_failure,
        }


class FailureIterationLoop:
    """Manage failure → retry cycle for feature execution."""

    def __init__(self, max_iterations: int = 3) -> None:
        """Initialize iteration loop.

        Args:
            max_iterations: Max retry attempts (including initial attempt)
        """
        self.max_iterations = max_iterations
        self.iterations: List[IterationAttempt] = []

    def collect_failure_evidence(
        self,
        feature_id: str,
        failure_type: FailureType,
        error_message: str,
        error_traceback: Optional[str] = None,
        diff: Optional[str] = None,
        changed_files: Optional[List[str]] = None,
        failed_tests: Optional[List[str]] = None,
        gate_failures: Optional[Dict[str, str]] = None,
        deployment_error: Optional[str] = None,
        production_metrics: Optional[Dict[str, Any]] = None,
    ) -> FailureEvidence:
        """Collect comprehensive failure evidence.

        Args:
            feature_id: Feature that failed
            failure_type: Type of failure
            error_message: Primary error message
            error_traceback: Full stack trace (if available)
            diff: Failed diff/changes
            changed_files: Files that were changed
            failed_tests: List of failed test names
            gate_failures: Dict of {gate_name: failure_reason}
            deployment_error: Vercel deployment error
            production_metrics: Production health metrics at failure time

        Returns:
            FailureEvidence with complete context
        """
        evidence = FailureEvidence(
            feature_id=feature_id,
            failure_type=failure_type,
            error_message=error_message,
            error_traceback=error_traceback,
            diff=diff,
            changed_files=changed_files,
            failed_tests=failed_tests,
            gate_failures=gate_failures,
            deployment_error=deployment_error,
            production_metrics=production_metrics,
        )

        logger.info(
            "collected failure evidence for %s: %s", feature_id, failure_type.value
        )

        return evidence

    def analyze_failure(
        self,
        evidence: FailureEvidence,
    ) -> str:
        """Analyze failure and suggest approach changes.

        Args:
            evidence: FailureEvidence to analyze

        Returns:
            Suggested approach change for next attempt
        """
        suggestions = []

        if evidence.failure_type == FailureType.COMPILATION:
            suggestions.append("Check syntax: review error message and fix type issues")
            suggestions.append("Try: add explicit type hints, review imports")

        elif evidence.failure_type == FailureType.UNIT_TESTS:
            suggestions.append("Rerun failing tests with more logging")
            suggestions.append("Try: fix test logic, add missing test setup")
            if evidence.failed_tests:
                suggestions.append(f"Failed: {', '.join(evidence.failed_tests[:3])}")

        elif evidence.failure_type == FailureType.ACCEPTANCE_TESTS:
            suggestions.append("Acceptance test failed on Preview/Production")
            suggestions.append("Try: revisit feature implementation, add defensive checks")

        elif evidence.failure_type == FailureType.SECURITY:
            suggestions.append("Security issue detected (secrets, injection risk)")
            suggestions.append(
                "Try: remove sensitive data, add input validation/escaping"
            )

        elif evidence.failure_type == FailureType.GATE_FAILURE:
            if evidence.gate_failures:
                for gate, reason in evidence.gate_failures.items():
                    suggestions.append(f"{gate}: {reason}")
            suggestions.append("Try: fix failing gates one by one")

        elif evidence.failure_type == FailureType.DEPLOYMENT:
            suggestions.append("Vercel deployment failed")
            if evidence.deployment_error:
                suggestions.append(f"Error: {evidence.deployment_error[:100]}")
            suggestions.append("Try: check build logs, fix build errors")

        elif evidence.failure_type == FailureType.PRODUCTION:
            suggestions.append("Feature caused issues in production")
            if evidence.production_metrics:
                if evidence.production_metrics.get("error_rate", 0) > 0.05:
                    suggestions.append("High error rate detected")
                if evidence.production_metrics.get("alerts", 0) > 0:
                    suggestions.append(f"Alerts triggered: {evidence.production_metrics['alerts']}")
            suggestions.append("Try: add error handling, defensive checks, monitoring")

        approach = " | ".join(suggestions)
        logger.info("analysis for %s: %s", evidence.feature_id, approach)

        return approach

    def build_retry_prompt(
        self,
        feature_id: str,
        original_request: str,
        attempt_number: int,
        evidence: FailureEvidence,
        analysis: str,
    ) -> str:
        """Build prompt for retry attempt.

        Includes failure context so Claude understands what went wrong
        and can try different approach.

        Args:
            feature_id: Feature being retried
            original_request: Original feature request
            attempt_number: Which attempt this is
            evidence: Failure evidence from previous attempt
            analysis: Analysis of root cause

        Returns:
            Prompt for new Claude session
        """
        prompt = f"""RETRY ATTEMPT {attempt_number}: {feature_id}

ORIGINAL REQUEST:
{original_request}

---

PREVIOUS ATTEMPT FAILED:
Type: {evidence.failure_type.value}
Error: {evidence.error_message}

ANALYSIS:
{analysis}

EVIDENCE:
- Changed files: {', '.join(evidence.changed_files) if evidence.changed_files else 'none'}
- Failed tests: {', '.join(evidence.failed_tests[:3]) if evidence.failed_tests else 'none'}
- Gate failures: {', '.join(f"{k}" for k in (evidence.gate_failures or {}).keys()) if evidence.gate_failures else 'none'}

---

RETRY INSTRUCTIONS:

1. Review the failure context above
2. Identify the root cause
3. Try a DIFFERENT approach based on the analysis
4. Do NOT repeat the same implementation
5. Add defensive checks, error handling, etc.
6. Run tests locally before submitting
7. If you need to understand the error better, add logging and re-run

Focus on: {analysis}

Can you implement the feature with this approach? Be specific about what you changed.
"""

        return prompt

    def add_iteration(
        self,
        attempt_number: int,
        feature_id: str,
        session_id: Optional[str] = None,
        approach_change: str = "",
        failure_evidence: Optional[FailureEvidence] = None,
        is_success: bool = False,
        is_terminal_failure: bool = False,
    ) -> IterationAttempt:
        """Add iteration attempt to loop.

        Args:
            attempt_number: Which attempt (1, 2, 3, etc)
            feature_id: Feature ID
            session_id: Claude session ID (if executed)
            approach_change: What changed from previous attempt
            failure_evidence: Evidence if this attempt failed
            is_success: Did this attempt succeed?
            is_terminal_failure: Is this a human-intervention failure?

        Returns:
            IterationAttempt record
        """
        attempt = IterationAttempt(
            attempt_number=attempt_number,
            feature_id=feature_id,
            session_id=session_id,
            approach_change=approach_change,
            failure_evidence=failure_evidence,
            is_success=is_success,
            is_terminal_failure=is_terminal_failure,
        )

        self.iterations.append(attempt)

        status = "✓ SUCCESS" if is_success else "✗ FAILED"
        logger.info(
            "%s attempt %d for %s (session: %s)",
            status,
            attempt_number,
            feature_id,
            session_id or "not_executed",
        )

        return attempt

    def should_retry(self) -> bool:
        """Should we retry the feature?

        Returns:
            True if more attempts available and not terminal failure
        """
        if not self.iterations:
            return False

        last_attempt = self.iterations[-1]

        # Don't retry if last attempt succeeded
        if last_attempt.is_success:
            return False

        # Don't retry if terminal failure (needs human)
        if last_attempt.is_terminal_failure:
            return False

        # Don't retry if max iterations reached
        if last_attempt.attempt_number >= self.max_iterations:
            return False

        return True

    def build_summary(self) -> str:
        """Build summary of all iteration attempts."""
        parts = []

        parts.append(f"Iteration Summary ({len(self.iterations)} attempts):")

        for attempt in self.iterations:
            status = "✓" if attempt.is_success else "✗"
            parts.append(f"\nAttempt {attempt.attempt_number}: {status}")
            if attempt.session_id:
                parts.append(f"  Session: {attempt.session_id}")
            if attempt.failure_evidence:
                parts.append(f"  Failure: {attempt.failure_evidence.failure_type.value}")
                parts.append(f"  Error: {attempt.failure_evidence.error_message[:60]}...")
            if attempt.approach_change:
                parts.append(f"  Change: {attempt.approach_change[:80]}...")

        if self.should_retry():
            parts.append(
                f"\nCan retry: Yes ({self.iterations[-1].attempt_number + 1}/{self.max_iterations})"
            )
        else:
            if self.iterations[-1].is_success:
                parts.append("\n✓ Feature implementation succeeded!")
            elif self.iterations[-1].is_terminal_failure:
                parts.append("\n⚠ Terminal failure: requires human intervention")
            else:
                parts.append("\n✗ Max iterations reached, no success")

        return "\n".join(parts)


__all__ = [
    "FailureType",
    "FailureEvidence",
    "IterationAttempt",
    "FailureIterationLoop",
]
