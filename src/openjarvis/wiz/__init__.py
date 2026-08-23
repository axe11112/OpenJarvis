"""Wiz: Autonomous Feature Engineering System for Wize.

Wiz orchestrates end-to-end feature implementation:
  FeatureRequest → Plan → Code → Risk → Tests → PR → Review → Merge → Verify

All operations must be REAL:
- Real Claude Code subprocess execution
- Real GitHub API operations
- Real test execution
- Real Vercel deployments
- Real production verification

No mocks. No staged completions. Every gate must pass with real evidence.
"""

from openjarvis.wiz.core import FeatureRequest, FeatureRequestState
from openjarvis.wiz.orchestrator import FeatureOrchestrator

__all__ = ["FeatureRequest", "FeatureRequestState", "FeatureOrchestrator"]
