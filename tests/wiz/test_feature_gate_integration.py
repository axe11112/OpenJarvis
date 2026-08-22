"""Tests for feature gate integration with existing merge gates."""

from __future__ import annotations

import pytest

from openjarvis.wiz.acceptance_test_executor import SuiteExecutionResult
from openjarvis.wiz.code_reviewer import (
    CodeReviewResult,
    ReviewFinding,
    ReviewSeverity,
)
from openjarvis.wiz.feature_gate_integration import (
    FeatureGateIntegration,
    GateDecision,
    GateInputs,
)
from openjarvis.wiz.vercel_preview_tracker import PreviewVerificationResult


class TestGateInputs:
    """GateInputs dataclass."""

    def test_create_minimal_gate_inputs(self) -> None:
        inputs = GateInputs(
            feature_id="WIZE-PILOT-001",
            branch_name="wiz/wize-pilot-001",
        )
        assert inputs.feature_id == "WIZE-PILOT-001"
        assert inputs.branch_name == "wiz/wize-pilot-001"
        assert inputs.pr_number is None
        assert inputs.pr_url is None

    def test_create_gate_inputs_with_all_data(self) -> None:
        test_result = SuiteExecutionResult(
            total_tests=3,
            passed_tests=3,
            failed_tests=0,
            skipped_tests=0,
            error_tests=0,
            results=[],
        )
        review_result = CodeReviewResult(
            feature_id="WIZE-PILOT-001",
            reviewed_sha="abc123",
            findings=[],
            recommendation="APPROVE",
            summary="OK",
            is_read_only=True,
        )
        inputs = GateInputs(
            feature_id="WIZE-PILOT-001",
            branch_name="wiz/wize-pilot-001",
            pr_number=42,
            pr_url="https://github.com/owner/repo/pull/42",
            acceptance_test_result=test_result,
            code_review_result=review_result,
            tests_passed=True,
            ci_all_checks_green=True,
            no_secrets_detected=True,
            no_breaking_changes=True,
        )
        assert inputs.pr_number == 42
        assert inputs.tests_passed is True
        assert inputs.ci_all_checks_green is True

    def test_gate_inputs_to_dict(self) -> None:
        inputs = GateInputs(
            feature_id="WIZE-001",
            branch_name="wiz/wize-001",
            pr_number=1,
            tests_passed=True,
            ci_all_checks_green=True,
        )
        d = inputs.to_dict()
        assert d["feature_id"] == "WIZE-001"
        assert d["tests_passed"] is True
        assert d["ci_all_checks_green"] is True


class TestGateDecision:
    """GateDecision dataclass."""

    def test_create_merge_approved(self) -> None:
        decision = GateDecision(
            feature_id="WIZE-001",
            can_merge=True,
            reasons_cannot_merge=[],
        )
        assert decision.can_merge is True
        assert len(decision.reasons_cannot_merge) == 0

    def test_create_merge_rejected(self) -> None:
        decision = GateDecision(
            feature_id="WIZE-001",
            can_merge=False,
            reasons_cannot_merge=["tests_passed", "code_review_approved"],
        )
        assert decision.can_merge is False
        assert "tests_passed" in decision.reasons_cannot_merge

    def test_gate_decision_to_dict(self) -> None:
        decision = GateDecision(
            feature_id="WIZE-001",
            can_merge=True,
            reasons_cannot_merge=[],
            gates_passed={"tests_passed": True, "code_review_approved": True},
        )
        d = decision.to_dict()
        assert d["feature_id"] == "WIZE-001"
        assert d["can_merge"] is True


