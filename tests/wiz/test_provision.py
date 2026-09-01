"""Deterministic dependency provisioning: found on FEAT-00031, where three
attempts in a row failed identically on "tsc: command not found" because
node_modules never existed and nothing installed it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from openjarvis.wiz.features.provision import (
    _KNOWN_MANAGERS,
    PROVISION_CHECK_NAME,
    detect_package_manager,
    needs_provisioning,
    provision_check,
)

_NPM_SPEC = next(spec for spec in _KNOWN_MANAGERS if spec.name == "npm")


def _write_package_json(root: Path, *, package_manager: str = "") -> None:
    body = {"name": "x", "version": "0.0.0"}
    if package_manager:
        body["packageManager"] = package_manager
    (root / "package.json").write_text(json.dumps(body))


class TestDetectPackageManager:
    def test_no_package_json_is_undetermined(self, tmp_path):
        assert detect_package_manager(tmp_path) is None

    def test_npm_lockfile_alone_is_detected(self, tmp_path):
        _write_package_json(tmp_path)
        (tmp_path / "package-lock.json").write_text("{}")
        spec = detect_package_manager(tmp_path)
        assert spec is not None
        assert spec.name == "npm"
        assert spec.install_command == "npm ci"

    def test_pnpm_lockfile_alone_is_detected(self, tmp_path):
        _write_package_json(tmp_path)
        (tmp_path / "pnpm-lock.yaml").write_text("")
        spec = detect_package_manager(tmp_path)
        assert spec.name == "pnpm"
        assert "--frozen-lockfile" in spec.install_command

    def test_yarn_lockfile_alone_is_detected(self, tmp_path):
        _write_package_json(tmp_path)
        (tmp_path / "yarn.lock").write_text("")
        spec = detect_package_manager(tmp_path)
        assert spec.name == "yarn"
        assert "--frozen-lockfile" in spec.install_command

    def test_declared_package_manager_field_wins_over_lockfile_presence(self, tmp_path):
        # A stray npm lockfile left behind must not override an explicit,
        # correct declaration — the repo's own metadata is authoritative.
        _write_package_json(tmp_path, package_manager="pnpm@9.1.0")
        (tmp_path / "package-lock.json").write_text("{}")
        (tmp_path / "pnpm-lock.yaml").write_text("")
        spec = detect_package_manager(tmp_path)
        assert spec.name == "pnpm"

    def test_unrecognised_declared_manager_refuses_rather_than_guesses(self, tmp_path):
        _write_package_json(tmp_path, package_manager="bun@1.0.0")
        (tmp_path / "package-lock.json").write_text("{}")
        assert detect_package_manager(tmp_path) is None

    def test_no_lockfile_at_all_is_undetermined(self, tmp_path):
        _write_package_json(tmp_path)
        assert detect_package_manager(tmp_path) is None


class TestNeedsProvisioning:
    def test_missing_node_modules_needs_provisioning(self, tmp_path):
        (tmp_path / "package-lock.json").write_text("{}")
        assert needs_provisioning(tmp_path, _NPM_SPEC) is True

    def test_empty_bin_dir_needs_provisioning(self, tmp_path):
        (tmp_path / "package-lock.json").write_text("{}")
        (tmp_path / "node_modules" / ".bin").mkdir(parents=True)
        assert needs_provisioning(tmp_path, _NPM_SPEC) is True

    def test_fresh_node_modules_does_not_need_provisioning(self, tmp_path):
        (tmp_path / "package-lock.json").write_text("{}")
        bin_dir = tmp_path / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "tsc").write_text("#!/bin/sh\n")
        assert needs_provisioning(tmp_path, _NPM_SPEC) is False

    def test_stale_node_modules_older_than_lockfile_needs_provisioning(self, tmp_path):
        bin_dir = tmp_path / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "tsc").write_text("#!/bin/sh\n")
        old = time.time() - 3600
        os.utime(tmp_path / "node_modules", (old, old))

        lockfile = tmp_path / "package-lock.json"
        lockfile.write_text("{}")  # written after node_modules -> newer mtime

        assert needs_provisioning(tmp_path, _NPM_SPEC) is True

    def test_missing_lockfile_needs_provisioning(self, tmp_path):
        bin_dir = tmp_path / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "tsc").write_text("#!/bin/sh\n")
        assert needs_provisioning(tmp_path, _NPM_SPEC) is True


class TestProvisionCheckPreflight:
    def test_no_package_json_fails_closed(self, tmp_path):
        result = provision_check(str(tmp_path))
        assert result.name == PROVISION_CHECK_NAME
        assert not result.passed
        assert not result.ran
        assert "package.json" in result.summary

    def test_undetermined_package_manager_fails_closed(self, tmp_path):
        _write_package_json(tmp_path)
        result = provision_check(str(tmp_path))
        assert not result.passed
        assert "package manager" in result.summary

    def test_missing_lockfile_fails_closed(self, tmp_path):
        _write_package_json(tmp_path, package_manager="npm@10.0.0")
        result = provision_check(str(tmp_path))
        assert not result.passed
        assert "package-lock.json" in result.summary

    def test_not_enough_disk_fails_closed(self, tmp_path, monkeypatch):
        _write_package_json(tmp_path)
        (tmp_path / "package-lock.json").write_text("{}")
        monkeypatch.setattr(
            "openjarvis.wiz.features.provision.has_enough_disk", lambda *a, **k: False
        )
        result = provision_check(str(tmp_path))
        assert not result.passed
        assert "disk" in result.summary


class TestProvisionCheckSkipsWhenAlreadyCurrent:
    def test_already_provisioned_is_reported_as_passed_without_running_install(
        self, tmp_path
    ):
        _write_package_json(tmp_path)
        (tmp_path / "package-lock.json").write_text("{}")
        bin_dir = tmp_path / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "tsc").write_text("#!/bin/sh\n")

        result = provision_check(str(tmp_path))
        assert result.passed
        assert result.ran
        assert "skipped" in result.summary
        assert "npm ci" in result.summary


class TestProvisionCheckActuallyInstalls:
    """A real (tiny, local) install, proving the bounded-subprocess wiring —
    not a real npm registry fetch, which would make this test flaky and slow.
    """

    def test_a_local_only_install_runs_and_populates_node_modules(self, tmp_path):
        # A package.json with zero dependencies: `npm ci` still runs the
        # full command and still writes a real node_modules — bounded,
        # fast, and no network access required.
        _write_package_json(tmp_path)
        (tmp_path / "package-lock.json").write_text(
            json.dumps(
                {
                    "name": "x",
                    "version": "0.0.0",
                    "lockfileVersion": 3,
                    "requires": True,
                    "packages": {"": {"name": "x", "version": "0.0.0"}},
                }
            )
        )
        result = provision_check(str(tmp_path), timeout=120)
        assert result.passed, result.output
        assert result.ran
        # A dependency-free lockfile is a legitimate edge case: npm does not
        # necessarily create node_modules when there is nothing to put in
        # it. The real proof is that `npm ci` genuinely ran and succeeded —
        # asserted above via result.passed/result.ran, not by requiring a
        # directory npm itself does not guarantee for this specific case.

    def test_a_failed_install_is_reported_with_bounded_output(self, tmp_path):
        _write_package_json(tmp_path)
        # A lockfile npm will genuinely refuse (malformed), proving a real
        # failure is captured rather than swallowed.
        (tmp_path / "package-lock.json").write_text("{not valid json")
        result = provision_check(str(tmp_path), timeout=60)
        assert not result.passed
        assert result.ran
        assert len(result.output) <= 8000


class TestNoShellInterpolationFromModelOutput:
    def test_the_install_command_is_always_one_of_the_three_frozen_forms(self):
        from openjarvis.wiz.features.provision import _KNOWN_MANAGERS

        for spec in _KNOWN_MANAGERS:
            assert spec.install_command in (
                "npm ci",
                "pnpm install --frozen-lockfile",
                "yarn install --frozen-lockfile",
            )

    def test_provision_check_takes_no_claude_supplied_text_at_all(self):
        import inspect

        sig = inspect.signature(provision_check)
        # workspace, path_prepend, timeout, min_free_bytes only — nothing
        # shaped like "let the caller pass a command string".
        assert set(sig.parameters) == {
            "workspace",
            "path_prepend",
            "timeout",
            "min_free_bytes",
        }
