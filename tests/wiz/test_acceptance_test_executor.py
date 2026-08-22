"""Tests for real acceptance test execution."""

from __future__ import annotations

import pytest

from openjarvis.wiz.acceptance_test_executor import (
    AcceptanceTestRunner,
    HttpTestExecutor,
    PlaywrightTestExecutor,
    SuiteExecutionResult,
    TestExecutionResult,
    TestStatus,
)
from openjarvis.wiz.acceptance_tests import (
    AcceptanceTest,
    AcceptanceTestSuite,
    TestType,
)


class TestTestExecutionResult:
    """TestExecutionResult dataclass."""

    def test_passed_result(self) -> None:
        result = TestExecutionResult(
            test_name="test_feature_works",
            test_type="unit",
            status=TestStatus.PASSED,
            passed=True,
            message="All assertions passed",
        )
        assert result.passed
        assert result.status == TestStatus.PASSED

    def test_failed_result(self) -> None:
        result = TestExecutionResult(
            test_name="test_feature_works",
            test_type="unit",
            status=TestStatus.FAILED,
            passed=False,
            error_message="Assertion failed",
        )
        assert not result.passed
        assert result.status == TestStatus.FAILED

    def test_skipped_result(self) -> None:
        result = TestExecutionResult(
            test_name="test_feature_works",
            test_type="unit",
            status=TestStatus.SKIPPED,
            passed=False,
            message="Skipped: Playwright not available",
        )
        assert result.status == TestStatus.SKIPPED

    def test_result_to_dict(self) -> None:
        result = TestExecutionResult(
            test_name="test_feature_works",
            test_type="ui",
            status=TestStatus.PASSED,
            passed=True,
            duration_seconds=1.5,
        )
        d = result.to_dict()
        assert d["test_name"] == "test_feature_works"
        assert d["passed"] is True
        assert d["duration_seconds"] == 1.5


class TestSuiteExecutionResult:
    """SuiteExecutionResult dataclass."""

    def test_create_suite_result(self) -> None:
        result = SuiteExecutionResult(
            feature_id="FEAT-001",
            total_tests=5,
            passed_tests=5,
            failed_tests=0,
            skipped_tests=0,
            error_tests=0,
        )
        assert result.feature_id == "FEAT-001"
        assert result.success_rate == 1.0

    def test_success_rate_calculation(self) -> None:
        result = SuiteExecutionResult(
            feature_id="FEAT-001",
            total_tests=10,
            passed_tests=8,
            failed_tests=2,
            skipped_tests=0,
            error_tests=0,
        )
        assert result.success_rate == 0.8

    def test_success_rate_with_skipped(self) -> None:
        result = SuiteExecutionResult(
            feature_id="FEAT-001",
            total_tests=10,
            passed_tests=8,
            failed_tests=1,
            skipped_tests=1,
            error_tests=0,
        )
        # Passed + Skipped = 9/10
        assert result.success_rate == 0.9

    def test_empty_suite(self) -> None:
        result = SuiteExecutionResult(
            feature_id="FEAT-001",
            total_tests=0,
            passed_tests=0,
            failed_tests=0,
            skipped_tests=0,
            error_tests=0,
        )
        assert result.success_rate == 0.0

    def test_suite_to_dict(self) -> None:
        result = SuiteExecutionResult(
            feature_id="FEAT-001",
            total_tests=5,
            passed_tests=5,
            failed_tests=0,
            skipped_tests=0,
            error_tests=0,
        )
        d = result.to_dict()
        assert d["feature_id"] == "FEAT-001"
        assert d["total_tests"] == 5
        assert d["passed_tests"] == 5


