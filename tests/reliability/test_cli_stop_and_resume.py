"""The emergency stop, and the other half of it.

Engaging a stop is something an operator does in a hurry and must be easy.
Lifting one is something they should do on purpose. Until `resume` existed the
second was `rm` against a path they had to work out -- a raw filesystem command
in the middle of a written procedure, composed under pressure, against a guess.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from openjarvis.cli.reliability_cmd import reliability


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A sandboxed OPENJARVIS_HOME, with the config cache cleared around it.

    ``load_config`` is memoised, so without this a test inherits whichever
    home the previous one used -- and these tests deliberately leave a stop
    engaged in one of them.
    """
    from openjarvis.core.config import load_config

    load_config.cache_clear()
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    yield tmp_path
    load_config.cache_clear()


def _stop_flag(home):
    from openjarvis.core.config import load_config
    from openjarvis.reliability.watch import stop_flag_path

    return stop_flag_path(load_config())


class TestTheStopCanBeLifted:
    def test_engaging_and_lifting_round_trips(self, home):
        runner = CliRunner()
        assert runner.invoke(reliability, ["stop"]).exit_code == 0
        flag = _stop_flag(home)
        assert flag.is_file()

        result = runner.invoke(reliability, ["resume"], input="y\n")
        assert result.exit_code == 0, result.output
        assert not flag.exists()
        assert "RESUMED" in result.output

    def test_lifting_asks_first(self, home):
        runner = CliRunner()
        runner.invoke(reliability, ["stop"])
        flag = _stop_flag(home)

        result = runner.invoke(reliability, ["resume"], input="n\n")
        assert result.exit_code != 0
        assert flag.is_file(), "the stop was lifted without an answer"

    def test_there_is_no_flag_to_skip_the_question(self, home):
        """Nothing automatic may lift a stop. A flag would make it scriptable,
        and a scriptable resume is a resume something can call."""
        result = CliRunner().invoke(reliability, ["resume", "--help"])
        assert "--yes" not in result.output
        assert "--force" not in result.output

    def test_lifting_when_nothing_is_engaged_is_not_an_error(self, home):
        result = CliRunner().invoke(reliability, ["resume"])
        assert result.exit_code == 0
        assert "nothing to lift" in result.output

    def test_it_says_plainly_that_it_started_nothing(self, home):
        runner = CliRunner()
        runner.invoke(reliability, ["stop"])
        result = runner.invoke(reliability, ["resume"], input="y\n")
        assert "Production:   UNCHANGED" in result.output
        assert "service start" in result.output

    def test_the_two_commands_resolve_the_same_file(self, home):
        """A resume that cleared a different file than stop wrote would report
        success while the stop stayed in force."""
        runner = CliRunner()
        runner.invoke(reliability, ["stop"])
        written = _stop_flag(home)
        assert written.is_file()
        runner.invoke(reliability, ["resume"], input="y\n")
        assert not written.exists()
