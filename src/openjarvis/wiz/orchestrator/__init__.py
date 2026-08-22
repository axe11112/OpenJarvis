"""Orchestrator: Coordinate the complete feature engineering pipeline.

Ties together:
- RequestDispatcher: Parse owner requests
- RepositoryManager: Git operations (create branches, push)
- TestRunner: Execute npm test, lint, typecheck
- GitHubIntegration: Create PRs, merge
- SafetyGates & MergeGates: Control autonomous decisions
- IncidentManager: Detect and repair failures
- NotificationManager: Notify owner
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from openjarvis.wiz.dispatcher import RequestDispatcher
from openjarvis.wiz.github_integration import GitHubIntegration
from openjarvis.wiz.merge_gates import MergeGates
from openjarvis.wiz.models import FeatureRequest, FeatureState, RiskLevel
from openjarvis.wiz.notifications import NotificationManager, NotificationSeverity
from openjarvis.wiz.repository import RepositoryManager
from openjarvis.wiz.review import CodeReview
from openjarvis.wiz.safety import SafetyGates
from openjarvis.wiz.testing import TestRunner, TestType

logger = logging.getLogger(__name__)


class WizOrchestrator:
    """Coordinate autonomous feature engineering from request to production.

    Pipeline stages:
    1. Receive owner request
    2. Create FeatureRequest
    3. Plan implementation
    4. Spawn Claude Code session
    5. Verify implementation
    6. Test changes
    7. Create PR
    8. Get Vercel Preview
    9. Run acceptance tests
    10. Independent review
    11. Check merge gates
    12. Merge when authorized
    13. Monitor production deployment
    14. Verify production health
    """

    def __init__(
        self,
        wize_repo_path: Path,
        github_owner: str = "axe11112",
        github_repo: str = "Wize-Performance",
        tool_executor: Optional[Callable[[str, dict[str, Any]], Any]] = None,
    ):
        """Initialize orchestrator with all components.

        Args:
            wize_repo_path: Path to Wize-Performance repository
            github_owner: GitHub owner (default: axe11112)
            github_repo: GitHub repo (default: Wize-Performance)
            tool_executor: Optional async function for MCP tool execution
                          Signature: async def(tool_name: str, params: dict) -> result
        """
        self.wize_repo_path = Path(wize_repo_path)
        self.github_owner = github_owner
        self.github_repo = github_repo

        # Initialize all components
        self.dispatcher = RequestDispatcher()
        self.repository = RepositoryManager(self.wize_repo_path)
        self.test_runner = TestRunner()
        self.github = GitHubIntegration(github_owner, github_repo, tool_executor)
        self.notifications = NotificationManager()

        self.requests: dict[str, FeatureRequest] = {}

    def handle_owner_request(self, owner_input: str) -> FeatureRequest:
        """Process owner request and create FeatureRequest.

        Args:
            owner_input: Natural language request from owner

        Returns:
            FeatureRequest ready for implementation
        """
        request = self.dispatcher.dispatch(owner_input)
        self.requests[request.id] = request
        logger.info(f"Created {request.id}: {request.description}")
        return request

    async def implement_feature(self, request: FeatureRequest) -> bool:
        """Implement a feature through the complete pipeline.

        Stages:
        1. Plan: Create branch, set up environment
        2. Implement: Spawn Claude Code session (TODO)
        3. Test: Run lint, typecheck, tests
        4. Preview: Deploy to Vercel (TODO)
        5. Review: Independent code review
        6. Merge: Check gates, create PR, merge
        7. Deploy: Verify production
        8. Notify: Send completion notification

        Args:
            request: FeatureRequest to implement

        Returns:
            True if implementation succeeded
        """
        try:
            # Stage 1: Planning
            request.update_state(FeatureState.PLANNED)
            logger.info(f"{request.id}: Planning implementation")

            # Create feature branch
            try:
                branch_name = self.repository.create_feature_branch(request.id)
                request.git_branch = branch_name
                logger.info(f"{request.id}: Created branch {branch_name}")
            except Exception as e:
                logger.error(f"{request.id}: Failed to create branch: {e}")
                request.update_state(FeatureState.FAILED)
                return False

            # Stage 2: Implementation
            request.update_state(FeatureState.IMPLEMENTING)
            logger.info(f"{request.id}: Spawning Claude Code for implementation")
            # TODO: Spawn Claude Code session and track commits
            # For now, assume implementation happened and we have code changes

            # Get current SHA after implementation
            try:
                request.feature_sha = self.repository.get_current_sha()
                logger.info(f"{request.id}: Implementation SHA {request.feature_sha}")
            except Exception as e:
                logger.error(f"{request.id}: Failed to get SHA: {e}")
                request.update_state(FeatureState.FAILED)
                return False

            # Stage 3: Testing
            request.update_state(FeatureState.TESTING)
            logger.info(f"{request.id}: Running test suite")

            test_result = await self.test_runner.run_unit_tests(str(self.wize_repo_path))
            request.test_results = test_result.summary
            logger.info(f"{request.id}: Tests: {test_result.summary}")

            if not test_result.is_passing:
                logger.warning(f"{request.id}: Tests failing - cannot proceed")
                request.update_state(FeatureState.FAILED)
                return False

            # Stage 4: Preview (TODO: Real Vercel integration)
            request.update_state(FeatureState.PREVIEWING)
            logger.info(f"{request.id}: Would create Vercel Preview")
            # For now, use feature_sha as preview_sha
            request.preview_sha = request.feature_sha

            # Stage 5: Independent Review
            request.update_state(FeatureState.REVIEWING)
            logger.info(f"{request.id}: Running independent code review")
            review = CodeReview(feature_id=request.id)
            # TODO: Run actual AI-driven review

            # Stage 6: Evaluate Gates
            logger.info(f"{request.id}: Checking safety and merge gates")

            safety_results = SafetyGates.evaluate_all_gates(request)
            blocking_safety = [r for r in safety_results if r.blocking]
            if blocking_safety:
                logger.error(f"{request.id}: Safety gates blocking: {blocking_safety}")
                request.update_state(FeatureState.REQUIRES_HUMAN)
                return False

            merge_result = MergeGates.evaluate(request, review)
            if not merge_result.can_merge:
                logger.warning(f"{request.id}: Merge gates failed: {merge_result.status.value}")
                request.update_state(FeatureState.REQUIRES_HUMAN)
                return False

            logger.info(f"{request.id}: All gates passed - proceeding to merge")

            # Stage 7: Create PR and Merge
            request.update_state(FeatureState.APPROVED_FOR_MERGE)
            logger.info(f"{request.id}: Creating GitHub PR")

            # Push branch
            try:
                self.repository.push_branch(request.git_branch)
                logger.info(f"{request.id}: Pushed branch {request.git_branch}")
            except Exception as e:
                logger.error(f"{request.id}: Failed to push branch: {e}")
                request.update_state(FeatureState.FAILED)
                return False

            # Create PR (would use real GitHub via tool executor if available)
            pr = await self.github.create_pull_request(
                branch=request.git_branch,
                title=f"Wiz: {request.description}",
                description=f"Autonomous feature implementation: {request.description}\n\nTests: {request.test_results}",
                base="main",
            )

            if pr:
                request.pull_request_number = pr.number
                logger.info(f"{request.id}: Created PR #{pr.number}")

                # Merge PR (would use real GitHub merge via tool executor if available)
                merged = await self.github.merge_pull_request(pr.number, merge_method="squash")
                if merged:
                    request.update_state(FeatureState.MERGED)
                    request.production_sha = request.feature_sha
                    logger.info(f"{request.id}: Merged PR #{pr.number}")
                else:
                    logger.error(f"{request.id}: Failed to merge PR #{pr.number}")
                    request.update_state(FeatureState.REQUIRES_HUMAN)
                    return False
            else:
                logger.warning(f"{request.id}: No tool executor - PR creation skipped")
                request.update_state(FeatureState.REQUIRES_HUMAN)
                return False

            # Stage 8: Deploy and Verify
            request.update_state(FeatureState.DEPLOYED_TO_PRODUCTION)
            logger.info(f"{request.id}: Deployed to production")
            # TODO: Verify production health

            # Final: Complete
            request.update_state(FeatureState.COMPLETE)
            logger.info(f"{request.id}: Complete")

            # Notify owner
            try:
                message = f"✅ Feature complete: {request.description}\n"
                message += f"PR #{request.pull_request_number} merged and deployed to production."
                logger.info(f"{request.id}: Would notify owner: {message}")
            except Exception as e:
                logger.warning(f"{request.id}: Failed to send notification: {e}")

            return True

        except Exception as e:
            logger.error(f"{request.id} failed: {e}")
            request.update_state(FeatureState.FAILED)
            return False


__all__ = ["WizOrchestrator"]
