"""Browser capability definitions for Wiz.

Capabilities exposed through the registry:
- browser.open: navigate to a URL
- browser.inspect: read element text/attributes
- browser.interact: click, fill, submit (bounded mutating actions)
- browser.assert: verify visual state
- browser.screenshot: capture viewport
- browser.console: read page console messages
- browser.network: read failed network requests
"""

from __future__ import annotations

from typing import Callable, List

from openjarvis.wiz.authority import Authority
from openjarvis.wiz.capabilities import CapabilitySpec, Risk


def browser_available() -> bool:
    """Check if Playwright is installed and a browser binary is available."""
    try:
        import shutil

        from playwright.sync_api import sync_playwright

        if not shutil.which("chromium"):
            return False
        return True
    except ImportError:
        return False


def browser_capabilities() -> List[CapabilitySpec]:
    """The browser capabilities Wiz declares."""
    from openjarvis.wiz.capabilities import Availability

    def check_browser() -> Availability:
        if browser_available():
            return Availability.ready()
        return Availability.missing("Playwright not installed or browser binary missing")

    return [
        CapabilitySpec(
            name="browser.open",
            summary="navigate to a URL in the browser",
            authority=Authority.READ,
            risk=Risk.LOW,
            probe=check_browser,
        ),
        CapabilitySpec(
            name="browser.inspect",
            summary="read element text, attributes, visibility, and state",
            authority=Authority.READ,
            risk=Risk.LOW,
            probe=check_browser,
        ),
        CapabilitySpec(
            name="browser.interact",
            summary="click, fill, submit, and other bounded user interactions",
            authority=Authority.READ,  # Not SAFE_ACTION; read-only by default
            risk=Risk.LOW,  # Context-dependent (Preview vs Production)
            probe=check_browser,
        ),
        CapabilitySpec(
            name="browser.assert",
            summary="verify element visibility, text content, and attributes",
            authority=Authority.READ,
            risk=Risk.LOW,
            probe=check_browser,
        ),
        CapabilitySpec(
            name="browser.screenshot",
            summary="capture a screenshot of the current viewport",
            authority=Authority.READ,
            risk=Risk.LOW,
            probe=check_browser,
        ),
        CapabilitySpec(
            name="browser.console",
            summary="read page console messages (errors, warnings, logs)",
            authority=Authority.READ,
            risk=Risk.LOW,
            probe=check_browser,
        ),
        CapabilitySpec(
            name="browser.network",
            summary="read failed network requests and response details",
            authority=Authority.READ,
            risk=Risk.LOW,
            probe=check_browser,
        ),
    ]
