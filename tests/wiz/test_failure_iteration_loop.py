"""Tests for failure → iteration loop."""

from __future__ import annotations

import pytest

from openjarvis.wiz.failure_iteration_loop import (
    FailureEvidence,
    FailureIterationLoop,
    FailureType,
    IterationAttempt,
)


class TestFailureType:
    """FailureType enum."""

    def test_failure_types(self) -> None:
        assert FailureType.COMPILATION.value == "compilation"
        assert FailureType.UNIT_TESTS.value == "unit_tests"
        assert FailureType.ACCEPTANCE_TESTS.value == "acceptance_tests"
        assert FailureType.SECURITY.value == "security"
        assert FailureType.GATE_FAILURE.value == "gate_failure"
        assert FailureType.DEPLOYMENT.value == "deployment"
        assert FailureType.PRODUCTION.value == "production"


class TestFailureEvidence:
    """FailureEvidence dataclass."""

    def test_create_compilation_failure(self) -> None:
        evidence = FailureEvidence(
            feature_id="WIZE-001",
            failure_type=FailureType.COMPILATION,
            error_message="TypeError: 'NoneType' object is not subscriptable",
            error_traceback="  File 'app.ts', line 42\n    return obj[key]",
        )
        assert evidence.failure_type == FailureType.COMPILATION
        assert "TypeError" in evidence.error_message

    def test_create_test_failure(self) -> None:
        evidence = FailureEvidence(
            feature_id="WIZE-001",
            failure_type=FailureType.UNIT_TESTS,
            error_message="3 tests failed",
            failed_tests=[
                "test_dark_mode_toggle_exists",
                "test_toggle_switches_theme",
                "test_preference_persists",
            ],
        )
        assert evidence.failure_type == FailureType.UNIT_TESTS
        assert len(evidence.failed_tests) == 3

    def test_create_security_failure(self) -> None:
        evidence = FailureEvidence(
            feature_id="WIZE-001",
            failure_type=FailureType.SECURITY,
            error_message="Secret detected: API_KEY in code",
        )
        assert evidence.failure_type == FailureType.SECURITY

    def test_failure_evidence_to_dict(self) -> None:
        evidence = FailureEvidence(
            feature_id="WIZE-001",
            failure_type=FailureType.UNIT_TESTS,
            error_message="Test failed",
            failed_tests=["test_1"],
        )
        d = evidence.to_dict()
        assert d["feature_id"] == "WIZE-001"
        assert d["failure_type"] == "unit_tests"
        assert d["failed_tests"] == ["test_1"]


class TestIterationAttempt:
    """IterationAttempt dataclass."""

    def test_create_successful_attempt(self) -> None:
        attempt = IterationAttempt(
            attempt_number=1,
            feature_id="WIZE-001",
            session_id="session-123",
            is_success=True,
        )
        assert attempt.is_success is True
        assert attempt.is_terminal_failure is False

    def test_create_failed_attempt(self) -> None:
        evidence = FailureEvidence(
            feature_id="WIZE-001",
            failure_type=FailureType.COMPILATION,
            error_message="Syntax error",
        )
        attempt = IterationAttempt(
            attempt_number=1,
            feature_id="WIZE-001",
            session_id="session-123",
            is_success=False,
            failure_evidence=evidence,
        )
        assert attempt.is_success is False
        assert attempt.failure_evidence is not None

    def test_iteration_attempt_to_dict(self) -> None:
        attempt = IterationAttempt(
            attempt_number=1,
            feature_id="WIZE-001",
            session_id="session-123",
            is_success=True,
        )
        d = attempt.to_dict()
        assert d["attempt_number"] == 1
        assert d["is_success"] is True


