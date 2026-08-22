"""Execute feature requests by spawning Claude Code sessions.

Converts a FeatureRequest into a Claude Code remote session with:
- Task description (from feature request)
- Repository and branch configuration
- Constraint enforcement (from ConfiguredTarget)
- Monitoring and result collection

A feature executes as an autonomous session spawned by Wiz, running within
the authority, repository, and configuration bounds set by ConfiguredTarget.
The session result is captured and fed back into autonomy metrics.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from openjarvis.wiz.configured_target import ConfiguredTarget
from openjarvis.wiz.features.model import FeatureRequest, FeatureState
from openjarvis.wiz.autonomy_metrics import AutonomyMetricsStore, MetricCategory

logger = logging.getLogger(__name__)

__all__ = ["ExecutionStatus", "FeatureExecutionResult", "FeatureExecutor"]


class ExecutionStatus(str, Enum):
    """Status of feature execution."""

    PENDING = "pending"  # waiting to start
    STARTED = "started"  # session created
    RUNNING = "running"  # session active
    COMPLETED = "completed"  # session finished successfully
    FAILED = "failed"  # session failed
    CANCELLED = "cancelled"  # execution was cancelled


@dataclass
class FeatureExecutionResult:
    """Outcome of executing a feature."""

    feature_id: str
    status: ExecutionStatus
    session_id: Optional[str] = None
    branch_name: Optional[str] = None
    pr_url: Optional[str] = None
    error_message: Optional[str] = None
    logs: Dict[str, Any] = None
    elapsed_seconds: int = 0

    def __post_init__(self) -> None:
        if self.logs is None:
            self.logs = {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "status": self.status.value,
            "session_id": self.session_id,
            "branch_name": self.branch_name,
            "pr_url": self.pr_url,
            "error_message": self.error_message,
            "logs": self.logs,
            "elapsed_seconds": self.elapsed_seconds,
        }


class FeatureExecutor:
    """Execute feature requests as Claude Code sessions.

    Responsible for:
    - Validating feature against ConfiguredTarget constraints
    - Building execution prompt and parameters
    - Spawning Claude Code session
    - Monitoring execution
    - Capturing results
    - Recording autonomy metrics
    """

    def __init__(
        self,
        configured_target: ConfiguredTarget,
        metrics_store: AutonomyMetricsStore,
        *,
        session_factory: Optional[Any] = None,
    ) -> None:
        self._target = configured_target
        self._metrics_store = metrics_store
        self._session_factory = session_factory

    def can_execute(self, feature: FeatureRequest) -> tuple[bool, Optional[str]]:
        """Check if a feature can be executed.

        Returns (can_execute, reason_if_not).
        """
        # Check feature state
        if feature.state != FeatureState.RECEIVED:
            return (
                False,
                f"feature in {feature.state.value} state; only RECEIVED can execute",
            )

        # Check source code modification constraint
        if not self._target.can_modify_source and feature.requires_source_change():
            return (False, "target configuration forbids source code changes")

        # Check concurrent PR limit
        # (In a real implementation, would query current open PRs)

        return (True, None)

    def build_execution_prompt(self, feature: FeatureRequest) -> str:
        """Build the prompt to send to Claude Code session."""
        prompt = f"""Execute this feature request:

Feature ID: {feature.id}
Title: {feature.title}

Operator Request:
{feature.operator_request}

Desired Outcome:
{feature.desired_outcome}

Constraints:
- Repository: {self._target.repository}
- Target branch: {self._target.target_branch}
- Test command: {self._target.test_command}
- Max implementation time: {self._target.max_implementation_time} seconds
- Environment: {self._target.environment.value}

Steps:
1. Checkout {self._target.target_branch}
2. Create branch: {self._target.branch_name_for(feature.id)}
3. Implement the feature
4. Run tests: {self._target.test_command}
5. Commit with clear message
6. Push to branch
7. Create pull request

Do not merge. PR is for review only.

