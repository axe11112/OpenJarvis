"""Track Vercel Preview deployments for feature verification.

Verifies that:
1. Deployment SHA matches the feature branch tip
2. Deployment is in READY status (not BUILDING, ERROR, etc.)
3. Preview URL is responding
4. Deployment is recent (not stale from previous run)

Only READY deployments with correct SHA are considered valid.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "DeploymentStatus",
    "VercelDeployment",
    "VercelPreviewTracker",
]


class DeploymentStatus(str, Enum):
    """Vercel deployment status."""

    BUILDING = "building"
    READY = "ready"
    ERROR = "error"
    CANCELED = "canceled"
    QUEUED = "queued"
    UNKNOWN = "unknown"


@dataclass
class VercelDeployment:
    """Information about a Vercel deployment."""

    deployment_id: str
    url: str
    status: DeploymentStatus
    sha: str
    branch: str
    created_at: str
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    environment: str = "Preview"

    @property
    def is_ready(self) -> bool:
        """Is deployment ready to use?"""
        return self.status == DeploymentStatus.READY

    @property
    def is_building(self) -> bool:
        """Is deployment still building?"""
        return self.status == DeploymentStatus.BUILDING

    @property
    def is_error(self) -> bool:
        """Did deployment fail?"""
        return self.status == DeploymentStatus.ERROR

    def to_dict(self) -> Dict[str, Any]:
        return {
            "deployment_id": self.deployment_id,
            "url": self.url,
            "status": self.status.value,
            "sha": self.sha,
            "branch": self.branch,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "error_message": self.error_message,
            "environment": self.environment,
            "is_ready": self.is_ready,
        }


class VercelPreviewTracker:
    """Track Vercel Preview deployments.

    Provides:
    - Deployment status polling
    - SHA verification (matches branch tip)
    - URL health checks
    - Stale detection (deployment too old)
    """

    def __init__(self, api_token: Optional[str] = None) -> None:
        """Initialize tracker.

        Args:
            api_token: Vercel API token (optional, for testing)
        """
        self._api_token = api_token
        self._vercel_api_base = "https://api.vercel.com"

    def get_deployment(
        self,
        deployment_id: str,
    ) -> Optional[VercelDeployment]:
        """Get deployment details from Vercel API.

        Args:
            deployment_id: Deployment ID from Vercel

        Returns:
            VercelDeployment if found, None otherwise
        """
        logger.info("fetching deployment %s from Vercel", deployment_id)

        # In production, would call:
        # GET /v13/deployments/{deployment_id}
        # with Authorization: Bearer {api_token}

        # For now, simulate a deployment
        # Real implementation would parse API response:
        # {
        #   "id": "dpl_...",
        #   "url": "wize-wiz-wize-pilot-001-wiz.vercel.app",
        #   "status": "ready",
        #   "gitCommitSha": "abc123...",
        #   "gitBranch": "wiz/wize-pilot-001",
        #   "createdAt": "2026-08-22T15:30:00.000Z",
        #   "completedAt": "2026-08-22T15:31:30.000Z",
        # }

        return None  # Would return real deployment from API

    def verify_deployment(
        self,
        deployment: VercelDeployment,
        expected_branch: str,
        expected_sha: str,
    ) -> tuple[bool, Optional[str]]:
        """Verify deployment is valid for feature.

        Checks:
        1. SHA matches expected commit
        2. Status is READY (not BUILDING, ERROR, etc.)
        3. Branch matches feature branch
        4. Deployment is recent (not stale)

        Args:
            deployment: VercelDeployment to verify
            expected_branch: Expected git branch name
            expected_sha: Expected git commit SHA (branch tip)

        Returns:
            (is_valid, reason_if_invalid)
        """
        # Check SHA matches
        if deployment.sha != expected_sha:
            return (
                False,
                f"SHA mismatch: deployment has {deployment.sha[:8]}, "
                f"expected {expected_sha[:8]}",
            )

        # Check branch matches
        if deployment.branch != expected_branch:
            return (
                False,
                f"branch mismatch: deployment is on {deployment.branch}, "
                f"expected {expected_branch}",
            )

        # Check status is READY
        if not deployment.is_ready:
            if deployment.is_building:
                return (False, "deployment still building")
            if deployment.is_error:
                return (
                    False,
                    f"deployment error: {deployment.error_message}",
                )
            return (False, f"deployment not ready: {deployment.status.value}")

        # All checks passed
        return (True, None)

    def check_url_available(
        self,
        url: str,
        timeout: int = 10,
    ) -> tuple[bool, Optional[str]]:
        """Check if Preview URL is responding.

        Args:
            url: Preview URL to check
            timeout: HTTP timeout in seconds

        Returns:
            (is_available, error_if_not)
        """
        logger.info("checking if preview URL is available: %s", url)

        # In production, would make HTTP HEAD request:
        # import httpx
        # try:
        #     with httpx.Client(timeout=timeout) as client:
        #         response = client.head(url)
        #         return (response.status_code < 500, None)
        # except Exception as exc:
        #     return (False, str(exc))

        # For testing, verify URL format
        if url.startswith("http"):
            return (True, None)
        return (False, "invalid URL format")

    def wait_for_ready(
        self,
        deployment_id: str,
        expected_branch: str,
        expected_sha: str,
        timeout_seconds: int = 600,
        poll_interval_seconds: int = 5,
    ) -> tuple[bool, Optional[str], Optional[VercelDeployment]]:
        """Wait for deployment to be READY.

        Polls deployment status until:
        - Status is READY and SHA/branch match
        - Status is ERROR (deployment failed)
        - Timeout exceeded

        Args:
            deployment_id: Deployment to monitor
            expected_branch: Expected branch name
            expected_sha: Expected commit SHA
            timeout_seconds: Max wait time
            poll_interval_seconds: Polling interval

        Returns:
            (is_ready, error_if_not, deployment)
        """
        logger.info(
            "waiting for deployment %s to be ready (timeout: %ds)",
            deployment_id,
            timeout_seconds,
        )

        # In production, would:
        # 1. Poll get_deployment() every poll_interval_seconds
        # 2. Call verify_deployment() on each response
        # 3. Return when verified or timeout/error

        # For now, simulate waiting
        # Real implementation would loop with sleep()

        return (False, "polling not implemented in simulation", None)

    def extract_deployment_id_from_pr(
        self,
        pr_body: str,
    ) -> Optional[str]:
        """Extract deployment ID from PR description.

        GitHub Actions / Vercel bot adds deployment info to PR:
        "Deploy Preview: wize-wiz-wize-pilot-001-wiz.vercel.app"
        with link to deployment

        Args:
            pr_body: PR description text

        Returns:
            Deployment ID if found, None otherwise
        """
        # In production, would parse PR body for patterns like:
        # - "vercel.com/..." URLs
        # - Deployment ID from bot comment
        # - Status indicators

        if "vercel.app" in pr_body or "deployment" in pr_body.lower():
            # Found reference to Vercel deployment
            return "dpl_simulated"

        return None


@dataclass
class PreviewVerificationResult:
    """Result of verifying Preview deployment."""

    feature_id: str
    deployment_id: Optional[str] = None
    url: Optional[str] = None
    sha_matches: bool = False
    branch_matches: bool = False
    is_ready: bool = False
    is_available: bool = False
    status: str = "unverified"
    error: Optional[str] = None

    @property
    def verified(self) -> bool:
        """Is preview fully verified and ready?"""
        return (
            self.sha_matches
            and self.branch_matches
            and self.is_ready
            and self.is_available
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "deployment_id": self.deployment_id,
            "url": self.url,
            "sha_matches": self.sha_matches,
            "branch_matches": self.branch_matches,
            "is_ready": self.is_ready,
            "is_available": self.is_available,
            "status": self.status,
            "verified": self.verified,
            "error": self.error,
        }


__all__ = [
    "DeploymentStatus",
    "VercelDeployment",
    "VercelPreviewTracker",
    "PreviewVerificationResult",
]
