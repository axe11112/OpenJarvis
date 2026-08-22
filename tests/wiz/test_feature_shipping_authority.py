"""Tests for feature shipping authority (risk-based merge/deploy)."""

from __future__ import annotations

import pytest

from openjarvis.wiz.feature_gate_integration import GateDecision
from openjarvis.wiz.feature_shipping_authority import (
    RiskTier,
    ShippingAuthority,
    ShippingDecision,
)


class TestRiskTier:
    """RiskTier enum."""

    def test_risk_tier_values(self) -> None:
        assert RiskTier.LOW.value == "low"
        assert RiskTier.MEDIUM.value == "medium"
        assert RiskTier.HIGH.value == "high"


class TestShippingDecision:
    """ShippingDecision dataclass."""

    def test_create_low_risk_approved(self) -> None:
        decision = ShippingDecision(
            feature_id="WIZE-PILOT-001",
            risk_tier=RiskTier.LOW,
            should_merge=True,
            should_deploy=True,
            reason="LOW risk: auto-eligible",
        )
        assert decision.risk_tier == RiskTier.LOW
        assert decision.should_merge is True
        assert decision.should_deploy is True

    def test_create_medium_risk_pending(self) -> None:
        decision = ShippingDecision(
            feature_id="WIZE-002",
            risk_tier=RiskTier.MEDIUM,
            should_merge=False,
            should_deploy=False,
            reason="MEDIUM risk: awaiting operator approval",
            approval_reason="Awaiting operator approval",
        )
        assert decision.risk_tier == RiskTier.MEDIUM
        assert decision.should_merge is False
        assert decision.approval_reason is not None

    def test_create_high_risk_owner_approval(self) -> None:
        decision = ShippingDecision(
            feature_id="WIZE-003",
            risk_tier=RiskTier.HIGH,
            should_merge=True,
            should_deploy=True,
            reason="HIGH risk: owner approved",
            approved_by="owner@example.com",
        )
        assert decision.risk_tier == RiskTier.HIGH
        assert decision.approved_by == "owner@example.com"

    def test_shipping_decision_to_dict(self) -> None:
        decision = ShippingDecision(
            feature_id="WIZE-001",
            risk_tier=RiskTier.LOW,
            should_merge=True,
            should_deploy=True,
            reason="Auto-eligible",
        )
        d = decision.to_dict()
        assert d["feature_id"] == "WIZE-001"
        assert d["risk_tier"] == "low"
        assert d["should_merge"] is True


