"""Core Wiz data structures and state management."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class FeatureRequestState(str, Enum):
    """Wiz feature pipeline states."""

    PENDING = "pending"  # Received, not yet planned
    PLANNED = "planned"  # Architecture designed
    IMPLEMENTING = "implementing"  # Claude Code running
    TESTING = "testing"  # Tests executing
    RISKING = "risking"  # Final risk assessment
    PULL_REQUEST = "pull_request"  # PR created
    REVIEWING = "reviewing"  # Independent review
    MERGING = "merging"  # Merge gates validating
    DEPLOYING = "deploying"  # Production deployment
    VERIFYING = "verifying"  # Production verification
    COMPLETE = "complete"  # Feature live and verified
    FAILED = "failed"  # Stopped due to error
    ROLLED_BACK = "rolled_back"  # Reverted after production issue


class RiskLevel(str, Enum):
    """Autonomous merge authority by risk level."""

    LOW = "low"  # Autonomous merge allowed
    MEDIUM = "medium"  # Requires operator approval
    HIGH = "high"  # Requires owner approval
    UNKNOWN = "unknown"  # Refuse merge


@dataclass
class FeatureRequest:
    """A feature request for autonomous implementation."""

    owner: str  # Who requested this
    feature: str  # Natural language description
    repository: str  # e.g., "axe11112/Wize"
    base_branch: str = "main"
    acceptance_criteria: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)  # e.g., ["no_auth_changes"]
    estimated_risk: RiskLevel = RiskLevel.UNKNOWN
    created_at: datetime = field(default_factory=datetime.utcnow)
    state: FeatureRequestState = FeatureRequestState.PENDING

    # Pipeline results
    plan_text: Optional[str] = None
    feature_branch: Optional[str] = None
    claude_session_id: Optional[str] = None
    changed_files: list[str] = field(default_factory=list)
    git_diff: Optional[str] = None
    final_risk: Optional[RiskLevel] = None
    test_results: Optional[dict] = None
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    review_notes: Optional[str] = None
    merge_sha: Optional[str] = None
    production_deployment_id: Optional[str] = None
    production_sha: Optional[str] = None
    acceptance_test_results: Optional[dict] = None
    completion_time: Optional[datetime] = None
    failure_reason: Optional[str] = None


@dataclass
class FeatureImplementation:
    """Result of Claude Code feature implementation."""

    success: bool
    changed_files: list[str]
    git_diff: str
    errors: list[str] = field(default_factory=list)
    test_output: Optional[str] = None


@dataclass
class PreviewDeployment:
    """Vercel Preview deployment state."""

    deployment_id: str
    url: str
    status: str  # "BUILDING", "READY", "FAILED"
    git_sha: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    ready_at: Optional[datetime] = None
