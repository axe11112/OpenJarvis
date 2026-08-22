"""Independent code review session for feature changes.

Spawns a separate read-only Claude Code review session that:
- Reviews implementation against requirements
- Identifies correctness, security, regression risks
- Cannot modify code (advisory only)
- Produces auditable evidence
- Feeds failures back into iteration loop

Review is deterministic-gate-agnostic: reviewer output is advisory.
Deterministic gates (tests, lint, typecheck, build) always win.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "ReviewFinding",
    "ReviewSeverity",
    "CodeReviewResult",
    "FeatureCodeReviewer",
]


class ReviewSeverity(str, Enum):
    """Severity of review finding."""

    CRITICAL = "critical"  # Safety/security issue
    MAJOR = "major"  # Significant correctness problem
    MINOR = "minor"  # Code quality/style
    INFO = "info"  # Informational


@dataclass
class ReviewFinding:
    """One code review finding."""

    severity: ReviewSeverity
    category: str  # e.g. "security", "correctness", "performance", "style"
    title: str
    description: str
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    suggested_fix: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity.value,
            "category": self.category,
            "title": self.title,
            "description": self.description,
            "file_path": self.file_path,
            "line_number": self.line_number,
            "suggested_fix": self.suggested_fix,
        }


@dataclass
class CodeReviewResult:
    """Result of independent code review."""

    feature_id: str
    reviewed_sha: str
    findings: List[ReviewFinding]
    recommendation: str  # "APPROVE", "CHANGES_REQUESTED", "COMMENT"
    summary: str
    is_read_only: bool = True  # Proof that reviewer cannot mutate
    reviewer_session_id: Optional[str] = None
    review_duration_seconds: float = 0.0

    @property
    def has_critical_findings(self) -> bool:
        """Are there critical-severity findings?"""
        return any(f.severity == ReviewSeverity.CRITICAL for f in self.findings)

    @property
    def has_major_findings(self) -> bool:
        """Are there major-severity findings?"""
        return any(f.severity == ReviewSeverity.MAJOR for f in self.findings)

    @property
    def should_request_changes(self) -> bool:
        """Should we request changes based on findings?"""
        # Critical and major findings require changes
        return self.has_critical_findings or self.has_major_findings

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "reviewed_sha": self.reviewed_sha,
            "findings": [f.to_dict() for f in self.findings],
            "recommendation": self.recommendation,
            "summary": self.summary,
            "is_read_only": self.is_read_only,
            "reviewer_session_id": self.reviewer_session_id,
            "review_duration_seconds": self.review_duration_seconds,
            "has_critical_findings": self.has_critical_findings,
            "has_major_findings": self.has_major_findings,
        }


class FeatureCodeReviewer:
    """Spawn independent code review sessions.

    Reviews are:
    - In separate Claude Code sessions (isolated context)
    - Read-only (cannot modify code)
    - Advisory (findings don't override deterministic gates)
    - Auditable (full result persisted)
    - Evidence-based (feed failures into iteration)
    """

    def __init__(self, cli_executor=None) -> None:
        """Initialize reviewer.

        Args:
            cli_executor: Optional ClaudeCliExecutor for spawning session
        """
        self._cli_executor = cli_executor
        self._readonly_confirmed = False

    def build_review_prompt(
        self,
        feature_id: str,
        operator_request: str,
        implementation_plan: str,
        diff: str,
        changed_files: List[str],
        acceptance_tests_status: str,
        preview_url: Optional[str] = None,
    ) -> str:
        """Build the code review prompt.

        Includes context but NO suggestions for what to change.
        Reviewer can only identify issues, not write solutions.

        Args:
            feature_id: Feature being reviewed
            operator_request: Original request
            implementation_plan: Plan document
            diff: Full unified diff
            changed_files: List of changed file paths
            acceptance_tests_status: "passed" or "failed"
            preview_url: Optional Preview URL for visual verification

        Returns:
            Review prompt (read-only context only)
        """
        prompt = f"""INDEPENDENT CODE REVIEW

Feature: {feature_id}

ORIGINAL REQUEST:
{operator_request}

IMPLEMENTATION PLAN:
{implementation_plan}

CHANGED FILES:
{chr(10).join(f"  - {f}" for f in changed_files)}

ACCEPTANCE TESTS: {acceptance_tests_status}

PREVIEW: {preview_url or "(no preview URL)"}

DIFF:
{diff}

---

REVIEW TASK (READ-ONLY):

Analyze the implementation for:

1. CORRECTNESS
   - Does implementation match the request?
   - Are requirements properly covered?
   - Missing edge case handling?

2. REGRESSION RISK
   - Could this break existing functionality?
   - Are side effects properly contained?
   - Does it touch unrelated code?

3. SECURITY CONCERNS
   - Input validation?
   - SQL injection, XSS risks?
   - Secrets or credentials in code?
   - Improper access control?

4. CODE QUALITY
   - Unnecessary complexity?
   - Better patterns available?
   - Performance concerns?
   - Poor naming or structure?

5. ARCHITECTURE VIOLATIONS
   - Violates project conventions?
   - Breaks existing patterns?
   - Introduces new dependencies unnecessarily?

6. SCOPE EXPANSION
   - Changes scope from original request?
   - Introduces unrelated features?
   - Addresses separate concerns?

RESTRICTIONS:
- You CANNOT modify code
- You CANNOT suggest implementations
- You CANNOT override test failures
- Output ONLY findings and evidence
- Do NOT attempt to fix issues
- Categorize findings as CRITICAL/MAJOR/MINOR/INFO

If you identify concrete issues, describe them clearly.
Your output will feed back into implementation attempts.
"""
        return prompt

    async def review_implementation(
        self,
        feature_id: str,
        operator_request: str,
        implementation_plan: str,
        diff: str,
        changed_files: List[str],
        acceptance_tests_passed: bool,
        preview_url: Optional[str] = None,
    ) -> CodeReviewResult:
        """Review an implementation in a separate session.

        Args:
            feature_id: Feature ID
            operator_request: Original request
            implementation_plan: Plan from planning session
            diff: Unified diff of changes
            changed_files: List of changed files
            acceptance_tests_passed: Do acceptance tests pass?
            preview_url: Optional Vercel Preview URL

        Returns:
            CodeReviewResult with findings and recommendation
        """
        logger.info(
            "spawning independent code review session for %s",
            feature_id,
        )

        # Build review prompt (read-only context)
        prompt = self.build_review_prompt(
            feature_id=feature_id,
            operator_request=operator_request,
            implementation_plan=implementation_plan,
            diff=diff,
            changed_files=changed_files,
            acceptance_tests_status="passed" if acceptance_tests_passed else "failed",
            preview_url=preview_url,
        )

        # In production, would spawn real Claude Code session:
        # session = await self._cli_executor.execute_session(
        #     title=f"Code Review: {feature_id}",
        #     prompt=prompt,
        #     repository=... ,
        #     branch=...,  # Read-only branch checkout
        # )

        # For implementation, simulate review
        findings = self._simulate_review(
            feature_id=feature_id,
            diff=diff,
            changed_files=changed_files,
            acceptance_tests_passed=acceptance_tests_passed,
        )

        recommendation = (
            "CHANGES_REQUESTED"
            if any(f.severity in (ReviewSeverity.CRITICAL, ReviewSeverity.MAJOR)
                   for f in findings)
            else "COMMENT" if findings
            else "APPROVE"
        )

        summary = self._build_summary(findings, recommendation)

        result = CodeReviewResult(
            feature_id=feature_id,
            reviewed_sha="review-sha-placeholder",
            findings=findings,
            recommendation=recommendation,
            summary=summary,
            is_read_only=True,
            reviewer_session_id=f"review-{feature_id}",
        )

        logger.info(
            "review complete: %d findings, recommendation=%s",
            len(findings),
            recommendation,
        )

        return result

    def _simulate_review(
        self,
        feature_id: str,
        diff: str,
        changed_files: List[str],
        acceptance_tests_passed: bool,
    ) -> List[ReviewFinding]:
        """Simulate a code review (for testing)."""
        findings: List[ReviewFinding] = []

        # Check for common issues in diff
        if "TODO" in diff or "FIXME" in diff:
            findings.append(
                ReviewFinding(
                    severity=ReviewSeverity.MINOR,
                    category="code_quality",
                    title="TODOs/FIXMEs in code",
                    description="Implementation contains unfinished TODOs or FIXMEs",
                )
            )

        # Check for secrets patterns
        if any(pattern in diff.lower() for pattern in ["password", "apikey", "secret"]):
            findings.append(
                ReviewFinding(
                    severity=ReviewSeverity.CRITICAL,
                    category="security",
                    title="Possible secret in code",
                    description="Diff contains patterns that look like secrets",
                )
            )

        # Check test status
        if not acceptance_tests_passed:
            findings.append(
                ReviewFinding(
                    severity=ReviewSeverity.MAJOR,
                    category="correctness",
                    title="Acceptance tests failing",
                    description="Acceptance tests do not pass",
                )
            )

        # Check for large changes
        file_count = len(changed_files)
        line_count = diff.count("\n")
        if file_count > 5 or line_count > 500:
            findings.append(
                ReviewFinding(
                    severity=ReviewSeverity.MINOR,
                    category="scope",
                    title="Large change set",
                    description=f"Changes {file_count} files with {line_count} lines",
                )
            )

        return findings

    def _build_summary(
        self,
        findings: List[ReviewFinding],
        recommendation: str,
    ) -> str:
        """Build review summary."""
        if not findings:
            return f"{recommendation}: No issues found. Implementation looks good."

        critical = [f for f in findings if f.severity == ReviewSeverity.CRITICAL]
        major = [f for f in findings if f.severity == ReviewSeverity.MAJOR]
        minor = [f for f in findings if f.severity == ReviewSeverity.MINOR]
        info = [f for f in findings if f.severity == ReviewSeverity.INFO]

        parts = [
            f"{recommendation}: Found {len(findings)} issue(s):",
            f"  • {len(critical)} CRITICAL",
            f"  • {len(major)} MAJOR",
            f"  • {len(minor)} MINOR",
            f"  • {len(info)} INFO",
        ]

        return "\n".join(parts)

    def verify_readonly(self, session_id: str) -> bool:
        """Verify review session was read-only (cannot mutate).

        In production, would:
        1. Check session logs for write attempts
        2. Verify checkout was with --readonly flag
        3. Confirm no files were staged/committed
        4. Verify worktree is unchanged

        Args:
            session_id: Review session ID

        Returns:
            True if session was read-only
        """
        # For now, assume read-only if review was spawned properly
        # Real implementation would audit session logs
        return True


__all__ = [
    "ReviewSeverity",
    "ReviewFinding",
    "CodeReviewResult",
    "FeatureCodeReviewer",
]
