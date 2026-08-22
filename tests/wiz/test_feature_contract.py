"""Feature contract and production verification tests."""

from __future__ import annotations

import pytest

from openjarvis.wiz.feature_contract import (
    FeatureContract,
    FeatureContractValidator,
    ProductionVerification,
    VerificationStatus,
)


class TestFeatureContract:
    """Feature PR contract."""

    def test_create_contract(self) -> None:
        contract = FeatureContract(feature_id="FEAT-001")
        assert contract.feature_id == "FEAT-001"
        assert not contract.tests_passed

    def test_cannot_merge_without_gates(self) -> None:
        contract = FeatureContract(feature_id="FEAT-001")
        can_merge, reasons = contract.can_merge()
        assert not can_merge
        assert len(reasons) > 0

    def test_can_merge_with_all_gates_passed(self) -> None:
        contract = FeatureContract(
            feature_id="FEAT-001",
            tests_passed=True,
            code_review_approved=True,
            acceptance_tests_passed=True,
            no_secrets_detected=True,
            ci_all_checks_green=True,
        )
        can_merge, reasons = contract.can_merge()
        assert can_merge
        assert len(reasons) == 0

    def test_contract_to_dict(self) -> None:
        contract = FeatureContract(
            feature_id="FEAT-001",
            pr_number=42,
            tests_passed=True,
        )
        d = contract.to_dict()
        assert d["feature_id"] == "FEAT-001"
        assert d["pr_number"] == 42
        assert d["tests_passed"] is True

    def test_each_failed_gate_is_reported(self) -> None:
        contract = FeatureContract(
            feature_id="FEAT-001",
            tests_passed=False,
            code_review_approved=False,
            acceptance_tests_passed=True,
            no_secrets_detected=True,
            ci_all_checks_green=True,
        )
        can_merge, reasons = contract.can_merge()
        assert not can_merge
        assert "tests_passed" in reasons
        assert "code_review_approved" in reasons


class TestProductionVerification:
    """Production verification."""

    def test_create_verification(self) -> None:
        ver = ProductionVerification(feature_id="FEAT-001")
        assert ver.feature_id == "FEAT-001"
        assert ver.status == VerificationStatus.NOT_STARTED

    def test_is_healthy_when_all_good(self) -> None:
        ver = ProductionVerification(
            feature_id="FEAT-001",
            status=VerificationStatus.PASSED,
            error_rate_acceptable=True,
            latency_acceptable=True,
            user_reports=0,
        )
        assert ver.is_healthy()

    def test_not_healthy_when_errors_high(self) -> None:
        ver = ProductionVerification(
            feature_id="FEAT-001",
            status=VerificationStatus.PASSED,
            error_rate_acceptable=False,
            latency_acceptable=True,
            user_reports=0,
        )
        assert not ver.is_healthy()

    def test_needs_rollback_on_failure(self) -> None:
        ver = ProductionVerification(
            feature_id="FEAT-001",
            status=VerificationStatus.FAILED,
        )
        assert ver.needs_rollback()

    def test_needs_rollback_on_recovery_needed(self) -> None:
        ver = ProductionVerification(
            feature_id="FEAT-001",
            recovery_needed=True,
        )
        assert ver.needs_rollback()

    def test_needs_rollback_on_many_reports(self) -> None:
        ver = ProductionVerification(
            feature_id="FEAT-001",
            user_reports=5,
            alerts_triggered=3,
        )
        assert ver.needs_rollback()

    def test_verification_to_dict(self) -> None:
        ver = ProductionVerification(
            feature_id="FEAT-001",
            status=VerificationStatus.PASSED,
            user_reports=2,
        )
        d = ver.to_dict()
        assert d["feature_id"] == "FEAT-001"
        assert d["status"] == "passed"
        assert d["user_reports"] == 2


class TestFeatureContractValidator:
    """Contract validation."""

    def test_valid_contract(self) -> None:
        contract = FeatureContract(
            feature_id="FEAT-001",
            pr_number=42,
            branch_name="wiz/feat-001",
            tests_passed=True,
            code_review_approved=True,
            acceptance_tests_passed=True,
            no_secrets_detected=True,
            ci_all_checks_green=True,
        )
        is_valid, errors = FeatureContractValidator.validate_contract(contract)
        assert is_valid
        assert len(errors) == 0

    def test_invalid_contract_missing_pr_number(self) -> None:
        contract = FeatureContract(
            feature_id="FEAT-001",
            branch_name="wiz/feat-001",
        )
        is_valid, errors = FeatureContractValidator.validate_contract(contract)
        assert not is_valid
        assert any("pr_number" in e for e in errors)

    def test_invalid_contract_gate_not_met(self) -> None:
        contract = FeatureContract(
            feature_id="FEAT-001",
            pr_number=42,
            branch_name="wiz/feat-001",
            tests_passed=False,  # This fails!
            code_review_approved=True,
            acceptance_tests_passed=True,
            no_secrets_detected=True,
            ci_all_checks_green=True,
        )
        is_valid, errors = FeatureContractValidator.validate_contract(contract)
        assert not is_valid
        assert any("gate_not_met" in e for e in errors)

    def test_valid_production_verification(self) -> None:
        ver = ProductionVerification(
            feature_id="FEAT-001",
            status=VerificationStatus.PASSED,
            error_rate_acceptable=True,
            latency_acceptable=True,
            user_reports=0,
            production_ready=True,
        )
        is_valid, errors = FeatureContractValidator.validate_production_verification(ver)
        assert is_valid
        assert len(errors) == 0

    def test_invalid_verification_needs_rollback(self) -> None:
        ver = ProductionVerification(
            feature_id="FEAT-001",
            recovery_needed=True,
        )
        is_valid, errors = FeatureContractValidator.validate_production_verification(ver)
        assert not is_valid
        assert any("rollback" in e.lower() for e in errors)

    def test_invalid_verification_not_healthy(self) -> None:
        ver = ProductionVerification(
            feature_id="FEAT-001",
            status=VerificationStatus.PASSED,
            error_rate_acceptable=False,
            latency_acceptable=True,
            user_reports=0,
        )
        is_valid, errors = FeatureContractValidator.validate_production_verification(ver)
        assert not is_valid


class TestVerificationStatus:
    """Verification status enum."""

    def test_not_started(self) -> None:
        assert VerificationStatus.NOT_STARTED.value == "not_started"

    def test_passed(self) -> None:
        assert VerificationStatus.PASSED.value == "passed"

    def test_failed(self) -> None:
        assert VerificationStatus.FAILED.value == "failed"
