"""Tests for GitHub integration."""

from __future__ import annotations

from openjarvis.wiz.github_integration import GitHubIntegration, GitHubPullRequest


def test_github_pr_creation():
    """Test creating PR object."""
    pr = GitHubPullRequest(
        number=42,
        title="Add dark mode",
        description="Implement dark mode support",
        branch="wiz/WIZE-dark-mode",
    )

    assert pr.number == 42
    assert pr.title
    assert pr.branch
    assert not pr.merged


def test_github_pr_mergeable():
    """Test PR mergeable state."""
    pr = GitHubPullRequest(
        number=42,
        title="Add dark mode",
        description="",
        branch="wiz/WIZE-dark-mode",
        head_sha="abc123",
        mergeable=True,
    )

    assert pr.can_merge


def test_github_pr_not_mergeable():
    """Test PR not mergeable."""
    pr = GitHubPullRequest(
        number=42,
        title="Add dark mode",
        description="",
        branch="wiz/WIZE-dark-mode",
        mergeable=False,
    )

    assert not pr.can_merge


def test_github_integration_initialization():
    """Test GitHub integration init."""
    integration = GitHubIntegration(owner="axe11112", repo="OpenJarvis")
    assert integration.owner == "axe11112"
    assert integration.repo == "OpenJarvis"
    assert integration.tool_executor is None  # No executor configured
