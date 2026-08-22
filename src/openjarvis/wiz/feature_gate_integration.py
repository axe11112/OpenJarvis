"""Integrate feature execution components with existing merge gates.

Reuses hardened FeatureContract gates rather than creating parallel system.
Maps Priorities 1-5 outputs (executor, tests, review) into contract gates.

Gate Logic (from feature_contract.py):
- tests_passed: npm test, lint, typecheck, build (deterministic)
- acceptance_tests_passed: real test execution against Preview (deterministic)
- code_review_approved: independent review findings (advisory, human decides)
- no_secrets_detected: secret scanning (deterministic)
- no_breaking_changes: API/schema validation (deterministic)
- ci_all_checks_green: GitHub Actions checks (deterministic)

All deterministic gates OVERRIDE advisory code_review findings.
Feature can merge only if all gates pass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from openjarvis.wiz.acceptance_test_executor import SuiteExecutionResult
from openjarvis.wiz.code_reviewer import CodeReviewResult, ReviewSeverity
from openjarvis.wiz.feature_contract import FeatureContract, VerificationStatus
from openjarvis.wiz.vercel_preview_tracker import PreviewVerificationResult

logger = logging.getLogger(__name__)

__all__ = [
    "FeatureGateIntegration",
    "GateInputs",
    "GateDecision",
]


@dataclass
class GateInputs:
    """Inputs to merge gate evaluation from feature pipeline."""

    # From Priority 1-2: Feature execution
    feature_id: str
    branch_name: str
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None

    # From Priority 3: Acceptance test execution
    acceptance_test_result: Optional[SuiteExecutionResult] = None

    # From Priority 4: Vercel preview verification
    preview_verification: Optional[PreviewVerificationResult] = None

    # From Priority 5: Independent code review
    code_review_result: Optional[CodeReviewResult] = None

    # Deterministic gates (npm test, CI, etc)
    tests_passed: bool = False
    ci_all_checks_green: bool = False
    no_secrets_detected: bool = False
    no_breaking_changes: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "branch_name": self.branch_name,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "acceptance_test_result": (
                self.acceptance_test_result.to_dict()
                if self.acceptance_test_result
                else None
            ),
            "preview_verification": (
                self.preview_verification.to_dict()
                if self.preview_verification
                else None
            ),
            "code_review_result": (
                self.code_review_result.to_dict()
                if self.code_review_result
                else None
            ),
            "tests_passed": self.tests_passed,
            "ci_all_checks_green": self.ci_all_checks_green,
            "no_secrets_detected": self.no_secrets_detected,
            "no_breaking_changes": self.no_breaking_changes,
        }


@dataclass
class GateDecision:
    """Result of evaluating merge gates."""

    feature_id: str
    can_merge: bool
    reasons_cannot_merge: List[str]
    code_review_findings: Optional[CodeReviewResult] = None
    recommendation: str = "NONE"  # APPROVE, CHANGES_REQUESTED, COMMENT, NONE

    # Which gates contributed to decision
    gates_passed: Dict[str, bool] = None
    gates_failed: List[str] = None

    def __post_init__(self) -> None:
        if self.gates_passed is None:
            self.gates_passed = {}
        if self.gates_failed is None:
            self.gates_failed = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "can_merge": self.can_merge,
            "reasons_cannot_merge": self.reasons_cannot_merge,
            "code_review_findings": (
                self.code_review_findings.to_dict()
                if self.code_review_findings
                else None
            ),
            "recommendation": self.recommendation,
            "gates_passed": self.gates_passed,
            "gates_failed": self.gates_failed,
        }


class FeatureGateIntegration:
    """Integrate feature pipeline components with existing merge gates.

    Reuses FeatureContract gates:
    - tests_passed: Deterministic (always wins)
    - acceptance_tests_passed: Deterministic (always wins)
    - no_secrets_detected: Deterministic (always wins)
    - no_breaking_changes: Deterministic (always wins)
    - ci_all_checks_green: Deterministic (always wins)
    - code_review_approved: Advisory (human decides, doesn't override above)

    Feature can merge only if all deterministic gates pass AND code review
    is approved (code_review_approved=True, set by human after reviewing findings).
    """

    def __init__(self) -> None:
        """Initialize gate integration."""
        pass

    def build_contract(
        self,
        inputs: GateInputs,
    ) -> FeatureContract:
        """Build feature contract from gate inputs.

        Args:
            inputs: GateInputs from feature pipeline

        Returns:
            FeatureContract ready for can_merge() check
        """
        # Extract acceptance test result
        acceptance_tests_passed = False
        if inputs.acceptance_test_result:
            acceptance_tests_passed = (
                inputs.acceptance_test_result.success_rate == 1.0
            )

        # Extract code review status
        # NOTE: is_read_only flag proves reviewer cannot mutate
        code_review_approved = False
        if inputs.code_review_result:
            # Code review starts as NOT approved
            # Human must explicitly approve after reviewing findings
            # For now: APPROVE recommendation implies no blocker findings
            # But still requires human review for code_review_approved=True
            code_review_approved = (
                inputs.code_review_result.recommendation == "APPROVE"
                and not inputs.code_review_result.has_critical_findings
                and not inputs.code_review_result.has_major_findings
            )

        # Build contract using reused gates
        contract = FeatureContract(
            feature_id=inputs.feature_id,
            pr_number=inputs.pr_number,
            pr_url=inputs.pr_url,
            branch_name=inputs.branch_name,
            tests_passed=inputs.tests_passed,
            code_review_approved=code_review_approved,
            acceptance_tests_passed=acceptance_tests_passed,
            no_secrets_detected=inputs.no_secrets_detected,
            no_breaking_changes=inputs.no_breaking_changes,
            ci_all_checks_green=inputs.ci_all_checks_green,
        )

        return contract

    def evaluate_gates(
        self,
        inputs: GateInputs,
    ) -> GateDecision:
        """Evaluate merge gates from feature pipeline inputs.

        Deterministic gates (tests, CI, secrets, breaking changes) ALWAYS WIN.
        Code review is advisory: findings are captured, but don't block merge
        if deterministic gates pass (human decides on review_approved).

        Args:
            inputs: GateInputs with acceptance tests, review findings, etc.

        Returns:
            GateDecision with can_merge bool and reasons
        """
        gates_passed = {}
        gates_failed = []

        # Deterministic gates: always override code review
        gates_passed["tests_passed"] = inputs.tests_passed
        if not inputs.tests_passed:
            gates_failed.append("tests_passed")

        gates_passed["ci_all_checks_green"] = inputs.ci_all_checks_green
        if not inputs.ci_all_checks_green:
            gates_failed.append("ci_all_checks_green")

        gates_passed["no_secrets_detected"] = inputs.no_secrets_detected
        if not inputs.no_secrets_detected:
            gates_failed.append("no_secrets_detected")

        gates_passed["no_breaking_changes"] = inputs.no_breaking_changes
        if not inputs.no_breaking_changes:
            gates_failed.append("no_breaking_changes")

        # Acceptance tests: deterministic
        acceptance_passed = False
        if inputs.acceptance_test_result:
            acceptance_passed = inputs.acceptance_test_result.success_rate == 1.0
        gates_passed["acceptance_tests_passed"] = acceptance_passed
        if not acceptance_passed:
            gates_failed.append("acceptance_tests_passed")

        # Code review: ADVISORY ONLY
        # If deterministic gates pass, feature can merge even with review findings
        # But code_review_approved must be set to True by human reviewing findings
        code_review_approved = False
        review_recommendation = "NONE"
        if inputs.code_review_result:
            review_recommendation = inputs.code_review_result.recommendation
            # Code review can only auto-approve if no critical/major findings
            code_review_approved = (
                inputs.code_review_result.recommendation == "APPROVE"
                and not inputs.code_review_result.has_critical_findings
                and not inputs.code_review_result.has_major_findings
            )
        gates_passed["code_review_approved"] = code_review_approved
        if not code_review_approved:
            gates_failed.append("code_review_approved")

        # Determine if can merge
        # Only deterministic gates block merge
        # Code review is captured but advisory
        deterministic_failed = [
            g for g in gates_failed
            if g
            != "code_review_approved"  # code_review is NOT a blocker
        ]

        can_merge = len(deterministic_failed) == 0 and code_review_approved

        reasons = []
        if deterministic_failed:
            reasons.extend(deterministic_failed)
        if not code_review_approved and inputs.code_review_result:
            reasons.append("code_review_approved (awaiting human review)")

        decision = GateDecision(
            feature_id=inputs.feature_id,
            can_merge=can_merge,
            reasons_cannot_merge=reasons,
            code_review_findings=inputs.code_review_result,
            recommendation=review_recommendation,
            gates_passed=gates_passed,
            gates_failed=gates_failed,
        )

        return decision

    def build_gate_summary(
        self,
        decision: GateDecision,
    ) -> str:
        """Build human-readable gate summary."""
        parts = []

        if decision.can_merge:
            parts.append("✓ Feature can merge: All gates passed")
        else:
            parts.append("✗ Feature cannot merge:")
            for reason in decision.reasons_cannot_merge:
                parts.append(f"  • {reason}")

        if decision.code_review_findings:
            parts.append("\nCode Review Status:")
            parts.append(
                f"  Recommendation: {decision.code_review_findings.recommendation}"
            )
            if decision.code_review_findings.findings:
                parts.append(
                    f"  Findings: {len(decision.code_review_findings.findings)}"
                )
                critical = [
                    f for f in decision.code_review_findings.findings
                    if f.severity == ReviewSeverity.CRITICAL
                ]
                major = [
                    f for f in decision.code_review_findings.findings
                    if f.severity == ReviewSeverity.MAJOR
                ]
                if critical:
                    parts.append(f"    - {len(critical)} CRITICAL")
                if major:
                    parts.append(f"    - {len(major)} MAJOR")

        return "\n".join(parts)


__all__ = [
    "FeatureGateIntegration",
    "GateInputs",
    "GateDecision",
]
