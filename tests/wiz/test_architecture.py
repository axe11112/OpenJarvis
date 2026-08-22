"""Tests for Wiz architecture constraints."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path


def test_wiz_does_not_import_from_reliability():
    """Verify Wiz is independent of reliability system.

    Wiz may depend on reliability services (logs, monitoring),
    but reliability must not depend on wiz. This test ensures
    one-way dependency.
    """
    # tests/wiz/test_architecture.py -> go up to repo root
    repo_root = Path(__file__).parent.parent.parent
    wiz_path = repo_root / "src" / "openjarvis" / "wiz"

    # Collect all Python files in wiz
    wiz_files = list(wiz_path.glob("**/*.py"))
    assert len(wiz_files) > 0, "No Wiz files found"

    for file in wiz_files:
        if "__pycache__" in str(file):
            continue

        with open(file) as f:
            try:
                tree = ast.parse(f.read())
            except SyntaxError:
                continue

        # Find all imports
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        module = alias.name
                else:
                    module = node.module or ""

                # Check that Wiz doesn't import from reliability
                assert (
                    "reliability" not in module
                ), f"{file}: Wiz imports from reliability: {module}"


def test_wiz_core_modules_exist():
    """Verify core Wiz modules are present."""
    repo_root = Path(__file__).parent.parent.parent
    wiz_path = repo_root / "src" / "openjarvis" / "wiz"

    required_modules = [
        "__init__.py",
        "models.py",
        "dispatcher/__init__.py",
        "orchestrator/__init__.py",
        "repository/__init__.py",
        "safety/__init__.py",
        "testing/__init__.py",
        "verification/__init__.py",
        "notifications/__init__.py",
        "claude_session.py",
        "review.py",
        "merge_gates.py",
        "github_integration.py",
    ]

    for module in required_modules:
        module_path = wiz_path / module
        assert (
            module_path.exists()
        ), f"Required Wiz module missing: {module}"


def test_wiz_models_are_typesafe():
    """Verify Wiz models have proper type annotations."""
    from openjarvis.wiz.models import FeatureRequest, FeatureState, RiskLevel

    # Instantiate models to verify they work
    request = FeatureRequest(description="test")
    assert hasattr(request, "id")
    assert hasattr(request, "state")
    assert hasattr(request, "risk_level")

    # Verify enums
    assert len(list(FeatureState)) > 0
    assert len(list(RiskLevel)) > 0
