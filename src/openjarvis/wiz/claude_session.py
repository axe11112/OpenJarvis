"""Claude Code session management for autonomous feature implementation."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ClaudeSessionResult:
    """Result of a Claude Code session."""

    session_id: str
    status: str  # success, failure, timeout
    exit_code: int
    output: str
    error_output: str
    commits_made: list[str] = None
    branch_pushed: Optional[str] = None
    duration_seconds: float = 0.0

    def __post_init__(self):
        """Initialize defaults."""
        if self.commits_made is None:
            self.commits_made = []


class ClaudeSessionManager:
    """Manage Claude Code sessions for feature implementation."""

    def __init__(self, claude_cli_path: str = "claude"):
        """Initialize Claude session manager.

        Args:
            claude_cli_path: Path to claude CLI executable
        """
        self.claude_cli = claude_cli_path
        self.verify_cli_available()

    def verify_cli_available(self):
        """Verify Claude CLI is available and working."""
        try:
            result = subprocess.run(
                [self.claude_cli, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Claude CLI returned non-zero: {result.stderr}")
            logger.info(f"Claude CLI available: {result.stdout.strip()}")
        except FileNotFoundError:
            raise RuntimeError(f"Claude CLI not found at {self.claude_cli}")
        except subprocess.TimeoutExpired:
            raise RuntimeError("Claude CLI timeout during verification")

    async def spawn_implementation_session(
        self,
        repo_path: Path,
        feature_id: str,
        feature_description: str,
        branch_name: str,
    ) -> ClaudeSessionResult:
        """Spawn a Claude Code session to implement a feature.

        Args:
            repo_path: Path to Wize repository
            feature_id: Feature ID for tracking
            feature_description: Description of what to build
            branch_name: Git branch for implementation

        Returns:
            ClaudeSessionResult with session details
        """
        start_time = datetime.now()

        prompt = f"""
Implement the following feature for the Wize Performance application:

FEATURE: {feature_id}
DESCRIPTION: {feature_description}

BRANCH: {branch_name}

Requirements:
1. Implement the feature based on the description
2. Run existing tests to ensure no regressions
3. Add tests for new functionality if applicable
4. Create a single well-formatted commit with your changes
5. Push to the feature branch

Do not merge the branch yourself. Let automation handle the merge.
"""

        try:
            # TODO: Implement actual Claude session spawning
            # For now, this is a placeholder that would eventually spawn a real session
            logger.info(f"Would spawn Claude session for {feature_id} on {branch_name}")

            result = ClaudeSessionResult(
                session_id=f"claude-{feature_id}",
                status="not_implemented",
                exit_code=0,
                output="Claude session spawning not yet implemented",
                error_output="",
            )

            duration = (datetime.now() - start_time).total_seconds()
            result.duration_seconds = duration

            return result

        except Exception as e:
            logger.error(f"Claude session failed: {e}")
            duration = (datetime.now() - start_time).total_seconds()
            return ClaudeSessionResult(
                session_id=f"claude-{feature_id}",
                status="failure",
                exit_code=1,
                output="",
                error_output=str(e),
                duration_seconds=duration,
            )


__all__ = ["ClaudeSessionManager", "ClaudeSessionResult"]
