"""Acceptance test generation tests."""

from __future__ import annotations

import pytest

from openjarvis.wiz.acceptance_tests import (
    AcceptanceTest,
    AcceptanceTestGenerator,
    AcceptanceTestSuite,
    TestType,
)
from openjarvis.wiz.features.model import FeatureRequest, FeatureState, Priority


@pytest.fixture
def sample_feature() -> FeatureRequest:
    """Sample feature for testing."""
    return FeatureRequest(
        id="FEAT-001",
        title="Add user authentication",
        operator_request="Add login and session management",
        desired_outcome="Users can log in and maintain sessions",
        source="cli",
        actor_id="test",
        target="wize",
        repository="owner/repo",
        priority=Priority.P2,
        state=FeatureState.RECEIVED,
        risk="MEDIUM",
    )


@pytest.fixture
def bugfix_feature() -> FeatureRequest:
    """Bug fix feature for testing."""
    return FeatureRequest(
        id="FEAT-002",
        title="Fix database connection pooling bug",
        operator_request="Fix the connection leak in database module",
        desired_outcome="Database connections are properly cleaned up",
        source="cli",
        actor_id="test",
        target="wize",
        repository="owner/repo",
        priority=Priority.P1,
        state=FeatureState.RECEIVED,
        risk="HIGH",
    )


class TestAcceptanceTest:
    """AcceptanceTest dataclass."""

    def test_create_test(self) -> None:
        test = AcceptanceTest(
            name="test_example",
            test_type=TestType.UNIT,
            description="Example test",
            code="def test_example(): pass",
            expected_result="test passes",
        )
        assert test.name == "test_example"
        assert test.test_type == TestType.UNIT

    def test_test_to_dict(self) -> None:
        test = AcceptanceTest(
            name="test_example",
            test_type=TestType.UNIT,
            description="Example test",
            code="def test_example(): pass",
            expected_result="test passes",
            is_critical=True,
        )
        d = test.to_dict()
        assert d["name"] == "test_example"
        assert d["test_type"] == "unit"
        assert d["is_critical"] is True


class TestAcceptanceTestSuite:
    """AcceptanceTestSuite management."""

    def test_create_suite(self) -> None:
        tests = [
            AcceptanceTest(
                name="test1",
                test_type=TestType.UNIT,
                description="Test 1",
                code="pass",
                expected_result="pass",
            )
        ]
        suite = AcceptanceTestSuite(feature_id="FEAT-001", tests=tests)
        assert suite.feature_id == "FEAT-001"
        assert len(suite.tests) == 1

    def test_critical_tests(self) -> None:
        tests = [
            AcceptanceTest(
                name="critical",
                test_type=TestType.UNIT,
                description="Critical",
                code="pass",
                expected_result="pass",
                is_critical=True,
            ),
            AcceptanceTest(
                name="optional",
                test_type=TestType.UNIT,
                description="Optional",
                code="pass",
                expected_result="pass",
                is_critical=False,
            ),
        ]
        suite = AcceptanceTestSuite(feature_id="FEAT-001", tests=tests)
        critical = suite.critical_tests()
        assert len(critical) == 1
        assert critical[0].name == "critical"

    def test_suite_to_dict(self) -> None:
        tests = [
            AcceptanceTest(
                name="test1",
                test_type=TestType.UNIT,
                description="Test 1",
                code="pass",
                expected_result="pass",
                is_critical=True,
            )
        ]
        suite = AcceptanceTestSuite(feature_id="FEAT-001", tests=tests)
        d = suite.to_dict()
        assert d["feature_id"] == "FEAT-001"
        assert d["total_count"] == 1
        assert d["critical_count"] == 1


