"""Testing framework for Wiz features.

Implements real test execution via subprocess commands (npm, pytest, etc).
Parses test output to extract pass/fail statistics.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TestType(Enum):
    """Types of tests Wiz can run."""

    UNIT = "unit"
    INTEGRATION = "integration"
    ACCEPTANCE = "acceptance"
    PRODUCTION = "production"
    REGRESSION = "regression"


@dataclass
class TestResult:
    """Result of running a test suite."""

    test_type: TestType
    passed: int
    failed: int
    skipped: int
    errors: int = 0
    output: str = ""
    duration_seconds: float = 0.0

    @property
    def is_passing(self) -> bool:
        """Test suite is passing if no failures or errors."""
        return self.failed == 0 and self.errors == 0

    @property
    def total_run(self) -> int:
        """Total tests run."""
        return self.passed + self.failed + self.errors

    @property
    def summary(self) -> str:
        """Generate summary line."""
        return (
            f"{self.passed} passed, {self.failed} failed, "
            f"{self.errors} errors, {self.skipped} skipped"
        )


class AcceptanceTestGenerator:
    """Generate acceptance tests for Wiz features."""

    @staticmethod
    def generate_basic_acceptance_tests(feature_description: str) -> str:
        """Generate basic acceptance test from feature description.

        Args:
            feature_description: Description of the feature

        Returns:
            Python test code as string
        """
        # TODO: Implement AI-driven acceptance test generation
        # For now, return placeholder
        return f"""
# Generated acceptance tests for: {feature_description}
# TODO: Implement specific acceptance criteria
"""


class TestRunner:
    """Run tests for Wiz features via subprocess commands."""

    async def _run_command(self, cmd: str, cwd: str) -> tuple[int, str]:
        """Run a shell command and return exit code and output.

        Args:
            cmd: Command to run
            cwd: Working directory

        Returns:
            Tuple of (exit_code, output)
        """
        try:
            process = await asyncio.create_subprocess_shell(
                cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await process.communicate()
            output = stdout.decode("utf-8", errors="replace")
            return process.returncode, output
        except Exception as e:
            logger.error(f"Failed to run command '{cmd}': {e}")
            return 1, str(e)

    def _parse_npm_test_output(self, output: str) -> tuple[int, int, int, int]:
        """Parse npm test output to extract pass/fail counts.

        Args:
            output: npm test command output

        Returns:
            Tuple of (passed, failed, skipped, errors)
        """
        passed = failed = skipped = errors = 0

        # Try to find Jest-style summary
        jest_match = re.search(
            r"Tests:\s+(\d+)\s+passed,\s+(\d+)\s+failed,\s+(\d+)\s+(?:skipped|todo)",
            output,
        )
        if jest_match:
            passed = int(jest_match.group(1))
            failed = int(jest_match.group(2))
            skipped = int(jest_match.group(3))
            return passed, failed, skipped, errors

        # Try alternative format
        if " passed" in output.lower():
            passed_match = re.search(r"(\d+)\s+passed", output)
            if passed_match:
                passed = int(passed_match.group(1))

        if " failed" in output.lower():
            failed_match = re.search(r"(\d+)\s+failed", output)
            if failed_match:
                failed = int(failed_match.group(1))

        if " skip" in output.lower():
            skip_match = re.search(r"(\d+)\s+skip", output)
            if skip_match:
                skipped = int(skip_match.group(1))

        return passed, failed, skipped, errors

    async def run_unit_tests(self, repo_path: str) -> TestResult:
        """Run unit tests for the repository.

        Tries: npm test, yarn test, pytest, or cargo test depending on project type.

        Args:
            repo_path: Path to repository

        Returns:
            TestResult with pass/fail summary
        """
        repo = Path(repo_path)

        # Determine test command based on project structure
        test_cmd = "npm test"
        if (repo / "yarn.lock").exists():
            test_cmd = "yarn test"
        elif (repo / "Cargo.toml").exists():
            test_cmd = "cargo test"
        elif (repo / "pytest.ini").exists() or (repo / "pyproject.toml").exists():
            test_cmd = "pytest tests/ -q"

        logger.info(f"Running tests in {repo_path}: {test_cmd}")
        exit_code, output = await self._run_command(test_cmd, str(repo))

        passed, failed, skipped, errors = self._parse_npm_test_output(output)

        return TestResult(
            test_type=TestType.UNIT,
            passed=passed,
            failed=failed,
            skipped=skipped,
            errors=errors,
            output=output,
        )

    async def run_lint(self, repo_path: str) -> TestResult:
        """Run linting for the repository.

        Args:
            repo_path: Path to repository

        Returns:
            TestResult with pass/fail summary
        """
        repo = Path(repo_path)
        lint_cmd = "npm run lint"

        if (repo / "yarn.lock").exists():
            lint_cmd = "yarn lint"
        elif (repo / "ruff.toml").exists() or (repo / "pyproject.toml").exists():
            lint_cmd = "ruff check ."

        logger.info(f"Running lint in {repo_path}: {lint_cmd}")
        exit_code, output = await self._run_command(lint_cmd, str(repo))

        # Lint passes if exit code is 0
        passed = 1 if exit_code == 0 else 0
        failed = 0 if exit_code == 0 else 1

        return TestResult(
            test_type=TestType.UNIT,
            passed=passed,
            failed=failed,
            skipped=0,
            output=output,
        )

    async def run_typecheck(self, repo_path: str) -> TestResult:
        """Run type checking for the repository.

        Args:
            repo_path: Path to repository

        Returns:
            TestResult with pass/fail summary
        """
        repo = Path(repo_path)
        typecheck_cmd = "npm run typecheck"

        if (repo / "yarn.lock").exists():
            typecheck_cmd = "yarn typecheck"
        elif (repo / "pyproject.toml").exists():
            typecheck_cmd = "mypy . --quiet"

        logger.info(f"Running typecheck in {repo_path}: {typecheck_cmd}")
        exit_code, output = await self._run_command(typecheck_cmd, str(repo))

        passed = 1 if exit_code == 0 else 0
        failed = 0 if exit_code == 0 else 1

        return TestResult(
            test_type=TestType.UNIT,
            passed=passed,
            failed=failed,
            skipped=0,
            output=output,
        )

    async def run_acceptance_tests(self, repo_path: str, preview_url: str) -> TestResult:
        """Run acceptance tests against Vercel Preview.

        Uses Playwright to test the Preview URL if tests are available.

        Args:
            repo_path: Path to repository
            preview_url: Vercel Preview URL to test

        Returns:
            TestResult with pass/fail summary
        """
        repo = Path(repo_path)

        # Try to find and run Playwright tests
        if (repo / "playwright.config.ts").exists() or (repo / "e2e" / "tests").exists():
            cmd = f"npx playwright test --base-url={preview_url}"
            logger.info(f"Running Playwright tests against {preview_url}")
            exit_code, output = await self._run_command(cmd, str(repo))

            passed, failed, skipped, errors = self._parse_npm_test_output(output)

            return TestResult(
                test_type=TestType.ACCEPTANCE,
                passed=passed,
                failed=failed,
                skipped=skipped,
                errors=errors,
                output=output,
            )

        logger.warning(f"No acceptance tests found in {repo_path}")
        return TestResult(
            test_type=TestType.ACCEPTANCE,
            passed=0,
            failed=0,
            skipped=0,
            output="No acceptance tests configured",
        )


__all__ = ["TestRunner", "TestResult", "TestType", "AcceptanceTestGenerator"]
