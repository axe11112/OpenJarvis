"""Tests for merge gates."""

from __future__ import annotations

from openjarvis.wiz.merge_gates import MergeGateStatus, MergeGates
from openjarvis.wiz.models import FeatureRequest, RiskLevel
from openjarvis.wiz.review import CodeReview


def test_merge_gates_low_risk_clean():
    """Test LOW risk feature with clean review can merge."""
    request = FeatureRequest(description="test")
    request.risk_level = RiskLevel.LOW
    request.test_results = "10 passed, 0 failed"
    request.feature_sha = "abc123"
    request.preview_sha = "abc123"

    review = CodeReview(feature_id=request.id)

    result = MergeGates.evaluate(request, review)
    assert result.can_merge
    assert result.status == MergeGateStatus.PASS
    assert "risk_low" in result.gates_passed
    assert "tests_passing" in result.gates_passed


def test_merge_gates_high_risk_fails():
    """Test HIGH risk feature fails autonomous merge gate."""
    request = FeatureRequest(description="test")
    request.risk_level = RiskLevel.HIGH
    request.test_results = "10 passed, 0 failed"
    request.feature_sha = "abc123"
    request.preview_sha = "abc123"

    review = CodeReview(feature_id=request.id)

    result = MergeGates.evaluate(request, review)
    # HIGH risk cannot merge autonomously - requires human approval
    assert result.status == MergeGateStatus.WARN
    assert "risk_high" in result.gates_warning


def test_merge_gates_blocking_review():
    """Test blocking review findings fail merge."""
    request = FeatureRequest(description="test")
    request.risk_level = RiskLevel.LOW
    request.test_results = "10 passed, 0 failed"
    request.feature_sha = "abc123"
    request.preview_sha = "abc123"

    review = CodeReview(feature_id=request.id)
    from openjarvis.wiz.review import CodeReviewFinding, FindingCategory, FindingSeverity
    review.findings.append(
        CodeReviewFinding(
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.BLOCKING,
            message="Hardcoded secret",
        )
    )

    result = MergeGates.evaluate(request, review)
    assert not result.can_merge
    assert result.status == MergeGateStatus.FAIL
    assert "review_blocking_findings" in result.gates_failed


def test_merge_gates_failed_tests():
    """Test failed tests prevent merge."""
    request = FeatureRequest(description="test")
    request.risk_level = RiskLevel.LOW
    request.test_results = "9 passed, 1 failed"

    review = CodeReview(feature_id=request.id)

    result = MergeGates.evaluate(request, review)
    assert not result.can_merge
    assert "tests_not_passing" in result.gates_failed


def test_merge_gates_unknown_risk():
    """Test UNKNOWN risk cannot merge autonomously."""
    request = FeatureRequest(description="test")
    request.risk_level = RiskLevel.UNKNOWN

    review = CodeReview(feature_id=request.id)

    result = MergeGates.evaluate(request, review)
    assert not result.can_merge
    assert "risk_unknown" in result.gates_failed
