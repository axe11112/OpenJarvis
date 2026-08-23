"""Tests for Wiz GitHub client (real API integration)."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from openjarvis.wiz.github_client import GitHubClient, GitHubClientError


@pytest.fixture
def temp_token_file():
    """Create a temporary token file."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump({"token": "ghp_test_token_12345"}, f)
        path = f.name
    yield path
    Path(path).unlink(missing_ok=True)


def test_github_client_is_configured(temp_token_file):
    """Test checking if GitHub is configured."""
    client = GitHubClient(token_path=temp_token_file)
    assert client.is_configured is True

    client_unconfigured = GitHubClient(token_path="/nonexistent/path.json")
    assert client_unconfigured.is_configured is False


def test_github_client_load_token(temp_token_file):
    """Test loading token from file."""
    client = GitHubClient(token_path=temp_token_file)
    token = client._load_token()
    assert token == "ghp_test_token_12345"


def test_github_client_missing_token():
    """Test error when token is missing."""
    client = GitHubClient(token_path="/nonexistent/path.json")
    with pytest.raises(GitHubClientError):
        client._load_token()


@patch("openjarvis.wiz.github_client.httpx.request")
def test_github_client_create_branch(mock_request, temp_token_file):
    """Test creating a branch."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "ref": "refs/heads/test-branch",
        "node_id": "test_id",
        "url": "https://api.github.com/repos/test/repo/git/refs/heads/test-branch",
    }
    mock_request.return_value = mock_response

    client = GitHubClient(token_path=temp_token_file)
    result = client.create_branch("test", "repo", "test-branch", "abc123def456")

    assert result["ref"] == "refs/heads/test-branch"
    mock_request.assert_called_once()
    call_kwargs = mock_request.call_args[1]
    assert "Authorization" in call_kwargs["headers"]
    assert "ghp_test_token_12345" in call_kwargs["headers"]["Authorization"]


@patch("openjarvis.wiz.github_client.httpx.request")
def test_github_client_create_pr(mock_request, temp_token_file):
    """Test creating a pull request."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "number": 42,
        "title": "test pr",
        "html_url": "https://github.com/test/repo/pull/42",
        "head": {"sha": "abc123"},
        "base": {"ref": "main"},
    }
    mock_request.return_value = mock_response

    client = GitHubClient(token_path=temp_token_file)
    result = client.create_pull_request(
        owner="test",
        repo="repo",
        title="test pr",
        head="feature-branch",
        base="main",
        body="Test PR",
    )

    assert result["number"] == 42
    assert result["html_url"] == "https://github.com/test/repo/pull/42"


@patch("openjarvis.wiz.github_client.httpx.request")
def test_github_client_merge_pr(mock_request, temp_token_file):
    """Test merging a pull request."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "sha": "merged123",
        "merged": True,
        "message": "Pull Request successfully merged",
    }
    mock_request.return_value = mock_response

    client = GitHubClient(token_path=temp_token_file)
    result = client.merge_pull_request(
        owner="test",
        repo="repo",
        pr_number=42,
        commit_title="wiz: test feature",
    )

    assert result["merged"] is True
    assert result["sha"] == "merged123"


def test_github_client_error_handling():
    """Test error handling in GitHub client."""
    client = GitHubClient(token_path="/nonexistent/path.json")

    with pytest.raises(GitHubClientError):
        client._load_token()


def test_github_client_verify_integration():
    """Verify GitHub client can be instantiated with real config path."""
    # This test verifies the client can find the config directory
    # It doesn't require an actual token to be configured
    client = GitHubClient()
    # Should not raise an error just to instantiate
    assert client.token_path is not None
