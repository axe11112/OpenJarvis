"""Repository management: Handle git operations for Wiz features."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


class RepositoryManager:
    """Manage git repository operations for autonomous feature branches."""

    def __init__(self, repo_path: Path):
        """Initialize with path to Wize repository.

        Args:
            repo_path: Path to Wize-Performance git repository
        """
        self.repo_path = Path(repo_path)
        if not (self.repo_path / ".git").exists():
            raise ValueError(f"Not a git repository: {repo_path}")

    def create_feature_branch(self, feature_id: str, base_branch: str = "main") -> str:
        """Create a new feature branch.

        Args:
            feature_id: Feature ID (e.g., WIZE-abc123)
            base_branch: Base branch to branch from

        Returns:
            Branch name created
        """
        branch_name = f"wiz/{feature_id.lower()}"
        try:
            subprocess.run(
                ["git", "fetch", "origin", base_branch],
                cwd=self.repo_path,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "checkout", "-b", branch_name, f"origin/{base_branch}"],
                cwd=self.repo_path,
                check=True,
                capture_output=True,
            )
            return branch_name
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to create feature branch: {e.stderr.decode()}")

    def get_current_sha(self) -> str:
        """Get current HEAD SHA."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_path,
                check=True,
                capture_output=True,
                text=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to get current SHA: {e.stderr}")

    def get_diff(self, base_branch: str = "origin/main") -> str:
        """Get diff of current branch against base.

        Args:
            base_branch: Base branch to compare against

        Returns:
            Diff output
        """
        try:
            result = subprocess.run(
                ["git", "diff", base_branch],
                cwd=self.repo_path,
                check=True,
                capture_output=True,
                text=True,
            )
            return result.stdout
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to get diff: {e.stderr}")

    def push_branch(self, branch_name: str) -> bool:
        """Push feature branch to origin.

        Args:
            branch_name: Branch to push

        Returns:
            True if successful
        """
        try:
            subprocess.run(
                ["git", "push", "-u", "origin", branch_name],
                cwd=self.repo_path,
                check=True,
                capture_output=True,
            )
            return True
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to push branch: {e.stderr.decode()}")


__all__ = ["RepositoryManager"]
