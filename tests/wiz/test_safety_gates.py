"""Tests for safety gates."""

from __future__ import annotations

import pytest

from openjarvis.wiz.models import FeatureRequest, RiskLevel
from openjarvis.wiz.safety import SafetyGates


def test_low_risk_passes_gate():
    """Test LOW risk feature passes safety gate."""
    request = FeatureRequest(description="test")
    request.risk_level = RiskLevel.LOW

    result = SafetyGates.check_risk_level(request)
    assert result.passed
    assert not result.blocking


def test_medium_risk_fails_gate():
    """Test MEDIUM risk feature fails autonomous gate."""
    request = FeatureRequest(description="test")
    request.risk_level = RiskLevel.MEDIUM

    result = SafetyGates.check_risk_level(request)
    assert not result.passed
    assert result.blocking


def test_high_risk_fails_gate():
    """Test HIGH risk feature fails autonomous gate."""
    request = FeatureRequest(description="test")
    request.risk_level = RiskLevel.HIGH

    result = SafetyGates.check_risk_level(request)
    assert not result.passed
    assert result.blocking


def test_unknown_risk_fails_gate():
    """Test UNKNOWN risk feature fails with blocking gate."""
    request = FeatureRequest(description="test")
    request.risk_level = RiskLevel.UNKNOWN

    result = SafetyGates.check_risk_level(request)
    assert not result.passed
    assert result.blocking
    assert "UNKNOWN" in result.reason


def test_passing_tests_gate():
    """Test passing tests pass the gate."""
    request = FeatureRequest(description="test")
    request.test_results = "5 passed, 0 failed"

    result = SafetyGates.check_test_results(request)
    assert result.passed


def test_failing_tests_gate():
    """Test failing tests fail the gate."""
    request = FeatureRequest(description="test")
    request.test_results = "5 passed, 1 failed"

    result = SafetyGates.check_test_results(request)
    assert not result.passed
    assert result.blocking


def test_no_test_results_fails_gate():
    """Test missing test results fail the gate."""
    request = FeatureRequest(description="test")

    result = SafetyGates.check_test_results(request)
    assert not result.passed
    assert result.blocking


def test_preview_sha_mismatch_fails():
    """Test mismatched Preview SHA fails gate."""
    request = FeatureRequest(description="test")
    request.feature_sha = "abc123"
    request.preview_sha = "def456"

    result = SafetyGates.check_preview_verification(request)
    assert not result.passed
    assert result.blocking


def test_preview_sha_match_passes():
    """Test matching Preview SHA passes gate."""
    request = FeatureRequest(description="test")
    request.feature_sha = "abc123"
    request.preview_sha = "abc123"

    result = SafetyGates.check_preview_verification(request)
    assert result.passed


def test_evaluate_all_gates_low_risk():
    """Test all gates for LOW risk feature with valid state."""
    request = FeatureRequest(description="test")
    request.risk_level = RiskLevel.LOW
    request.test_results = "10 passed, 0 failed"
    request.git_branch = "wiz/WIZE-123"
    request.feature_sha = "abc123"
    request.preview_sha = "abc123"

    failures = SafetyGates.evaluate_all_gates(request)
    # Should have no blocking failures
    blocking_failures = [f for f in failures if f.blocking]
    assert len(blocking_failures) == 0


def test_evaluate_all_gates_high_risk():
    """Test all gates for HIGH risk feature."""
    request = FeatureRequest(description="test")
    request.risk_level = RiskLevel.HIGH
    request.test_results = "10 passed, 0 failed"

    failures = SafetyGates.evaluate_all_gates(request)
    # HIGH risk should produce at least one blocking failure
    blocking_failures = [f for f in failures if f.blocking]
    assert len(blocking_failures) > 0