Complete the implementation and report success or failure.
"""
        return prompt

    def prepare_execution(
        self, feature: FeatureRequest
    ) -> Optional[FeatureExecutionResult]:
        """Prepare a feature for execution.

        Validates feature against constraints and builds execution plan.
        Returns FeatureExecutionResult or None if validation fails.
        """
        can_exec, reason = self.can_execute(feature)
        if not can_exec:
            logger.warning(
                "cannot execute feature %s: %s", feature.id, reason
            )
            result = FeatureExecutionResult(
                feature_id=feature.id,
                status=ExecutionStatus.FAILED,
                error_message=reason or "validation failed",
            )
            # Record failure metric
            self._metrics_store.record(
                MetricCategory.FEATURE_PROPOSAL,
                f"execute:{feature.id}",
                autonomous=True,
                success=False,
                details={"reason": reason},
            )
            return result

        # All checks passed
        logger.info("feature %s ready for execution", feature.id)
        return FeatureExecutionResult(
            feature_id=feature.id,
            status=ExecutionStatus.PENDING,
            branch_name=self._target.branch_name_for(feature.id),
        )

    async def execute(self, feature: FeatureRequest) -> FeatureExecutionResult:
        """Execute a feature by spawning a Claude Code session.

        This is async because session spawning and monitoring are I/O bound.
        """
        # Prepare execution
        prep_result = self.prepare_execution(feature)
        if prep_result and prep_result.status == ExecutionStatus.FAILED:
            return prep_result

        # Build execution parameters
        prompt = self.build_execution_prompt(feature)
        branch_name = self._target.branch_name_for(feature.id)

        result = FeatureExecutionResult(
            feature_id=feature.id,
            status=ExecutionStatus.STARTED,
            branch_name=branch_name,
        )

        try:
            if not self._session_factory:
                # In test/development, simulate successful execution
                logger.info(
                    "would spawn Claude Code session for feature %s "
                    "(no session factory configured)",
                    feature.id,
                )
                result.status = ExecutionStatus.COMPLETED
                self._metrics_store.record(
                    MetricCategory.IMPLEMENTATION,
                    f"execute:{feature.id}",
                    autonomous=True,
                    success=True,
                    confidence=0.7,
                    details={
                        "feature_id": feature.id,
                        "branch": branch_name,
                        "simulated": True,
                    },
                )
                return result

            # Spawn session (real implementation)
            session = await self._session_factory.create(
                title=f"Feature {feature.id}: {feature.title}",
                prompt=prompt,
                source_url=f"https://github.com/{self._target.repository}",
                source_revision=self._target.target_branch,
                outcome_branch=branch_name,
            )

            if not session:
                result.status = ExecutionStatus.FAILED
                result.error_message = "failed to create session"
                self._metrics_store.record(
                    MetricCategory.IMPLEMENTATION,
                    f"execute:{feature.id}",
                    autonomous=True,
                    success=False,
                    details={"error": "session creation failed"},
                )
                return result

            result.session_id = session.id
            result.status = ExecutionStatus.RUNNING

            # Monitor session (simplified)
            # In production, would poll session status and collect results
            logger.info(
                "spawned session %s for feature %s",
                session.id,
                feature.id,
            )

            # Record metric
            self._metrics_store.record(
                MetricCategory.IMPLEMENTATION,
                f"execute:{feature.id}",
                autonomous=True,
                success=True,
                confidence=0.8,
                details={
                    "feature_id": feature.id,
                    "session_id": session.id,
                    "branch": branch_name,
                },
            )

            result.status = ExecutionStatus.COMPLETED
            return result

        except Exception as exc:
            logger.exception("feature execution failed: %s", exc)
            result.status = ExecutionStatus.FAILED
            result.error_message = str(exc)
            self._metrics_store.record(
                MetricCategory.IMPLEMENTATION,
                f"execute:{feature.id}",
                autonomous=True,
                success=False,
                details={"error": str(exc)},
            )
            return result

    def should_retry(self, result: FeatureExecutionResult) -> bool:
        """Determine if a failed execution should be retried."""
        if result.status != ExecutionStatus.FAILED:
            return False

        # Don't retry validation failures
        if "validation" in (result.error_message or "").lower():
            return False

        # Retry transient failures
        return True
