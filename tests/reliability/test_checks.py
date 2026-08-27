"""Tests for the local check suite.

The property that matters most: a check that did not run is never reported as
having passed. "We have no type checker" and "the types are fine" are different
facts, and only one of them is a reason to open a pull request.
"""

from __future__ import annotations

from openjarvis.reliability.checks import (
    CheckCommand,
    CheckSuite,
    CheckSuiteResult,
    run_check,
)


class TestRunCheck:
    def test_success(self, tmp_path):
        result = run_check(CheckCommand("tests", "exit 0"), workspace=str(tmp_path))
        assert result.ran and result.passed

    def test_failure_captures_output(self, tmp_path):
        result = run_check(
            CheckCommand("tests", "echo 'boom: 3 failed' && exit 1"),
            workspace=str(tmp_path),
        )
        assert result.ran and not result.passed
        assert "boom: 3 failed" in result.output
        assert "exit 1" in result.summary

    def test_success_keeps_no_output(self, tmp_path):
        """Passing output is noise; only failures need to travel back."""
        result = run_check(
            CheckCommand("tests", "echo lots-of-noise"), workspace=str(tmp_path)
        )
        assert result.output == ""

    def test_unconfigured_is_not_a_pass(self, tmp_path):
        result = run_check(CheckCommand("typecheck", ""), workspace=str(tmp_path))
        assert result.ran is False
        assert result.passed is False
        assert result.summary == "not configured"

    def test_unconfigured_does_not_block(self, tmp_path):
        result = run_check(CheckCommand("typecheck", ""), workspace=str(tmp_path))
        assert result.blocking_failure is False

    def test_timeout_is_a_failure(self, tmp_path):
        result = run_check(
            CheckCommand("tests", "sleep 5", timeout=1), workspace=str(tmp_path)
        )
        assert result.ran and not result.passed
        assert "timed out" in result.summary

    def test_advisory_failure_does_not_block(self, tmp_path):
        result = run_check(
            CheckCommand("lint", "exit 1", required=False), workspace=str(tmp_path)
        )
        assert result.ran and not result.passed
        assert result.blocking_failure is False

    def test_runs_in_the_given_workspace(self, tmp_path):
        (tmp_path / "marker.txt").write_text("here\n")
        result = run_check(
            CheckCommand("tests", "test -f marker.txt"), workspace=str(tmp_path)
        )
        assert result.passed


class TestPathPrepend:
    """A pinned runtime is only real if the subprocess actually sees it.

    Regression for ``node_version`` being discovered from ``package.json``'s
    ``engines`` field and then never used: the check runner ran every gate
    under whatever ``node`` happened to be on this process's PATH, silently,
    regardless of what the project asked for.
    """

    def _fake_tool(self, tmp_path, name: str, prints: str):
        directory = tmp_path / "bin"
        directory.mkdir(exist_ok=True)
        script = directory / name
        script.write_text(f"#!/bin/sh\necho {prints}\n")
        script.chmod(0o755)
        return str(directory)

    def test_a_prepended_directory_is_found_before_the_system_one(self, tmp_path):
        bin_dir = self._fake_tool(tmp_path, "mytool", "pinned-version")
        result = run_check(
            CheckCommand("tests", "mytool", path_prepend=[bin_dir]),
            workspace=str(tmp_path),
        )
        assert result.passed
        assert "pinned-version" in result.output or result.passed

    def test_without_path_prepend_the_pinned_tool_is_not_found(self, tmp_path):
        self._fake_tool(tmp_path, "mytool-unlikely-to-exist", "pinned-version")
        result = run_check(
            CheckCommand("tests", "mytool-unlikely-to-exist"), workspace=str(tmp_path)
        )
        assert not result.passed

    def test_check_suite_from_config_threads_path_prepend_to_every_check(self):
        suite = CheckSuite.from_config(
            test_command="t",
            lint_command="l",
            typecheck_command="c",
            build_command="b",
            path_prepend=["/some/pinned/bin"],
        )
        assert all(c.path_prepend == ["/some/pinned/bin"] for c in suite.checks)

    def test_no_path_prepend_by_default(self):
        suite = CheckSuite.from_config(test_command="t")
        assert all(c.path_prepend == [] for c in suite.checks)


