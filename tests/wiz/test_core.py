"""Tests for Wiz core data structures."""

from datetime import datetime

import pytest

from openjarvis.wiz.core import (
    FeatureRequest,
    FeatureRequestState,
    RiskLevel,
)


def test_feature_request_creation():
    """Test creating a feature request."""
    req = FeatureRequest(
        owner="test-user",
        feature="Add dark mode toggle",
        repository="test/repo",
        acceptance_criteria=[
            "Toggle appears on dashboard",
            "Settings persist",
            "CSS theme changes",
        ],
        constraints=["no_auth_changes", "no_database_schema"],
    )

    assert req.owner == "test-user"
    assert req.feature == "Add dark mode toggle"
    assert req.state == FeatureRequestState.PENDING
    assert req.estimated_risk == RiskLevel.UNKNOWN
    assert len(req.acceptance_criteria) == 3
    assert len(req.constraints) == 2
    assert isinstance(req.created_at, datetime)


def test_feature_request_state_transitions():
    """Test valid state transitions."""
    req = FeatureRequest(
        owner="test",
        feature="test",
        repository="test/repo",
    )

    assert req.state == FeatureRequestState.PENDING
    req.state = FeatureRequestState.PLANNED
    assert req.state == FeatureRequestState.PLANNED
    req.state = FeatureRequestState.IMPLEMENTING
    assert req.state == FeatureRequestState.IMPLEMENTING


def test_risk_levels():
    """Test risk level classifications."""
    assert RiskLevel.LOW.value == "low"
    assert RiskLevel.MEDIUM.value == "medium"
    assert RiskLevel.HIGH.value == "high"
    assert RiskLevel.UNKNOWN.value == "unknown"

    # LOW allows autonomous merge
    # MEDIUM/HIGH/UNKNOWN refuse autonomous merge
    assert RiskLevel.LOW != RiskLevel.HIGH


def test_feature_request_completion():
    """Test tracking feature request completion."""
    req = FeatureRequest(
        owner="test",
        feature="test feature",
        repository="test/repo",
    )

    req.state = FeatureRequestState.COMPLETE
    req.completion_time = datetime.utcnow()
    req.merge_sha = "abc123def456"
    req.production_sha = "abc123def456"

    assert req.state == FeatureRequestState.COMPLETE
    assert req.merge_sha is not None
    assert req.production_sha is not None
    assert req.completion_time is not None


def test_feature_request_failure_tracking():
    """Test tracking failed features."""
    req = FeatureRequest(
        owner="test",
        feature="broken feature",
        repository="test/repo",
    )

    req.state = FeatureRequestState.FAILED
    req.failure_reason = "Tests failed: auth module broken"

    assert req.state == FeatureRequestState.FAILED
    assert "auth module" in req.failure_reason