class TestShippingAuthority:
    """Shipping authority functionality."""

    def test_initialization(self) -> None:
        authority = ShippingAuthority()
        assert authority is not None

    def test_assess_risk_low(self) -> None:
        authority = ShippingAuthority()

        risk = authority.assess_risk(
            feature_id="WIZE-PILOT-001",
            changed_files=["src/component.tsx"],
            changed_lines=50,
        )

        assert risk == RiskTier.LOW

    def test_assess_risk_medium_many_files(self) -> None:
        authority = ShippingAuthority()

        risk = authority.assess_risk(
            feature_id="WIZE-001",
            changed_files=[f"src/file{i}.ts" for i in range(10)],
            changed_lines=200,
        )

        assert risk == RiskTier.MEDIUM

    def test_assess_risk_high_critical_files(self) -> None:
        authority = ShippingAuthority()

        risk = authority.assess_risk(
            feature_id="WIZE-001",
            changed_files=[
                "src/auth/security.py",
                "src/database/schema.sql",
                "src/payment/processor.ts",
            ],
            changed_lines=300,
        )

        assert risk == RiskTier.HIGH

    def test_assess_risk_high_many_lines(self) -> None:
        authority = ShippingAuthority()

        risk = authority.assess_risk(
            feature_id="WIZE-001",
            changed_files=["src/rewrite.ts"],
            changed_lines=1000,
        )

        assert risk == RiskTier.HIGH

    def test_decide_shipping_low_risk_gates_pass(self) -> None:
        authority = ShippingAuthority()

        gate_decision = GateDecision(
            feature_id="WIZE-PILOT-001",
            can_merge=True,
            reasons_cannot_merge=[],
        )

        shipping = authority.decide_shipping(
            feature_id="WIZE-PILOT-001",
            risk_tier=RiskTier.LOW,
            gate_decision=gate_decision,
        )

        # LOW risk + gates pass = auto-ship
        assert shipping.should_merge is True
        assert shipping.should_deploy is True
        assert "auto-eligible" in shipping.reason.lower()

    def test_decide_shipping_low_risk_gates_fail(self) -> None:
        authority = ShippingAuthority()

        gate_decision = GateDecision(
            feature_id="WIZE-PILOT-001",
            can_merge=False,
            reasons_cannot_merge=["tests_passed"],
        )

        shipping = authority.decide_shipping(
            feature_id="WIZE-PILOT-001",
            risk_tier=RiskTier.LOW,
            gate_decision=gate_decision,
        )

        # Gates fail = cannot ship (regardless of risk)
        assert shipping.should_merge is False
        assert shipping.should_deploy is False
        assert "gates" in shipping.approval_reason.lower()

    def test_decide_shipping_medium_risk_no_approval(self) -> None:
        authority = ShippingAuthority()

        gate_decision = GateDecision(
            feature_id="WIZE-002",
            can_merge=True,
            reasons_cannot_merge=[],
        )

        shipping = authority.decide_shipping(
            feature_id="WIZE-002",
            risk_tier=RiskTier.MEDIUM,
            gate_decision=gate_decision,
            operator_approved=False,
        )

        # MEDIUM risk requires operator approval
        assert shipping.should_merge is False
        assert shipping.should_deploy is False
        assert "operator" in shipping.approval_reason.lower()

    def test_decide_shipping_medium_risk_with_approval(self) -> None:
        authority = ShippingAuthority()

        gate_decision = GateDecision(
            feature_id="WIZE-002",
            can_merge=True,
            reasons_cannot_merge=[],
        )

        shipping = authority.decide_shipping(
            feature_id="WIZE-002",
            risk_tier=RiskTier.MEDIUM,
            gate_decision=gate_decision,
            operator_approved=True,
        )

        # Operator approved: can merge but not auto-deploy
        assert shipping.should_merge is True
        assert shipping.should_deploy is False
        assert "manual deploy" in shipping.reason.lower()

    def test_decide_shipping_high_risk_no_approval(self) -> None:
        authority = ShippingAuthority()

        gate_decision = GateDecision(
            feature_id="WIZE-003",
            can_merge=True,
            reasons_cannot_merge=[],
        )

        shipping = authority.decide_shipping(
            feature_id="WIZE-003",
            risk_tier=RiskTier.HIGH,
            gate_decision=gate_decision,
            owner_approved=False,
        )

        # HIGH risk requires owner approval
        assert shipping.should_merge is False
        assert shipping.should_deploy is False
        assert "owner" in shipping.approval_reason.lower()

    def test_decide_shipping_high_risk_with_owner_approval(self) -> None:
        authority = ShippingAuthority()

        gate_decision = GateDecision(
            feature_id="WIZE-003",
            can_merge=True,
            reasons_cannot_merge=[],
        )

        shipping = authority.decide_shipping(
            feature_id="WIZE-003",
            risk_tier=RiskTier.HIGH,
            gate_decision=gate_decision,
            owner_approved=True,
        )

        # Owner approved: can merge and deploy
        assert shipping.should_merge is True
        assert shipping.should_deploy is True
        assert "owner approved" in shipping.reason.lower()

    def test_build_shipping_summary_low_risk_auto_ship(self) -> None:
        authority = ShippingAuthority()

        decision = ShippingDecision(
            feature_id="WIZE-PILOT-001",
            risk_tier=RiskTier.LOW,
            should_merge=True,
            should_deploy=True,
            reason="LOW risk: auto-eligible for merge and deploy",
        )

        summary = authority.build_shipping_summary(decision)

        assert "low" in summary.lower()
        assert "can merge" in summary.lower()
        assert "can deploy" in summary.lower()

    def test_build_shipping_summary_medium_risk_pending(self) -> None:
        authority = ShippingAuthority()

        decision = ShippingDecision(
            feature_id="WIZE-002",
            risk_tier=RiskTier.MEDIUM,
            should_merge=False,
            should_deploy=False,
            reason="MEDIUM risk: awaiting operator approval",
            approval_reason="Awaiting operator approval (MEDIUM risk)",
        )

        summary = authority.build_shipping_summary(decision)

        assert "medium" in summary.lower()
        assert "cannot merge" in summary.lower()
        assert "manual deploy" in summary.lower()

    def test_build_shipping_summary_high_risk_approved(self) -> None:
        authority = ShippingAuthority()

        decision = ShippingDecision(
            feature_id="WIZE-003",
            risk_tier=RiskTier.HIGH,
            should_merge=True,
            should_deploy=True,
            reason="HIGH risk: owner approved for merge and deploy",
            approved_by="owner@example.com",
        )

        summary = authority.build_shipping_summary(decision)

        assert "high" in summary.lower()
        assert "can merge" in summary.lower()
        assert "can deploy" in summary.lower()


