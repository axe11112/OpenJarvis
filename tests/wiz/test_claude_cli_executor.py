"""Tests for real Claude CLI executor."""

from __future__ import annotations

from pathlib import Path

import pytest

from openjarvis.wiz.claude_cli_executor import (
    ClaudeAvailability,
    ClaudeCliExecutor,
    ClaudeDiagnostics,
    SessionResult,
)


class TestClaudeDiagnostics:
    """ClaudeDiagnostics dataclass."""

    def test_create_diagnostics(self) -> None:
        diag = ClaudeDiagnostics(
            available=True,
            availability=ClaudeAvailability.AVAILABLE,
            cli_found=True,
            cli_path="/usr/local/bin/claude",
            authenticated=True,
        )
        assert diag.available
        assert diag.cli_found
        assert diag.authenticated

    def test_diagnostics_to_dict(self) -> None:
        diag = ClaudeDiagnostics(
            available=True,
            availability=ClaudeAvailability.AVAILABLE,
            cli_found=True,
            cli_path="/usr/local/bin/claude",
            authenticated=True,
            version="1.0.0",
        )
        d = diag.to_dict()
        assert d["available"] is True
        assert d["availability"] == "available"
        assert d["cli_found"] is True
        assert d["cli_path"] == "/usr/local/bin/claude"
        assert d["authenticated"] is True
        assert d["version"] == "1.0.0"

    def test_diagnostics_not_found(self) -> None:
        diag = ClaudeDiagnostics(
            available=False,
            availability=ClaudeAvailability.CLI_NOT_FOUND,
            cli_found=False,
            error="claude CLI not found in PATH",
        )
        assert not diag.available
        assert not diag.cli_found
        assert diag.error == "claude CLI not found in PATH"

    def test_diagnostics_not_authenticated(self) -> None:
        diag = ClaudeDiagnostics(
            available=False,
            availability=ClaudeAvailability.NOT_AUTHENTICATED,
            cli_found=True,
            cli_path="/usr/local/bin/claude",
            authenticated=False,
            error="User not authenticated",
        )
        assert not diag.available
        assert diag.cli_found
        assert not diag.authenticated


class TestSessionResult:
    """SessionResult dataclass."""

    def test_create_success_result(self) -> None:
        result = SessionResult(
            success=True,
            session_id="sess-123",
            returncode=0,
        )
        assert result.success
        assert result.session_id == "sess-123"
        assert result.returncode == 0

    def test_create_failure_result(self) -> None:
        result = SessionResult(
            success=False,
            error="Session creation failed",
            returncode=1,
        )
        assert not result.success
        assert result.error == "Session creation failed"

    def test_result_to_dict(self) -> None:
        result = SessionResult(
            success=True,
            session_id="sess-123",
            command="claude session create ...",
            stdout="Session created",
            returncode=0,
        )
        d = result.to_dict()
        assert d["success"] is True
        assert d["session_id"] == "sess-123"
        assert d["command"] == "claude session create ..."
        assert d["returncode"] == 0


class TestClaudeCliExecutor:
    """Claude CLI executor functionality."""

    def test_executor_initialization(self) -> None:
        executor = ClaudeCliExecutor()
        assert executor is not None
        # No diagnostics cached yet
        assert executor._diagnostics_cache is None

    def test_get_diagnostics(self) -> None:
        """Test getting diagnostics (real system may or may not have claude)."""
        executor = ClaudeCliExecutor()
        diag = executor.get_diagnostics()

        # Should return ClaudeDiagnostics regardless
        assert isinstance(diag, ClaudeDiagnostics)
        assert diag.cli_found in (True, False)
        assert diag.available in (True, False)
        assert diag.availability in ClaudeAvailability

    def test_diagnostics_cached(self) -> None:
        """Diagnostics should be cached after first call."""
        executor = ClaudeCliExecutor()
        diag1 = executor.get_diagnostics()
        diag2 = executor.get_diagnostics()
        # Should be same object (cached)
        assert diag1 is diag2

    def test_find_claude_cli(self) -> None:
        """Test finding claude CLI in PATH (may or may not be installed)."""
        executor = ClaudeCliExecutor()
        path = executor._find_claude_cli()
        # path can be None or a Path object depending on system
        if path is not None:
            assert isinstance(path, type(Path()))

    def test_parse_session_id_json(self) -> None:
        """Test parsing session ID from JSON output."""
        executor = ClaudeCliExecutor()
        output = '{"session_id": "sess-abc-123", "status": "created"}'
        session_id = executor._parse_session_id(output)
        assert session_id == "sess-abc-123"

    def test_parse_session_id_text(self) -> None:
        """Test parsing session ID from text output."""
        executor = ClaudeCliExecutor()
        output = "Session created.\nSession ID: sess-xyz-789\nStatus: running"
        session_id = executor._parse_session_id(output)
        assert session_id is not None
        # Should extract something that looks like an ID

    def test_parse_session_id_not_found(self) -> None:
        """Test parsing when no session ID is present."""
        executor = ClaudeCliExecutor()
        output = "Some random output without session ID"
        session_id = executor._parse_session_id(output)
        assert session_id is None

    def test_build_session_command(self) -> None:
        """Test building session command."""
        executor = ClaudeCliExecutor()
        cmd = executor._build_session_command(
            "/usr/local/bin/claude",
            "Feature FEAT-001",
            "Implement X",
            "owner/repo",
            "wiz/feat-001",
        )
        # Should be a list
        assert isinstance(cmd, list)
        assert len(cmd) > 0
        # Should contain expected elements
        assert "session" in cmd
        assert "create" in cmd
        assert "Feature FEAT-001" in cmd
        assert "owner/repo" in cmd
        assert "wiz/feat-001" in cmd

    def test_execute_session_claude_unavailable(self) -> None:
        """Test executing when Claude is not available."""
        executor = ClaudeCliExecutor()

        # Monkey-patch to simulate unavailable Claude
        original_diag = executor.get_diagnostics

        def mock_diag():
            return ClaudeDiagnostics(
                available=False,
                availability=ClaudeAvailability.CLI_NOT_FOUND,
                cli_found=False,
                error="not installed",
            )

        executor.get_diagnostics = mock_diag

        result = executor.execute_session(
            title="Test Feature",
            prompt="Do something",
            repository="owner/repo",
            branch="wiz/test",
        )

        assert not result.success
        assert "not available" in (result.error or "").lower()

    def test_availability_enum(self) -> None:
        """Test ClaudeAvailability enum values."""
        assert ClaudeAvailability.AVAILABLE.value == "available"
        assert ClaudeAvailability.CLI_NOT_FOUND.value == "cli_not_found"
        assert ClaudeAvailability.NOT_AUTHENTICATED.value == "not_authenticated"
        assert ClaudeAvailability.UNKNOWN.value == "unknown"

    def test_multiple_executors_independent(self) -> None:
        """Multiple executors should be independent."""
        exec1 = ClaudeCliExecutor()
        exec2 = ClaudeCliExecutor()

        diag1 = exec1.get_diagnostics()
        diag2 = exec2.get_diagnostics()

        # Should be different objects
        assert diag1 is not diag2
        # But same content
        assert diag1.available == diag2.available
