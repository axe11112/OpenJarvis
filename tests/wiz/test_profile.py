"""Engineering profiles: configured, discovered, and never hardcoded."""

from __future__ import annotations

import json

from openjarvis.wiz.features.profile import (
    EngineeringProfile,
    _major_version,
    discover_profile,
    load_profiles,
)


def _node_project(tmp_path, scripts, **extra):
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "wize", "scripts": scripts, **extra})
    )
    return tmp_path


class TestDiscovery:
    def test_scripts_are_read_from_package_json(self, tmp_path):
        root = _node_project(
            tmp_path,
            {
                "test": "vitest",
                "lint": "eslint .",
                "build": "next build",
                "typecheck": "tsc --noEmit",
            },
        )
        profile = discover_profile("wize", root)
        assert profile.test_command == "npm test"
        assert profile.lint_command == "npm run lint"
        assert profile.build_command == "npm run build"
        assert profile.typecheck_command == "npm run typecheck"

    def test_a_missing_script_produces_no_gate_rather_than_a_failing_one(
        self, tmp_path
    ):
        root = _node_project(tmp_path, {"test": "vitest"})
        profile = discover_profile("wize", root)
        assert profile.typecheck_command == ""
        assert profile.configured_gates == ["test"]

    def test_alternative_typecheck_names_are_found(self, tmp_path):
        root = _node_project(tmp_path, {"test": "vitest", "type-check": "tsc"})
        assert discover_profile("wize", root).typecheck_command == "npm run type-check"

    def test_the_node_version_comes_from_engines(self, tmp_path):
        root = _node_project(tmp_path, {"test": "vitest"}, engines={"node": "22"})
        assert discover_profile("wize", root).node_version == "22"

    def test_the_node_version_falls_back_to_nvmrc(self, tmp_path):
        root = _node_project(tmp_path, {"test": "vitest"})
        (root / ".nvmrc").write_text("22.4.0\n")
        assert discover_profile("wize", root).node_version == "22.4.0"

    def test_vercel_is_detected(self, tmp_path):
        root = _node_project(tmp_path, {"test": "vitest"})
        (root / "vercel.json").write_text("{}")
        assert discover_profile("wize", root).preview_provider == "vercel"

    def test_a_non_node_repository_is_none_not_an_empty_profile(self, tmp_path):
        # "I could not tell" and "there are no gates" must stay distinguishable.
        assert discover_profile("mystery", tmp_path) is None

    def test_a_malformed_package_json_is_none(self, tmp_path):
        (tmp_path / "package.json").write_text("{not json")
        assert discover_profile("wize", tmp_path) is None


class TestConfigurationWins:
    def test_an_explicit_command_survives_discovery(self, tmp_path):
        root = _node_project(tmp_path, {"test": "vitest"})
        configured = EngineeringProfile(
            name="wize", checkout=str(root), test_command="npm run test:ci"
        )
        merged = configured.merged_with_discovery()
        assert merged.test_command == "npm run test:ci"

    def test_unset_commands_are_filled_in(self, tmp_path):
        root = _node_project(tmp_path, {"test": "vitest", "build": "next build"})
        configured = EngineeringProfile(
            name="wize", checkout=str(root), test_command="npm run test:ci"
        )
        merged = configured.merged_with_discovery()
        assert merged.build_command == "npm run build"

    def test_merging_without_a_checkout_changes_nothing(self):
        configured = EngineeringProfile(name="wize", test_command="npm test")
        assert configured.merged_with_discovery() is configured


class TestCompleteness:
    def test_a_profile_without_a_test_command_cannot_gate(self, tmp_path):
        assert not EngineeringProfile(name="x", checkout=str(tmp_path)).complete

    def test_a_profile_without_a_checkout_cannot_gate(self):
        assert not EngineeringProfile(name="x", test_command="npm test").complete

    def test_a_usable_profile_is_complete(self, tmp_path):
        assert EngineeringProfile(
            name="x", checkout=str(tmp_path), test_command="npm test"
        ).complete

    def test_configured_gates_names_only_what_exists(self):
        profile = EngineeringProfile(
            name="x", test_command="npm test", build_command="npm run build"
        )
        assert profile.configured_gates == ["test", "build"]


class TestLoading:
    def test_profiles_load_from_a_mapping(self):
        profiles = load_profiles(
            {
                "wize": {
                    "repository": "axe11112/Wize-Performance",
                    "node_version": "22",
                    "test_command": "npm test",
                }
            }
        )
        assert profiles["wize"].repository == "axe11112/Wize-Performance"
        assert profiles["wize"].node_version == "22"

    def test_a_malformed_entry_is_skipped_not_fatal(self):
        profiles = load_profiles({"wize": "not a table"})
        assert profiles == {}


class TestNothingIsHardcoded:
    def test_no_repository_is_special_cased_in_code(self):
        # The brief: prefer configuration and discovery over special-casing one
        # repository in code. Prose may name Wize while explaining why it is not
        # hardcoded, so this reads the string *values* the module actually
        # evaluates and ignores docstrings.
        import ast
        import inspect

        from openjarvis.wiz.features import profile as module

        tree = ast.parse(inspect.getsource(module))

        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                doc = ast.get_docstring(node, clean=False)
                if doc is not None:
                    docstrings.add(doc)

        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value not in docstrings
        ]

        for literal in literals:
            assert "wize" not in literal.lower(), literal


class TestMajorVersion:
    def test_plain_number(self):
        assert _major_version("22") == 22

    def test_dotted_x_range(self):
        assert _major_version("24.x") == 24

    def test_caret_range(self):
        assert _major_version("^24.0.0") == 24

    def test_comparator(self):
        assert _major_version(">=22.4.0") == 22

    def test_no_digits_is_unresolvable(self):
        assert _major_version("lts/*") is None

    def test_empty_is_unresolvable(self):
        assert _major_version("") is None


def _fake_node(bin_dir, version: str) -> None:
    """A real, runnable executable -- resolution must run it, not read its path."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    node = bin_dir / "node"
    node.write_text(f"#!/bin/sh\necho v{version}\n")
    node.chmod(0o755)


class TestResolveNodeBinDir:
    def test_no_node_version_resolves_to_nothing(self, tmp_path):
        profile = EngineeringProfile(name="x", node_version="")
        assert profile.resolve_node_bin_dir() == ""

    def test_finds_a_matching_openjarvis_managed_install(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        bin_dir = tmp_path / ".openjarvis" / "tools" / "node24" / "bin"
        _fake_node(bin_dir, "24.20.0")
        profile = EngineeringProfile(name="x", node_version="24.x")
        assert profile.resolve_node_bin_dir() == str(bin_dir)

    def test_a_wrong_major_version_is_not_matched(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        bin_dir = tmp_path / ".openjarvis" / "tools" / "node24" / "bin"
        # Directory name says 24, but the binary itself reports 22 -- the
        # binary is what gets trusted, never the path name.
        _fake_node(bin_dir, "22.4.0")
        profile = EngineeringProfile(name="x", node_version="24.x")
        assert profile.resolve_node_bin_dir() == ""

    def test_nothing_installed_resolves_to_empty_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        profile = EngineeringProfile(name="x", node_version="24.x")
        assert profile.resolve_node_bin_dir() == ""

    def test_nvm_install_is_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        bin_dir = tmp_path / ".nvm" / "versions" / "node" / "v24.20.0" / "bin"
        _fake_node(bin_dir, "24.20.0")
        profile = EngineeringProfile(name="x", node_version="24")
        assert profile.resolve_node_bin_dir() == str(bin_dir)
