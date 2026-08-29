"""Browser-based feature acceptance results and criteria."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(slots=True)
class BrowserAcceptanceResult:
    """Structured result from browser acceptance verification.

    Binds evidence to exact feature SHA, deployment ID, and Preview URL
    to prevent stale or mismatched results from being used.
    """

    feature_id: str
    feature_sha: str
    deployment_id: str
    preview_url: str

    # Acceptance outcome
    passed: bool = False
    criterion_name: str = ""
    criterion_description: str = ""

    # Interaction evidence
    actions: List[str] = field(default_factory=list)
    locator: str = ""
    expected_value: str = ""
    actual_value: str = ""

    # Browser evidence
    console_errors: List[Dict[str, Any]] = field(default_factory=list)
    network_failures: List[Dict[str, Any]] = field(default_factory=list)

    # Screenshots
    desktop_screenshot_path: str = ""
    mobile_screenshot_path: str = ""

    # Metadata
    timestamp: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for storage/transmission."""
        return {
            "feature_id": self.feature_id,
            "feature_sha": self.feature_sha,
            "deployment_id": self.deployment_id,
            "preview_url": self.preview_url,
            "passed": self.passed,
            "criterion_name": self.criterion_name,
            "criterion_description": self.criterion_description,
            "actions": self.actions,
            "locator": self.locator,
            "expected_value": self.expected_value,
            "actual_value": self.actual_value,
            "console_error_count": len(self.console_errors),
            "network_failure_count": len(self.network_failures),
            "desktop_screenshot": self.desktop_screenshot_path,
            "mobile_screenshot": self.mobile_screenshot_path,
            "timestamp": self.timestamp,
            "error": self.error,
        }

    def evidence_for_retry(self) -> str:
        """Render as feedback for Claude retry."""
        lines = []

        if self.error:
            return f"Browser acceptance error: {self.error}"

        if not self.passed:
            lines.append(f"Criterion FAILED: {self.criterion_description}")
            if self.actual_value != self.expected_value:
                lines.append(f"  Expected: {self.expected_value}")
                lines.append(f"  Actual: {self.actual_value}")

        if self.locator:
            lines.append(f"Locator: {self.locator}")

        if self.actions:
            lines.append(f"Actions: {' → '.join(self.actions)}")

        if self.console_errors:
            lines.append(f"\nConsole errors ({len(self.console_errors)}):")
            for err in self.console_errors[:3]:
                text = err.get("text", "")[:100]
                lines.append(f"  - {text}")

        if self.network_failures:
            lines.append(f"\nNetwork failures ({len(self.network_failures)}):")
            for failure in self.network_failures[:3]:
                url = failure.get("url", "")[:60]
                error = failure.get("error", "")[:40]
                lines.append(f"  - {failure.get('method')} {url}: {error}")

        if self.desktop_screenshot_path:
            lines.append(f"\nScreenshot: {self.desktop_screenshot_path}")

        return "\n".join(lines)