class TestAcceptanceTestGenerator:
    """Test generation."""

    def test_generate_for_new_feature(
        self, sample_feature: FeatureRequest
    ) -> None:
        gen = AcceptanceTestGenerator()
        suite = gen.generate(sample_feature)
        assert suite.feature_id == "FEAT-001"
        assert len(suite.tests) > 0

    def test_generate_for_bugfix(
        self, bugfix_feature: FeatureRequest
    ) -> None:
        gen = AcceptanceTestGenerator()
        suite = gen.generate(bugfix_feature)
        assert len(suite.tests) > 0
        # Bug fixes should have regression tests
        test_names = [t.name for t in suite.tests]
        assert any("regression" in name for name in test_names)

    def test_generates_critical_tests(
        self, sample_feature: FeatureRequest
    ) -> None:
        gen = AcceptanceTestGenerator()
        suite = gen.generate(sample_feature)
        critical = suite.critical_tests()
        assert len(critical) > 0

    def test_high_risk_generates_more_tests(self) -> None:
        gen = AcceptanceTestGenerator()

        low_risk = FeatureRequest(
            id="LOW",
            title="Add logging",
            operator_request="Add debug logging",
            desired_outcome="Debug logging available",
            source="cli",
            actor_id="test",
            target="wize",
            repository="owner/repo",
            priority=Priority.P3,
            state=FeatureState.RECEIVED,
            risk="LOW",
        )

        high_risk = FeatureRequest(
            id="HIGH",
            title="Add logging",
            operator_request="Add debug logging",
            desired_outcome="Debug logging available",
            source="cli",
            actor_id="test",
            target="wize",
            repository="owner/repo",
            priority=Priority.P1,
            state=FeatureState.RECEIVED,
            risk="CRITICAL",
        )

        low_suite = gen.generate(low_risk)
        high_suite = gen.generate(high_risk)
        assert len(high_suite.tests) >= len(low_suite.tests)

    def test_tests_are_deterministic(
        self, sample_feature: FeatureRequest
    ) -> None:
        gen = AcceptanceTestGenerator()
        suite1 = gen.generate(sample_feature)
        suite2 = gen.generate(sample_feature)
        assert len(suite1.tests) == len(suite2.tests)
        assert [t.name for t in suite1.tests] == [t.name for t in suite2.tests]

    def test_generated_tests_have_code(
        self, sample_feature: FeatureRequest
    ) -> None:
        gen = AcceptanceTestGenerator()
        suite = gen.generate(sample_feature)
        for test in suite.tests:
            assert test.code is not None
            assert len(test.code) > 0

    def test_tests_are_sorted_by_criticality(
        self, bugfix_feature: FeatureRequest
    ) -> None:
        gen = AcceptanceTestGenerator()
        suite = gen.generate(bugfix_feature)
        # First test should be critical
        if len(suite.tests) > 0:
            assert suite.tests[0].is_critical or not suite.tests[-1].is_critical

    def test_feature_classification(self) -> None:
        gen = AcceptanceTestGenerator()

        new_feature = FeatureRequest(
            id="NEW",
            title="Add new dashboard",
            operator_request="Add dashboard",
            desired_outcome="Dashboard visible",
            source="cli",
            actor_id="test",
            target="wize",
            repository="owner/repo",
            priority=Priority.P2,
            state=FeatureState.RECEIVED,
        )

        bugfix = FeatureRequest(
            id="BUG",
            title="Fix crash on login",
            operator_request="Fix bug",
            desired_outcome="No crash",
            source="cli",
            actor_id="test",
            target="wize",
            repository="owner/repo",
            priority=Priority.P1,
            state=FeatureState.RECEIVED,
        )

        refactor = FeatureRequest(
            id="REF",
            title="Refactor database module",
            operator_request="Reorganize code",
            desired_outcome="Better structure",
            source="cli",
            actor_id="test",
            target="wize",
            repository="owner/repo",
            priority=Priority.P3,
            state=FeatureState.RECEIVED,
        )

        assert gen._classify_feature(new_feature) == "new_feature"
        assert gen._classify_feature(bugfix) == "bug_fix"
        assert gen._classify_feature(refactor) == "refactor"


class TestTestTypes:
    """Test type enum."""

    def test_unit_type(self) -> None:
        assert TestType.UNIT.value == "unit"

    def test_integration_type(self) -> None:
        assert TestType.INTEGRATION.value == "integration"

    def test_smoke_type(self) -> None:
        assert TestType.SMOKE.value == "smoke"

    def test_regression_type(self) -> None:
        assert TestType.REGRESSION.value == "regression"

    def test_performance_type(self) -> None:
        assert TestType.PERFORMANCE.value == "performance"