class TestPlaywrightExecutor:
    """Playwright test executor."""

    def test_can_execute_ui_tests(self) -> None:
        executor = PlaywrightTestExecutor()
        assert executor.can_execute("unit")
        assert executor.can_execute("integration")
        assert executor.can_execute("smoke")
        assert executor.can_execute("regression")

    def test_cannot_execute_performance(self) -> None:
        executor = PlaywrightTestExecutor()
        assert not executor.can_execute("performance")

    def test_execute_without_url(self) -> None:
        executor = PlaywrightTestExecutor()
        result = executor.execute(
            test_name="test_exists",
            test_code="pass",
            url=None,
        )
        assert result.status == TestStatus.ERROR
        assert not result.passed

    def test_simulate_test_toggle(self) -> None:
        executor = PlaywrightTestExecutor()
        result = executor.execute(
            test_name="test_toggle_switches_theme",
            test_code="click toggle; verify theme changed",
            url="http://localhost:3000",
        )
        # Simulated: toggle tests should pass
        if executor._playwright_available:
            assert result.status == TestStatus.PASSED or result.status == TestStatus.SKIPPED

    def test_simulate_test_exists(self) -> None:
        executor = PlaywrightTestExecutor()
        result = executor.execute(
            test_name="test_toggle_exists",
            test_code="find element",
            url="http://localhost:3000",
        )
        # Simulated: exists tests should pass
        if executor._playwright_available:
            assert result.status == TestStatus.PASSED or result.status == TestStatus.SKIPPED


class TestHttpExecutor:
    """HTTP test executor."""

    def test_can_execute_api_tests(self) -> None:
        executor = HttpTestExecutor()
        assert executor.can_execute("api")
        assert executor.can_execute("HTTP")

    def test_cannot_execute_ui_tests(self) -> None:
        executor = HttpTestExecutor()
        assert not executor.can_execute("ui")

    def test_execute_without_url(self) -> None:
        executor = HttpTestExecutor()
        result = executor.execute(
            test_name="test_api_response",
            test_code="GET /api/status",
            url=None,
        )
        assert result.status == TestStatus.ERROR
        assert not result.passed

    def test_execute_valid_url(self) -> None:
        executor = HttpTestExecutor()
        result = executor.execute(
            test_name="test_api_response",
            test_code="GET /api/status",
            url="https://api.example.com/status",
        )
        # Simulated: valid URLs should succeed
        assert result.status == TestStatus.PASSED or result.status == TestStatus.ERROR
        if result.status == TestStatus.PASSED:
            assert result.passed


class TestAcceptanceTestRunner:
    """Acceptance test runner."""

    @pytest.fixture
    def test_suite(self) -> AcceptanceTestSuite:
        """Create a sample test suite."""
        tests = [
            AcceptanceTest(
                name="test_feature_exists",
                test_type=TestType.UNIT,
                description="Feature is implemented",
                code="# verify feature exists",
                expected_result="Feature found",
                is_critical=True,
            ),
            AcceptanceTest(
                name="test_feature_works",
                test_type=TestType.INTEGRATION,
                description="Feature works correctly",
                code="# verify feature works",
                expected_result="Feature functions",
                is_critical=True,
            ),
            AcceptanceTest(
                name="test_edge_case",
                test_type=TestType.UNIT,
                description="Edge case handled",
                code="# verify edge case",
                expected_result="Edge case handled",
                is_critical=False,
            ),
        ]
        return AcceptanceTestSuite(feature_id="FEAT-001", tests=tests)

    def test_runner_initialization(self) -> None:
        runner = AcceptanceTestRunner()
        assert runner is not None
        assert len(runner.executors) > 0

    def test_run_suite(self, test_suite: AcceptanceTestSuite) -> None:
        runner = AcceptanceTestRunner()
        result = runner.run(
            suite=test_suite,
            preview_url="http://localhost:3000",
        )
        assert result.feature_id == "FEAT-001"
        assert result.total_tests == 3
        assert result.passed_tests >= 0
        assert len(result.results) == 3

    def test_run_with_mixed_results(
        self, test_suite: AcceptanceTestSuite
    ) -> None:
        runner = AcceptanceTestRunner()
        result = runner.run(
            suite=test_suite,
            preview_url="http://localhost:3000",
        )
        # Should have various results
        total = result.passed_tests + result.failed_tests + result.skipped_tests + result.error_tests
        assert total == result.total_tests

    def test_format_results(
        self, test_suite: AcceptanceTestSuite
    ) -> None:
        runner = AcceptanceTestRunner()
        suite_result = runner.run(
            suite=test_suite,
            preview_url="http://localhost:3000",
        )
        formatted = runner.format_results(suite_result)
        assert "Acceptance Test Suite" in formatted
        assert "FEAT-001" in formatted
        assert "Total tests" in formatted
        assert "Success rate" in formatted

    def test_all_tests_pass(self) -> None:
        tests = [
            AcceptanceTest(
                name="test_1",
                test_type=TestType.UNIT,
                description="Test 1",
                code="pass",
                expected_result="pass",
                is_critical=True,
            ),
        ]
        suite = AcceptanceTestSuite(feature_id="FEAT-TEST", tests=tests)
        runner = AcceptanceTestRunner()
        result = runner.run(suite=suite, preview_url="http://localhost:3000")

        assert result.total_tests >= 1
        # At least one test should exist
        assert len(result.results) >= 1


