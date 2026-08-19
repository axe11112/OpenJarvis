"""Wiz's settings, and the assembly they produce."""

from __future__ import annotations

import json

from openjarvis.wiz.assemble import assemble, describe
from openjarvis.wiz.settings import (
    ALWAYS_PROTECTED,
    SETTINGS_FILENAME,
    WizSettings,
    load_settings,
)


def write_settings(tmp_path, payload):
    (tmp_path / SETTINGS_FILENAME).write_text(json.dumps(payload))
    return tmp_path


class TestTheDefaultIsToBuildNothing:
    def test_a_missing_file_configures_no_target(self, tmp_path):
        settings = load_settings(tmp_path / SETTINGS_FILENAME)
        assert not settings.configured
        assert settings.profile() is None

    def test_a_malformed_file_is_a_warning_not_a_crash(self, tmp_path):
        # Wiz refusing to start over a missing comma would take the reliability
        # subsystem down with it, and that one is watching production.
        (tmp_path / SETTINGS_FILENAME).write_text("{not json")
        settings = load_settings(tmp_path / SETTINGS_FILENAME)
        assert not settings.configured

    def test_a_file_that_is_not_an_object_is_ignored(self, tmp_path):
        (tmp_path / SETTINGS_FILENAME).write_text("[1, 2, 3]")
        assert not load_settings(tmp_path / SETTINGS_FILENAME).configured

    def test_nothing_merges_by_default(self, tmp_path):
        settings = load_settings(tmp_path / SETTINGS_FILENAME)
        assert not settings.shipping.merge_low_risk
        assert not settings.shipping.merge_medium_risk


class TestProtectedCheckouts:
    def test_the_live_checkouts_are_protected_even_with_an_empty_file(self, tmp_path):
        protected = load_settings(tmp_path / SETTINGS_FILENAME).all_protected()
        for path in ALWAYS_PROTECTED:
            assert path in protected

    def test_removing_them_from_configuration_does_not_make_them_editable(
        self, tmp_path
    ):
        write_settings(tmp_path, {"protected_checkouts": []})
        protected = load_settings(tmp_path / SETTINGS_FILENAME).all_protected()
        assert any("Wize" in p for p in protected)
        assert any("OpenJarvis" in p for p in protected)

    def test_an_operators_own_additions_are_kept(self, tmp_path):
        write_settings(tmp_path, {"protected_checkouts": ["~/other-project"]})
        assert "~/other-project" in (
            load_settings(tmp_path / SETTINGS_FILENAME).all_protected()
        )


class TestTargets:
    def test_a_single_target_is_used_without_being_named(self, tmp_path):
        write_settings(
            tmp_path,
            {
                "targets": {
                    "wize": {"checkout": "/tmp/wize", "test_command": "npm test"}
                }
            },
        )
        settings = load_settings(tmp_path / SETTINGS_FILENAME)
        assert settings.profile().name == "wize"

    def test_with_several_targets_one_must_be_named_the_default(self, tmp_path):
        write_settings(
            tmp_path,
            {
                "targets": {
                    "wize": {"checkout": "/tmp/a", "test_command": "npm test"},
                    "other": {"checkout": "/tmp/b", "test_command": "npm test"},
                }
            },
        )
        settings = load_settings(tmp_path / SETTINGS_FILENAME)
        # Ambiguous: refusing to guess is the right answer when the wrong guess
        # means building in the wrong repository.
        assert settings.profile() is None

        write_settings(
            tmp_path,
            {
                "default_target": "wize",
                "targets": {
                    "wize": {"checkout": "/tmp/a", "test_command": "npm test"},
                    "other": {"checkout": "/tmp/b", "test_command": "npm test"},
                },
            },
        )
        assert load_settings(tmp_path / SETTINGS_FILENAME).profile().name == "wize"

    def test_commands_are_discovered_from_the_repository(self, tmp_path):
        checkout = tmp_path / "app"
        checkout.mkdir()
        (checkout / "package.json").write_text(
            json.dumps({"scripts": {"test": "vitest", "lint": "eslint ."}})
        )
        write_settings(tmp_path, {"targets": {"app": {"checkout": str(checkout)}}})
        profile = load_settings(tmp_path / SETTINGS_FILENAME).profile()
        assert profile.test_command == "npm test"
        assert profile.lint_command == "npm run lint"
        # No typecheck script, so no typecheck gate — an always-failing gate
        # teaches everyone to ignore gates.
        assert "typecheck" not in profile.configured_gates

    def test_configuration_beats_discovery(self, tmp_path):
        checkout = tmp_path / "app"
        checkout.mkdir()
        (checkout / "package.json").write_text(json.dumps({"scripts": {"test": "x"}}))
        write_settings(
            tmp_path,
            {
                "targets": {
                    "app": {
                        "checkout": str(checkout),
                        "test_command": "npm run test:ci",
                    }
                }
            },
        )
        profile = load_settings(tmp_path / SETTINGS_FILENAME).profile()
        assert profile.test_command == "npm run test:ci"


class TestAssembly:
    def test_without_a_target_nothing_is_assembled(self, tmp_path):
        assert assemble(home=tmp_path) is None

    def test_a_target_that_cannot_be_proved_is_refused(self, tmp_path):
        # A profile with no test command cannot prove anything about a change,
        # and a pipeline running on it would be theatre.
        settings = WizSettings.from_mapping(
            {"targets": {"app": {"checkout": str(tmp_path)}}}
        )
        assert assemble(home=tmp_path, settings=settings) is None

    def test_a_complete_target_assembles(self, tmp_path):
        checkout = tmp_path / "app"
        checkout.mkdir()
        settings = WizSettings.from_mapping(
            {
                "targets": {
                    "app": {"checkout": str(checkout), "test_command": "npm test"}
                },
                "worktree_root": str(tmp_path / "worktrees"),
            }
        )
        product = assemble(home=tmp_path, settings=settings)
        assert product is not None
        assert product.pipeline.profile.name == "app"
        assert product.memory is not None

    def test_the_diagnosis_names_the_missing_piece(self, tmp_path):
        report = describe(home=tmp_path)
        assert not report["can_build"]
        failed = [c["name"] for c in report["checks"] if not c["ok"]]
        assert "target" in failed
        # And it says which settings file it looked for, so the operator can
        # go and create it.
        assert SETTINGS_FILENAME in report["checks"][0]["detail"]

    def test_the_diagnosis_reports_the_shipping_policy_honestly(self, tmp_path):
        report = describe(home=tmp_path)
        assert report["shipping"]["merge_low_risk"] is False
        assert report["shipping"]["merge_high_risk"] is False
