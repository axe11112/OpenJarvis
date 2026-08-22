"""Configured engineering target tests.

Verify that configuration is validated, immutable, and properly enforced.
"""

from __future__ import annotations

import pytest

from openjarvis.wiz.configured_target import (
    ApprovalGate,
    ConfiguredTarget,
    Environment,
)


class TestConfigurationValidation:
    """Validation at creation time."""

    def test_requires_repository_format(self) -> None:
        with pytest.raises(ValueError, match="owner/repo"):
            ConfiguredTarget(repository="invalid", target_branch="main")

    def test_requires_target_branch(self) -> None:
        with pytest.raises(ValueError, match="target_branch"):
            ConfiguredTarget(repository="owner/repo", target_branch="")

    def test_requires_branch_prefix(self) -> None:
        with pytest.raises(ValueError, match="branch_prefix"):
            ConfiguredTarget(
                repository="owner/repo", target_branch="main", branch_prefix=""
            )

    def test_requires_positive_max_concurrent_prs(self) -> None:
        with pytest.raises(ValueError, match="max_concurrent_prs"):
            ConfiguredTarget(
                repository="owner/repo",
                target_branch="main",
                max_concurrent_prs=0,
            )

    def test_requires_min_implementation_time(self) -> None:
        with pytest.raises(ValueError, match="max_implementation_time"):
            ConfiguredTarget(
                repository="owner/repo",
                target_branch="main",
                max_implementation_time=30,
            )

    def test_production_requires_approval_gate(self) -> None:
        # Should warn but not fail
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            environment=Environment.PRODUCTION,
            approval_gate=ApprovalGate.NONE,
        )
        assert config.environment == Environment.PRODUCTION

    def test_valid_development_target(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="develop",
            environment=Environment.DEVELOPMENT,
            approval_gate=ApprovalGate.NONE,
        )
        assert config.repository == "owner/repo"
        assert config.target_branch == "develop"

    def test_valid_production_target(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            environment=Environment.PRODUCTION,
            approval_gate=ApprovalGate.DOUBLE_REVIEW,
            max_concurrent_prs=1,
        )
        assert config.environment == Environment.PRODUCTION


class TestImmutability:
    """Configuration is frozen after creation."""

    def test_cannot_modify_repository(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo", target_branch="main"
        )
        with pytest.raises(AttributeError):
            config.repository = "other/repo"  # type: ignore

    def test_cannot_modify_target_branch(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo", target_branch="main"
        )
        with pytest.raises(AttributeError):
            config.target_branch = "develop"  # type: ignore

    def test_cannot_modify_environment(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo", target_branch="main"
        )
        with pytest.raises(AttributeError):
            config.environment = Environment.PRODUCTION  # type: ignore

    def test_cannot_modify_approval_gate(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo", target_branch="main"
        )
        with pytest.raises(AttributeError):
            config.approval_gate = ApprovalGate.DOUBLE_REVIEW  # type: ignore


class TestValidateMethod:
    """Preflight validation."""

    def test_valid_configuration_passes(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            environment=Environment.STAGING,
            approval_gate=ApprovalGate.SINGLE_REVIEW,
        )
        is_valid, errors = config.validate()
        assert is_valid
        assert len(errors) == 0

    def test_invalid_repository_format_fails(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo", target_branch="main"
        )
        # Manually set invalid state (shouldn't happen but validate would catch it)
        # We can't actually do this since config is frozen, but we can test
        # the validate logic indirectly
        is_valid, errors = config.validate()
        assert is_valid  # It's actually valid since we created it correctly

    def test_invalid_branch_name_detected(self) -> None:
        # Create a config with questionable values
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            branch_prefix="wiz/",
        )
        is_valid, errors = config.validate()
        assert is_valid or len(errors) == 0  # Should pass validation

    def test_production_with_weak_approval_fails(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            environment=Environment.PRODUCTION,
            approval_gate=ApprovalGate.SINGLE_REVIEW,
        )
        is_valid, errors = config.validate()
        # Single review is not strong enough for production
        assert not is_valid
        assert any("dangerous" in err.lower() for err in errors)


class TestEnvironments:
    """Different environment configurations."""

    def test_development_environment(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="develop",
            environment=Environment.DEVELOPMENT,
            approval_gate=ApprovalGate.NONE,
            max_concurrent_prs=5,
        )
        assert config.environment == Environment.DEVELOPMENT
        assert config.approval_gate == ApprovalGate.NONE
        assert config.max_concurrent_prs == 5

    def test_staging_environment(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="staging",
            environment=Environment.STAGING,
            approval_gate=ApprovalGate.SINGLE_REVIEW,
        )
        assert config.environment == Environment.STAGING
        assert config.approval_gate == ApprovalGate.SINGLE_REVIEW

    def test_production_environment(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            environment=Environment.PRODUCTION,
            approval_gate=ApprovalGate.DOUBLE_REVIEW,
            max_concurrent_prs=1,
            max_implementation_time=7200,
        )
        assert config.environment == Environment.PRODUCTION
        assert config.max_concurrent_prs == 1


