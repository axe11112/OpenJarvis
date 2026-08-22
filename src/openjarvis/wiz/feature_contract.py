"""Feature delivery contract: PR → Merge → Production.

Defines the gates and verification required before a feature can merge and
be deployed to production. This is Wiz's answer to: "Is it safe to merge?"
and "Is it safe to deploy?"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["VerificationStatus", "FeatureContract", "ProductionVerification"]


class VerificationStatus(str, Enum):
    """Status of feature verification."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class FeatureContract:
    """Contract for a feature PR: what must be true to merge.

    A feature can only merge if all required verifications pass.
    """

    feature_id: str

    # PR requirements
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    branch_name: Optional[str] = None

    # Verification gates
    tests_passed: bool = False
    code_review_approved: bool = False
    acceptance_tests_passed: bool = False
    no_secrets_detected: bool = False
    no_breaking_changes: bool = False
    ci_all_checks_green: bool = False

    # Audit trail
    verified_at: Optional[str] = None
    verified_by: Optional[str] = None

    def can_merge(self) -> tuple[bool, List[str]]:
        """Check if feature can merge.

        Returns (can_merge, reasons_why_not).
        """
        required = {
            "tests_passed": self.tests_passed,
            "code_review_approved": self.code_review_approved,
            "acceptance_tests_passed": self.acceptance_tests_passed,
            "no_secrets_detected": self.no_secrets_detected,
            "ci_all_checks_green": self.ci_all_checks_green,
        }

        failed = [k for k, v in required.items() if not v]
        return (len(failed) == 0, failed)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "branch_name": self.branch_name,
            "tests_passed": self.tests_passed,
            "code_review_approved": self.code_review_approved,
            "acceptance_tests_passed": self.acceptance_tests_passed,
            "no_secrets_detected": self.no_secrets_detected,
            "no_breaking_changes": self.no_breaking_changes,
            "ci_all_checks_green": self.ci_all_checks_green,
            "verified_at": self.verified_at,
            "verified_by": self.verified_by,
        }


@dataclass
class ProductionVerification:
    """Verification that a feature works in production.

    After a feature merges and deploys, this tracks that it's working
    correctly and doesn't need a rollback.
    """

    feature_id: str
    status: VerificationStatus = VerificationStatus.NOT_STARTED

    # Deployment tracking
    deployed_at: Optional[str] = None
    deployment_hash: Optional[str] = None

    # Production health
    error_rate_acceptable: bool = False
    latency_acceptable: bool = False
    user_reports: int = 0  # count of user-reported issues

    # Monitoring
    alerts_triggered: int = 0
    recovery_needed: bool = False

    # Final verdict
    production_ready: bool = False
    verified_at: Optional[str] = None

    def is_healthy(self) -> bool:
        """Is the feature healthy in production?"""
        return (
            self.status == VerificationStatus.PASSED
            and self.error_rate_acceptable
            and self.latency_acceptable
            and self.user_reports == 0
        )

    def needs_rollback(self) -> bool:
        """Should this feature be rolled back?"""
        return (
            self.status == VerificationStatus.FAILED
            or self.recovery_needed
            or (self.user_reports > 0 and self.alerts_triggered > 2)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "status": self.status.value,
            "deployed_at": self.deployed_at,
            "deployment_hash": self.deployment_hash,
            "error_rate_acceptable": self.error_rate_acceptable,
            "latency_acceptable": self.latency_acceptable,
            "user_reports": self.user_reports,
            "alerts_triggered": self.alerts_triggered,
            "recovery_needed": self.recovery_needed,
            "production_ready": self.production_ready,
            "verified_at": self.verified_at,
        }


class FeatureContractValidator:
    """Validate that feature contracts are met before merge/deploy."""

    @staticmethod
    def validate_contract(contract: FeatureContract) -> tuple[bool, List[str]]:
        """Validate a feature contract.

        Returns (is_valid, error_messages).
        """
        errors = []

        # Check required fields
        if not contract.feature_id:
            errors.append("feature_id is required")

        if not contract.pr_number:
            errors.append("pr_number must be set")

        if not contract.branch_name:
            errors.append("branch_name must be set")

        # Check gates
        can_merge, reasons = contract.can_merge()
        if not can_merge:
            errors.extend(f"gate_not_met: {r}" for r in reasons)

        return (len(errors) == 0, errors)

    @staticmethod
    def validate_production_verification(
        verification: ProductionVerification,
    ) -> tuple[bool, List[str]]:
        """Validate production verification.

        Returns (is_valid, error_messages).
        """
        errors = []

        if not verification.feature_id:
            errors.append("feature_id is required")

        if verification.needs_rollback():
            errors.append("feature needs rollback")

        if not verification.is_healthy():
            errors.append("feature is not healthy in production")

        return (len(errors) == 0, errors)
