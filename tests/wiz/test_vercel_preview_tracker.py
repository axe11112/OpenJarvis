"""Tests for Vercel Preview deployment tracking."""

from __future__ import annotations

import pytest

from openjarvis.wiz.vercel_preview_tracker import (
    DeploymentStatus,
    PreviewVerificationResult,
    VercelDeployment,
    VercelPreviewTracker,
)


class TestDeploymentStatus:
    """DeploymentStatus enum."""

    def test_deployment_statuses(self) -> None:
        assert DeploymentStatus.BUILDING.value == "building"
        assert DeploymentStatus.READY.value == "ready"
        assert DeploymentStatus.ERROR.value == "error"
        assert DeploymentStatus.CANCELED.value == "canceled"
        assert DeploymentStatus.QUEUED.value == "queued"
        assert DeploymentStatus.UNKNOWN.value == "unknown"


class TestVercelDeployment:
    """VercelDeployment dataclass."""

    def test_create_ready_deployment(self) -> None:
        deployment = VercelDeployment(
            deployment_id="dpl_abc123",
            url="https://wize-wiz-wize-pilot-001-wiz.vercel.app",
            status=DeploymentStatus.READY,
            sha="abc123def456",
            branch="wiz/wize-pilot-001",
            created_at="2026-08-22T15:30:00Z",
            completed_at="2026-08-22T15:31:30Z",
        )
        assert deployment.is_ready
        assert not deployment.is_building
        assert not deployment.is_error

    def test_create_building_deployment(self) -> None:
        deployment = VercelDeployment(
            deployment_id="dpl_xyz789",
            url="https://wize-wiz-wize-pilot-001-wiz.vercel.app",
            status=DeploymentStatus.BUILDING,
            sha="xyz789abc123",
            branch="wiz/wize-pilot-001",
            created_at="2026-08-22T15:40:00Z",
        )
        assert not deployment.is_ready
        assert deployment.is_building
        assert not deployment.is_error

    def test_create_error_deployment(self) -> None:
        deployment = VercelDeployment(
            deployment_id="dpl_err001",
            url="https://wize-wiz-wize-pilot-001-wiz.vercel.app",
            status=DeploymentStatus.ERROR,
            sha="err001xyz",
            branch="wiz/wize-pilot-001",
            created_at="2026-08-22T15:50:00Z",
            error_message="Build failed: npm test failed",
        )
        assert not deployment.is_ready
        assert not deployment.is_building
        assert deployment.is_error
        assert deployment.error_message == "Build failed: npm test failed"

    def test_deployment_to_dict(self) -> None:
        deployment = VercelDeployment(
            deployment_id="dpl_abc123",
            url="https://wize-wiz-wize-pilot-001-wiz.vercel.app",
            status=DeploymentStatus.READY,
            sha="abc123def456",
            branch="wiz/wize-pilot-001",
            created_at="2026-08-22T15:30:00Z",
        )
        d = deployment.to_dict()
        assert d["deployment_id"] == "dpl_abc123"
        assert d["status"] == "ready"
        assert d["is_ready"] is True


