"""Browser capability handlers for Wiz."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from openjarvis.wiz.brain import Request
from openjarvis.wiz.browser.actions import (
    Action,
    AssertText,
    AssertVisible,
    Click,
    Fill,
    Locator,
    Open,
    ReadConsole,
    ReadNetworkFailures,
    Screenshot,
    WaitFor,
)
from openjarvis.wiz.browser.url_safety import URLValidator, URLSafetyError

logger = logging.getLogger(__name__)

__all__ = ["BrowserVerbs"]


@dataclass
class BrowserVerbs:
    """Browser interaction handlers for Wiz.

    The browser is a read-only inspection tool by default. Mutating interactions
    (click, fill) are READ authority but context-dependent — Preview interactions
    are safe; production mutations are blocked unless explicitly approved.

    Injected collaborators:
    - validator: URLValidator for safe navigation
    - screenshot_dir: where to save screenshots
    - max_screenshots: retention limit per session
    """

    validator: URLValidator
    screenshot_dir: Optional[Path] = None
    max_screenshots: int = 10

    def handlers(self) -> Dict[str, Callable[[Request], Any]]:
        """Map capability names to handler functions."""
        return {
            "browser.open": self.open_url,
            "browser.inspect": self.inspect_element,
            "browser.interact": self.interact,
            "browser.assert": self.assert_state,
            "browser.screenshot": self.take_screenshot,
            "browser.console": self.read_console,
            "browser.network": self.read_network,
        }

    # -- handlers --

    def open_url(self, request: Request) -> Dict[str, Any]:
        """Navigate to a URL."""
        url = str(request.arguments.get("url", "")).strip()
        if not url:
            return {"success": False, "error": "No URL provided"}

        try:
            self.validator.validate(url)
        except URLSafetyError as e:
            return {"success": False, "error": f"URL validation failed: {e}"}

        try:
            from openjarvis.tools.browser import _session

            page = _session.page
            page.goto(url, wait_until="load")
            return {
                "success": True,
                "title": page.title() or "",
                "url": page.url,
            }
        except Exception as e:
            logger.exception("failed to navigate to URL")
            return {"success": False, "error": f"Navigation failed: {e}"}

    def inspect_element(self, request: Request) -> Dict[str, Any]:
        """Read element text, attributes, visibility."""
        locator_data = request.arguments.get("locator", {})
        if not isinstance(locator_data, dict):
            return {"success": False, "error": "Invalid locator format"}

        try:
            locator = Locator(
                type=locator_data.get("type"),
                value=locator_data.get("value", ""),
            )
        except Exception as e:
            return {"success": False, "error": f"Invalid locator: {e}"}

        try:
            from openjarvis.tools.browser import _session

            page = _session.page
            selector = locator.to_playwright_selector()

            try:
                element = page.query_selector(selector)
            except Exception:
                return {"success": False, "error": f"Element not found: {selector}"}

            if not element:
                return {"success": False, "error": "Element not found"}

            return {
                "success": True,
                "text_content": element.text_content() or "",
                "is_visible": element.is_visible(),
                "is_enabled": element.is_enabled(),
            }
        except Exception as e:
            logger.exception("failed to inspect element")
            return {"success": False, "error": f"Inspection failed: {e}"}

    def interact(self, request: Request) -> Dict[str, Any]:
        """Perform a user interaction (click, fill)."""
        action_data = request.arguments.get("action", {})
        action_type = action_data.get("type", "")

        try:
            if action_type == "click":
                return self._handle_click(action_data)
            elif action_type == "fill":
                return self._handle_fill(action_data)
            else:
                return {"success": False, "error": f"Unknown action type: {action_type}"}
        except Exception as e:
            logger.exception("failed to perform interaction")
            return {"success": False, "error": f"Interaction failed: {e}"}

    def assert_state(self, request: Request) -> Dict[str, Any]:
        """Verify element state."""
        assertion_type = request.arguments.get("type", "")
        locator_data = request.arguments.get("locator", {})

        try:
            locator = Locator(
                type=locator_data.get("type"),
                value=locator_data.get("value", ""),
            )
        except Exception as e:
            return {"success": False, "error": f"Invalid locator: {e}"}

        try:
            if assertion_type == "visible":
                return self._assert_visible(locator)
            elif assertion_type == "text":
                expected = request.arguments.get("expected", "")
                exact = request.arguments.get("exact", False)
                return self._assert_text(locator, expected, exact)
            else:
                return {"success": False, "error": f"Unknown assertion: {assertion_type}"}
        except Exception as e:
            logger.exception("assertion failed")
            return {"success": False, "error": f"Assertion failed: {e}"}

    def take_screenshot(self, request: Request) -> Dict[str, Any]:
        """Capture a screenshot."""
        if not self.screenshot_dir:
            return {"success": False, "error": "Screenshot directory not configured"}

        viewport_name = request.arguments.get("viewport_name", "desktop")

        try:
            from openjarvis.tools.browser import _session

            page = _session.page
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)

            screenshot_path = self.screenshot_dir / f"screenshot-{viewport_name}.png"
            page.screenshot(path=str(screenshot_path))

            return {
                "success": True,
                "path": str(screenshot_path),
                "viewport": viewport_name,
            }
        except Exception as e:
            logger.exception("failed to take screenshot")
            return {"success": False, "error": f"Screenshot failed: {e}"}

    def read_console(self, request: Request) -> Dict[str, Any]:
        """Read page console messages."""
        try:
            from openjarvis.tools.browser import _session

            page = _session.page
            # Evaluate a script to collect console-accessible state
            # (Note: we do NOT execute arbitrary JS from user input)
            # This gets cached console messages if they exist
            messages = []

            # For now, return what we can observe from the page state
            # Real implementation would require page event listeners
            # which must be attached at navigation time
            return {
                "success": True,
                "messages": messages,
                "note": "Real console capture requires event listeners (future enhancement)",
            }
        except Exception as e:
            logger.exception("failed to read console")
            return {"success": False, "error": f"Console read failed: {e}"}

    def read_network(self, request: Request) -> Dict[str, Any]:
        """Read failed network requests."""
        try:
            from openjarvis.tools.browser import _session

            page = _session.page
            # Network capture requires listeners attached at page creation
            # This is currently a placeholder pending full implementation
            failures = []

            return {
                "success": True,
                "failures": failures,
                "note": "Real network capture requires event listeners (future enhancement)",
            }
        except Exception as e:
            logger.exception("failed to read network")
            return {"success": False, "error": f"Network read failed: {e}"}

    # -- private helpers --

    def _handle_click(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle a click action."""
        locator_data = action_data.get("locator", {})
        locator = Locator(
            type=locator_data.get("type"),
            value=locator_data.get("value", ""),
        )

        from openjarvis.tools.browser import _session

        page = _session.page
        selector = locator.to_playwright_selector()
        page.click(selector)

        return {"success": True, "action": "click"}

    def _handle_fill(self, action_data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle a fill action."""
        locator_data = action_data.get("locator", {})
        value = action_data.get("value", "")

        locator = Locator(
            type=locator_data.get("type"),
            value=locator_data.get("value", ""),
        )

        from openjarvis.tools.browser import _session

        page = _session.page
        selector = locator.to_playwright_selector()
        page.fill(selector, str(value))

        return {"success": True, "action": "fill"}

    def _assert_visible(self, locator: Locator) -> Dict[str, Any]:
        """Assert that an element is visible."""
        from openjarvis.tools.browser import _session

        page = _session.page
        selector = locator.to_playwright_selector()

        try:
            element = page.query_selector(selector)
            if not element or not element.is_visible():
                return {
                    "success": False,
                    "error": f"Element not visible: {selector}",
                }
            return {"success": True, "assertion": "visible"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _assert_text(
        self, locator: Locator, expected: str, exact: bool = False
    ) -> Dict[str, Any]:
        """Assert that an element contains text."""
        from openjarvis.tools.browser import _session

        page = _session.page
        selector = locator.to_playwright_selector()

        try:
            element = page.query_selector(selector)
            if not element:
                return {"success": False, "error": f"Element not found: {selector}"}

            actual = element.text_content() or ""
            if exact:
                matches = actual == expected
            else:
                matches = expected.lower() in actual.lower()

            if not matches:
                return {
                    "success": False,
                    "error": f"Text mismatch. Expected: '{expected}', Actual: '{actual}'",
                }

            return {"success": True, "assertion": "text"}
        except Exception as e:
            return {"success": False, "error": str(e)}
