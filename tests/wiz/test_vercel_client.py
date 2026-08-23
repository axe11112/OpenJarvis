"""Tests for Wiz Vercel client (deployment verification)."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from openjarvis.wiz.vercel_client import VercelClient, VercelClientError


@pytest.fixture
def temp_token_file():
    """Create a temporary Vercel token file."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump({"token": "test_vercel_token"}, f)
        path = f.name
    yield path
    Path(path).unlink(missing_ok=True)


def test_vercel_client_is_configured(temp_token_file):
    """Test checking if Vercel is configured."""
    client = VercelClient(token_path=temp_token_file)
    assert client.is_configured is True

    client_unconfigured = VercelClient(token_path="/nonexistent/path.json")
    assert client_unconfigured.is_configured is False


def test_vercel_client_load_token(temp_token_file):
    """Test loading token from file."""
    client = VercelClient(token_path=temp_token_file)
    token = client._load_token()
    assert token == "test_vercel_token"


def test_vercel_client_missing_token():
    """Test error when token is missing."""
    client = VercelClient(token_path="/nonexistent/path.json")
    with pytest.raises(VercelClientError):
        client._load_token()


@patch("openjarvis.wiz.vercel_client.httpx.request")
def test_vercel_client_get_deployment(mock_request, temp_token_file):
    """Test getting deployment details."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "id": "dpl_abc123",
        "status": "READY",
        "state": "READY",
        "url": "https://feature-preview.vercel.app",
        "meta": {
            "githubCommitSha": "abc123def456def456",
            "pullRequestId": 42,
        },
    }
    mock_request.return_value = mock_response

    client = VercelClient(token_path=temp_token_file)
    result = client.get_deployment("dpl_abc123")

    assert result["id"] == "dpl_abc123"
    assert result["status"] == "READY"
    assert result["url"] == "https://feature-preview.vercel.app"


@patch("openjarvis.wiz.vercel_client.httpx.request")
def test_vercel_client_get_preview_url(mock_request, temp_token_file):
    """Test extracting Preview URL from deployment."""
    deployment = {
        "id": "dpl_test",
        "url": "feature-preview.vercel.app",
        "status": "READY",
    }

    client = VercelClient(token_path=temp_token_file)
    url = client.get_preview_url(deployment)
    assert url == "https://feature-preview.vercel.app"


@patch("openjarvis.wiz.vercel_client.httpx.request")
def test_vercel_client_get_deployment_sha(mock_request, temp_token_file):
    """Test extracting deployment SHA."""
    deployment = {
        "id": "dpl_test",
        "meta": {
            "githubCommitSha": "abc123def456",
        },
    }

    client = VercelClient(token_path=temp_token_file)
    sha = client.get_deployment_sha(deployment)
    assert sha == "abc123def456"


@patch("openjarvis.wiz.vercel_client.httpx.request")
def test_vercel_client_verify_sha_match(mock_request, temp_token_file):
    """Test SHA verification."""
    deployment = {
        "id": "dpl_test",
        "meta": {
            "githubCommitSha": "abc123def456",
        },
    }

    client = VercelClient(token_path=temp_token_file)

    # Test matching SHA
    assert client.verify_sha_match(deployment, "abc123def456") is True

    # Test non-matching SHA
    assert client.verify_sha_match(deployment, "different1234") is False

    # Test case-insensitive matching
    assert client.verify_sha_match(deployment, "ABC123DEF456") is True


@patch("openjarvis.wiz.vercel_client.httpx.request")
def test_vercel_client_get_deployments(mock_request, temp_token_file):
    """Test listing deployments."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "deployments": [
            {
                "id": "dpl_1",
                "status": "READY",
                "meta": {"pullRequestId": 1},
            },
            {
                "id": "dpl_2",
                "status": "READY",
                "meta": {"pullRequestId": 2},
            },
        ]
    }
    mock_request.return_value = mock_response

    client = VercelClient(token_path=temp_token_file)
    deployments = client.get_deployments("proj_test", limit=2)

    assert len(deployments) == 2
    assert deployments[0]["id"] == "dpl_1"


def test_vercel_client_error_handling():
    """Test error handling in Vercel client."""
    client = VercelClient(token_path="/nonexistent/path.json")

    with pytest.raises(VercelClientError):
        client._load_token()


def test_vercel_client_verify_integration():
    """Verify Vercel client can be instantiated."""
    client = VercelClient()
    assert client.token_path is not None
