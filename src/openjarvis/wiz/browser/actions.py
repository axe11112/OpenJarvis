"""Typed browser actions for safe, bounded interaction.

Actions are explicitly typed and validated. No arbitrary JavaScript evaluation,
no constructed commands, no authority mutation from natural language.

Locator preference (resilient over brittle):
1. data-testid, test-id
2. role + accessible name
3. label text
4. placeholder
5. aria-label

Avoid generated CSS selectors (too fragile).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class LocatorType(str, Enum):
    """Resilient locator strategies."""

    TEST_ID = "test-id"  # data-testid attribute
    ROLE = "role"  # accessibility role + accessible name
    LABEL = "label"  # form label text
    PLACEHOLDER = "placeholder"  # input placeholder
    ARIA_LABEL = "aria-label"  # aria-label attribute
    TEXT = "text"  # visible text content (fragile, last resort)


@dataclass(frozen=True)
class Locator:
    """A resilient, typed element selector."""

    type: LocatorType
    value: str

    def to_playwright_selector(self) -> str:
        """Convert to Playwright locator syntax."""
        if self.type == LocatorType.TEST_ID:
            return f"[data-testid='{self._escape_attr(self.value)}']"
        elif self.type == LocatorType.ROLE:
            # value is "button:Submit" or similar
            parts = self.value.split(":", 1)
            role = parts[0]
            name = parts[1] if len(parts) > 1 else ""
            if name:
                return f"[role='{role}'][aria-label='{self._escape_attr(name)}']"
            return f"[role='{role}']"
        elif self.type == LocatorType.LABEL:
            return f"label:text('{self._escape_text(self.value)}') + *"
        elif self.type == LocatorType.PLACEHOLDER:
            return f"[placeholder='{self._escape_attr(self.value)}']"
        elif self.type == LocatorType.ARIA_LABEL:
            return f"[aria-label='{self._escape_attr(self.value)}']"
        elif self.type == LocatorType.TEXT:
            return f":text('{self._escape_text(self.value)}')"
        return ""

    @staticmethod
    def _escape_attr(value: str) -> str:
        """Escape attribute value."""
        return value.replace("'", "\\'")

    @staticmethod
    def _escape_text(value: str) -> str:
        """Escape text selector value."""
        return value.replace("'", "\\'")


# ---------------------------------------------------------------------------
# Action types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Open:
    """Navigate to a URL."""

    url: str

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise ValueError("URL cannot be empty")


@dataclass(frozen=True)
class Click:
    """Click an element."""

    locator: Locator


@dataclass(frozen=True)
class Fill:
    """Fill a text input."""

    locator: Locator
    value: str


@dataclass(frozen=True)
class WaitFor:
    """Wait for an element to appear or disappear."""

    locator: Locator
    state: str = "visible"  # "visible" or "hidden"
    timeout_ms: int = 5000


@dataclass(frozen=True)
class AssertText:
    """Assert element contains text."""

    locator: Locator
    expected: str
    exact: bool = False  # if True, must match exactly


@dataclass(frozen=True)
class AssertVisible:
    """Assert element is visible."""

    locator: Locator


@dataclass(frozen=True)
class Screenshot:
    """Take a screenshot of the viewport."""

    viewport_name: str = "desktop"  # "desktop" or "mobile"


@dataclass(frozen=True)
class ReadConsole:
    """Read console messages (errors, warnings)."""

    pass


@dataclass(frozen=True)
class ReadNetworkFailures:
    """Read failed network requests."""

    pass


# Union of all action types
Action = Open | Click | Fill | WaitFor | AssertText | AssertVisible | Screenshot | ReadConsole | ReadNetworkFailures
