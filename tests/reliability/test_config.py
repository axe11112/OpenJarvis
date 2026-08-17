"""Tests for the ``[reliability]`` configuration section.

The defaults are load-bearing safety controls, not cosmetics: every one of them
is asserted here so a future edit cannot quietly loosen them.
"""

from __future__ import annotations

import pytest

from openjarvis.core.config import (
    JarvisConfig,
    ReliabilityConfig,
    load_config,
    validate_config_key,
)


class TestSafeDefaults:
    def test_everything_is_off_by_default(self):
        rc = ReliabilityConfig()
        assert rc.enabled is False
        assert rc.vercel.enabled is False
        assert rc.supabase.enabled is False
        assert rc.github.enabled is False
        assert rc.repair.enabled is False
        assert rc.notify.enabled is False

    def test_dangerous_capabilities_default_off(self):
        rc = ReliabilityConfig()
        assert rc.policy.deploy_mode == "pr_only"
        assert rc.policy.allow_push_to_default_branch is False
        assert rc.policy.auto_deploy_fix_classes == []
        assert rc.supabase.allow_production_writes is False

    def test_critical_is_not_auto_repaired_by_default(self):
        assert "CRITICAL" not in ReliabilityConfig().policy.auto_repair_severities

    def test_max_attempts_default(self):
        assert ReliabilityConfig().repair.max_attempts == 3

    def test_preview_verification_required_by_default(self):
        assert ReliabilityConfig().repair.require_preview_verification is True

    def test_repair_loop_defaults_are_the_narrow_ones(self):
        """§26/§27: nothing that widens JARVIS's reach may default to true."""
        repair = ReliabilityConfig().repair
        assert repair.enabled is False
        assert repair.workspace == ""
        assert repair.worktree_root == ""
        # Local gates are unset, which is reported as not-run rather than passed.
        assert repair.test_command == ""
        assert repair.lint_command == ""
        assert repair.typecheck_command == ""
        assert repair.build_command == ""

    def test_scope_limits_have_a_ceiling(self):
        repair = ReliabilityConfig().repair
        assert repair.max_changed_files > 0
        assert repair.max_changed_lines > 0

    def test_the_agent_may_not_reach_the_network(self):
        """Evidence text must not be able to send the agent to a chosen URL."""
        repair = ReliabilityConfig().repair
        assert "WebFetch" in repair.agent_disallowed_tools
        assert "WebSearch" in repair.agent_disallowed_tools
        assert "WebFetch" not in repair.agent_allowed_tools

    def test_failed_worktrees_are_kept_for_inspection(self):
        assert ReliabilityConfig().repair.keep_failed_worktrees is True

    def test_protected_paths_cover_ci_and_auth(self):
        paths = ReliabilityConfig().policy.protected_paths
        assert any(".github/workflows" in p for p in paths)
        assert any("auth" in p for p in paths)


class TestCredentialIndirection:
    @pytest.mark.parametrize(
        ("section", "expected"),
        [
            ("vercel", "VERCEL_READONLY_TOKEN"),
            ("supabase", "SUPABASE_READONLY_TOKEN"),
            ("github", "GITHUB_READONLY_TOKEN"),
        ],
    )
    def test_token_fields_name_env_vars(self, section, expected):
        assert getattr(ReliabilityConfig(), section).token_env == expected

    def test_no_section_has_a_value_bearing_credential_field(self):
        """Config stores env-var *names*; a field that could hold a secret
        value would defeat docs/JARVIS_SECURITY.md §3.1."""
        forbidden = {"token", "password", "secret", "api_key", "key"}
        rc = ReliabilityConfig()
        for name in rc.__dataclass_fields__:
            section = getattr(rc, name)
            if not hasattr(section, "__dataclass_fields__"):
                continue
            assert set(section.__dataclass_fields__) & forbidden == set(), name


class TestWiring:
    def test_attached_to_jarvis_config(self):
        assert isinstance(JarvisConfig().reliability, ReliabilityConfig)

    @pytest.mark.parametrize(
        ("key", "expected_type"),
        [
            ("reliability.enabled", bool),
            ("reliability.site.base_url", str),
            ("reliability.repair.max_attempts", int),
            ("reliability.policy.deploy_mode", str),
            ("reliability.supabase.allow_production_writes", bool),
        ],
    )
    def test_config_keys_validate(self, key, expected_type):
        assert validate_config_key(key) is expected_type

    def test_unknown_key_rejected(self):
        with pytest.raises(ValueError, match="Unknown config key"):
            validate_config_key("reliability.nope")


class TestTomlOverlay:
    def test_toml_overrides_apply(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
[reliability]
enabled = true

[reliability.site]
base_url = "https://example.com"
environment = "staging"

[reliability.repair]
enabled = true
max_attempts = 5

[reliability.policy]
auto_repair_severities = ["HIGH"]

[reliability.github]
enabled = true
repo = "owner/name"
""",
            encoding="utf-8",
        )
        rc = load_config(config_file).reliability
        assert rc.enabled is True
        assert rc.site.base_url == "https://example.com"
        assert rc.site.environment == "staging"
        assert rc.repair.max_attempts == 5
        assert rc.policy.auto_repair_severities == ["HIGH"]
        assert rc.github.repo == "owner/name"

    def test_unspecified_fields_keep_safe_defaults(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[reliability]\nenabled = true\n", encoding="utf-8")
        rc = load_config(config_file).reliability
        assert rc.enabled is True
        assert rc.policy.deploy_mode == "pr_only"
        assert rc.policy.allow_push_to_default_branch is False
        assert rc.supabase.allow_production_writes is False

    def test_absent_section_yields_defaults(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("[agent]\nmax_turns = 3\n", encoding="utf-8")
        assert load_config(config_file).reliability.enabled is False