class TestFeatureGateIntegration:
    """Feature gate integration functionality."""

    def test_initialization(self) -> None:
        integration = FeatureGateIntegration()
        assert integration is not None

    def test_build_contract_minimal(self) -> None:
        integration = FeatureGateIntegration()
        inputs = GateInputs(
            feature_id="WIZE-001",
            branch_name="wiz/wize-001",
            tests_passed=True,
            ci_all_checks_green=True,
            no_secrets_detected=True,
            no_breaking_changes=True,
        )

        contract = integration.build_contract(inputs)
        assert contract.feature_id == "WIZE-001"
        assert contract.branch_name == "wiz/wize-001"
        assert contract.tests_passed is True
        assert contract.ci_all_checks_green is True

    def test_build_contract_with_acceptance_tests(self) -> None:
        integration = FeatureGateIntegration()

        # Perfect test result (all pass)
        test_result = SuiteExecutionResult(
            total_tests=3,
            passed_tests=3,
            failed_tests=0,
            skipped_tests=0,
            error_tests=0,
            results=[],
        )

        inputs = GateInputs(
            feature_id="WIZE-001",
            branch_name="wiz/wize-001",
            acceptance_test_result=test_result,
            tests_passed=True,
            ci_all_checks_green=True,
            no_secrets_detected=True,
            no_breaking_changes=True,
        )

        contract = integration.build_contract(inputs)
        assert contract.acceptance_tests_passed is True

    def test_build_contract_with_failed_acceptance_tests(self) -> None:
        integration = FeatureGateIntegration()

        # Failed tests (success_rate < 1.0)
        test_result = SuiteExecutionResult(
            total_tests=3,
            passed_tests=2,
            failed_tests=1,
            skipped_tests=0,
            error_tests=0,
            results=[],
        )

        inputs = GateInputs(
            feature_id="WIZE-001",
            branch_name="wiz/wize-001",
            acceptance_test_result=test_result,
            tests_passed=True,
            ci_all_checks_green=True,
            no_secrets_detected=True,
            no_breaking_changes=True,
        )

        contract = integration.build_contract(inputs)
        assert contract.acceptance_tests_passed is False

    def test_build_contract_with_approved_review(self) -> None:
        integration = FeatureGateIntegration()

        review_result = CodeReviewResult(
            feature_id="WIZE-001",
            reviewed_sha="abc123",
            findings=[],
            recommendation="APPROVE",
            summary="No issues",
            is_read_only=True,
        )

        inputs = GateInputs(
            feature_id="WIZE-001",
            branch_name="wiz/wize-001",
            code_review_result=review_result,
            tests_passed=True,
            ci_all_checks_green=True,
            no_secrets_detected=True,
            no_breaking_changes=True,
        )

        contract = integration.build_contract(inputs)
        # Auto-approved because APPROVE recommendation + no critical/major
        assert contract.code_review_approved is True

    def test_build_contract_with_critical_review_findings(self) -> None:
        integration = FeatureGateIntegration()

        review_result = CodeReviewResult(
            feature_id="WIZE-001",
            reviewed_sha="abc123",
            findings=[
                ReviewFinding(
                    severity=ReviewSeverity.CRITICAL,
                    category="security",
                    title="Secret in code",
                    description="API key exposed",
                )
            ],
            recommendation="CHANGES_REQUESTED",
            summary="Has critical issue",
            is_read_only=True,
        )

        inputs = GateInputs(
            feature_id="WIZE-001",
            branch_name="wiz/wize-001",
            code_review_result=review_result,
            tests_passed=True,
            ci_all_checks_green=True,
            no_secrets_detected=True,
            no_breaking_changes=True,
        )

        contract = integration.build_contract(inputs)
        # Not auto-approved due to critical findings
        assert contract.code_review_approved is False

    def test_evaluate_gates_all_pass(self) -> None:
        integration = FeatureGateIntegration()

        inputs = GateInputs(
            feature_id="WIZE-001",
            branch_name="wiz/wize-001",
            tests_passed=True,
            ci_all_checks_green=True,
            no_secrets_detected=True,
            no_breaking_changes=True,
            acceptance_test_result=SuiteExecutionResult(
                total_tests=3,
                passed_tests=3,
                failed_tests=0,
                skipped_tests=0,
                error_tests=0,
                results=[],
            ),
        )

        decision = integration.evaluate_gates(inputs)

        # Cannot merge yet - needs code review approval
        assert decision.can_merge is False
        assert "code_review_approved" in decision.reasons_cannot_merge

    def test_evaluate_gates_tests_fail(self) -> None:
        integration = FeatureGateIntegration()

        inputs = GateInputs(
            feature_id="WIZE-001",
            branch_name="wiz/wize-001",
            tests_passed=False,  # FAIL
            ci_all_checks_green=True,
            no_secrets_detected=True,
            no_breaking_changes=True,
        )

        decision = integration.evaluate_gates(inputs)

        # Cannot merge - deterministic gate failed
        assert decision.can_merge is False
        assert "tests_passed" in decision.reasons_cannot_merge

    def test_evaluate_gates_ci_fails(self) -> None:
        integration = FeatureGateIntegration()

        inputs = GateInputs(
            feature_id="WIZE-001",
            branch_name="wiz/wize-001",
            tests_passed=True,
            ci_all_checks_green=False,  # FAIL
            no_secrets_detected=True,
            no_breaking_changes=True,
        )

        decision = integration.evaluate_gates(inputs)

        # Cannot merge - CI failed
        assert decision.can_merge is False
        assert "ci_all_checks_green" in decision.reasons_cannot_merge

    def test_evaluate_gates_secrets_detected(self) -> None:
        integration = FeatureGateIntegration()

        inputs = GateInputs(
            feature_id="WIZE-001",
            branch_name="wiz/wize-001",
            tests_passed=True,
            ci_all_checks_green=True,
            no_secrets_detected=False,  # FAIL - secrets found
            no_breaking_changes=True,
        )

        decision = integration.evaluate_gates(inputs)

        # Cannot merge - secrets detected
        assert decision.can_merge is False
        assert "no_secrets_detected" in decision.reasons_cannot_merge

    def test_evaluate_gates_code_review_advisory_only(self) -> None:
        """Code review findings are advisory, don't block if deterministic gates pass."""
        integration = FeatureGateIntegration()

        # Review has CRITICAL findings (would suggest changes)
        review_result = CodeReviewResult(
            feature_id="WIZE-001",
            reviewed_sha="abc123",
            findings=[
                ReviewFinding(
                    severity=ReviewSeverity.CRITICAL,
                    category="design",
                    title="Architecture question",
                    description="High-level concern",
                )
            ],
            recommendation="CHANGES_REQUESTED",
            summary="Has concern",
            is_read_only=True,
        )

        inputs = GateInputs(
            feature_id="WIZE-001",
            branch_name="wiz/wize-001",
            code_review_result=review_result,
            tests_passed=True,
            ci_all_checks_green=True,
            no_secrets_detected=True,
            no_breaking_changes=True,
            acceptance_test_result=SuiteExecutionResult(
                total_tests=3,
                passed_tests=3,
                failed_tests=0,
                skipped_tests=0,
                error_tests=0,
                results=[],
            ),
        )

        decision = integration.evaluate_gates(inputs)

        # Deterministic gates all pass
        # Code review is advisory: findings captured but don't override gates
        # However, code_review_approved must still be true to merge
        assert decision.can_merge is False  # Awaiting review approval
        assert decision.code_review_findings is not None
        assert "code_review_approved" in decision.reasons_cannot_merge

    def test_evaluate_gates_complete_success(self) -> None:
        """All gates pass, ready to merge."""
        integration = FeatureGateIntegration()

        review_result = CodeReviewResult(
            feature_id="WIZE-001",
            reviewed_sha="abc123",
            findings=[],
            recommendation="APPROVE",
            summary="OK",
            is_read_only=True,
        )

        inputs = GateInputs(
            feature_id="WIZE-001",
            branch_name="wiz/wize-001",
            pr_number=42,
            pr_url="https://github.com/owner/repo/pull/42",
            code_review_result=review_result,
            tests_passed=True,
            ci_all_checks_green=True,
            no_secrets_detected=True,
            no_breaking_changes=True,
            acceptance_test_result=SuiteExecutionResult(
                total_tests=3,
                passed_tests=3,
                failed_tests=0,
                skipped_tests=0,
                error_tests=0,
                results=[],
            ),
        )

        decision = integration.evaluate_gates(inputs)

        # All gates pass
        assert decision.can_merge is True
        assert len(decision.reasons_cannot_merge) == 0
        assert decision.gates_passed.get("tests_passed") is True
        assert decision.gates_passed.get("code_review_approved") is True

    def test_build_gate_summary_merge_approved(self) -> None:
        integration = FeatureGateIntegration()

        decision = GateDecision(
            feature_id="WIZE-001",
            can_merge=True,
            reasons_cannot_merge=[],
            gates_passed={"tests_passed": True, "code_review_approved": True},
        )

        summary = integration.build_gate_summary(decision)
        assert "can merge" in summary.lower()
        assert "All gates passed" in summary

    def test_build_gate_summary_merge_rejected(self) -> None:
        integration = FeatureGateIntegration()

        review_result = CodeReviewResult(
            feature_id="WIZE-001",
            reviewed_sha="abc123",
            findings=[
                ReviewFinding(
                    severity=ReviewSeverity.MAJOR,
                    category="correctness",
                    title="Logic error",
                    description="Condition wrong",
                )
            ],
            recommendation="CHANGES_REQUESTED",
            summary="Has issue",
            is_read_only=True,
        )

        decision = GateDecision(
            feature_id="WIZE-001",
            can_merge=False,
            reasons_cannot_merge=["tests_passed"],
            code_review_findings=review_result,
            recommendation="CHANGES_REQUESTED",
            gates_passed={"tests_passed": False},
        )

        summary = integration.build_gate_summary(decision)
        assert "cannot merge" in summary.lower()
        assert "tests_passed" in summary
        assert "Code Review Status" in summary
        assert "CHANGES_REQUESTED" in summary
        assert "1 MAJOR" in summary