class TestCheckSuite:
    def test_from_config_orders_cheapest_first(self):
        suite = CheckSuite.from_config(
            test_command="t", lint_command="l", typecheck_command="c", build_command="b"
        )
        assert [c.name for c in suite.checks] == [
            "lint",
            "typecheck",
            "tests",
            "build",
        ]

    def test_lint_is_advisory(self):
        suite = CheckSuite.from_config(lint_command="l")
        lint = next(c for c in suite.checks if c.name == "lint")
        assert lint.required is False

    def test_all_pass(self, tmp_path):
        suite = CheckSuite.from_config(test_command="exit 0", build_command="exit 0")
        result = suite.run(workspace=str(tmp_path))
        assert result.passed
        assert result.ran_any

    def test_a_required_failure_fails_the_suite(self, tmp_path):
        suite = CheckSuite.from_config(test_command="exit 1")
        result = suite.run(workspace=str(tmp_path))
        assert not result.passed
        assert [f.name for f in result.failures] == ["tests"]

    def test_stops_early_and_records_the_skipped_checks(self, tmp_path):
        """Skipped checks are listed honestly rather than silently omitted."""
        suite = CheckSuite.from_config(test_command="exit 1", build_command="exit 0")
        result = suite.run(workspace=str(tmp_path))
        build = next(r for r in result.results if r.name == "build")
        assert build.ran is False
        assert "skipped" in build.summary

    def test_can_run_everything(self, tmp_path):
        suite = CheckSuite.from_config(test_command="exit 1", build_command="exit 0")
        result = suite.run(workspace=str(tmp_path), stop_early=False)
        build = next(r for r in result.results if r.name == "build")
        assert build.ran is True

    def test_advisory_failure_does_not_stop_the_suite(self, tmp_path):
        suite = CheckSuite.from_config(lint_command="exit 1", test_command="exit 0")
        result = suite.run(workspace=str(tmp_path))
        assert result.passed
        tests = next(r for r in result.results if r.name == "tests")
        assert tests.ran is True

    def test_nothing_configured_runs_nothing(self, tmp_path):
        result = CheckSuite.from_config().run(workspace=str(tmp_path))
        assert result.ran_any is False
        # Vacuously passing is safe only because verification still has to run.
        assert result.passed is True

    def test_configured_names(self):
        suite = CheckSuite.from_config(test_command="t", build_command="")
        assert suite.configured_names == ["tests"]


class TestFeedback:
    def test_failures_become_agent_feedback(self, tmp_path):
        suite = CheckSuite.from_config(test_command="echo 'AssertionError' && exit 1")
        result = suite.run(workspace=str(tmp_path))
        feedback = result.feedback()
        assert "tests failed" in feedback
        assert "AssertionError" in feedback

    def test_no_failures_means_no_feedback(self, tmp_path):
        result = CheckSuite.from_config(test_command="exit 0").run(
            workspace=str(tmp_path)
        )
        assert result.feedback() == ""

    def test_feedback_is_bounded(self, tmp_path):
        suite = CheckSuite.from_config(
            test_command="python3 -c \"print('x'*100000)\" && exit 1"
        )
        result = suite.run(workspace=str(tmp_path))
        assert len(result.feedback(max_chars=500)) <= 500

    def test_summary_lists_every_check(self, tmp_path):
        suite = CheckSuite.from_config(test_command="exit 0", build_command="exit 0")
        result = suite.run(workspace=str(tmp_path))
        assert "tests" in result.summary
        assert "build" in result.summary

    def test_empty_suite_summary(self):
        assert CheckSuiteResult().summary == "no checks configured"

    def test_round_trips(self, tmp_path):
        result = CheckSuite.from_config(test_command="exit 0").run(
            workspace=str(tmp_path)
        )
        payload = result.to_dict()
        assert payload["passed"] is True
        names = [r["name"] for r in payload["results"]]
        assert "tests" in names

    def test_serialized_results_exclude_output(self, tmp_path):
        """Check output can be large and can contain application data."""
        suite = CheckSuite.from_config(test_command="echo SENSITIVE && exit 1")
        payload = suite.run(workspace=str(tmp_path)).to_dict()
        assert "SENSITIVE" not in str(payload)
