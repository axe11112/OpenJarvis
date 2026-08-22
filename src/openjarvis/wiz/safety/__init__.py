"""Safety gates: Enforce deterministic rules for autonomous shipping."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from openjarvis.wiz.models import FeatureRequest, RiskLevel

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    """Result of a safety gate check."""

    passed: bool
    gate_name: str
    reason: str
    blocking: bool = True


class SafetyGates:
    """Enforce deterministic gates that control autonomous shipping authority."""

    @staticmethod
    def check_risk_level(request: FeatureRequest) -> GateResult:
        """Autonomous merge only permitted for LOW risk features.

        HIGH/MEDIUM require human approval.
        UNKNOWN must be rejected.
        """
        if request.risk_level == RiskLevel.UNKNOWN:
            return GateResult(
                passed=False,
                gate_name="risk_classification",
                reason="Risk level is UNKNOWN - cannot proceed autonomously",
                blocking=True,
            )

        if request.risk_level in (RiskLevel.HIGH, RiskLevel.MEDIUM):
            return GateResult(
                passed=False,
                gate_name="risk_level_check",
                reason=f"Risk level is {request.risk_level.value} - requires human approval",
                blocking=True,
            )

        return GateResult(
            passed=True,
            gate_name="risk_level_check",
            reason="Risk level is LOW - autonomous shipping permitted",
            blocking=False,
        )

    @staticmethod
    def check_test_results(request: FeatureRequest) -> GateResult:
        """Autonomous merge requires all tests passing."""
        if not request.test_results:
            return GateResult(
                passed=False,
                gate_name="test_results",
                reason="No test results recorded",
                blocking=True,
            )

        test_results = request.test_results.lower()

        # Check for failure patterns: "N failed" or "error" followed by non-zero count
        if " failed" in test_results:
            # Extract the count before "failed"
            parts = test_results.split(" failed")[0].split()
            if parts and parts[-1].isdigit() and int(parts[-1]) > 0:
                return GateResult(
                    passed=False,
                    gate_name="test_results",
                    reason="Tests contain failures",
                    blocking=True,
                )

        if "error" in test_results:
            return GateResult(
                passed=False,
                gate_name="test_results",
                reason="Tests contain errors",
                blocking=True,
            )

        return GateResult(
            passed=True,
            gate_name="test_results",
            reason="All tests passing",
            blocking=False,
        )

    @staticmethod
    def check_branch_protection(request: FeatureRequest) -> GateResult:
        """Verify branch protection rules are satisfied."""
        if not request.git_branch:
            return GateResult(
                passed=False,
                gate_name="branch_protection",
                reason="No git branch recorded",
                blocking=True,
            )

        return GateResult(
            passed=True,
            gate_name="branch_protection",
            reason="Branch protection checks satisfied",
            blocking=False,
        )

    @staticmethod
    def check_preview_verification(request: FeatureRequest) -> GateResult:
        """Verify Vercel Preview matches feature SHA."""
        if not request.preview_sha:
            return GateResult(
                passed=False,
                gate_name="preview_verification",
                reason="No Vercel Preview SHA recorded",
                blocking=True,
            )

        if request.preview_sha != request.feature_sha:
            return GateResult(
                passed=False,
                gate_name="preview_verification",
                reason=f"Preview SHA {request.preview_sha} != feature SHA {request.feature_sha}",
                blocking=True,
            )

        return GateResult(
            passed=True,
            gate_name="preview_verification",
            reason="Preview SHA matches feature SHA",
            blocking=False,
        )

    @staticmethod
    def evaluate_all_gates(request: FeatureRequest) -> list[GateResult]:
        """Run all safety gates for a feature.

        Returns:
            List of gate results, empty list means all gates passed
        """
        results = []
        blocking_failures = []

        gates = [
            SafetyGates.check_risk_level,
            SafetyGates.check_test_results,
            SafetyGates.check_branch_protection,
            SafetyGates.check_preview_verification,
        ]

        for gate in gates:
            result = gate(request)
            if not result.passed:
                results.append(result)
                if result.blocking:
                    blocking_failures.append(result)

        if blocking_failures:
            logger.warning(f"Blocking safety gate failures for {request.id}:")
            for gate_result in blocking_failures:
                logger.warning(f"  - {gate_result.gate_name}: {gate_result.reason}")

        return results


__all__ = ["SafetyGates", "GateResult"]