class TestVercelPreviewTracker:
    """Vercel preview tracker functionality."""

    def test_tracker_initialization(self) -> None:
        tracker = VercelPreviewTracker()
        assert tracker is not None
        assert tracker._vercel_api_base == "https://api.vercel.com"

    def test_tracker_with_api_token(self) -> None:
        tracker = VercelPreviewTracker(api_token="test_token")
        assert tracker._api_token == "test_token"

    def test_verify_deployment_sha_mismatch(self) -> None:
        tracker = VercelPreviewTracker()
        deployment = VercelDeployment(
            deployment_id="dpl_abc123",
            url="https://wize-wiz-wize-pilot-001-wiz.vercel.app",
            status=DeploymentStatus.READY,
            sha="abc123def456",
            branch="wiz/wize-pilot-001",
            created_at="2026-08-22T15:30:00Z",
        )

        is_valid, reason = tracker.verify_deployment(
            deployment=deployment,
            expected_branch="wiz/wize-pilot-001",
            expected_sha="different_sha",
        )
        assert not is_valid
        assert "SHA mismatch" in reason

    def test_verify_deployment_branch_mismatch(self) -> None:
        tracker = VercelPreviewTracker()
        deployment = VercelDeployment(
            deployment_id="dpl_abc123",
            url="https://wize-wiz-wize-pilot-001-wiz.vercel.app",
            status=DeploymentStatus.READY,
            sha="abc123def456",
            branch="wiz/wize-pilot-001",
            created_at="2026-08-22T15:30:00Z",
        )

        is_valid, reason = tracker.verify_deployment(
            deployment=deployment,
            expected_branch="wiz/different-feature",
            expected_sha="abc123def456",
        )
        assert not is_valid
        assert "branch mismatch" in reason.lower()

    def test_verify_deployment_not_ready(self) -> None:
        tracker = VercelPreviewTracker()
        deployment = VercelDeployment(
            deployment_id="dpl_abc123",
            url="https://wize-wiz-wize-pilot-001-wiz.vercel.app",
            status=DeploymentStatus.BUILDING,
            sha="abc123def456",
            branch="wiz/wize-pilot-001",
            created_at="2026-08-22T15:30:00Z",
        )

        is_valid, reason = tracker.verify_deployment(
            deployment=deployment,
            expected_branch="wiz/wize-pilot-001",
            expected_sha="abc123def456",
        )
        assert not is_valid
        assert "building" in reason.lower()

    def test_verify_deployment_error_status(self) -> None:
        tracker = VercelPreviewTracker()
        deployment = VercelDeployment(
            deployment_id="dpl_err001",
            url="https://wize-wiz-wize-pilot-001-wiz.vercel.app",
            status=DeploymentStatus.ERROR,
            sha="abc123def456",
            branch="wiz/wize-pilot-001",
            created_at="2026-08-22T15:30:00Z",
            error_message="Build failed",
        )

        is_valid, reason = tracker.verify_deployment(
            deployment=deployment,
            expected_branch="wiz/wize-pilot-001",
            expected_sha="abc123def456",
        )
        assert not is_valid
        assert "error" in reason.lower()

    def test_verify_deployment_success(self) -> None:
        tracker = VercelPreviewTracker()
        deployment = VercelDeployment(
            deployment_id="dpl_abc123",
            url="https://wize-wiz-wize-pilot-001-wiz.vercel.app",
            status=DeploymentStatus.READY,
            sha="abc123def456",
            branch="wiz/wize-pilot-001",
            created_at="2026-08-22T15:30:00Z",
        )

        is_valid, reason = tracker.verify_deployment(
            deployment=deployment,
            expected_branch="wiz/wize-pilot-001",
            expected_sha="abc123def456",
        )
        assert is_valid
        assert reason is None

    def test_check_url_available_valid(self) -> None:
        tracker = VercelPreviewTracker()
        is_available, error = tracker.check_url_available(
            url="https://wize-wiz-wize-pilot-001-wiz.vercel.app"
        )
        # With valid URL format, should be available
        assert is_available
        assert error is None

    def test_check_url_available_invalid(self) -> None:
        tracker = VercelPreviewTracker()
        is_available, error = tracker.check_url_available(
            url="not-a-valid-url"
        )
        # With invalid URL format
        assert not is_available
        assert error is not None

    def test_extract_deployment_id_from_pr_vercel_reference(self) -> None:
        tracker = VercelPreviewTracker()
        pr_body = """
This PR deploys to Vercel Preview:
https://wize-wiz-wize-pilot-001-wiz.vercel.app

The deployment is ready for testing.
        """
        deployment_id = tracker.extract_deployment_id_from_pr(pr_body)
        # Should find deployment reference
        assert deployment_id is not None

    def test_extract_deployment_id_from_pr_no_deployment(self) -> None:
        tracker = VercelPreviewTracker()
        pr_body = """
This PR makes some code changes.
All tests pass locally.
        """
        deployment_id = tracker.extract_deployment_id_from_pr(pr_body)
        # Should not find deployment reference
        assert deployment_id is None