class TestShippingAuthorityFlow:
    """End-to-end shipping authority flow."""

    def test_complete_low_risk_flow(self) -> None:
        """LOW risk feature: assess → gates pass → auto-ship."""
        authority = ShippingAuthority()

        # Step 1: Assess risk
        risk = authority.assess_risk(
            feature_id="WIZE-PILOT-001",
            changed_files=["src/theme.tsx"],
            changed_lines=100,
        )
        assert risk == RiskTier.LOW

        # Step 2: Check gates
        gate_decision = GateDecision(
            feature_id="WIZE-PILOT-001",
            can_merge=True,
            reasons_cannot_merge=[],
        )

        # Step 3: Shipping decision
        shipping = authority.decide_shipping(
            feature_id="WIZE-PILOT-001",
            risk_tier=risk,
            gate_decision=gate_decision,
        )

        assert shipping.should_merge is True
        assert shipping.should_deploy is True

    def test_complete_medium_risk_flow(self) -> None:
        """MEDIUM risk feature: assess → gates pass → operator approves → merge."""
        authority = ShippingAuthority()

        # Step 1: Assess risk
        risk = authority.assess_risk(
            feature_id="WIZE-002",
            changed_files=[f"src/file{i}.ts" for i in range(8)],
            changed_lines=300,
        )
        assert risk == RiskTier.MEDIUM

        # Step 2: Check gates
        gate_decision = GateDecision(
            feature_id="WIZE-002",
            can_merge=True,
            reasons_cannot_merge=[],
        )

        # Step 3: Without operator approval - cannot ship
        shipping = authority.decide_shipping(
            feature_id="WIZE-002",
            risk_tier=risk,
            gate_decision=gate_decision,
            operator_approved=False,
        )
        assert shipping.should_merge is False

        # Step 4: With operator approval - can merge (manual deploy)
        shipping = authority.decide_shipping(
            feature_id="WIZE-002",
            risk_tier=risk,
            gate_decision=gate_decision,
            operator_approved=True,
        )
        assert shipping.should_merge is True
        assert shipping.should_deploy is False

    def test_complete_high_risk_flow(self) -> None:
        """HIGH risk feature: assess → gates pass → owner approves → ship."""
        authority = ShippingAuthority()

        # Step 1: Assess risk
        risk = authority.assess_risk(
            feature_id="WIZE-003",
            changed_files=["src/auth/security.py", "src/database/schema.sql"],
            changed_lines=800,
        )
        assert risk == RiskTier.HIGH

        # Step 2: Check gates
        gate_decision = GateDecision(
            feature_id="WIZE-003",
            can_merge=True,
            reasons_cannot_merge=[],
        )

        # Step 3: Without owner approval - cannot ship
        shipping = authority.decide_shipping(
            feature_id="WIZE-003",
            risk_tier=risk,
            gate_decision=gate_decision,
            owner_approved=False,
        )
        assert shipping.should_merge is False
        assert shipping.should_deploy is False

        # Step 4: With owner approval - can ship
        shipping = authority.decide_shipping(
            feature_id="WIZE-003",
            risk_tier=risk,
            gate_decision=gate_decision,
            owner_approved=True,
        )
        assert shipping.should_merge is True
        assert shipping.should_deploy is True

    def test_gates_are_mandatory(self) -> None:
        """Even LOW risk features need gates to pass."""
        authority = ShippingAuthority()

        # Gates fail
        gate_decision = GateDecision(
            feature_id="WIZE-PILOT-001",
            can_merge=False,
            reasons_cannot_merge=["acceptance_tests_passed"],
        )

        shipping = authority.decide_shipping(
            feature_id="WIZE-PILOT-001",
            risk_tier=RiskTier.LOW,
            gate_decision=gate_decision,
        )

        # Gates failure blocks merge even for LOW risk
        assert shipping.should_merge is False
        assert shipping.should_deploy is False
