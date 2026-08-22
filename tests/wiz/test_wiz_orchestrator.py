"""Tests for Wiz Orchestrator (Priority 10: owner experience)."""

from __future__ import annotations

import pytest

from openjarvis.wiz.wiz_orchestrator import (
    OwnerInterface,
    WizOrchestrator,
)


class TestOwnerInterface:
    """Owner interaction interface."""

    def test_initialization(self) -> None:
        interface = OwnerInterface()
        assert interface.telegram_enabled is True
        assert interface.control_center_enabled is True
        assert interface.voice_commands_enabled is True

    @pytest.mark.asyncio
    async def test_send_telegram_notification(self) -> None:
        interface = OwnerInterface(telegram_enabled=True)

        result = await interface.send_telegram_notification(
            feature_id="WIZE-PILOT-001",
            message="Feature ready for review",
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_send_telegram_disabled(self) -> None:
        interface = OwnerInterface(telegram_enabled=False)

        result = await interface.send_telegram_notification(
            feature_id="WIZE-PILOT-001",
            message="Feature ready",
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_update_control_center(self) -> None:
        interface = OwnerInterface(control_center_enabled=True)

        result = await interface.update_control_center(
            feature_id="WIZE-PILOT-001",
            state={"status": "approved", "progress": 0.75},
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_update_control_center_disabled(self) -> None:
        interface = OwnerInterface(control_center_enabled=False)

        result = await interface.update_control_center(
            feature_id="WIZE-PILOT-001",
            state={"status": "approved"},
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_request_owner_approval(self) -> None:
        interface = OwnerInterface()

        result = await interface.request_owner_approval(
            feature_id="WIZE-003",
            risk_tier="high",
            reason="Touches authentication system",
        )

        assert result is True


class TestWizOrchestrator:
    """Wiz orchestrator (Priorities 1-9 + 10)."""

    def test_initialization(self) -> None:
        orchestrator = WizOrchestrator()
        assert orchestrator.owner_interface is not None

    def test_initialization_with_interface(self) -> None:
        interface = OwnerInterface(telegram_enabled=False)
        orchestrator = WizOrchestrator(owner_interface=interface)
        assert orchestrator.owner_interface == interface

    def test_build_end_to_end_summary_all_pass(self) -> None:
        orchestrator = WizOrchestrator()

        summary = orchestrator.build_end_to_end_summary(
            feature_id="WIZE-PILOT-001",
            status="DEPLOYED",
            autonomy_rate=0.95,
            approval_gates={
                "tests_passed": True,
                "acceptance_tests_passed": True,
                "code_review_approved": True,
                "ci_all_checks_green": True,
                "no_secrets_detected": True,
            },
            next_steps="Monitor production health",
        )

        assert "WIZE-PILOT-001" in summary
        assert "DEPLOYED" in summary
        assert "95%" in summary
        assert "✓" in summary
        assert "Monitor production health" in summary

    def test_build_end_to_end_summary_partial_fail(self) -> None:
        orchestrator = WizOrchestrator()

        summary = orchestrator.build_end_to_end_summary(
            feature_id="WIZE-001",
            status="AWAITING_APPROVAL",
            autonomy_rate=0.75,
            approval_gates={
                "tests_passed": True,
                "acceptance_tests_passed": True,
                "code_review_approved": False,
                "ci_all_checks_green": True,
            },
            next_steps="Awaiting operator approval",
        )

        assert "WIZE-001" in summary
        assert "AWAITING_APPROVAL" in summary
        assert "75%" in summary
        assert "✗" in summary
        assert "code_review_approved" in summary

    def test_build_telegram_notification_created(self) -> None:
        orchestrator = WizOrchestrator()

        message = orchestrator.build_telegram_notification(
            feature_id="WIZE-PILOT-001",
            event_type="CREATED",
            details={},
        )

        assert "🎯" in message
        assert "WIZE-PILOT-001" in message
        assert "Claude" in message

    def test_build_telegram_notification_reviewing(self) -> None:
        orchestrator = WizOrchestrator()

        message = orchestrator.build_telegram_notification(
            feature_id="WIZE-001",
            event_type="REVIEWING",
            details={"critical_findings": 0, "major_findings": 1},
        )

        assert "🔍" in message
        assert "WIZE-001" in message
        assert "1 major" in message

    def test_build_telegram_notification_approved(self) -> None:
        orchestrator = WizOrchestrator()

        message = orchestrator.build_telegram_notification(
            feature_id="WIZE-001",
            event_type="APPROVED",
            details={},
        )

        assert "✅" in message
        assert "WIZE-001" in message
        assert "merge" in message.lower()

    def test_build_telegram_notification_deployed(self) -> None:
        orchestrator = WizOrchestrator()

        message = orchestrator.build_telegram_notification(
            feature_id="WIZE-001",
            event_type="DEPLOYED",
            details={
                "metrics": {
                    "error_rate": 0.005,
                    "latency_p99_ms": 300.0,
                }
            },
        )

        assert "🚀" in message
        assert "WIZE-001" in message
        assert "0.50%" in message or "0.5%" in message
        assert "300" in message

    def test_build_telegram_notification_failed(self) -> None:
        orchestrator = WizOrchestrator()

        message = orchestrator.build_telegram_notification(
            feature_id="WIZE-001",
            event_type="FAILED",
            details={
                "failure_reason": "Tests failed: test_toggle",
                "attempt_number": 1,
            },
        )

        assert "❌" in message
        assert "WIZE-001" in message
        assert "Tests failed" in message
        assert "Retry attempt 2" in message

    def test_build_control_center_view(self) -> None:
        orchestrator = WizOrchestrator()

        view = orchestrator.build_control_center_view(
            feature_id="WIZE-PILOT-001",
            status="EXECUTING",
            progress=0.35,
            current_stage="Testing",
            metrics={
                "autonomy_rate": 0.95,
                "session_id": "session-123",
            },
        )

        assert view["feature_id"] == "WIZE-PILOT-001"
        assert view["status"] == "EXECUTING"
        assert view["progress"] == 0.35
        assert view["current_stage"] == "Testing"
        assert view["metrics"]["autonomy_rate"] == 0.95
        assert "timeline" in view
        assert len(view["timeline"]) == 5

    def test_control_center_view_timeline_progression(self) -> None:
        orchestrator = WizOrchestrator()

        # Early stage (validation)
        view = orchestrator.build_control_center_view(
            feature_id="WIZE-001",
            status="VALIDATING",
            progress=0.1,
            current_stage="Validation",
            metrics={},
        )

        assert view["timeline"][0]["status"] == "passed"
        assert view["timeline"][1]["status"] == "pending"

        # Mid stage (testing)
        view = orchestrator.build_control_center_view(
            feature_id="WIZE-001",
            status="TESTING",
            progress=0.45,
            current_stage="Testing",
            metrics={},
        )

        assert view["timeline"][2]["status"] == "in_progress"
        assert view["timeline"][3]["status"] == "pending"

        # Late stage (deployment)
        view = orchestrator.build_control_center_view(
            feature_id="WIZE-001",
            status="DEPLOYING",
            progress=0.85,
            current_stage="Deployment",
            metrics={},
        )

        assert view["timeline"][4]["status"] == "pending"


class TestIntegrationEndToEnd:
    """Integration of all Priorities 1-10."""

    def test_pilot_feature_full_lifecycle(self) -> None:
        """Simulate dark mode toggle pilot through entire lifecycle."""
        orchestrator = WizOrchestrator()

        feature_id = "WIZE-PILOT-001"

        # Stage 1: Feature created
        view = orchestrator.build_control_center_view(
            feature_id=feature_id,
            status="CREATED",
            progress=0.05,
            current_stage="Validation",
            metrics={"risk_tier": "low"},
        )
        assert view["status"] == "CREATED"

        # Stage 2: Executing (Claude session)
        view = orchestrator.build_control_center_view(
            feature_id=feature_id,
            status="EXECUTING",
            progress=0.30,
            current_stage="Execution",
            metrics={"session_id": "session-123", "real_claude_cli": True},
        )
        assert view["metrics"]["real_claude_cli"] is True

        # Stage 3: Testing (acceptance tests)
        view = orchestrator.build_control_center_view(
            feature_id=feature_id,
            status="TESTING",
            progress=0.50,
            current_stage="Testing",
            metrics={"acceptance_tests_passed": True, "success_rate": 1.0},
        )

        # Stage 4: Review (code review session)
        view = orchestrator.build_control_center_view(
            feature_id=feature_id,
            status="REVIEWING",
            progress=0.60,
            current_stage="Review",
            metrics={"review_status": "APPROVE", "findings": 0},
        )

        # Stage 5: Approved (gates pass)
        summary = orchestrator.build_end_to_end_summary(
            feature_id=feature_id,
            status="APPROVED",
            autonomy_rate=0.95,
            approval_gates={
                "tests_passed": True,
                "acceptance_tests_passed": True,
                "code_review_approved": True,
                "ci_all_checks_green": True,
                "no_secrets_detected": True,
            },
            next_steps="Auto-merge (LOW risk)",
        )
        assert "95%" in summary
        assert "APPROVED" in summary

        # Stage 6: Deployed
        message = orchestrator.build_telegram_notification(
            feature_id=feature_id,
            event_type="DEPLOYED",
            details={
                "metrics": {
                    "error_rate": 0.001,
                    "latency_p99_ms": 250.0,
                }
            },
        )
        assert "🚀" in message
        assert "deployed" in message.lower()

        # Final: Production healthy
        view = orchestrator.build_control_center_view(
            feature_id=feature_id,
            status="HEALTHY",
            progress=1.0,
            current_stage="Production",
            metrics={
                "error_rate": 0.001,
                "active_users": 5000,
                "adoption_rate": 0.75,
            },
        )
        assert view["progress"] == 1.0
