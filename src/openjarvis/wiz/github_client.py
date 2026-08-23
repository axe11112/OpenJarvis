"""GitHub API client for Wiz autonomous operations.

This client enables Wiz to operate independently without relying on
Claude Code MCP tools. All operations use the real GitHub REST API
with a Personal Access Token stored in the config directory.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx

from openjarvis.core.config import DEFAULT_CONFIG_DIR

logger = logging.getLogger(__name__)

_DEFAULT_TOKEN_PATH = str(DEFAULT_CONFIG_DIR / "connectors" / "github.json")
_GITHUB_API = "https://api.github.com"
_GITHUB_TIMEOUT = 60.0


class GitHubClientError(Exception):
    """GitHub API error."""

    pass


class GitHubClient:
    """Real GitHub REST API client for Wiz operations.

    Requires a GitHub Personal Access Token stored at:
    ~/.config/openjarvis/connectors/github.json
    with content: {"token": "ghp_..."}

    Token must have scopes:
    - repo (full repo access)
    - workflow (Actions)
    - read:org (for merge checks)
    """

    def __init__(self, token_path: str = _DEFAULT_TOKEN_PATH) -> None:
        self.token_path = Path(token_path)
        self._token: Optional[str] = None
        self._client: Optional[httpx.Client] = None

    @property
    def is_configured(self) -> bool:
        """Check if GitHub token is configured."""
        return self.token_path.exists()

    def _load_token(self) -> str:
        """Load GitHub PAT from disk."""
        if not self.token_path.exists():
            raise GitHubClientError(
                f"GitHub token not found at {self.token_path}. "
                f"Create it with: mkdir -p {self.token_path.parent} && "
                f'echo \'{{"token": "ghp_..."}}\' > {self.token_path}'
            )
        data = json.loads(self.token_path.read_text(encoding="utf-8"))
        token = data.get("token")
        if not token:
            raise GitHubClientError(f"No 'token' key in {self.token_path}")
        return token

    def _headers(self) -> dict[str, str]:
        """Get HTTP headers for GitHub API."""
        if self._token is None:
            self._token = self._load_token()
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Make a GitHub API request."""
        url = f"{_GITHUB_API}{path}"
        kwargs.setdefault("timeout", _GITHUB_TIMEOUT)
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
            raise GitHubClientError(f"{method} {url}: {msg}") from e

        return resp.json() if resp.content else {}

    def create_branch(
        self, owner: str, repo: str, branch: str, sha: str
    ) -> dict[str, Any]:
        """Create a branch from a commit SHA."""
        logger.info(f"Creating branch {branch} from {sha[:7]}")
        return self._request(
            "POST",
            f"/repos/{owner}/{repo}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": sha},
        )

    def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str = "",
    ) -> dict[str, Any]:
        """Create a pull request."""
        logger.info(f"Creating PR: {title[:50]}...")
        return self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={
                "title": title,
                "head": head,
                "base": base,
                "body": body,
            },
        )

    def get_pull_request(
        self, owner: str, repo: str, pr_number: int
    ) -> dict[str, Any]:
        """Get pull request details."""
        return self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}")

    def merge_pull_request(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        commit_title: str,
        commit_message: str = "",
    ) -> dict[str, Any]:
        """Merge a pull request with squash."""
        logger.info(f"Merging PR #{pr_number}")
        return self._request(
            "PUT",
            f"/repos/{owner}/{repo}/pulls/{pr_number}/merge",
            json={
                "commit_title": commit_title,
                "commit_message": commit_message,
                "merge_method": "squash",
            },
        )

    def get_commit(self, owner: str, repo: str, ref: str) -> dict[str, Any]:
        """Get commit details."""
        return self._request("GET", f"/repos/{owner}/{repo}/commits/{ref}")

    def get_combined_status(self, owner: str, repo: str, ref: str) -> dict[str, Any]:
        """Get combined status for a commit."""
        return self._request(
            "GET", f"/repos/{owner}/{repo}/commits/{ref}/status"
        )

    def add_comment_to_pr(
        self, owner: str, repo: str, pr_number: int, body: str
    ) -> dict[str, Any]:
        """Add a comment to a pull request."""
        logger.info(f"Adding comment to PR #{pr_number}")
        return self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
            json={"body": body},
        )

    def list_branches(self, owner: str, repo: str) -> list[dict[str, Any]]:
        """List branches in a repository."""
        return self._request("GET", f"/repos/{owner}/{repo}/branches")

    def delete_branch(self, owner: str, repo: str, branch: str) -> None:
        """Delete a branch."""
        logger.info(f"Deleting branch {branch}")
        self._request("DELETE", f"/repos/{owner}/{repo}/git/refs/heads/{branch}")

    def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        """Get repository details."""
        return self._request("GET", f"/repos/{owner}/{repo}")

    def get_user(self) -> dict[str, Any]:
        """Get authenticated user."""
        return self._request("GET", "/user")
