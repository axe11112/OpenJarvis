"""Tests for Claude session management."""

from __future__ import annotations

import pytest

from openjarvis.wiz.claude_session import ClaudeSessionManager, ClaudeSessionResult


def test_claude_cli_verification():
    """Test that Claude CLI is available."""
    try:
        manager = ClaudeSessionManager()
        # If this succeeds, Claude CLI is available
        assert manager.claude_cli == "claude"
    except RuntimeError as e:
        # Claude CLI may not be available in test environment
        pytest.skip(f"Claude CLI not available: {e}")


def test_session_result_creation():
    """Test creating session result."""
    result = ClaudeSessionResult(
        session_id="claude-test",
        status="success",
        exit_code=0,
        output="Test output",
        error_output="",
        commits_made=["abc123"],
    )

    assert result.session_id == "claude-test"
    assert result.status == "success"
    assert len(result.commits_made) == 1
    assert result.duration_seconds == 0.0