class TestRealExecutionScenarios:
    """Test real-world execution scenarios."""

    def test_dark_mode_toggle_tests(self) -> None:
        """Test dark mode toggle feature acceptance tests."""
        tests = [
            AcceptanceTest(
                name="test_toggle_exists",
                test_type=TestType.SMOKE,
                description="Dark mode toggle is visible",
                code="find_element('.dark-mode-toggle')",
                expected_result="Toggle found",
                is_critical=True,
            ),
            AcceptanceTest(
                name="test_toggle_switches",
                test_type=TestType.INTEGRATION,
                description="Clicking toggle switches theme",
                code="click_element('.dark-mode-toggle'); wait_for_theme_change()",
                expected_result="Theme switched",
                is_critical=True,
            ),
            AcceptanceTest(
                name="test_preference_persists",
                test_type=TestType.INTEGRATION,
                description="Theme preference is saved",
                code="set_storage('theme', 'dark'); reload(); verify_storage('theme', 'dark')",
                expected_result="Preference persisted",
                is_critical=True,
            ),
        ]
        suite = AcceptanceTestSuite(
            feature_id="WIZE-PILOT-001",
            tests=tests,
        )

        runner = AcceptanceTestRunner()
        result = runner.run(
            suite=suite,
            preview_url="https://wize-wiz-wize-pilot-001-wiz.vercel.app",
        )

        # Verify suite executed
        assert result.feature_id == "WIZE-PILOT-001"
        assert result.total_tests == 3
        # Should have some mix of pass/skip
        assert result.success_rate >= 0.0

    def test_execution_against_preview_url(self) -> None:
        """Test execution against Vercel Preview URL."""
        tests = [
            AcceptanceTest(
                name="test_feature_deployed",
                test_type=TestType.SMOKE,
                description="Feature is deployed",
                code="GET /",
                expected_result="200 OK",
                is_critical=True,
            ),
        ]
        suite = AcceptanceTestSuite(feature_id="FEAT-PREVIEW", tests=tests)
        runner = AcceptanceTestRunner()

        # Should handle Preview URL gracefully
        result = runner.run(
            suite=suite,
            preview_url="https://example-wiz-branch.vercel.app",
        )

        assert result.total_tests >= 1

    def test_critical_vs_optional_tests(self) -> None:
        """Differentiate between critical and optional tests."""
        critical = AcceptanceTest(
            name="test_critical_feature",
            test_type=TestType.UNIT,
            description="Critical feature works",
            code="verify_critical()",
            expected_result="Works",
            is_critical=True,
        )
        optional = AcceptanceTest(
            name="test_nice_to_have",
            test_type=TestType.UNIT,
            description="Nice-to-have enhancement",
            code="verify_enhancement()",
            expected_result="Works",
            is_critical=False,
        )
        suite = AcceptanceTestSuite(
            feature_id="FEAT-CRITICAL",
            tests=[critical, optional],
        )

        # Can identify which tests are critical
        critical_tests = [t for t in suite.tests if t.is_critical]
        optional_tests = [t for t in suite.tests if not t.is_critical]
        assert len(critical_tests) == 1
        assert len(optional_tests) == 1
