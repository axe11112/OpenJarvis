"""Tests for independent code review."""

from __future__ import annotations

from openjarvis.wiz.review import CodeReview, CodeReviewFinding, FindingCategory, FindingSeverity


def test_code_review_creation():
    """Test creating code review."""
    review = CodeReview(feature_id="WIZE-123")

    assert review.feature_id == "WIZE-123"
    assert len(review.findings) == 0
    assert not review.has_blocking_issues


def test_blocking_findings():
    """Test blocking findings block merge."""
    review = CodeReview(feature_id="WIZE-123")
    review.findings.append(
        CodeReviewFinding(
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.BLOCKING,
            message="Hardcoded secret",
        )
    )

    assert review.has_blocking_issues
    assert review.findings[0].is_blocking


def test_non_blocking_findings():
    """Test minor findings don't block."""
    review = CodeReview(feature_id="WIZE-123")
    review.findings.append(
        CodeReviewFinding(
            category=FindingCategory.STYLE,
            severity=FindingSeverity.MINOR,
            message="Use camelCase",
        )
    )

    assert not review.has_blocking_issues


def test_finding_summary():
    """Test finding summary counts."""
    review = CodeReview(feature_id="WIZE-123")
    review.findings.extend([
        CodeReviewFinding(
            category=FindingCategory.STYLE,
            severity=FindingSeverity.INFO,
            message="Info",
        ),
        CodeReviewFinding(
            category=FindingCategory.TESTING,
            severity=FindingSeverity.MAJOR,
            message="Major",
        ),
        CodeReviewFinding(
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.BLOCKING,
            message="Blocking",
        ),
    ])

    summary = review.finding_summary
    assert summary["info"] == 1
    assert summary["major"] == 1
    assert summary["blocking"] == 1