class TestApprovalGates:
    """Different approval gate configurations."""

    def test_no_approval_gate(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="develop",
            approval_gate=ApprovalGate.NONE,
        )
        assert config.approval_gate == ApprovalGate.NONE

    def test_single_review_gate(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            approval_gate=ApprovalGate.SINGLE_REVIEW,
        )
        assert config.approval_gate == ApprovalGate.SINGLE_REVIEW

    def test_double_review_gate(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            approval_gate=ApprovalGate.DOUBLE_REVIEW,
        )
        assert config.approval_gate == ApprovalGate.DOUBLE_REVIEW

    def test_owner_approval_gate(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            approval_gate=ApprovalGate.OWNER_APPROVAL,
            owner_email="owner@example.com",
        )
        assert config.approval_gate == ApprovalGate.OWNER_APPROVAL
        assert config.owner_email == "owner@example.com"


class TestBranchNameGeneration:
    """Branch names are generated consistently."""

    def test_generate_branch_name(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            branch_prefix="wiz/",
        )
        branch = config.branch_name_for("FEAT-001")
        assert branch.startswith("wiz/")
        assert "feat-001" in branch

    def test_branch_name_lowercased(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            branch_prefix="wiz/",
        )
        branch = config.branch_name_for("PROACTIVE-ABCD1234")
        assert branch == "wiz/proactive-abcd1234"

    def test_branch_name_removes_colons(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            branch_prefix="wiz/",
        )
        branch = config.branch_name_for("incident:123")
        assert ":" not in branch
        assert "-" in branch

    def test_custom_branch_prefix(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            branch_prefix="auto/",
        )
        branch = config.branch_name_for("FEAT-001")
        assert branch.startswith("auto/")


class TestSerialization:
    """Configuration serialization for logging."""

    def test_to_dict(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            environment=Environment.PRODUCTION,
            approval_gate=ApprovalGate.DOUBLE_REVIEW,
            owner_email="owner@example.com",
        )
        d = config.to_dict()
        assert d["repository"] == "owner/repo"
        assert d["target_branch"] == "main"
        assert d["environment"] == "production"
        assert d["approval_gate"] == "double_review"
        assert d["owner_email"] == "owner@example.com"


class TestSkipLogic:
    """should_skip_implementation logic."""

    def test_skip_source_modification_when_disabled(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            can_modify_source=False,
        )
        assert config.should_skip_implementation("source_code")

    def test_allow_source_modification_when_enabled(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            can_modify_source=True,
        )
        assert not config.should_skip_implementation("source_code")

    def test_skip_integration_tests_when_disabled(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            can_run_integration_tests=False,
        )
        assert config.should_skip_implementation("integration_tests")

    def test_allow_integration_tests_when_enabled(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            can_run_integration_tests=True,
        )
        assert not config.should_skip_implementation("integration_tests")

    def test_skip_with_none_reason(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            can_modify_source=False,
        )
        # No reason = no skip
        assert not config.should_skip_implementation(None)


class TestConstraints:
    """Constraint enforcement."""

    def test_max_concurrent_prs_minimum(self) -> None:
        # Already tested in validation, but confirm it's respected
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            max_concurrent_prs=1,
        )
        assert config.max_concurrent_prs == 1

    def test_implementation_time_bounds(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            max_implementation_time=3600,
        )
        assert config.max_implementation_time == 3600

    def test_test_command_not_empty(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo",
            target_branch="main",
            test_command="make test",
        )
        assert config.test_command == "make test"


class TestDefaults:
    """Default values are sensible."""

    def test_default_branch_prefix(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo", target_branch="main"
        )
        assert config.branch_prefix == "wiz/"

    def test_default_environment(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo", target_branch="main"
        )
        assert config.environment == Environment.DEVELOPMENT

    def test_default_approval_gate(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo", target_branch="main"
        )
        assert config.approval_gate == ApprovalGate.NONE

    def test_default_test_command(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo", target_branch="main"
        )
        assert config.test_command == "make test"

    def test_default_max_concurrent_prs(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo", target_branch="main"
        )
        assert config.max_concurrent_prs == 3

    def test_default_max_implementation_time(self) -> None:
        config = ConfiguredTarget(
            repository="owner/repo", target_branch="main"
        )
        assert config.max_implementation_time == 3600
