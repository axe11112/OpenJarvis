"""Core Wiz data models for feature requests and engineering state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import uuid4


class FeatureState(Enum):
    """State of a feature request through the engineering pipeline."""

    CREATED = "created"
    PLANNED = "planned"
    IMPLEMENTING = "implementing"
    TESTING = "testing"
    PREVIEWING = "previewing"
    REVIEWING = "reviewing"
    APPROVED_FOR_MERGE = "approved_for_merge"
    MERGED = "merged"
    DEPLOYED_TO_PRODUCTION = "deployed_to_production"
    COMPLETE = "complete"
    FAILED = "failed"
    BLOCKED = "blocked"
    REQUIRES_HUMAN = "requires_human"


class RiskLevel(Enum):
    """Risk classification of a feature based on its scope and impact."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


@dataclass
class FeatureRequest:
    """Represents a request to build or fix a feature in Wize."""

    id: str = field(default_factory=lambda: f"WIZE-{uuid4().hex[:12].upper()}")
    description: str = ""
    owner_input: str = ""
    state: FeatureState = field(default=FeatureState.CREATED)
    risk_level: RiskLevel = field(default=RiskLevel.UNKNOWN)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    git_branch: Optional[str] = None
    feature_sha: Optional[str] = None
    pull_request_number: Optional[int] = None
    preview_url: Optional[str] = None
    preview_sha: Optional[str] = None
    production_sha: Optional[str] = None

    implementation_notes: str = ""
    test_results: str = ""
    review_findings: str = ""

    def __post_init__(self):
        """Validate feature request on creation."""
        if not self.description and not self.owner_input:
            raise ValueError("Either description or owner_input must be provided")

    def update_state(self, new_state: FeatureState):
        """Update state and timestamp."""
        self.state = new_state
        self.updated_at = datetime.utcnow()
