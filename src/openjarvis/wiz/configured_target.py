"""Configured engineering target for Wize.

Wize operates within a pinned configuration: repository, branch, test suite,
approval gate, environment. This configuration cannot be changed at runtime
or by feature requests. It is baked in at startup and is the boundary of
all Wize engineering work.

The target is immutable and frozen. Configuration errors are caught at
initialization, not discovered mid-feature.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

__all__ = ["Environment", "ApprovalGate", "ConfiguredTarget"]


class Environment(str, Enum):
    """Where Wize is operating."""

    DEVELOPMENT = "development"  # local, branch-only
    STAGING = "staging"  # shared staging, PR to main
    PRODUCTION = "production"  # production, full approval gates


class ApprovalGate(str, Enum):
    """What approval is required before merge."""

    NONE = "none"  # development: auto-merge after tests pass
    SINGLE_REVIEW = "single_review"  # staging: one reviewer
    DOUBLE_REVIEW = "double_review"  # production: two reviewers
    OWNER_APPROVAL = "owner_approval"  # production: owner must approve


@dataclass(frozen=True, slots=True)
class ConfiguredTarget:
    """Immutable configuration for Wize engineering.

    The target is:
    - Frozen: cannot be changed after creation
    - Validated: all paths and settings checked at init
    - Documented: why each setting is what it is
    - Atomic: applied as a whole, not piecemeal

    No feature request can override any of these settings.
    """

    #: Repository Wize will work within (e.g., "axe11112/OpenJarvis")
    repository: str

    #: Branch Wize will create PRs against (e.g., "main", "develop")
    target_branch: str

    #: Branch prefix for Wize-created branches (e.g., "wiz/")
    branch_prefix: str = "wiz/"

    #: Environment: development, staging, or production
    environment: Environment = Environment.DEVELOPMENT

    #: Approval gate before merge
    approval_gate: ApprovalGate = ApprovalGate.NONE

    #: Test suite to run on all PRs (e.g., "pytest tests/")
    test_command: str = "make test"

    #: Path to repository (local or URL)
    repo_path: str = ""

    #: Maximum number of PRs Wize can have open simultaneously
    max_concurrent_prs: int = 3

    #: Maximum implementation time before auto-close (seconds)
    max_implementation_time: int = 3600

    #: Whether Wize can modify existing non-test files (vs. only test updates)
    can_modify_source: bool = True

    #: Whether Wize can run integration tests
    can_run_integration_tests: bool = False

    #: Slack channel for notifications (if any)
    notification_channel: Optional[str] = None

    #: Owner email for critical approvals
    owner_email: Optional[str] = None

    def __post_init__(self) -> None:
        """Validate configuration at creation time."""
        if not self.repository or "/" not in self.repository:
            raise ValueError(
                f"repository must be in 'owner/repo' format, got {self.repository!r}"
            )

        if not self.target_branch:
            raise ValueError("target_branch cannot be empty")

        if not self.branch_prefix:
            raise ValueError("branch_prefix cannot be empty")

        if self.max_concurrent_prs < 1:
            raise ValueError("max_concurrent_prs must be >= 1")

        if self.max_implementation_time < 60:
            raise ValueError("max_implementation_time must be >= 60 seconds")

        # Production requires strong approval gates
        if (
            self.environment == Environment.PRODUCTION
            and self.approval_gate == ApprovalGate.NONE
        ):
            logger.warning(
                "PRODUCTION environment with NO approval gate; "
                "this is likely a misconfiguration"
            )

        # Development with double review is wasteful
        if (
            self.environment == Environment.DEVELOPMENT
            and self.approval_gate == ApprovalGate.DOUBLE_REVIEW
        ):
            logger.debug(
                "DEVELOPMENT environment with DOUBLE_REVIEW; "
                "consider using SINGLE_REVIEW or NONE"
            )

    def validate(self) -> tuple[bool, List[str]]:
        """Validate that the target is ready to use.

        Returns (is_valid, error_messages). Useful for preflight checks.
        """
        errors: List[str] = []

        # Check repository format
        if "/" not in self.repository:
            errors.append(f"repository '{self.repository}' is not owner/repo format")

        # Check branch names are valid
        if not self.target_branch.replace("-", "").replace("_", "").isalnum():
            errors.append(f"target_branch '{self.target_branch}' contains invalid chars")

        if self.branch_prefix and "/" in self.branch_prefix:
            errors.append(f"branch_prefix '{self.branch_prefix}' contains /")

        # Check test command is not empty
        if not self.test_command:
            errors.append("test_command cannot be empty")

        # Check constraints
        if self.max_concurrent_prs < 1:
            errors.append("max_concurrent_prs must be >= 1")

        if self.max_implementation_time < 60:
            errors.append("max_implementation_time must be >= 60 seconds")

        # Check environment/gate compatibility
        if (
            self.environment == Environment.PRODUCTION
            and self.approval_gate in (ApprovalGate.NONE, ApprovalGate.SINGLE_REVIEW)
        ):
            errors.append(
                f"PRODUCTION with {self.approval_gate.value} approval is dangerous"
            )

        return (len(errors) == 0, errors)

    def to_dict(self) -> dict:
        """Serialize configuration (for logging, not persistence)."""
        return {
            "repository": self.repository,
            "target_branch": self.target_branch,
            "branch_prefix": self.branch_prefix,
            "environment": self.environment.value,
            "approval_gate": self.approval_gate.value,
            "test_command": self.test_command,
            "repo_path": self.repo_path,
            "max_concurrent_prs": self.max_concurrent_prs,
            "max_implementation_time": self.max_implementation_time,
            "can_modify_source": self.can_modify_source,
            "can_run_integration_tests": self.can_run_integration_tests,
            "notification_channel": self.notification_channel,
            "owner_email": self.owner_email,
        }

    def branch_name_for(self, feature_id: str) -> str:
        """Generate a branch name for a feature."""
        # feature IDs are like FEAT-001 or PROACTIVE-ABCD1234
        # Branch names can't have colons, so replace with dash
        safe_id = feature_id.lower().replace(":", "-").replace(" ", "-")
        return f"{self.branch_prefix}{safe_id}"

    def should_skip_implementation(self, reason: Optional[str] = None) -> bool:
        """Determine if implementation should be skipped.

        Used for preflight checks before starting work.
        """
        if not self.can_modify_source and reason == "source_code":
            return True
        if not self.can_run_integration_tests and reason == "integration_tests":
            return True
        return False
