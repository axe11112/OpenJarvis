"""Tests for Wiz testing framework."""

from __future__ import annotations

from openjarvis.wiz.testing import TestResult, TestType


def test_test_result_passing():
    """Test passing test result."""
    result = TestResult(
        test_type=TestType.UNIT,
        passed=10,
        failed=0,
        skipped=0,
    )

    assert result.is_passing
    assert result.total_run == 10
    assert "10 passed" in result.summary


def test_test_result_failing():
    """Test failing test result."""
    result = TestResult(
        test_type=TestType.UNIT,
        passed=9,
        failed=1,
        skipped=0,
    )

    assert not result.is_passing
    assert result.total_run == 10


def test_test_result_with_errors():
    """Test result with errors."""
    result = TestResult(
        test_type=TestType.INTEGRATION,
        passed=5,
        failed=0,
        skipped=1,
        errors=2,
    )

    assert not result.is_passing
    assert "5 passed" in result.summary
    assert "2 errors" in result.summary
