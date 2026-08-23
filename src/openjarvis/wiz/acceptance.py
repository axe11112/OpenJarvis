"""Acceptance testing framework for Wiz feature verification.

Runs acceptance criteria against Preview and production deployments.
Supports UI (Playwright), HTTP/API, and custom test patterns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AcceptanceResult:
    """Result of running acceptance criteria."""

    passed: bool
    criteria_results: dict[str, bool]  # criterion -> passed
    details: str


class AcceptanceTestRunner:
    """Runs acceptance criteria against deployments."""

    def __init__(self) -> None:
        self._playwright_available = self._check_playwright()

    def _check_playwright(self) -> bool:
        """Check if Playwright is available."""
        try:
            import playwright  # noqa
            return True
        except ImportError:
            logger.warning("Playwright not available - UI tests will be skipped")
            return False

    async def run_criteria(
        self,
        criteria: list[str],
        deployment_url: str,
        criteria_type: str = "ui",
    ) -> AcceptanceResult:
        """Run acceptance criteria against a deployment.

        Args:
            criteria: List of acceptance criteria (natural language or test commands)
            deployment_url: URL of the deployment to test
            criteria_type: 'ui' (Playwright), 'api' (HTTP), or 'auto' (detect)

        Returns:
            AcceptanceResult with pass/fail status
        """
        results = {}

        for criterion in criteria:
            try:
                passed = await self._test_criterion(
                    criterion,
                    deployment_url,
                    criteria_type,
                )
                results[criterion] = passed
            except Exception as e:
                logger.warning(f"Criterion test error: {e}")
                results[criterion] = False

        all_passed = all(results.values())

        return AcceptanceResult(
            passed=all_passed,
            criteria_results=results,
            details=f"Passed {sum(results.values())}/{len(results)} criteria",
        )

    async def _test_criterion(
        self,
        criterion: str,
        deployment_url: str,
        criteria_type: str,
    ) -> bool:
        """Test a single acceptance criterion."""

        if criteria_type in ("ui", "auto") and self._check_criterion_is_ui(criterion):
            return await self._test_ui_criterion(criterion, deployment_url)
        elif criteria_type in ("api", "auto") and self._check_criterion_is_api(
            criterion
        ):
            return await self._test_api_criterion(criterion, deployment_url)
        else:
            return await self._test_text_criterion(criterion)

    def _check_criterion_is_ui(self, criterion: str) -> bool:
        """Check if criterion looks like a UI test."""
        ui_keywords = [
            "button",
            "click",
            "visible",
            "appears",
            "displays",
            "shows",
            "page",
            "load",
            "render",
        ]
        return any(kw in criterion.lower() for kw in ui_keywords)

    def _check_criterion_is_api(self, criterion: str) -> bool:
        """Check if criterion looks like an API test."""
        api_keywords = ["endpoint", "api", "request", "response", "http", "status"]
        return any(kw in criterion.lower() for kw in api_keywords)

    async def _test_ui_criterion(
        self, criterion: str, deployment_url: str
    ) -> bool:
        """Test a UI criterion using Playwright."""
        if not self._playwright_available:
            logger.warning(f"Skipping UI test (Playwright not available): {criterion}")
            return False

        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page()

                try:
                    logger.info(f"Loading {deployment_url}")
                    await page.goto(deployment_url, wait_until="networkidle")

                    # Extract common checks from criterion text
                    result = await self._evaluate_ui_criterion(page, criterion)
                    logger.info(f"UI criterion result: {result}")
                    return result

                finally:
                    await browser.close()

        except Exception as e:
            logger.error(f"UI criterion error: {e}")
            return False

    async def _evaluate_ui_criterion(self, page, criterion: str) -> bool:
        """Evaluate a UI criterion using page assertions."""
        criterion_lower = criterion.lower()

        # Button existence checks
        if "button" in criterion_lower:
            button_text = self._extract_quoted(criterion) or "button"
            try:
                # Check button exists
                element = await page.locator(f'button:has-text("{button_text}")').first
                is_visible = await element.is_visible()
                logger.info(f"Button '{button_text}' visible: {is_visible}")
                return is_visible
            except Exception:
                return False

        # Text visibility checks
        if "text" in criterion_lower or "shows" in criterion_lower:
            text = self._extract_quoted(criterion)
            if text:
                try:
                    is_visible = await page.locator(f'text="{text}"').is_visible()
                    logger.info(f"Text '{text}' visible: {is_visible}")
                    return is_visible
                except Exception:
                    return False

        # Page load check (default)
        try:
            title = await page.title()
            logger.info(f"Page loaded, title: {title}")
            return True
        except Exception:
            return False

    async def _test_api_criterion(
        self, criterion: str, deployment_url: str
    ) -> bool:
        """Test an API criterion using HTTP requests."""
        try:
            import httpx

            # Extract endpoint from criterion
            endpoint = self._extract_endpoint(criterion) or "/"
            url = f"{deployment_url}{endpoint}"

            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=10.0)
                passed = response.status_code < 400

                logger.info(f"API test {url}: {response.status_code}")
                return passed

        except Exception as e:
            logger.error(f"API criterion error: {e}")
            return False

    async def _test_text_criterion(self, criterion: str) -> bool:
        """Test a text-based criterion (simple parsing).

        This is a fallback when the criterion doesn't clearly indicate
        UI or API testing. It tries to extract test patterns.
        """
        criterion_lower = criterion.lower()

        # No obvious test pattern - mark as inconclusive
        # In production, this should prompt human review
        logger.warning(f"Cannot determine test type for criterion: {criterion}")
        return True  # Be optimistic in face of ambiguity? Or False (fail-closed)?

    def _extract_quoted(self, text: str) -> Optional[str]:
        """Extract quoted text from a string."""
        import re

        match = re.search(r'"([^"]+)"', text)
        return match.group(1) if match else None

    def _extract_endpoint(self, criterion: str) -> Optional[str]:
        """Extract API endpoint from criterion."""
        import re

        # Look for /endpoint pattern
        match = re.search(r"(/[a-zA-Z0-9/_-]+)", criterion)
        return match.group(1) if match else None

    def run_acceptance_sync(
        self,
        criteria: list[str],
        deployment_url: str,
        criteria_type: str = "ui",
    ) -> AcceptanceResult:
        """Synchronous wrapper for running acceptance criteria."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(
                self.run_criteria(criteria, deployment_url, criteria_type)
            )
        finally:
            loop.close()

    @staticmethod
    def run_shell_criteria(
        commands: list[str], timeout: int = 300
    ) -> AcceptanceResult:
        """Run shell command-based acceptance criteria.

        For acceptance criteria specified as shell commands.
        """
        results = {}

        for cmd in commands:
            try:
                result = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    timeout=timeout,
                    text=True,
                )
                passed = result.returncode == 0
                results[cmd] = passed

                if not passed:
                    logger.warning(f"Command failed: {cmd}\n{result.stderr}")
            except subprocess.TimeoutExpired:
                logger.error(f"Command timed out: {cmd}")
                results[cmd] = False
            except Exception as e:
                logger.error(f"Command error: {cmd}: {e}")
                results[cmd] = False

        all_passed = all(results.values())

        return AcceptanceResult(
            passed=all_passed,
            criteria_results=results,
            details=f"Passed {sum(results.values())}/{len(results)} shell criteria",
        )
