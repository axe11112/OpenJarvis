#!/usr/bin/env python
"""Claude Code availability diagnostics.

This tool verifies that real Claude CLI integration is working, not mocks.
Run this to verify Claude Code is available and properly configured.

Usage:
    python -m openjarvis.wiz.claude_diagnostics
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict

from openjarvis.wiz.claude_cli_executor import (
    ClaudeAvailability,
    ClaudeCliExecutor,
)


def format_diagnostics(diag: Dict[str, Any]) -> str:
    """Format diagnostics for human-readable output."""
    lines = [
        "=" * 70,
        "Claude Code Availability Diagnostics",
        "=" * 70,
        "",
    ]

    lines.append(f"CLI Found:        {diag['cli_found']}")
    lines.append(f"Available:        {diag['available']}")
    lines.append(f"Status:           {diag['availability']}")

    if diag.get("cli_path"):
        lines.append(f"CLI Path:         {diag['cli_path']}")

    if diag.get("authenticated") is not None:
        auth = "YES" if diag["authenticated"] else "NO"
        lines.append(f"Authenticated:    {auth}")

    if diag.get("version"):
        lines.append(f"Version:          {diag['version']}")

    if diag.get("error"):
        lines.append("")
        lines.append(f"Error:            {diag['error']}")

    lines.append("")
    lines.append("=" * 70)

    return "\n".join(lines)


def main() -> int:
    """Run diagnostics and report results."""
    print("\nChecking Claude Code integration...\n")

    executor = ClaudeCliExecutor()
    diag = executor.get_diagnostics()
    diag_dict = diag.to_dict()

    # Print formatted output
    print(format_diagnostics(diag_dict))

    # Print JSON for programmatic access
    print("\nJSON output:")
    print(json.dumps(diag_dict, indent=2))

    # Return appropriate exit code
    if diag.available:
        print("\n✓ Claude Code is available and ready for use.")
        return 0
    else:
        print("\n✗ Claude Code is not available.")
        print(f"  Reason: {diag.error}")
        print(f"  Status: {diag.availability.value}")

        # Provide guidance based on status
        if diag.availability == ClaudeAvailability.CLI_NOT_FOUND:
            print("\n  Action: Install Claude CLI")
            print("  See: https://claude.ai/code")
        elif diag.availability == ClaudeAvailability.NOT_AUTHENTICATED:
            print("\n  Action: Authenticate with Claude CLI")
            print("  Run: claude login")
        elif diag.availability == ClaudeAvailability.UNKNOWN:
            print("\n  Action: Check Claude CLI status manually")
            print("  Run: claude status")

        return 1


if __name__ == "__main__":
    sys.exit(main())