class TestPreviewVerificationResult:
    """PreviewVerificationResult dataclass."""

    def test_unverified_result(self) -> None:
        result = PreviewVerificationResult(
            feature_id="WIZE-PILOT-001",
        )
        assert not result.verified
        assert result.status == "unverified"

    def test_partial_verification(self) -> None:
        result = PreviewVerificationResult(
            feature_id="WIZE-PILOT-001",
            sha_matches=True,
            branch_matches=True,
            is_ready=True,
            is_available=False,  # URL not available yet
        )
        assert not result.verified  # Not complete

    def test_full_verification(self) -> None:
        result = PreviewVerificationResult(
            feature_id="WIZE-PILOT-001",
            deployment_id="dpl_abc123",
            url="https://wize-wiz-wize-pilot-001-wiz.vercel.app",
            sha_matches=True,
            branch_matches=True,
            is_ready=True,
            is_available=True,
            status="ready",
        )
        assert result.verified

    def test_verification_with_error(self) -> None:
        result = PreviewVerificationResult(
            feature_id="WIZE-PILOT-001",
            status="failed",
            error="Deployment failed to build",
        )
        assert not result.verified
        assert result.error is not None

    def test_verification_result_to_dict(self) -> None:
        result = PreviewVerificationResult(
            feature_id="WIZE-PILOT-001",
            deployment_id="dpl_abc123",
            url="https://wize-wiz-wize-pilot-001-wiz.vercel.app",
            sha_matches=True,
            branch_matches=True,
            is_ready=True,
            is_available=True,
        )
        d = result.to_dict()
        assert d["feature_id"] == "WIZE-PILOT-001"
        assert d["verified"] is True
        assert d["sha_matches"] is True
        assert d["branch_matches"] is True


class TestPreviewVerificationFlow:
    """End-to-end preview verification flow."""

    def test_complete_verification_flow(self) -> None:
        """Simulate complete preview verification for pilot feature."""
        tracker = VercelPreviewTracker()

        # Step 1: Create deployment
        deployment = VercelDeployment(
            deployment_id="dpl_pilot001",
            url="https://wize-wiz-wize-pilot-001-wiz.vercel.app",
            status=DeploymentStatus.READY,
            sha="pilot001abc",
            branch="wiz/wize-pilot-001",
            created_at="2026-08-22T15:30:00Z",
            completed_at="2026-08-22T15:31:30Z",
        )

        # Step 2: Verify SHA and branch
        is_valid, reason = tracker.verify_deployment(
            deployment=deployment,
            expected_branch="wiz/wize-pilot-001",
            expected_sha="pilot001abc",
        )
        assert is_valid, f"Deployment verification failed: {reason}"

        # Step 3: Check URL availability
        is_available, error = tracker.check_url_available(deployment.url)
        assert is_available, f"URL not available: {error}"

        # Step 4: Build verification result
        result = PreviewVerificationResult(
            feature_id="WIZE-PILOT-001",
            deployment_id=deployment.deployment_id,
            url=deployment.url,
            sha_matches=deployment.sha == "pilot001abc",
            branch_matches=deployment.branch == "wiz/wize-pilot-001",
            is_ready=deployment.is_ready,
            is_available=is_available,
            status="ready",
        )

        # Final: Check result
        assert result.verified
        assert result.status == "ready"

    def test_wrong_sha_detection(self) -> None:
        """Verify detection of wrong SHA deployment."""
        tracker = VercelPreviewTracker()

        # Deployment with wrong SHA
        deployment = VercelDeployment(
            deployment_id="dpl_wrongsha",
            url="https://wize-wiz-wize-pilot-001-wiz.vercel.app",
            status=DeploymentStatus.READY,
            sha="old_sha_123",  # Wrong!
            branch="wiz/wize-pilot-001",
            created_at="2026-08-22T14:00:00Z",
        )

        # Verification should fail
        is_valid, reason = tracker.verify_deployment(
            deployment=deployment,
            expected_branch="wiz/wize-pilot-001",
            expected_sha="new_sha_456",
        )

        assert not is_valid
        assert "SHA mismatch" in reason

    def test_stale_deployment_detection(self) -> None:
        """Verify detection of stale deployments."""
        # Deployment from a previous run (too old)
        deployment = VercelDeployment(
            deployment_id="dpl_stale",
            url="https://wize-wiz-wize-pilot-001-wiz.vercel.app",
            status=DeploymentStatus.READY,
            sha="stale_sha",
            branch="wiz/wize-pilot-001",
            created_at="2026-08-21T15:30:00Z",  # Yesterday
            completed_at="2026-08-21T15:31:30Z",
        )

        # Current branch has different SHA
        # Verification would fail on SHA mismatch (different commit)
        tracker = VercelPreviewTracker()
        is_valid, reason = tracker.verify_deployment(
            deployment=deployment,
            expected_branch="wiz/wize-pilot-001",
            expected_sha="new_current_sha",
        )

        assert not is_valid
        assert "SHA mismatch" in reason
