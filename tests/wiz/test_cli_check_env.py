"""`jarvis wiz check-env`: what the gates will see, asked before they run.

A local check no longer inherits the watcher's environment -- it would hand
code the coding agent just wrote the Supabase service_role key, a GitHub token
that can merge, and the Telegram bot token. That is the right default and it is
a behaviour change on a live machine: a build that silently depended on an
inherited variable starts failing.

So there has to be a way to find out *before* restarting the watcher there,
and it has to be safe to run on that machine: no check executed, no worktree,
no network, and no values printed.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from openjarvis.cli.wiz_cmd import wiz


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    return tmp_path / "wiz"


def _write_settings(home, target: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "wiz.json").write_text(
        json.dumps({"targets": {"wize": target}, "default_target": "wize"})
    )


def _report(args=("check-env", "--json")) -> dict:
    result = CliRunner().invoke(wiz, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


BASIC = {
    "repository": "example/wize",
    "test_command": "npm test",
    "build_command": "npm run build",
}


class TestItAnswersWhatTheOperatorHasToKnow:
    def test_it_names_what_each_gate_would_not_inherit(self, home, monkeypatch):
        monkeypatch.setenv("SOME_BUILD_FLAG", "1")
        _write_settings(home, BASIC)
        report = _report()
        gates = {g["name"]: g for g in report["gates"]}
        assert set(gates) == {"lint", "typecheck", "tests", "build"}
        assert "SOME_BUILD_FLAG" in gates["build"]["withheld"]

    def test_it_names_what_each_gate_would_inherit(self, home, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        _write_settings(home, BASIC)
        gates = {g["name"]: g for g in _report()["gates"]}
        assert "PATH" in gates["tests"]["inherited"]

    def test_a_named_variable_shows_as_named(self, home, monkeypatch):
        monkeypatch.setenv("NPM_TOKEN", "secret")
        _write_settings(home, {**BASIC, "check_env_pass_through": ["NPM_TOKEN"]})
        gates = {g["name"]: g for g in _report()["gates"]}
        assert gates["build"]["named_pass_through"] == ["NPM_TOKEN"]
        assert "NPM_TOKEN" in gates["build"]["inherited"]
        assert "NPM_TOKEN" not in gates["build"]["withheld"]

    def test_an_unconfigured_gate_is_reported_as_such(self, home):
        _write_settings(home, {"repository": "example/wize", "test_command": ""})
        gates = {g["name"]: g for g in _report()["gates"]}
        assert gates["tests"]["configured"] is False


class TestItIsSafeToRunOnTheLiveMachine:
    def test_it_prints_no_values(self, home, monkeypatch):
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sbp_do_not_print_me")
        _write_settings(home, BASIC)
        result = CliRunner().invoke(wiz, ["check-env"])
        assert result.exit_code == 0
        assert "sbp_do_not_print_me" not in result.output
        assert "SUPABASE_SERVICE_ROLE_KEY" in result.output

    def test_it_prints_no_values_in_json_either(self, home, monkeypatch):
        monkeypatch.setenv("NPM_TOKEN", "npm_do_not_print_me")
        _write_settings(home, {**BASIC, "check_env_pass_through": ["NPM_TOKEN"]})
        result = CliRunner().invoke(wiz, ["check-env", "--json"])
        assert "npm_do_not_print_me" not in result.output

    def test_it_runs_nothing(self, home, monkeypatch):
        """A preflight that executed the build would be the opposite of a
        preflight."""
        import openjarvis.reliability.checks as checks_mod

        called = []
        monkeypatch.setattr(
            checks_mod, "run_check", lambda *a, **kw: called.append(a)
        )
        _write_settings(home, BASIC)
        _report()
        assert called == []

    def test_no_target_is_not_an_error(self, home):
        home.mkdir(parents=True, exist_ok=True)
        (home / "wiz.json").write_text(json.dumps({"targets": {}}))
        result = CliRunner().invoke(wiz, ["check-env"])
        assert result.exit_code == 0
        assert "No engineering target" in result.output
