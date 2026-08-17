"""The test suite must be deterministic on a machine configured for real use.

A JARVIS developer's machine exports ``$TARGET_REPOSITORY``,
``$TARGET_PRODUCTION_URL``, ``$VERCEL_PROJECT`` and ``$SUPABASE_PROJECT_REF`` —
that is what makes ``jarvis reliability live-diagnostic`` work at all.  Those
same variables used to leak into unit tests and beat monkeypatched values,
because ``resolve_target`` reads canonical names before aliases.

The failure mode is the nasty one: the suite is green in CI and on a fresh
checkout, and red only for the person who has actually configured JARVIS.  That
trains exactly the wrong reflex — "those tests always fail on my machine" — on
the tests that guard how JARVIS finds production.

These tests guard the guard.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from openjarvis.reliability.target import ENV_ALIASES

_REPO_ROOT = Path(__file__).resolve().parent.parent


class TestAmbientTargetVarsAreCleared:
    @pytest.mark.parametrize(
        "name", sorted({n for aliases in ENV_ALIASES.values() for n in aliases})
    )
    def test_every_known_spelling_is_absent(self, name):
        """Parametrized over ``ENV_ALIASES`` itself, so a newly added alias is
        covered the moment it is added rather than the moment somebody
        remembers to update a hand-written list here."""
        assert name not in os.environ, (
            f"${name} leaked into a unit test; the autouse fixture in "
            f"tests/conftest.py should have cleared it"
        )

    def test_credential_variables_are_absent(self):
        """No unit test should be able to reach a real integration, even by
        accident — a passing test that silently used a live token is worse
        than a failing one."""
        for name in (
            "VERCEL_READONLY_TOKEN",
            "SUPABASE_READONLY_TOKEN",
            "GITHUB_READONLY_TOKEN",
            "TELEGRAM_BOT_TOKEN",
        ):
            assert name not in os.environ, f"${name} leaked into a unit test"

    def test_monkeypatched_value_wins(self, monkeypatch):
        """The positive half: isolation must not mean 'unsettable'."""
        from openjarvis.core.config import JarvisConfig
        from openjarvis.reliability.target import resolve_target

        monkeypatch.setenv("TARGET_REPO", "fixture/repo")
        assert resolve_target(JarvisConfig()).repository == "fixture/repo"


class TestIsolationHoldsUnderAPollutedEnvironment:
    """The tests above run in an already-cleaned environment, so on their own
    they cannot prove the cleaning works — only that it happened.  This runs a
    real pytest subprocess with the production variables exported, which is the
    condition that actually broke fourteen tests on this machine.
    """

    def test_alias_tests_pass_with_ambient_production_vars_exported(self):
        polluted = {
            **os.environ,
            "TARGET_REPOSITORY": "ambient-org/ambient-repo",
            "TARGET_PRODUCTION_URL": "https://ambient.example",
            "VERCEL_PROJECT": "ambient-vercel-project",
            "SUPABASE_PROJECT_REF": "ambientsupabaseref",
            "TARGET_BRANCH": "ambient-branch",
            "VERCEL_TEAM": "ambient-team",
        }
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/reliability/test_target.py::TestEnvironmentAliases",
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ],
            cwd=_REPO_ROOT,
            env=polluted,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, (
            "alias resolution tests failed with production variables exported:\n"
            f"{result.stdout[-4000:]}\n{result.stderr[-2000:]}"
        )
        # Nothing ambient should have reached the assertions.
        assert "ambient-org/ambient-repo" not in result.stdout
