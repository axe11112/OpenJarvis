"""A Claude Code usage/session limit is a capacity problem, not a code failure.

Found on FEAT-00017: the planning session hit "You've hit your session limit"
as its last line of output, exited non-zero, and :class:`ClaudeCliAgent`
reported the failure as a bare "exit code 1" because the message landed on
stdout (the CLI's normal ``--output-format text`` channel) rather than
stderr. The feature pipeline then told the operator "I could not work out how
to build this: exit code 1" — losing the one fact that mattered.
"""

from __future__ import annotations

from openjarvis.reliability.code_agent import ClaudeCliAgent


def _agent(runner):
    return ClaudeCliAgent(runner=runner, executable="echo")


class _Proc:
    def __init__(self, returncode=1, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestTransientCapacityDetection:
    def test_a_session_limit_on_stdout_is_recognised(self):
        proc = _Proc(
            returncode=1,
            stdout="I was investigating...\nYou've hit your session limit · resets 6:30pm (Europe/Stockholm)",
        )
        result = _agent(lambda *a, **k: proc).run("plan it", workspace=".")

        assert result.succeeded is False
        assert "session limit" in result.error
        assert result.metadata.get("transient_reason") == "usage_limit"

    def test_a_session_limit_on_stderr_is_also_recognised(self):
        proc = _Proc(returncode=1, stderr="Error: usage limit reached, try later")
        result = _agent(lambda *a, **k: proc).run("plan it", workspace=".")

        assert result.succeeded is False
        assert result.metadata.get("transient_reason") == "usage_limit"

    def test_an_ordinary_non_zero_exit_is_not_called_transient(self):
        proc = _Proc(returncode=1, stderr="TypeError: cannot read property 'x'")
        result = _agent(lambda *a, **k: proc).run("plan it", workspace=".")

        assert result.succeeded is False
        assert result.error == "TypeError: cannot read property 'x'"
        assert "transient_reason" not in result.metadata

    def test_a_bare_nonzero_exit_with_no_output_still_falls_back_to_the_exit_code(self):
        proc = _Proc(returncode=1, stdout="", stderr="")
        result = _agent(lambda *a, **k: proc).run("plan it", workspace=".")

        assert result.error == "exit code 1"
        assert "transient_reason" not in result.metadata

    def test_success_is_unaffected(self):
        proc = _Proc(returncode=0, stdout="done")
        result = _agent(lambda *a, **k: proc).run("plan it", workspace=".")

        assert result.succeeded is True
