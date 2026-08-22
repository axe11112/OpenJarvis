"""Integration tests: End-to-end Wiz pipeline."""

from __future__ import annotations

import pytest

from openjarvis.wiz.dispatcher import RequestDispatcher
from openjarvis.wiz.github_integration import GitHubIntegration
from openjarvis.wiz.merge_gates import MergeGates
from openjarvis.wiz.models import FeatureState, RiskLevel
from openjarvis.wiz.notifications import NotificationManager
from openjarvis.wiz.review import CodeReview, CodeReviewFinding, FindingCategory, FindingSeverity
from openjarvis.wiz.safety import SafetyGates


def test_complete_low_risk_feature_pipeline():
    """Test complete pipeline for a LOW risk feature.

    Pipeline stages:
    1. Owner request -> FeatureRequest
    2. Risk assessment
    3. Implementation (mocked)
    4. Testing
    5. Code review
    6. Safety gates
    7. Merge gates
    8. PR creation
    9. Notifications
    """

    # Stage 1: Owner request
    dispatcher = RequestDispatcher()
    request = dispatcher.dispatch("Add a tooltip to the dashboard")
    assert request.state == FeatureState.CREATED
    assert request.risk_level == RiskLevel.LOW

    # Stage 2: Update state through pipeline
    request.update_state(FeatureState.PLANNED)
    request.git_branch = "wiz/WIZE-tooltip"
    request.feature_sha = "abc123def456"

    # Stage 3: Implementation happens (mocked)
    request.update_state(FeatureState.IMPLEMENTING)
    request.update_state(FeatureState.TESTING)

    # Stage 4: Test results
    request.test_results = "50 passed, 0 failed, 2 skipped"

    # Stage 5: Vercel Preview
    request.update_state(FeatureState.PREVIEWING)
    request.preview_sha = "abc123def456"  # Matches feature SHA

    # Stage 6: Code review
    request.update_state(FeatureState.REVIEWING)
    review = CodeReview(feature_id=request.id)
    # No blocking findings for simple feature

    # Stage 7: Safety gates
    safety_gates = SafetyGates.evaluate_all_gates(request)
    assert len([g for g in safety_gates if g.blocking]) == 0

    # Stage 8: Merge gates
    merge_result = MergeGates.evaluate(request, review)
    assert merge_result.can_merge
    assert merge_result.status.value == "pass"

    # Stage 9: PR creation (would be done by GitHub integration)
    github = GitHubIntegration(owner="test-owner", repo="test-repo")
    # Would create real PR here via github.create_pull_request()

    # Stage 10: Notifications
    notifications = NotificationManager()
    # Would send completion notification


def test_medium_risk_feature_requires_approval():
    """Test that MEDIUM risk features require human approval."""

    dispatcher = RequestDispatcher()
    request = dispatcher.dispatch("Update the dashboard API endpoint")
    assert request.risk_level == RiskLevel.MEDIUM

    # Set up as if implementation completed
    request.test_results = "100 passed, 0 failed"
    request.feature_sha = "abc123"
    request.preview_sha = "abc123"

    review = CodeReview(feature_id=request.id)

    # Merge gates should produce WARNING, not PASS
    merge_result = MergeGates.evaluate(request, review)
    assert not merge_result.can_merge or merge_result.status.value == "warn"


def test_feature_with_blocking_review_cannot_merge():
    """Test that blocking review findings prevent merge."""

    dispatcher = RequestDispatcher()
    request = dispatcher.dispatch("Add dark mode")
    request.risk_level = RiskLevel.LOW
    request.test_results = "100 passed, 0 failed"
    request.feature_sha = "abc123"
    request.preview_sha = "abc123"

    # Add blocking review finding
    review = CodeReview(feature_id=request.id)
    review.findings.append(
        CodeReviewFinding(
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.BLOCKING,
            message="Potential XSS vulnerability",
        )
    )

    merge_result = MergeGates.evaluate(request, review)
    assert not merge_result.can_merge
    assert "review_blocking_findings" in merge_result.gates_failed


def test_failed_tests_prevent_merge():
    """Test that failed tests prevent merge."""

    dispatcher = RequestDispatcher()
    request = dispatcher.dispatch("Refactor dashboard component")
    request.risk_level = RiskLevel.LOW
    request.test_results = "95 passed, 5 failed"  # Failures!
    request.feature_sha = "abc123"
    request.preview_sha = "abc123"

    review = CodeReview(feature_id=request.id)

    merge_result = MergeGates.evaluate(request, review)
    assert not merge_result.can_merge
    assert "tests_not_passing" in merge_result.gates_failed


def test_unknown_risk_cannot_merge():
    """Test that UNKNOWN risk features cannot merge autonomously."""

    request = dispatcher = RequestDispatcher()
    request = dispatcher.dispatch("Do some magic optimization")  # Vague!
    request.risk_level = RiskLevel.UNKNOWN  # Set explicitly

    request.test_results = "100 passed, 0 failed"
    request.feature_sha = "abc123"
    request.preview_sha = "abc123"

    review = CodeReview(feature_id=request.id)

    # Safety gates should reject UNKNOWN risk
    safety_result = SafetyGates.check_risk_level(request)
    assert not safety_result.passed

    # Merge gates should also reject
    merge_result = MergeGates.evaluate(request, review)
    assert not merge_result.can_merge
