"""Tests for change-scope control and the hardened path guard.

Two different questions live here:

* the **protected-path guard** — may this file be touched at all? — which must
  not be defeatable by spelling the same path differently;
* **scope assessment** — is this diff the shape of the repair we asked for? —
  which stops a runaway agent without second-guessing a legitimate fix.
"""

from __future__ import annotations

import pytest

from openjarvis.reliability.scope import (
    ScopeLimits,
    assess_scope,
    find_test_files,
    looks_like_a_test,
)
from openjarvis.reliability.sources.github import (
    escapes_repository,
    is_protected_path,
    normalize_path,
)


class TestPathNormalization:
    """§9: the guard must not be defeatable by spelling a path differently."""

    @pytest.mark.parametrize(
        "path",
        [
            ".github/workflows/ci.yml",
            "./.github/workflows/ci.yml",
            ".github//workflows//ci.yml",
            ".github/./workflows/ci.yml",
            "a/../.github/workflows/ci.yml",
            "a/b/../../.github/workflows/ci.yml",
            ".github\\workflows\\ci.yml",
            ".GITHUB/WORKFLOWS/CI.YML",
        ],
        ids=[
            "plain",
            "dot-slash",
            "double-slash",
            "dot-segment",
            "parent-segment",
            "nested-parent",
            "windows-separators",
            "uppercase",
        ],
    )
    def test_ci_config_is_protected_however_it_is_spelled(self, path):
        assert is_protected_path(path) is True

    def test_repository_root_files_are_matched(self):
        assert is_protected_path("middleware.ts", ["middleware.*"]) is True

    def test_nested_files_are_matched(self):
        assert is_protected_path("src/app/auth/login.ts", ["**/auth/**"]) is True

    @pytest.mark.parametrize(
        "path",
        ["../../.ssh/config", "../secrets.env", "/etc/passwd", "C:\\Windows\\x"],
    )
    def test_paths_leaving_the_repository_are_always_protected(self, path):
        """No pattern list makes writing outside the checkout a legitimate repair."""
        assert escapes_repository(path) is True
        assert is_protected_path(path) is True

    @pytest.mark.parametrize("path", ["src/app/page.tsx", "lib/util.ts", "a/b/c/d.py"])
    def test_ordinary_paths_are_not_protected(self, path):
        assert is_protected_path(path) is False
        assert escapes_repository(path) is False

    def test_normalization_keeps_escape_visible(self):
        """`..` must not silently collapse into a harmless-looking path."""
        assert normalize_path("../x").startswith("../")


class TestScopeCategories:
    def test_a_small_ordinary_diff_is_allowed(self):
        verdict = assess_scope(["src/app/page.tsx"], lines_changed=10)
        assert verdict
        assert verdict.reasons == []

    def test_protected_paths_stop_the_repair(self):
        verdict = assess_scope([".github/workflows/ci.yml"])
        assert not verdict
        assert verdict.protected == [".github/workflows/ci.yml"]

    @pytest.mark.parametrize(
        "path",
        [".env", ".env.production", "config/.env.local", "certs/server.key", ".npmrc"],
    )
    def test_credential_bearing_files_stop_the_repair(self, path):
        verdict = assess_scope([path])
        assert not verdict
        assert path in verdict.secret_like

    @pytest.mark.parametrize(
        "path", ["Dockerfile", "docker-compose.yml", "vercel.json", "infra/main.tf"]
    )
    def test_infrastructure_changes_stop_the_repair(self, path):
        verdict = assess_scope([path])
        assert not verdict
        assert path in verdict.infrastructure

    @pytest.mark.parametrize(
        "path",
        [
            "supabase/migrations/0001_init.sql",
            "db/migrations/add_column.sql",
            "app/rls_policies.ts",
            "middleware.ts",
        ],
    )
    def test_declarative_security_config_stops_the_repair(self, path):
        verdict = assess_scope([path])
        assert not verdict
        assert path in verdict.security_config

    def test_application_auth_code_is_flagged_but_allowed(self):
        """The reference incident is a login failure — blocking this would make
        JARVIS unable to repair the exact class of bug it exists for."""
        verdict = assess_scope(["app/auth.ts"], lines_changed=20)
        assert verdict
        assert "app/auth.ts" in verdict.review_required

    def test_review_required_does_not_add_a_reason(self):
        verdict = assess_scope(["src/session_store.ts"])
        assert verdict.reasons == []
        assert verdict.review_required


class TestScopeLimits:
    def test_too_many_files_stops_the_repair(self):
        verdict = assess_scope(
            [f"src/f{i}.ts" for i in range(30)], limits=ScopeLimits(max_files=20)
        )
        assert not verdict
        assert "30 files changed" in verdict.reason

    def test_too_many_lines_stops_the_repair(self):
        verdict = assess_scope(
            ["src/a.ts"], lines_changed=5000, limits=ScopeLimits(max_lines_changed=800)
        )
        assert not verdict
        assert "5000 lines changed" in verdict.reason

    def test_files_outside_a_declared_scope_are_reported(self):
        verdict = assess_scope(
            ["src/auth/login.ts", "docs/unrelated.md"],
            limits=ScopeLimits(expected_paths=["src/**"]),
        )
        assert not verdict
        assert verdict.unexpected == ["docs/unrelated.md"]

    def test_no_declared_scope_means_no_unexpected_files(self):
        verdict = assess_scope(["anything/at/all.ts"])
        assert verdict.unexpected == []

    def test_every_reason_is_collected_not_just_the_first(self):
        """One escalation should tell the owner everything that is wrong."""
        verdict = assess_scope(
            [".env", "Dockerfile", ".github/workflows/ci.yml"],
            lines_changed=99999,
        )
        assert len(verdict.reasons) >= 4

    def test_counts_are_recorded(self):
        verdict = assess_scope(["a.ts", "b.ts"], lines_changed=42)
        assert verdict.files_changed == 2
        assert verdict.lines_changed == 42

    def test_round_trips(self):
        payload = assess_scope([".env"]).to_dict()
        assert payload["allowed"] is False
        assert payload["secret_like"] == [".env"]


class TestRegressionTestDetection:
    @pytest.mark.parametrize(
        "path",
        [
            "tests/test_login.py",
            "src/__tests__/login.test.tsx",
            "app/login.spec.ts",
            "test/helpers.js",
        ],
    )
    def test_recognizes_test_files(self, path):
        assert looks_like_a_test(path) is True

    @pytest.mark.parametrize("path", ["src/app/page.tsx", "lib/latest.ts"])
    def test_ignores_ordinary_files(self, path):
        assert looks_like_a_test(path) is False

    def test_filters_a_list(self):
        assert find_test_files(["a.ts", "tests/test_a.py"]) == ["tests/test_a.py"]
