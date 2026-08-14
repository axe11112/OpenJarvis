"""Shared fixtures for reliability tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.reliability.fixture_site import FixtureSite

#: Locations where a usable Chromium may live.  Playwright's own resolution is
#: tried first (empty string = let Playwright decide); the globbed paths cover
#: images that ship a browser revision older than the installed Playwright.
_BROWSER_SEARCH_ROOT = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")


def _discover_chromium() -> str:
    """Return a Chromium executable path, or '' to let Playwright choose."""
    explicit = os.environ.get("JARVIS_BROWSER_EXECUTABLE", "")
    if explicit:
        return explicit
    if not _BROWSER_SEARCH_ROOT:
        return ""
    root = Path(_BROWSER_SEARCH_ROOT)
    for candidate in sorted(root.glob("chromium-*/chrome-linux/chrome"), reverse=True):
        if candidate.is_file():
            return str(candidate)
    for candidate in sorted(root.glob("chromium-*/chrome-mac/*.app"), reverse=True):
        if candidate.exists():
            return str(candidate)
    return ""


@pytest.fixture(scope="session")
def chromium_path() -> str:
    """Path to a usable Chromium, or '' for Playwright's default."""
    return _discover_chromium()


@pytest.fixture(scope="session")
def browser_available(chromium_path: str) -> bool:
    """Whether a browser can actually be launched in this environment."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    kwargs = {"headless": True}
    if chromium_path:
        kwargs["executable_path"] = chromium_path
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(**kwargs)
            browser.close()
    except Exception:
        return False
    return True


@pytest.fixture
def require_browser(browser_available: bool) -> None:
    """Skip a test when no browser can be launched."""
    if not browser_available:
        pytest.skip("No launchable Chromium in this environment")


@pytest.fixture
def site():
    """A running fixture site with a healthy login workflow."""
    with FixtureSite() as running:
        yield running


@pytest.fixture
def broken_site():
    """A running fixture site whose login bounces back to /login."""
    with FixtureSite(broken_login=True) as running:
        yield running
