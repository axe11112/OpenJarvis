"""Merge gates: Final checks before autonomously merging features."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from openjarvis.wiz.models import FeatureRequest, RiskLevel
from openjarvis.wiz.review import CodeReview

logger = logging.getLogger(__name__)


class MergeGateStatus(Enum):
    """Result of a merge gate check."""

    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"


@dataclass
class MergeGateResult:
    """Result of evaluating merge gates."""

    feature_id: str
    status: MergeGateStatus
    gates_passed: list[str]
    gates_failed: list[str]
    gates_warning: list[str]

    @property
    def can_merge(self) -> bool:
        """Can merge if all gates passed or only warnings."""
        return self.status in (MergeGateStatus.PASS, MergeGateStatus.WARN)


class MergeGates:
    """Evaluate deterministic merge gates."""

    @staticmethod
    def evaluate(
        request: FeatureRequest,
        review: CodeReview,
    ) -> MergeGateResult:
        """Evaluate all merge gates for a feature.

        Args:
            request: FeatureRequest with all state
            review: CodeReview findings

        Returns:
            MergeGateResult indicating if merge is approved
        """
        result = MergeGateResult(
            feature_id=request.id,
            status=MergeGateStatus.PASS,
            gates_passed=[],
            gates_failed=[],
            gates_warning=[],
        )

        # Gate 1: Risk level
        if request.risk_level == RiskLevel.UNKNOWN:
            result.gates_failed.append("risk_unknown")
            result.status = MergeGateStatus.FAIL
        elif request.risk_level in (RiskLevel.HIGH, RiskLevel.MEDIUM):
            result.gates_warning.append(f"risk_{request.risk_level.value}")
            if result.status == MergeGateStatus.PASS:
                result.status = MergeGateStatus.WARN
        else:
            result.gates_passed.append("risk_low")

        # Gate 2: No blocking review findings
        if review.has_blocking_issues:
            result.gates_failed.append("review_blocking_findings")
            result.status = MergeGateStatus.FAIL
        else:
            result.gates_passed.append("review_no_blocking")

        # Gate 3: Tests passing
        if not request.test_results:
            result.gates_failed.append("tests_not_passing")
            result.status = MergeGateStatus.FAIL
        else:
            test_results = request.test_results.lower()
            # Check for failure patterns: "N failed" or "error" followed by non-zero count
            has_failures = False
            if " failed" in test_results:
                parts = test_results.split(" failed")[0].split()
                if parts and parts[-1].isdigit() and int(parts[-1]) > 0:
                    has_failures = True
            if "error" in test_results:
                has_failures = True

            if has_failures:
                result.gates_failed.append("tests_not_passing")
                result.status = MergeGateStatus.FAIL
            else:
                result.gates_passed.append("tests_passing")

        # Gate 4: Preview verified
        if request.preview_sha != request.feature_sha:
            result.gates_failed.append("preview_sha_mismatch")
            result.status = MergeGateStatus.FAIL
        else:
            result.gates_passed.append("preview_verified")

        # Gate 5: PR mergeable
        # TODO: Check actual GitHub PR state

        # Gate 6: No recent production incidents
        # TODO: Check production monitoring

        logger.info(f"{request.id} merge gate evaluation:")
        logger.info(f"  Passed: {result.gates_passed}")
        if result.gates_failed:
            logger.info(f"  Failed: {result.gates_failed}")
        if result.gates_warning:
            logger.info(f"  Warnings: {result.gates_warning}")

        return result


__all__ = ["MergeGates", "MergeGateResult", "MergeGateStatus"]