class TestGateIntegrationReusesExisting:
    """Verify integration reuses existing FeatureContract gates, not parallel."""

    def test_gate_integration_uses_feature_contract_gates(self) -> None:
        """Integration builds FeatureContract from inputs, doesn't duplicate gates."""
        integration = FeatureGateIntegration()

        inputs = GateInputs(
            feature_id="WIZE-001",
            branch_name="wiz/wize-001",
            tests_passed=True,
            ci_all_checks_green=True,
            no_secrets_detected=True,
            no_breaking_changes=True,
        )

        # Builds actual FeatureContract, uses its can_merge() method
        contract = integration.build_contract(inputs)

        # Contract has the gates we passed
        assert contract.tests_passed is True
        assert contract.ci_all_checks_green is True
        assert contract.no_secrets_detected is True
        assert contract.no_breaking_changes is True

    def test_deterministic_gates_always_win(self) -> None:
        """Deterministic gates override code review findings (advisory)."""
        integration = FeatureGateIntegration()

        # Even with perfect code review
        review_result = CodeReviewResult(
            feature_id="WIZE-001",
            reviewed_sha="abc123",
            findings=[],
            recommendation="APPROVE",
            summary="OK",
            is_read_only=True,
        )

        # If deterministic gates fail, merge is blocked
        inputs = GateInputs(
            feature_id="WIZE-001",
            branch_name="wiz/wize-001",
            code_review_result=review_result,
            tests_passed=False,  # Deterministic gate FAILS
            ci_all_checks_green=True,
            no_secrets_detected=True,
            no_breaking_changes=True,
        )

        decision = integration.evaluate_gates(inputs)

        # Cannot merge because tests_passed is false (deterministic)
        assert decision.can_merge is False
        assert "tests_passed" in decision.reasons_cannot_merge
