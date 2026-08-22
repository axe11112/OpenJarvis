"""Independent code review for Wiz features."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class FindingCategory(Enum):
    """Category of code review finding."""

    BUG = "bug"
    SECURITY = "security"
    PERFORMANCE = "performance"
    STYLE = "style"
    TESTING = "testing"
    ARCHITECTURE = "architecture"


class FindingSeverity(Enum):
    """Severity of a finding."""

    INFO = "info"
    MINOR = "minor"
    MAJOR = "major"
    BLOCKING = "blocking"


@dataclass
class CodeReviewFinding:
    """A single code review finding."""

    category: FindingCategory
    severity: FindingSeverity
    message: str
    file: Optional[str] = None
    line: Optional[int] = None
    suggested_fix: Optional[str] = None

    @property
    def is_blocking(self) -> bool:
        """Finding blocks autonomous merge."""
        return self.severity == FindingSeverity.BLOCKING


@dataclass
class CodeReview:
    """Result of independent code review."""

    feature_id: str
    findings: list[CodeReviewFinding] = field(default_factory=list)
    overall_assessment: str = ""
    reviewer_notes: str = ""

    @property
    def has_blocking_issues(self) -> bool:
        """Review has findings that block merge."""
        return any(f.is_blocking for f in self.findings)

    @property
    def finding_summary(self) -> dict:
        """Summary of findings by severity."""
        summary = {
            "info": 0,
            "minor": 0,
            "major": 0,
            "blocking": 0,
        }
        for finding in self.findings:
            summary[finding.severity.value] += 1
        return summary


class IndependentReviewer:
    """Perform independent code review of Wiz features."""

    async def review_feature(
        self, feature_id: str, diff: str, description: str
    ) -> CodeReview:
        """Review a feature's implementation.

        Args:
            feature_id: Feature ID
            diff: Git diff of changes
            description: Feature description

        Returns:
            CodeReview with findings
        """
        review = CodeReview(feature_id=feature_id)

        logger.info(f"Reviewing {feature_id}")
        # TODO: Implement AI-driven code review
        # For now, basic checks

        # Check for hardcoded values
        if "TODO" in diff or "FIXME" in diff:
            review.findings.append(
                CodeReviewFinding(
                    category=FindingCategory.STYLE,
                    severity=FindingSeverity.MINOR,
                    message="Contains TODO or FIXME comments",
                )
            )

        # Check for obvious security issues (incomplete, for demo)
        if "password" in diff.lower() or "secret" in diff.lower():
            review.findings.append(
                CodeReviewFinding(
                    category=FindingCategory.SECURITY,
                    severity=FindingSeverity.BLOCKING,
                    message="Potential hardcoded secrets detected",
                )
            )

        return review


__all__ = [
    "IndependentReviewer",
    "CodeReview",
    "CodeReviewFinding",
    "FindingCategory",
    "FindingSeverity",
]
