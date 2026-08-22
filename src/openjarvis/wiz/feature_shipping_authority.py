"""Feature shipping authority: Risk-based merge and deployment decisions.

Separate from repair auto-merge logic. Decides whether feature can automatically
merge based on risk tier:

- LOW risk: Can merge and deploy automatically (if all gates pass)
- MEDIUM risk: Can merge (if gates pass + operator approval), manual deploy decision
- HIGH risk: Requires owner approval, manual merge, manual deploy

This is independent of merge gates (Priority 6). Gates determine IF a feature
can merge. Authority determines IF it SHOULD merge based on risk assessment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from openjarvis.wiz.configured_target import ConfiguredTarget
from openjarvis.wiz.feature_gate_integration import GateDecision

logger = logging.getLogger(__name__)

__all__ = [
    "RiskTier",
    "ShippingAuthority",
    "ShippingDecision",
]


class RiskTier(str, Enum):
    """Feature risk tier."""

    LOW = "low"  # Can auto-merge (if gates pass)
    MEDIUM = "medium"  # Can merge, manual deploy (if gates pass + operator approval)
    HIGH = "high"  # Requires owner approval, manual merge and deploy


@dataclass
class ShippingDecision:
    """Decision on whether feature should merge/deploy."""

    feature_id: str
    risk_tier: RiskTier
    should_merge: bool = False
    should_deploy: bool = False
    reason: str = ""

    # Who approved
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None

    # Why not approved (if not)
    approval_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "risk_tier": self.risk_tier.value,
            "should_merge": self.should_merge,
            "should_deploy": self.should_deploy,
            "reason": self.reason,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "approval_reason": self.approval_reason,
        }


class ShippingAuthority:
    """Determine if feature should merge and deploy based on risk tier.

    Separate from merge gates (Priority 6). Gates check: Is code sound?
    Authority checks: Should we ship this based on risk?

    LOW risk features can auto-ship if gates pass.
    MEDIUM risk features need operator decision to merge, manual deploy.
    HIGH risk features need owner approval for everything.
    """

    def __init__(self) -> None:
        """Initialize shipping authority."""
        pass

    def assess_risk(
        self,
        feature_id: str,
        configured_target: Optional[ConfiguredTarget] = None,
        changed_files: Optional[List[str]] = None,
        changed_lines: int = 0,
    ) -> RiskTier:
        """Assess feature risk tier.

        Args:
            feature_id: Feature ID
            configured_target: ConfiguredTarget constraints (if available)
            changed_files: List of changed file paths
            changed_lines: Total lines changed

        Returns:
            RiskTier assessment
        """
        # Start with configured target constraints
        risk = RiskTier.LOW

        # If target specifies risk level, use it
        if configured_target and configured_target.risk_tier:
            if configured_target.risk_tier == "high":
                risk = RiskTier.HIGH
            elif configured_target.risk_tier == "medium":
                risk = RiskTier.MEDIUM
            else:
                risk = RiskTier.LOW
        else:
            # Auto-assess based on scope
            if changed_files:
                # Multiple files or critical files = higher risk
                if len(changed_files) > 5:
                    risk = RiskTier.MEDIUM

                critical_patterns = [
                    "schema",
                    "migration",
                    "security",
                    "auth",
                    "payment",
                    "database",
                ]
                if any(
                    pattern in f.lower()
                    for f in changed_files
                    for pattern in critical_patterns
                ):
                    risk = RiskTier.HIGH

            if changed_lines > 500:
                if risk == RiskTier.LOW:
                    risk = RiskTier.MEDIUM
                elif risk == RiskTier.MEDIUM:
                    risk = RiskTier.HIGH

        logger.info(
            "assessed risk for %s: %s (files=%d, lines=%d)",
            feature_id,
            risk.value,
            len(changed_files) if changed_files else 0,
            changed_lines,
        )

        return risk

    def decide_shipping(
        self,
        feature_id: str,
        risk_tier: RiskTier,
        gate_decision: GateDecision,
        operator_approved: bool = False,
        owner_approved: bool = False,
    ) -> ShippingDecision:
        """Decide if feature should merge and deploy.

        Args:
            feature_id: Feature ID
            risk_tier: Assessed risk tier
            gate_decision: Gate evaluation result
            operator_approved: Did operator approve? (for MEDIUM risk)
            owner_approved: Did owner approve? (for HIGH risk)

        Returns:
            ShippingDecision with should_merge, should_deploy
        """
        should_merge = False
        should_deploy = False
        reason = ""
        approval_reason = None

        # Gates must pass first
        if not gate_decision.can_merge:
            approval_reason = f"Gates not passing: {', '.join(gate_decision.reasons_cannot_merge)}"
            reason = f"Cannot ship: merge gates not passing"
        else:
            # Gates pass - now check risk tier
            if risk_tier == RiskTier.LOW:
                # Low risk: can auto-merge and auto-deploy
                should_merge = True
                should_deploy = True
                reason = "LOW risk: auto-eligible for merge and deploy"

            elif risk_tier == RiskTier.MEDIUM:
                # MEDIUM risk: can merge if operator approves, deploy is manual
                if operator_approved:
                    should_merge = True
                    should_deploy = False
                    reason = "MEDIUM risk: approved for merge (manual deploy)"
                else:
                    approval_reason = "Awaiting operator approval (MEDIUM risk)"
                    reason = "MEDIUM risk: awaiting operator approval"

            elif risk_tier == RiskTier.HIGH:
                # HIGH risk: requires owner approval for merge and deploy
                if owner_approved:
                    should_merge = True
                    should_deploy = True
                    reason = "HIGH risk: owner approved for merge and deploy"
                else:
                    approval_reason = "Awaiting owner approval (HIGH risk)"
                    reason = "HIGH risk: awaiting owner approval"

        decision = ShippingDecision(
            feature_id=feature_id,
            risk_tier=risk_tier,
            should_merge=should_merge,
            should_deploy=should_deploy,
            reason=reason,
            approval_reason=approval_reason,
        )

        return decision

    def build_shipping_summary(
        self,
        decision: ShippingDecision,
    ) -> str:
        """Build human-readable shipping decision summary."""
        parts = []

        parts.append(f"Risk Tier: {decision.risk_tier.value.upper()}")

        if decision.should_merge:
            parts.append("✓ Can merge: " + decision.reason)
        else:
            parts.append("✗ Cannot merge: " + decision.reason)
            if decision.approval_reason:
                parts.append(f"  Reason: {decision.approval_reason}")

        if decision.should_deploy:
            parts.append("✓ Can deploy to production")
        else:
            parts.append("✗ Manual deploy decision required")

        return "\n".join(parts)


__all__ = [
    "RiskTier",
    "ShippingAuthority",
    "ShippingDecision",
]
