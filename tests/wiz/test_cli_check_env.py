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
import logging

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


@pytest.fixture(autouse=True)
def _isolate_logging():
    """Keep another suite's broken log handler out of this command's stdout.

    A suite earlier in the session can leave a root handler pointed at a
    stream pytest has since closed. Python then prints "--- Logging error ---"
    and a traceback -- into this command's captured output, where it is
    indistinguishable from what the command wrote. The command is fine; the
    channel it is being read through is not, so give these tests a clean one.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_raise = logging.raiseExceptions
    root.handlers = [logging.NullHandler()]
    # The traceback is printed by logging itself, from Handler.handleError, to
    # sys.stderr -- which CliRunner merges into the command's output. Swapping
    # the root handlers is not enough, because the broken one may be attached
    # to any logger in the tree.
    logging.raiseExceptions = False
    try:
        yield
    finally:
        logging.raiseExceptions = saved_raise
        root.handlers = saved_handlers


def _first_json_object(output: str) -> dict:
    """The command's JSON, even when something else has written to stdout.

    Another suite in the same session can leave a logging handler pointed at a
    stream pytest has since closed; Python then prints "--- Logging error ---"
    and a traceback wherever it lands, which here is around this command's
    output. That is a wart in the other suite, not in this command -- so parse
    the JSON value rather than the whole stream, and still fail loudly if there
    is no JSON value at all.
    """
    start = output.find("{")
    assert start >= 0, f"no JSON in the output:\n{output}"
    return json.JSONDecoder().raw_decode(output[start:])[0]


def _report(args=("check-env", "--json")) -> dict:
    result = CliRunner().invoke(wiz, list(args))
    assert result.exit_code == 0, result.output
    return _first_json_object(result.output)


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
