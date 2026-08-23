"""Vercel client for Wiz preview and production verification.

Real Vercel API integration for:
- Getting Preview deployment status
- Obtaining exact deployment SHA
- Verifying deployment readiness
- Production deployment verification
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from openjarvis.core.config import DEFAULT_CONFIG_DIR

logger = logging.getLogger(__name__)

_DEFAULT_TOKEN_PATH = str(DEFAULT_CONFIG_DIR / "connectors" / "vercel.json")
_VERCEL_API = "https://api.vercel.com"
_VERCEL_TIMEOUT = 60.0


class VercelClientError(Exception):
    """Vercel API error."""

    pass


class VercelClient:
    """Real Vercel API client for deployment verification.

    Requires a Vercel API token stored at:
    ~/.config/openjarvis/connectors/vercel.json
    with content: {"token": "..."}
    """

    def __init__(self, token_path: str = _DEFAULT_TOKEN_PATH) -> None:
        self.token_path = Path(token_path)
        self._token: Optional[str] = None

    @property
    def is_configured(self) -> bool:
        """Check if Vercel token is configured."""
        return self.token_path.exists()

    def _load_token(self) -> str:
        """Load Vercel API token from disk."""
        if not self.token_path.exists():
            raise VercelClientError(
                f"Vercel token not found at {self.token_path}. "
                f"Create it with: mkdir -p {self.token_path.parent} && "
                f'echo \'{{"token": "..."}}\' > {self.token_path}'
            )
        data = json.loads(self.token_path.read_text(encoding="utf-8"))
        token = data.get("token")
        if not token:
            raise VercelClientError(f"No 'token' key in {self.token_path}")
        return token

    def _headers(self) -> dict[str, str]:
        """Get HTTP headers for Vercel API."""
        if self._token is None:
            self._token = self._load_token()
        return {
            "Authorization": f"Bearer {self._token}",
        }

    def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> dict:
        """Make a Vercel API request."""
        url = f"{_VERCEL_API}{path}"
        kwargs.setdefault("timeout", _VERCEL_TIMEOUT)
        kwargs.setdefault("headers", self._headers())

        resp = httpx.request(method, url, **kwargs)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            try:
                body = e.response.json()
                msg = body.get("message", str(e))
            except Exception:
                msg = str(e)
            raise VercelClientError(f"{method} {url}: {msg}") from e

        return resp.json() if resp.content else {}

    def get_project(self, project_id: str) -> dict:
        """Get project details."""
        logger.info(f"Fetching project {project_id}")
        return self._request("GET", f"/v1/projects/{project_id}")

    def get_deployment(self, deployment_id: str) -> dict:
        """Get deployment details."""
        logger.info(f"Fetching deployment {deployment_id}")
        result = self._request("GET", f"/v11/deployments/{deployment_id}")
        return result

    def get_deployments(
        self, project_id: str, limit: int = 10
    ) -> list[dict]:
        """List deployments for a project."""
        logger.info(f"Listing deployments for project {project_id}")
        result = self._request(
            "GET",
            f"/v1/projects/{project_id}/deployments",
            params={"limit": limit},
        )
        return result.get("deployments", [])

    def get_preview_for_pr(
        self, project_id: str, pr_number: int
    ) -> Optional[dict]:
        """Get Preview deployment for a PR.

        Returns the deployment details if found, None otherwise.
        """
        deployments = self.get_deployments(project_id)
        for deploy in deployments:
            meta = deploy.get("meta", {})
            if meta.get("pullRequestId") == pr_number:
                return deploy
        return None

    def wait_for_deployment(
        self,
        deployment_id: str,
        timeout_seconds: int = 600,
        check_interval: int = 5,
    ) -> dict:
        """Wait for a deployment to be ready.

        Polls deployment status until READY or FAILED, or timeout.

        Args:
            deployment_id: ID of the deployment
            timeout_seconds: Maximum time to wait
            check_interval: Seconds between checks

        Returns:
            Final deployment state

        Raises:
            VercelClientError if deployment fails or times out
        """
        import time

        start = datetime.utcnow()
        while True:
            deploy = self.get_deployment(deployment_id)
            status = deploy.get("status")
            state = deploy.get("state")

            logger.info(f"Deployment status: {status} / {state}")

            if status == "READY":
                logger.info(f"Deployment {deployment_id} is ready")
                return deploy

            if status in ("FAILED", "CANCELED", "ERROR"):
                raise VercelClientError(
                    f"Deployment {deployment_id} failed: {status}"
                )

            elapsed = (datetime.utcnow() - start).total_seconds()
            if elapsed > timeout_seconds:
                raise VercelClientError(
                    f"Deployment {deployment_id} timed out after {timeout_seconds}s"
                )

            time.sleep(check_interval)

    def get_production_deployment(self, project_id: str) -> dict:
        """Get the current production deployment."""
        deployments = self.get_deployments(project_id, limit=50)
        for deploy in deployments:
            # Production deployments have target="production"
            if deploy.get("target") == "production":
                return deploy
        raise VercelClientError(f"No production deployment found for {project_id}")

    def get_preview_url(self, deployment: dict) -> str:
        """Extract Preview URL from deployment."""
        url = deployment.get("url")
        if not url:
            raise VercelClientError("No URL in deployment")
        # Ensure it has protocol
        if not url.startswith("http"):
            url = f"https://{url}"
        return url

    def get_deployment_sha(self, deployment: dict) -> Optional[str]:
        """Extract git SHA from deployment."""
        meta = deployment.get("meta", {})
        return meta.get("githubCommitSha") or meta.get("gitCommitSha")

    def verify_sha_match(
        self, deployment: dict, expected_sha: str
    ) -> bool:
        """Verify deployment SHA matches expected SHA."""
        actual_sha = self.get_deployment_sha(deployment)
        if not actual_sha:
            logger.warning("No SHA in deployment metadata")
            return False
        match = actual_sha.lower() == expected_sha.lower()
        logger.info(f"SHA verification: {actual_sha[:7]}... {'✓' if match else '✗'}")
        return match
