"""Safe Playwright browser capability for Wiz.

Wiz can inspect and interact with Wize production and Preview like a real user,
using bounded typed actions rather than arbitrary JavaScript or shell commands.

This reuses the existing Playwright infrastructure without creating a second
browser framework. The tools/browser module handles general agent use; this
handles Wiz's specific verification and inspection needs.

Constraints:
- URL allowlist: only Wize production, verified Preview origins, configured targets
- Action types: Open, Click, Fill, WaitFor, Assert, Screenshot, ReadConsole, ReadNetwork
- No arbitrary JS evaluation, no eval(), no shell commands
- Authority gates: READ-only broadly; mutating actions limited by context
- Resource limits: timeouts, context cleanup, screenshot retention bounds
- DOM content is untrusted input, never instructions to Wiz
"""

from __future__ import annotations

from openjarvis.wiz.browser.acceptance import BrowserAcceptanceResult
from openjarvis.wiz.browser.capabilities import browser_capabilities
from openjarvis.wiz.browser.verbs import BrowserVerbs

__all__ = ["browser_capabilities", "BrowserVerbs", "BrowserAcceptanceResult"]