class TestFailureIterationLoop:
    """Failure iteration loop functionality."""

    def test_initialization(self) -> None:
        loop = FailureIterationLoop(max_iterations=3)
        assert loop.max_iterations == 3
        assert len(loop.iterations) == 0

    def test_collect_failure_evidence_compilation(self) -> None:
        loop = FailureIterationLoop()

        evidence = loop.collect_failure_evidence(
            feature_id="WIZE-001",
            failure_type=FailureType.COMPILATION,
            error_message="TypeError: Cannot read property 'x' of undefined",
            error_traceback="at Object.<anonymous> (app.ts:42:10)",
        )

        assert evidence.feature_id == "WIZE-001"
        assert evidence.failure_type == FailureType.COMPILATION

    def test_collect_failure_evidence_tests(self) -> None:
        loop = FailureIterationLoop()

        evidence = loop.collect_failure_evidence(
            feature_id="WIZE-001",
            failure_type=FailureType.UNIT_TESTS,
            error_message="2 tests failed",
            failed_tests=["test_dark_mode_toggle", "test_theme_persistence"],
        )

        assert evidence.failed_tests is not None
        assert len(evidence.failed_tests) == 2

    def test_collect_failure_evidence_gates(self) -> None:
        loop = FailureIterationLoop()

        evidence = loop.collect_failure_evidence(
            feature_id="WIZE-001",
            failure_type=FailureType.GATE_FAILURE,
            error_message="Code review gate failed",
            gate_failures={
                "tests_passed": "npm test failed",
                "code_review_approved": "MAJOR findings",
            },
        )

        assert evidence.gate_failures is not None
        assert "tests_passed" in evidence.gate_failures

    def test_analyze_failure_compilation(self) -> None:
        loop = FailureIterationLoop()

        evidence = FailureEvidence(
            feature_id="WIZE-001",
            failure_type=FailureType.COMPILATION,
            error_message="TypeError: Cannot read property of undefined",
        )

        analysis = loop.analyze_failure(evidence)

        assert "syntax" in analysis.lower() or "type" in analysis.lower()

    def test_analyze_failure_tests(self) -> None:
        loop = FailureIterationLoop()

        evidence = FailureEvidence(
            feature_id="WIZE-001",
            failure_type=FailureType.UNIT_TESTS,
            error_message="Tests failed",
            failed_tests=["test_toggle", "test_persist"],
        )

        analysis = loop.analyze_failure(evidence)

        assert "test" in analysis.lower()

    def test_analyze_failure_security(self) -> None:
        loop = FailureIterationLoop()

        evidence = FailureEvidence(
            feature_id="WIZE-001",
            failure_type=FailureType.SECURITY,
            error_message="Secret detected",
        )

        analysis = loop.analyze_failure(evidence)

        assert "secret" in analysis.lower() or "security" in analysis.lower()

    def test_analyze_failure_gates(self) -> None:
        loop = FailureIterationLoop()

        evidence = FailureEvidence(
            feature_id="WIZE-001",
            failure_type=FailureType.GATE_FAILURE,
            error_message="Gate failed",
            gate_failures={"tests_passed": "npm test error"},
        )

        analysis = loop.analyze_failure(evidence)

        assert "gate" in analysis.lower() or "test" in analysis.lower()

    def test_analyze_failure_production(self) -> None:
        loop = FailureIterationLoop()

        evidence = FailureEvidence(
            feature_id="WIZE-001",
            failure_type=FailureType.PRODUCTION,
            error_message="High error rate",
            production_metrics={"error_rate": 0.15, "alerts": 5},
        )

        analysis = loop.analyze_failure(evidence)

        assert "production" in analysis.lower() or "error" in analysis.lower()

    def test_build_retry_prompt(self) -> None:
        loop = FailureIterationLoop()

        evidence = FailureEvidence(
            feature_id="WIZE-001",
            failure_type=FailureType.COMPILATION,
            error_message="TypeError in toggle handler",
            changed_files=["src/theme.tsx"],
        )

        analysis = "Check null checks, add type guards"

        prompt = loop.build_retry_prompt(
            feature_id="WIZE-001",
            original_request="Add dark mode toggle",
            attempt_number=2,
            evidence=evidence,
            analysis=analysis,
        )

        assert "RETRY ATTEMPT 2" in prompt
        assert "WIZE-001" in prompt
        assert "Add dark mode toggle" in prompt
        assert "compilation" in prompt
        assert "Check null checks" in prompt

    def test_add_iteration_success(self) -> None:
        loop = FailureIterationLoop()

        attempt = loop.add_iteration(
            attempt_number=1,
            feature_id="WIZE-001",
            session_id="session-123",
            is_success=True,
        )

        assert len(loop.iterations) == 1
        assert attempt.is_success is True
        assert loop.iterations[0] == attempt

    def test_add_iteration_failure(self) -> None:
        loop = FailureIterationLoop(max_iterations=3)

        evidence = FailureEvidence(
            feature_id="WIZE-001",
            failure_type=FailureType.UNIT_TESTS,
            error_message="Test failed",
        )

        attempt = loop.add_iteration(
            attempt_number=1,
            feature_id="WIZE-001",
            session_id="session-123",
            is_success=False,
            failure_evidence=evidence,
        )

        assert len(loop.iterations) == 1
        assert attempt.is_success is False

    def test_should_retry_after_success(self) -> None:
        loop = FailureIterationLoop()

        loop.add_iteration(
            attempt_number=1,
            feature_id="WIZE-001",
            session_id="session-123",
            is_success=True,
        )

        # Don't retry if already successful
        assert loop.should_retry() is False

    def test_should_retry_after_failure(self) -> None:
        loop = FailureIterationLoop(max_iterations=3)

        evidence = FailureEvidence(
            feature_id="WIZE-001",
            failure_type=FailureType.UNIT_TESTS,
            error_message="Test failed",
        )

        loop.add_iteration(
            attempt_number=1,
            feature_id="WIZE-001",
            session_id="session-123",
            is_success=False,
            failure_evidence=evidence,
        )

        # Can retry if within limits
        assert loop.should_retry() is True

    def test_should_retry_terminal_failure(self) -> None:
        loop = FailureIterationLoop(max_iterations=3)

        evidence = FailureEvidence(
            feature_id="WIZE-001",
            failure_type=FailureType.PRODUCTION,
            error_message="Production down",
        )

        loop.add_iteration(
            attempt_number=1,
            feature_id="WIZE-001",
            is_success=False,
            failure_evidence=evidence,
            is_terminal_failure=True,
        )

        # Don't retry on terminal failure
        assert loop.should_retry() is False

    def test_should_retry_max_iterations(self) -> None:
        loop = FailureIterationLoop(max_iterations=2)

        evidence = FailureEvidence(
            feature_id="WIZE-001",
            failure_type=FailureType.UNIT_TESTS,
            error_message="Test failed",
        )

        # Attempt 1
        loop.add_iteration(
            attempt_number=1,
            feature_id="WIZE-001",
            is_success=False,
            failure_evidence=evidence,
        )

        # Should retry (attempt 1 < max 2)
        assert loop.should_retry() is True

        # Attempt 2
        loop.add_iteration(
            attempt_number=2,
            feature_id="WIZE-001",
            is_success=False,
            failure_evidence=evidence,
        )

        # Should NOT retry (attempt 2 >= max 2)
        assert loop.should_retry() is False

    def test_build_summary_single_attempt_success(self) -> None:
        loop = FailureIterationLoop()

        loop.add_iteration(
            attempt_number=1,
            feature_id="WIZE-001",
            session_id="session-123",
            is_success=True,
        )

        summary = loop.build_summary()

        assert "Iteration Summary" in summary
        assert "Attempt 1" in summary
        assert "✓" in summary
        assert "succeeded" in summary.lower()

    def test_build_summary_multiple_failures_then_success(self) -> None:
        loop = FailureIterationLoop(max_iterations=3)

        # Attempt 1: Fail
        evidence1 = FailureEvidence(
            feature_id="WIZE-001",
            failure_type=FailureType.COMPILATION,
            error_message="Syntax error",
        )
        loop.add_iteration(
            attempt_number=1,
            feature_id="WIZE-001",
            session_id="session-1",
            is_success=False,
            failure_evidence=evidence1,
            approach_change="Fix syntax",
        )

        # Attempt 2: Fail
        evidence2 = FailureEvidence(
            feature_id="WIZE-001",
            failure_type=FailureType.UNIT_TESTS,
            error_message="Test failed",
            failed_tests=["test_1"],
        )
        loop.add_iteration(
            attempt_number=2,
            feature_id="WIZE-001",
            session_id="session-2",
            is_success=False,
            failure_evidence=evidence2,
            approach_change="Fix test logic",
        )

        # Attempt 3: Success
        loop.add_iteration(
            attempt_number=3,
            feature_id="WIZE-001",
            session_id="session-3",
            is_success=True,
            approach_change="Add defensive checks",
        )

        summary = loop.build_summary()

        assert "3 attempts" in summary
        assert "Attempt 1" in summary
        assert "Attempt 2" in summary
        assert "Attempt 3" in summary
        assert "succeeded" in summary.lower()

    def test_build_summary_max_iterations_reached(self) -> None:
        loop = FailureIterationLoop(max_iterations=2)

        evidence = FailureEvidence(
            feature_id="WIZE-001",
            failure_type=FailureType.UNIT_TESTS,
            error_message="Tests failed",
        )

        loop.add_iteration(
            attempt_number=1,
            feature_id="WIZE-001",
            is_success=False,
            failure_evidence=evidence,
        )

        loop.add_iteration(
            attempt_number=2,
            feature_id="WIZE-001",
            is_success=False,
            failure_evidence=evidence,
        )

        summary = loop.build_summary()

        assert "2 attempts" in summary
        assert "Max iterations reached" in summary


