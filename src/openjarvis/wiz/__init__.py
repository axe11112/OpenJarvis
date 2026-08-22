"""Wiz: Autonomous Wize engineering and operations system.

Wiz monitors, repairs, develops, tests, and operates Wize with minimal human
involvement. The system is designed to handle feature requests, diagnose failures,
and make production changes autonomously within defined safety boundaries.

Core responsibilities:
- Parse owner requests and generate engineering tasks
- Orchestrate Claude Code sessions for implementation
- Manage git branches, PRs, and deployment pipelines
- Verify changes through testing and production verification
- Notify owners of completion or required intervention
"""

from __future__ import annotations

__all__ = [
    "FeatureRequest",
    "FeatureState",
    "Wiz",
]
