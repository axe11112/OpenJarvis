"""Tests for Wiz acceptance testing framework."""

import pytest

from openjarvis.wiz.acceptance import AcceptanceTestRunner


def test_acceptance_runner_creation():
    """Test creating an acceptance runner."""
    runner = AcceptanceTestRunner()
    assert runner is not None


def test_acceptance_runner_ui_detection():
    """Test UI criterion detection."""
    runner = AcceptanceTestRunner()

    ui_criteria = [
        "Button 'Save' is visible on the page",
        "Click the delete button",
        "The modal displays correctly",
        "Navigation menu appears on load",
    ]

    for criterion in ui_criteria:
        assert runner._check_criterion_is_ui(criterion), f"Failed to detect UI: {criterion}"


def test_acceptance_runner_api_detection():
    """Test API criterion detection."""
    runner = AcceptanceTestRunner()

    api_criteria = [
        "GET /api/users returns 200",
        "API endpoint /health is responsive",
        "HTTP request to /status returns 200",
    ]

    for criterion in api_criteria:
        assert runner._check_criterion_is_api(
            criterion
        ), f"Failed to detect API: {criterion}"


def test_acceptance_runner_extract_quoted():
    """Test extracting quoted text."""
    runner = AcceptanceTestRunner()

    text = 'Click the button labeled "Save"'
    extracted = runner._extract_quoted(text)
    assert extracted == "Save"

    text_no_quotes = "Click any button"
    extracted_none = runner._extract_quoted(text_no_quotes)
    assert extracted_none is None


def test_acceptance_runner_extract_endpoint():
    """Test extracting API endpoint."""
    runner = AcceptanceTestRunner()

    criterion = "GET /api/users returns 200"
    endpoint = runner._extract_endpoint(criterion)
    assert endpoint == "/api/users"

    criterion_nested = "POST /api/v1/users/123/delete"
    endpoint_nested = runner._extract_endpoint(criterion_nested)
    assert endpoint_nested == "/api/v1/users/123/delete"


def test_acceptance_run_shell_criteria_pass():
    """Test running shell criteria that pass."""
    runner = AcceptanceTestRunner()

    criteria = [
        "true",  # Always succeeds
        "[ 1 -eq 1 ]",  # Arithmetic check
    ]

    result = runner.run_shell_criteria(criteria)
    assert result.passed is True
    assert all(result.criteria_results.values())


def test_acceptance_run_shell_criteria_fail():
    """Test running shell criteria that fail."""
    runner = AcceptanceTestRunner()

    criteria = [
        "false",  # Always fails
    ]

    result = runner.run_shell_criteria(criteria)
    assert result.passed is False
    assert not result.criteria_results["false"]


def test_acceptance_run_shell_criteria_mixed():
    """Test running mixed shell criteria."""
    runner = AcceptanceTestRunner()

    criteria = [
        "true",  # Pass
        "false",  # Fail
    ]

    result = runner.run_shell_criteria(criteria)
    assert result.passed is False
    assert result.criteria_results["true"] is True
    assert result.criteria_results["false"] is False


def test_acceptance_run_shell_criteria_timeout():
    """Test timeout handling in shell criteria."""
    runner = AcceptanceTestRunner()

    criteria = [
        "sleep 10",  # Will timeout
    ]

    result = runner.run_shell_criteria(criteria, timeout=1)
    assert result.passed is False


def test_acceptance_result_summary():
    """Test acceptance result summary."""
    from openjarvis.wiz.acceptance import AcceptanceResult

    result = AcceptanceResult(
        passed=True,
        criteria_results={
            "Criterion 1": True,
            "Criterion 2": True,
        },
        details="All criteria passed",
    )

    assert result.passed is True
    assert len(result.criteria_results) == 2
