"""Wiz — the assistant layer that sits *around* reliability, never inside it.

``wiz`` may import from ``openjarvis.reliability``. ``reliability`` may never
import from ``wiz``. That direction is not a style preference: the reliability
subsystem keeps a production website alive and must continue to work when every
assistant feature in this package is broken, half-written or switched off. A
dependency pointing the other way would make the always-on system depend on the
optional one.

``tests/wiz/test_dependency_direction.py`` enforces this by reading the imports,
so the rule fails a test rather than a code review.
"""

from __future__ import annotations

__all__ = ["__doc__"]
