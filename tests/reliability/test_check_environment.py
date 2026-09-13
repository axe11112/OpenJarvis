"""What a local check is allowed to see.

A check is a shell command running code a coding agent wrote minutes earlier,
against a failure it does not understand, inside a worktree of a repository it
may only partly know. The process that starts it is the watcher, and the
watcher holds the production keyring: a Supabase ``service_role`` key, a
GitHub token that can merge, the Telegram bot token, Vercel and Anthropic
credentials.

``subprocess.run`` with no ``env`` hands the child all of it. Every test in
these files is about that not happening -- and about the check still having
what it legitimately needs, since a build that fails for want of ``PATH`` is a
false failure that blocks a repair just as surely.
"""

from __future__ import annotations

import ast
import os

from openjarvis.reliability.checks import (
    BASE_ENV,
    CheckCommand,
    CheckSuite,
    check_environment,
    run_check,
)

#: What the watcher actually has in its environment on the real machine.
PRODUCTION_KEYRING = {
    "SUPABASE_SERVICE_ROLE_KEY": "sbp_service_role_secret",
    "SUPABASE_ANON_KEY": "sbp_anon",
    "GITHUB_TOKEN": "ghp_can_merge",
    "TELEGRAM_BOT_TOKEN": "bot123:abc",
    "VERCEL_TOKEN": "vercel_secret",
    "ANTHROPIC_API_KEY": "sk-ant-secret",
    "OPENAI_API_KEY": "sk-openai-secret",
    "AWS_SECRET_ACCESS_KEY": "aws_secret",
    "DATABASE_URL": "postgres://user:password@host/db",
    "SENTRY_DSN": "https://key@sentry.io/1",
    "NPM_TOKEN": "npm_secret",
    "SESSION_SECRET": "cookies",
}

SAFE_ENVIRONMENT = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/Users/jarvis",
    "LANG": "en_US.UTF-8",
    "LC_ALL": "en_US.UTF-8",
    "NODE_OPTIONS": "--max-old-space-size=4096",
    "TMPDIR": "/tmp",
}


def _env(check: CheckCommand, extra=None):
    parent = dict(SAFE_ENVIRONMENT)
    parent.update(PRODUCTION_KEYRING)
    parent.update(extra or {})
    return check_environment(check, parent=parent)


class TestNoSecretTravelsByProximity:
    def test_not_one_production_credential_reaches_a_check(self):
        env = _env(CheckCommand("tests", "npm test"))
        for name in PRODUCTION_KEYRING:
            assert name not in env, (
                f"{name} reached a check that runs agent-written code, because "
                f"it happened to be set in the watcher's own process"
            )

    def test_no_value_of_a_credential_appears_under_any_other_name(self):
        """Not just the names -- the values must be gone entirely."""
        env = _env(CheckCommand("tests", "npm test"))
        leaked = set(PRODUCTION_KEYRING.values()) & set(env.values())
        assert not leaked

    def test_an_unrecognised_variable_is_dropped_rather_than_kept(self):
        """Allowlist, not denylist. A name nobody thought of is not passed.

        The failure mode of a denylist is a credential with a name the list
        did not anticipate -- and the next service this system integrates with
        will name its key something nobody here has written down yet.
        """
        env = _env(
            CheckCommand("tests", "npm test"),
            extra={"WIZE_INTERNAL_THING": "probably-a-secret"},
        )
        assert "WIZE_INTERNAL_THING" not in env

    def test_a_secret_shaped_name_is_dropped_even_if_it_reaches_the_base_set(
        self, monkeypatch
    ):
        """Defence in depth: an unsafe addition to BASE_ENV still does not pass."""
        monkeypatch.setattr(
            "openjarvis.reliability.checks.BASE_ENV",
            frozenset(BASE_ENV | {"HELPFUL_API_TOKEN"}),
        )
        env = _env(
            CheckCommand("tests", "npm test"), extra={"HELPFUL_API_TOKEN": "oops"}
        )
        assert "HELPFUL_API_TOKEN" not in env


class TestAChecksStillGetsWhatItNeeds:
    def test_the_toolchain_is_still_findable(self):
        env = _env(CheckCommand("tests", "npm test"))
        assert env["PATH"] == "/usr/bin:/bin"
        assert env["HOME"] == "/Users/jarvis"

    def test_the_locale_survives(self):
        env = _env(CheckCommand("tests", "npm test"))
        assert env["LANG"] == "en_US.UTF-8"
        assert env["LC_ALL"] == "en_US.UTF-8"

    def test_a_pinned_runtime_still_goes_first_on_path(self):
        check = CheckCommand("tests", "npm test", path_prepend=["/opt/node-24/bin"])
        env = _env(check)
        assert env["PATH"].startswith("/opt/node-24/bin" + os.pathsep)

    def test_a_configured_variable_still_reaches_the_build(self):
        check = CheckCommand(
            "build", "npm run build", env={"NODE_OPTIONS": "--max-old-space-size=8192"}
        )
        assert _env(check)["NODE_OPTIONS"] == "--max-old-space-size=8192"

    def test_a_check_with_no_overrides_still_gets_an_environment(self):
        """The regression that made this necessary.

        ``env`` was left as ``None`` unless a PATH or variable override was
        configured -- which is to say, almost always -- and ``None`` means
        "inherit everything".
        """
        env = _env(CheckCommand("lint", "npm run lint"))
        assert env["PATH"]
        assert "GITHUB_TOKEN" not in env


