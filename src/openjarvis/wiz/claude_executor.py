"""Claude Code executor for Wiz autonomous feature implementation.

This module enables Wiz to launch real Claude Code subprocesses,
not relying on MCP tools available only in the current Claude session.

Key requirements:
- Real subprocess execution
- Real installed Claude CLI
- Bounded timeout
- Stdout/stderr captured
- Exit code captured
- No arbitrary shell from owner input
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ClaudeExecutionResult:
    """Result of Claude Code execution."""

    success: bool
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    error_message: Optional[str] = None


class ClaudeExecutor:
    """Manages Claude Code subprocess execution for feature implementation."""

    # Bounded timeout: 30 minutes max per feature implementation
    DEFAULT_TIMEOUT = 1800

    def __init__(self, timeout_seconds: int = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout_seconds
        self._verify_claude_installed()

    def _verify_claude_installed(self) -> None:
        """Verify 'claude' CLI is installed and accessible."""
        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                timeout=5.0,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"claude --version failed: {result.stderr}"
                )
            logger.info(f"Claude CLI available: {result.stdout.strip()}")
        except FileNotFoundError:
            raise RuntimeError(
                "Claude CLI not found. Install with: curl -fsSL https://claude.ai/install | bash"
            )

    def implement_feature(
        self,
        feature_description: str,
        acceptance_criteria: list[str],
        repository_path: str,
        branch_name: str,
        constraints: list[str],
        test_commands: list[str],
    ) -> ClaudeExecutionResult:
        """Execute Claude Code to implement a feature.

        Launches the real Claude CLI in non-interactive mode (-p/--print)
        to generate code, then applies it to the repository.

        Args:
            feature_description: Natural language feature request
            acceptance_criteria: List of acceptance criteria
            repository_path: Path to target repository
            branch_name: Feature branch name
            constraints: List of constraints (e.g., "no_auth_changes")
            test_commands: Commands to run for testing

        Returns:
            ClaudeExecutionResult with implementation status and output
        """
        logger.info(f"Launching Claude to implement: {feature_description[:50]}")

        # Build the prompt for Claude
        prompt = self._build_prompt(
            feature_description=feature_description,
            acceptance_criteria=acceptance_criteria,
            constraints=constraints,
            test_commands=test_commands,
            repository_path=repository_path,
            branch_name=branch_name,
        )

        try:
            start_time = datetime.utcnow()

            # Launch Claude CLI in non-interactive mode with -p (print and exit)
            # This uses the real installed Claude binary
            cmd = [
                "claude",
                "-p",
                "--model", "claude-opus-5",  # Use most capable model
                prompt,
            ]

            logger.info(f"Running Claude CLI for implementation")

            result = subprocess.run(
                cmd,
                cwd=repository_path,
                capture_output=True,
                timeout=self.timeout,
                text=True,
            )

            duration = (datetime.utcnow() - start_time).total_seconds()

            return ClaudeExecutionResult(
                success=result.returncode == 0,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                duration_seconds=duration,
                error_message=None if result.returncode == 0 else result.stderr,
            )

        except subprocess.TimeoutExpired:
            return ClaudeExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=-1,
                duration_seconds=self.timeout,
                error_message=f"Claude execution timed out after {self.timeout}s",
            )
        except Exception as e:
            return ClaudeExecutionResult(
                success=False,
                stdout="",
                stderr="",
                exit_code=-1,
                duration_seconds=0,
                error_message=str(e),
            )

    def _build_prompt(
        self,
        feature_description: str,
        acceptance_criteria: list[str],
        constraints: list[str],
        test_commands: list[str],
        repository_path: str,
        branch_name: str,
    ) -> str:
        """Build the Claude Code prompt for feature implementation."""
        return f"""You are implementing a feature for Wize.

FEATURE DESCRIPTION:
{feature_description}

ACCEPTANCE CRITERIA:
{chr(10).join(f"- {c}" for c in acceptance_criteria)}

CONSTRAINTS:
{chr(10).join(f"- {c}" for c in constraints)}

REQUIRED TESTS:
{chr(10).join(f"- {cmd}" for cmd in test_commands)}

INSTRUCTIONS:
1. Create a feature branch '{branch_name}' from main
2. Implement the feature as described
3. Ensure all acceptance criteria are met
4. Run all tests and verify they pass
5. Do NOT merge to main (feature branch only)
6. Verify git status shows your changes

REPOSITORY:
{repository_path}

When done, verify with:
  git status
  git diff --stat
  git log --oneline -5
"""
