#!/usr/bin/env python3
"""Demonstration of Wiz autonomous feature engineering pipeline.

This example shows how Wiz would process a complete feature request from
owner input through all pipeline stages to shipping.

Run this to see the current capabilities of the Wiz system.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from openjarvis.wiz.dispatcher import RequestDispatcher
from openjarvis.wiz.github_integration import GitHubIntegration
from openjarvis.wiz.memory import WizMemory
from openjarvis.wiz.merge_gates import MergeGates
from openjarvis.wiz.models import FeatureState
from openjarvis.wiz.notifications import NotificationManager, NotificationSeverity
from openjarvis.wiz.review import CodeReview, CodeReviewFinding, FindingCategory, FindingSeverity
from openjarvis.wiz.safety import SafetyGates


def demo_low_risk_feature():
    """Demonstrate a LOW risk feature going through the complete pipeline."""
    print("\n" + "=" * 70)
    print("WIZ AUTONOMOUS FEATURE DEMO")
    print("=" * 70)

    # Setup memory
    memory = WizMemory()

    # Stage 1: Owner request
    print("\n[1] OWNER REQUEST")
    print("-" * 70)
    owner_input = "Add a refresh button to the dashboard"
    print(f"Owner: '{owner_input}'")

    dispatcher = RequestDispatcher()
    request = dispatcher.dispatch(owner_input)
    memory.save_feature(request)
    memory.add_audit(request.id, "Feature request received from owner")

    print(f"Created: {request.id}")
    print(f"Risk Level: {request.risk_level.value}")
    print(f"State: {request.state.value}")

    # Stage 2: Planning
    print("\n[2] PLANNING")
    print("-" * 70)
    request.update_state(FeatureState.PLANNED)
    memory.save_feature(request)
    memory.add_audit(request.id, "Feature planned for implementation")
    print("✓ Feature plan validated")

    # Stage 3: Implementation (mocked)
    print("\n[3] IMPLEMENTATION")
    print("-" * 70)
    request.update_state(FeatureState.IMPLEMENTING)
    request.git_branch = f"wiz/{request.id.lower()}"
    request.feature_sha = "abc123def456789"
    memory.save_feature(request)
    memory.add_audit(request.id, "Claude Code session spawned for implementation")
    print(f"✓ Implementation branch: {request.git_branch}")
    print(f"✓ Feature SHA: {request.feature_sha}")

    # Stage 4: Testing
    print("\n[4] TESTING")
    print("-" * 70)
    request.update_state(FeatureState.TESTING)
    request.test_results = "42 passed, 0 failed, 1 skipped"
    memory.save_feature(request)
    memory.add_audit(request.id, "All tests passing")
    print(f"✓ Tests: {request.test_results}")

    # Stage 5: Vercel Preview
    print("\n[5] VERCEL PREVIEW")
    print("-" * 70)
    request.update_state(FeatureState.PREVIEWING)
    request.preview_sha = request.feature_sha
    memory.save_feature(request)
    memory.add_audit(request.id, "Vercel Preview deployed and verified")
    print(f"✓ Preview URL: https://wize-perf-wiz-{request.id.lower()}.vercel.app")
    print(f"✓ Preview SHA: {request.preview_sha}")

    # Stage 6: Code Review
    print("\n[6] CODE REVIEW")
    print("-" * 70)
    request.update_state(FeatureState.REVIEWING)
    review = CodeReview(feature_id=request.id)
    # No blocking findings for this simple feature
    review.findings.append(
        CodeReviewFinding(
            category=FindingCategory.STYLE,
            severity=FindingSeverity.MINOR,
            message="Consider adding JSDoc comment",
        )
    )
    memory.add_audit(request.id, "Independent code review completed")
    print("✓ Review findings:")
    for finding in review.findings:
        print(f"  - {finding.severity.value}: {finding.message}")

    # Stage 7: Safety Gates
    print("\n[7] SAFETY GATES")
    print("-" * 70)
    safety_results = SafetyGates.evaluate_all_gates(request)
    blocking = [r for r in safety_results if r.blocking]
    print(f"✓ Gates evaluated: {len(safety_results)} checks")
    print(f"✓ Blocking failures: {len(blocking)}")

    # Stage 8: Merge Gates
    print("\n[8] MERGE GATES")
    print("-" * 70)
    merge_result = MergeGates.evaluate(request, review)
    print(f"✓ Result: {merge_result.status.value.upper()}")
    print(f"✓ Gates passed: {len(merge_result.gates_passed)}")
    for gate in merge_result.gates_passed:
        print(f"  ✓ {gate}")
    if merge_result.gates_warning:
        print(f"✓ Warnings: {len(merge_result.gates_warning)}")
        for gate in merge_result.gates_warning:
            print(f"  ⚠ {gate}")
    if merge_result.gates_failed:
        print(f"✓ Failures: {len(merge_result.gates_failed)}")
        for gate in merge_result.gates_failed:
            print(f"  ✗ {gate}")

    # Stage 9: GitHub PR
    print("\n[9] GITHUB PULL REQUEST")
    print("-" * 70)
    github = GitHubIntegration()
    request.pull_request_number = 42
    memory.save_feature(request)
    memory.add_audit(request.id, "GitHub PR created (#42)")
    print(f"✓ PR #42 created for {request.git_branch}")

    # Stage 10: Merge Decision
    print("\n[10] MERGE DECISION")
    print("-" * 70)
    if merge_result.can_merge:
        print("✓ AUTONOMOUS MERGE AUTHORIZED")
        request.update_state(FeatureState.APPROVED_FOR_MERGE)
    else:
        print("⚠ REQUIRES HUMAN APPROVAL")
        request.update_state(FeatureState.REQUIRES_HUMAN)
    memory.save_feature(request)
    memory.add_audit(request.id, f"Merge decision: {request.state.value}")

    # Stage 11: Merge
    print("\n[11] MERGE")
    print("-" * 70)
    if merge_result.can_merge:
        request.update_state(FeatureState.MERGED)
        request.production_sha = "abc123def456789"
        memory.save_feature(request)
        memory.add_audit(request.id, f"Merged to main with SHA {request.production_sha}")
        print(f"✓ Merged to main")
        print(f"✓ Merge SHA: {request.production_sha}")

        # Stage 12: Production Deployment
        print("\n[12] PRODUCTION DEPLOYMENT")
        print("-" * 70)
        request.update_state(FeatureState.DEPLOYED_TO_PRODUCTION)
        memory.save_feature(request)
        memory.add_audit(request.id, "Deployed to production")
        print("✓ Feature live in production")

        # Stage 13: Notifications
        print("\n[13] OWNER NOTIFICATION")
        print("-" * 70)
        notifications = NotificationManager()
        message = f"Sir, it's live.\n{request.id} has been deployed to production."
        print(f"Sending notification: {message}")

        # Final state
        request.update_state(FeatureState.COMPLETE)
        memory.save_feature(request)
        memory.add_audit(request.id, "Feature complete and live in production")

    print("\n" + "=" * 70)
    print("FEATURE PIPELINE SUMMARY")
    print("=" * 70)
    print(f"Feature ID: {request.id}")
    print(f"Final State: {request.state.value}")
    print(f"Risk Level: {request.risk_level.value}")
    print(f"PR Number: #{request.pull_request_number}")
    if merge_result.can_merge:
        print(f"Merge Status: AUTONOMOUS APPROVED ✓")
    else:
        print(f"Merge Status: REQUIRES HUMAN APPROVAL")

    print(f"\nAudit Trail ({len(memory.features[request.id].audit_trail)} entries):")
    for entry in memory.features[request.id].audit_trail:
        print(f"  {entry}")

    print("\n" + "=" * 70)
    print("DEMONSTRATION COMPLETE")
    print("=" * 70)
    print(
        "\nThis demo shows Wiz orchestrating a feature from owner request"
        " to production deployment.\n"
        "Real implementation would integrate with:\n"
        "  - Claude Code for actual implementation\n"
        "  - Vercel API for real Previews\n"
        "  - GitHub API for real PRs\n"
        "  - Actual test execution\n"
        "  - Real code review\n"
        "  - Production health monitoring\n"
        "  - Telegram notifications\n"
    )


if __name__ == "__main__":
    demo_low_risk_feature()