class TestIterationLoopFlow:
    """End-to-end iteration flow."""

    def test_complete_multi_attempt_flow(self) -> None:
        """Feature fails, retry with new approach, eventually succeeds."""
        loop = FailureIterationLoop(max_iterations=3)

        # Attempt 1: Compilation error
        evidence1 = loop.collect_failure_evidence(
            feature_id="WIZE-001",
            failure_type=FailureType.COMPILATION,
            error_message="Cannot read property of undefined",
            error_traceback="line 42 in toggle handler",
        )
        analysis1 = loop.analyze_failure(evidence1)
        loop.add_iteration(
            attempt_number=1,
            feature_id="WIZE-001",
            session_id="session-1",
            is_success=False,
            failure_evidence=evidence1,
            approach_change=analysis1,
        )

        # Should retry
        assert loop.should_retry() is True

        # Build retry prompt
        prompt = loop.build_retry_prompt(
            feature_id="WIZE-001",
            original_request="Add dark mode toggle",
            attempt_number=2,
            evidence=evidence1,
            analysis=analysis1,
        )
        assert "RETRY ATTEMPT 2" in prompt

        # Attempt 2: Test failures
        evidence2 = loop.collect_failure_evidence(
            feature_id="WIZE-001",
            failure_type=FailureType.UNIT_TESTS,
            error_message="2 tests failed",
            failed_tests=["test_toggle", "test_persist"],
        )
        analysis2 = loop.analyze_failure(evidence2)
        loop.add_iteration(
            attempt_number=2,
            feature_id="WIZE-001",
            session_id="session-2",
            is_success=False,
            failure_evidence=evidence2,
            approach_change=analysis2,
        )

        # Should retry again
        assert loop.should_retry() is True

        # Attempt 3: Success
        loop.add_iteration(
            attempt_number=3,
            feature_id="WIZE-001",
            session_id="session-3",
            is_success=True,
            approach_change="Added null checks and persistence logic",
        )

        # Should not retry (succeeded)
        assert loop.should_retry() is False

        # Summary
        summary = loop.build_summary()
        assert "succeeded" in summary.lower()
        assert "3 attempts" in summary
