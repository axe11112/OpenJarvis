"""GitHub integration for Wiz PR management.

This module uses MCP GitHub tools to interact with real GitHub repositories.
Available MCP tools:
- mcp__github__create_pull_request: Create new PRs
- mcp__github__merge_pull_request: Merge existing PRs
- mcp__github__pull_request_read: Get PR details, diffs, status, etc.
- mcp__github__add_issue_comment: Add comments to PRs
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class GitHubPullRequest:
    """GitHub pull request information."""

    number: int
    title: str
    description: str
    branch: str
    base_branch: str = "main"
    head_sha: Optional[str] = None
    mergeable: bool = False
    merged: bool = False
    merge_sha: Optional[str] = None

    @property
    def can_merge(self) -> bool:
        """PR is mergeable and all checks pass."""
        return self.mergeable and not self.merged


class GitHubIntegration:
    """Manage GitHub PRs for Wiz features using MCP GitHub tools."""

    def __init__(
        self,
        owner: str,
        repo: str,
        tool_executor: Optional[Callable[[str, dict[str, Any]], Any]] = None,
    ):
        """Initialize GitHub integration.

        Args:
            owner: GitHub repository owner
            repo: GitHub repository name
            tool_executor: Optional async function to execute MCP tools.
                          Signature: async def(tool_name: str, params: dict) -> result
                          If None, operations return placeholders (for testing).
        """
        self.owner = owner
        self.repo = repo
        self.tool_executor = tool_executor

    async def create_pull_request(
        self,
        branch: str,
        title: str,
        description: str,
        base: str = "main",
    ) -> Optional[GitHubPullRequest]:
        """Create a GitHub pull request.

        Args:
            branch: Feature branch (head)
            title: PR title
            description: PR description
            base: Base branch (default: main)

        Returns:
            GitHubPullRequest if successful, None otherwise
        """
        if not self.tool_executor:
            logger.warning(
                f"No tool executor configured. Would create PR: {title} on {branch}"
            )
            return None

        try:
            params = {
                "owner": self.owner,
                "repo": self.repo,
                "title": title,
                "head": branch,
                "base": base,
                "body": description,
            }
            result = await self.tool_executor("mcp__github__create_pull_request", params)

            if result and isinstance(result, dict):
                pr_number = result.get("number")
                head_sha = result.get("head", {}).get("sha")
                mergeable = result.get("mergeable", False)

                logger.info(f"Created PR #{pr_number} for branch {branch}")
                return GitHubPullRequest(
                    number=pr_number,
                    title=title,
                    description=description,
                    branch=branch,
                    base_branch=base,
                    head_sha=head_sha,
                    mergeable=mergeable,
                )
            return None
        except Exception as e:
            logger.error(f"Failed to create PR: {e}")
            return None

    async def get_pr_status(self, pr_number: int) -> Optional[GitHubPullRequest]:
        """Get PR status from GitHub.

        Args:
            pr_number: PR number

        Returns:
            GitHubPullRequest if found, None otherwise
        """
        if not self.tool_executor:
            logger.warning(f"No tool executor configured. Would fetch PR #{pr_number}")
            return None

        try:
            params = {
                "method": "get",
                "owner": self.owner,
                "repo": self.repo,
                "pullNumber": pr_number,
            }
            result = await self.tool_executor("mcp__github__pull_request_read", params)

            if result and isinstance(result, dict):
                pr_data = result.get("pullRequest", result)
                mergeable = pr_data.get("mergeable", False)
                merged = pr_data.get("merged", False)
                head_sha = pr_data.get("head", {}).get("sha")

                return GitHubPullRequest(
                    number=pr_number,
                    title=pr_data.get("title", ""),
                    description=pr_data.get("body", ""),
                    branch=pr_data.get("headRefName", ""),
                    base_branch=pr_data.get("baseRefName", "main"),
                    head_sha=head_sha,
                    mergeable=mergeable,
                    merged=merged,
                )
            return None
        except Exception as e:
            logger.error(f"Failed to get PR status: {e}")
            return None

    async def merge_pull_request(
        self, pr_number: int, merge_method: str = "squash"
    ) -> bool:
        """Merge a pull request.

        Args:
            pr_number: PR number
            merge_method: Merge method (merge, squash, rebase)

        Returns:
            True if merge successful
        """
        if not self.tool_executor:
            logger.warning(
                f"No tool executor configured. Would merge PR #{pr_number} using {merge_method}"
            )
            return False

        try:
            params = {
                "owner": self.owner,
                "repo": self.repo,
                "pullNumber": pr_number,
                "merge_method": merge_method,
            }
            result = await self.tool_executor("mcp__github__merge_pull_request", params)

            if result and isinstance(result, dict):
                merged = result.get("merged", False)
                if merged:
                    logger.info(f"Successfully merged PR #{pr_number}")
                return merged
            return False
        except Exception as e:
            logger.error(f"Failed to merge PR #{pr_number}: {e}")
            return False

    async def get_pr_diff(self, pr_number: int) -> Optional[str]:
        """Get the diff of a pull request.

        Args:
            pr_number: PR number

        Returns:
            Diff as string if found
        """
        if not self.tool_executor:
            logger.warning(f"No tool executor configured. Would fetch diff for PR #{pr_number}")
            return None

        try:
            params = {
                "method": "get_diff",
                "owner": self.owner,
                "repo": self.repo,
                "pullNumber": pr_number,
            }
            result = await self.tool_executor("mcp__github__pull_request_read", params)
            return result if isinstance(result, str) else None
        except Exception as e:
            logger.error(f"Failed to get PR diff: {e}")
            return None

    async def add_pr_comment(self, pr_number: int, comment: str) -> bool:
        """Add a comment to a PR.

        Args:
            pr_number: PR number
            comment: Comment text

        Returns:
            True if comment added successfully
        """
        if not self.tool_executor:
            logger.warning(f"No tool executor configured. Would add comment to PR #{pr_number}")
            return False

        try:
            params = {
                "owner": self.owner,
                "repo": self.repo,
                "issue_number": pr_number,
                "body": comment,
            }
            result = await self.tool_executor("mcp__github__add_issue_comment", params)

            if result and isinstance(result, dict):
                comment_id = result.get("id")
                if comment_id:
                    logger.info(f"Added comment to PR #{pr_number}")
                    return True
            return False
        except Exception as e:
            logger.error(f"Failed to add comment to PR: {e}")
            return False


__all__ = ["GitHubIntegration", "GitHubPullRequest"]
