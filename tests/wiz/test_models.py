"""Tests for Wiz core models."""

from __future__ import annotations

import pytest

from openjarvis.wiz.models import FeatureRequest, FeatureState, RiskLevel


def test_feature_request_creation():
    """Test creating a FeatureRequest."""
    request = FeatureRequest(
        description="Add dark mode to dashboard",
        owner_input="Build dark mode",
    )

    assert request.description
    assert request.state == FeatureState.CREATED
    assert request.risk_level == RiskLevel.UNKNOWN
    assert request.id.startswith("WIZE-")


def test_feature_request_requires_description():
    """Test that FeatureRequest requires description or owner_input."""
    with pytest.raises(ValueError):
        FeatureRequest()


def test_feature_state_transitions():
    """Test state transitions."""
    request = FeatureRequest(description="test")

    assert request.state == FeatureState.CREATED

    request.update_state(FeatureState.IMPLEMENTING)
    assert request.state == FeatureState.IMPLEMENTING

    request.update_state(FeatureState.COMPLETE)
    assert request.state == FeatureState.COMPLETE


def test_feature_request_stores_metadata():
    """Test storing feature metadata."""
    request = FeatureRequest(description="test feature")

    request.git_branch = "wiz/WIZE-abc123"
    request.feature_sha = "abc123def456"
    request.preview_sha = "abc123def456"
    request.pull_request_number = 42

    assert request.git_branch == "wiz/WIZE-abc123"
    assert request.feature_sha == "abc123def456"
    assert request.preview_sha == "abc123def456"
    assert request.pull_request_number == 42


def test_risk_level_enum():
    """Test RiskLevel enum values."""
    assert RiskLevel.LOW.value == "low"
    assert RiskLevel.MEDIUM.value == "medium"
    assert RiskLevel.HIGH.value == "high"
    assert RiskLevel.UNKNOWN.value == "unknown"


def test_feature_state_enum():
    """Test FeatureState enum has all required states."""
    states = {state.value for state in FeatureState}
    expected = {
        "created",
        "planned",
        "implementing",
        "testing",
        "previewing",
        "reviewing",
        "approved_for_merge",
        "merged",
        "deployed_to_production",
        "complete",
        "failed",
        "blocked",
        "requires_human",
    }
    assert expected.issubset(states)
