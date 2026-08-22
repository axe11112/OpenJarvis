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
from openjarvis.wiz.claude_cli_executor import (
    ClaudeCliExecutor,
    ClaudeDiagnostics,
)

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
        cli_executor: Optional[ClaudeCliExecutor] = None,
    ) -> None:
        self._target = configured_target
        self._metrics_store = metrics_store
        self._cli_executor = cli_executor or ClaudeCliExecutor()
        self._claude_diagnostics: Optional[ClaudeDiagnostics] = None

    def get_claude_diagnostics(self) -> ClaudeDiagnostics:
        """Get diagnostics about Claude Code availability.

        Returns:
            ClaudeDiagnostics with CLI status, authentication, version, etc.
        """
        if self._claude_diagnostics is None:
            self._claude_diagnostics = self._cli_executor.get_diagnostics()
        return self._claude_diagnostics

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
        # Record success metric
        self._metrics_store.record(
            MetricCategory.FEATURE_PROPOSAL,
            f"execute:{feature.id}",
            autonomous=True,
            success=True,
            details={"status": "ready"},
        )
        return FeatureExecutionResult(
            feature_id=feature.id,
            status=ExecutionStatus.PENDING,
            branch_name=self._target.branch_name_for(feature.id),
        )

    async def execute(self, feature: FeatureRequest) -> FeatureExecutionResult:
        """Execute a feature by spawning a real Claude Code session.

        Uses the REAL installed Claude CLI tool, not mocks or simulations.
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
            # Check Claude availability first
            diag = self.get_claude_diagnostics()
            if not diag.available:
                result.status = ExecutionStatus.FAILED
                result.error_message = (
                    f"Claude CLI not available: {diag.error} "
                    f"(availability={diag.availability.value})"
                )
                logger.warning(
                    "cannot execute feature %s: %s",
                    feature.id,
                    result.error_message,
                )
                self._metrics_store.record(
                    MetricCategory.IMPLEMENTATION,
                    f"execute:{feature.id}",
                    autonomous=True,
                    success=False,
                    details={"error": "claude_unavailable", "diagnostics": diag.to_dict()},
                )
                return result

            # Spawn real Claude Code session via CLI
            cli_result = self._cli_executor.execute_session(
                title=f"Feature {feature.id}: {feature.title}",
                prompt=prompt,
                repository=self._target.repository,
                branch=branch_name,
            )

            if not cli_result.success:
                result.status = ExecutionStatus.FAILED
                result.error_message = cli_result.error or "session execution failed"
                logger.warning(
                    "claude session failed for feature %s: %s",
                    feature.id,
                    result.error_message,
                )
                self._metrics_store.record(
                    MetricCategory.IMPLEMENTATION,
                    f"execute:{feature.id}",
                    autonomous=True,
                    success=False,
                    details={
                        "error": "session_failed",
                        "returncode": cli_result.returncode,
                        "stderr": cli_result.stderr,
                    },
                )
                return result

            # Session succeeded
            result.session_id = cli_result.session_id or f"claude-{feature.id}"
            result.status = ExecutionStatus.RUNNING

            logger.info(
                "spawned real Claude Code session %s for feature %s "
                "(branch=%s, repository=%s)",
                result.session_id,
                feature.id,
                branch_name,
                self._target.repository,
            )

            # Record success metric
            self._metrics_store.record(
                MetricCategory.IMPLEMENTATION,
                f"execute:{feature.id}",
                autonomous=True,
                success=True,
                confidence=0.9,
                details={
                    "feature_id": feature.id,
                    "session_id": result.session_id,
                    "branch": branch_name,
                    "real_claude_cli": True,
                    "repository": self._target.repository,
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
                details={"error": str(exc), "exception_type": type(exc).__name__},
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
