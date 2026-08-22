"""Tests for Wiz CLI."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from openjarvis.wiz.cli import app

runner = CliRunner()


def test_cli_health_command():
    """Test health check command."""
    result = runner.invoke(app, ["health"])
    assert result.exit_code == 0
    assert "Wiz System Health" in result.stdout


def test_cli_list_features_command():
    """Test list features command."""
    result = runner.invoke(app, ["list-features"])
    assert result.exit_code == 0
    assert "feature" in result.stdout.lower()


def test_cli_requires_repo_for_feature():
    """Test that feature command fails without repo."""
    result = runner.invoke(app, ["feature", "test"])
    # Should fail because repo doesn't exist
    assert result.exit_code != 0