class TestTheOperatorCanStillSayYes:
    def test_a_named_variable_is_inherited(self):
        check = CheckCommand("build", "npm run build", pass_through=["NPM_TOKEN"])
        assert _env(check)["NPM_TOKEN"] == "npm_secret"

    def test_naming_one_does_not_admit_the_rest(self):
        check = CheckCommand("build", "npm run build", pass_through=["NPM_TOKEN"])
        env = _env(check)
        assert "GITHUB_TOKEN" not in env
        assert "SUPABASE_SERVICE_ROLE_KEY" not in env

    def test_a_named_variable_that_is_not_set_is_simply_absent(self):
        check = CheckCommand("build", "npm run build", pass_through=["NOT_SET_HERE"])
        assert "NOT_SET_HERE" not in _env(check)

    def test_the_suite_gives_every_gate_the_same_answer(self):
        """Four gates that disagree about what they can see would make "did the
        tests run the way the build did" a question with two honest answers."""
        suite = CheckSuite.from_config(
            test_command="npm test",
            lint_command="npm run lint",
            typecheck_command="npm run typecheck",
            build_command="npm run build",
            pass_through=["NPM_TOKEN"],
        )
        for check in suite.checks:
            assert check.pass_through == ["NPM_TOKEN"]


class TestTheRealSubprocessSeesTheSameThing:
    def test_a_command_cannot_read_a_secret_out_of_its_own_environment(
        self, tmp_path, monkeypatch
    ):
        """End to end, through the actual subprocess, not just the helper."""
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sbp_service_role_secret")
        result = run_check(
            CheckCommand("tests", 'echo "[${SUPABASE_SERVICE_ROLE_KEY}]"'),
            workspace=str(tmp_path),
        )
        assert result.ran
        # The command succeeded, so its output is not retained -- run it again
        # in a shape that fails, which is what a repair's tests would do.
        result = run_check(
            CheckCommand("tests", 'echo "[${SUPABASE_SERVICE_ROLE_KEY}]" >&2; exit 1'),
            workspace=str(tmp_path),
        )
        assert "sbp_service_role_secret" not in result.output
        assert "[]" in result.output

    def test_a_named_secret_does_reach_the_command(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NPM_TOKEN", "npm_secret_value")
        result = run_check(
            CheckCommand(
                "tests",
                'echo "[${NPM_TOKEN}]" >&2; exit 1',
                pass_through=["NPM_TOKEN"],
            ),
            workspace=str(tmp_path),
        )
        assert "npm_secret_value" in result.output


class TestTheRealCallersActuallyAskForThis:
    """A guard that is not wired is not a guard.

    Matched against the parsed call rather than the source text: a string
    search for "check_environment" is satisfied by the comment explaining it,
    and one for "env=" is satisfied by ``env=None`` -- which is the defect.
    """

    def _run_check_subprocess_call(self) -> ast.Call:
        import openjarvis.reliability.checks as mod

        tree = ast.parse(open(mod.__file__).read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "run_check":
                for inner in ast.walk(node):
                    if (
                        isinstance(inner, ast.Call)
                        and getattr(inner.func, "attr", "") == "run"
                    ):
                        return inner
        raise AssertionError("run_check no longer starts a subprocess")

    def test_run_check_never_passes_a_none_environment(self):
        call = self._run_check_subprocess_call()
        given = {kw.arg: kw.value for kw in call.keywords}
        assert "env" in given
        value = given["env"]
        assert not (isinstance(value, ast.Constant) and value.value is None), (
            "env=None means the check inherits every secret the watcher holds"
        )

    def test_the_watcher_passes_its_configured_allowlist(self):
        import openjarvis.cli.reliability_cmd as mod

        tree = ast.parse(open(mod.__file__).read())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") == "from_config"
            and getattr(node.func.value, "id", "") == "CheckSuite"
        ]
        assert calls, "the watcher no longer builds a CheckSuite"
        for call in calls:
            assert "pass_through" in {kw.arg for kw in call.keywords}

    def test_wiz_passes_its_repository_allowlist(self):
        import openjarvis.wiz.assemble as mod

        tree = ast.parse(open(mod.__file__).read())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") == "from_config"
        ]
        assert calls, "wiz no longer builds a CheckSuite"
        for call in calls:
            assert "pass_through" in {kw.arg for kw in call.keywords}

    def test_a_repository_cannot_name_its_own_allowlist_by_being_read(self):
        """Discovery reads package.json. package.json is in the repository the
        agent is editing, so anything it could say about secrets is a decision
        made by the thing being repaired."""
        from openjarvis.wiz.features.profile import EngineeringProfile

        profile = EngineeringProfile(
            name="wize", checkout="", check_env_pass_through=["NPM_TOKEN"]
        )
        merged = profile.merged_with_discovery()
        assert merged.check_env_pass_through == ["NPM_TOKEN"]


class TestAWithheldVariableIsDiagnosable:
    def test_a_failing_check_says_its_environment_was_narrowed(
        self, tmp_path, monkeypatch
    ):
        """The first thing an operator will think is "it works in my shell".

        A check that used to pass and now fails for want of a variable must
        say so where they will read it, or the narrowing is indistinguishable
        from the change being broken.
        """
        monkeypatch.setenv("SOME_PROJECT_FLAG", "1")
        result = run_check(CheckCommand("tests", "exit 1"), workspace=str(tmp_path))
        assert "not inherited" in result.output
        assert "check_env_pass_through" in result.output

    def test_the_note_names_no_variable(self, tmp_path, monkeypatch):
        """A count, not a list. These strings travel into incident evidence
        and pull request bodies."""
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sbp_secret")
        result = run_check(CheckCommand("tests", "exit 1"), workspace=str(tmp_path))
        assert "SUPABASE_SERVICE_ROLE_KEY" not in result.output
        assert "sbp_secret" not in result.output

    def test_a_passing_check_says_nothing(self, tmp_path):
        result = run_check(CheckCommand("tests", "true"), workspace=str(tmp_path))
        assert result.output == ""
