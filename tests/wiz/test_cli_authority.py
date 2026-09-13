"""`jarvis wiz authority`: the read-only preflight before enabling the ceiling.

The channel ceiling is enforced at both gates, and AuthorityPolicy.default()
deliberately grants no CODE_WRITE, PR_WRITE or PRODUCTION_CHANGE to anyone --
writing code is an opt-in an owner makes on purpose, not what happens when a
config file is missing. Without a way to ask, the first sign of that on a live
machine would be a refused build.

So this command exists to be run *before* the switch, and it has to be safe to
run there: policy only, no feature store, no checkout, no network, no secrets.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from openjarvis.cli.wiz_cmd import wiz


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A sandboxed OPENJARVIS_HOME, so nothing reads the operator's real one."""
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    return tmp_path / "wiz"


def _write_policy(home, grants: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "authority.json").write_text(json.dumps({"grants": grants}))


def _report(args=("authority", "--json")) -> dict:
    result = CliRunner().invoke(wiz, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


class TestItAnswersTheQuestionsTheOperatorHasToAsk:
    def test_it_names_who_can_write_code_open_prs_and_change_production(self, home):
        _write_policy(
            home,
            {
                "control_center": ["CODE_WRITE", "PR_WRITE", "PRODUCTION_CHANGE"],
                "cli": ["CODE_WRITE"],
            },
        )
        report = _report()
        assert "control_center" in report["code_write"]
        assert "cli" in report["code_write"]
        assert "control_center" in report["pr_write"]
        assert report["production_change"] == ["control_center"]

    def test_it_says_whether_build_and_ship_would_be_allowed(self, home):
        _write_policy(home, {"control_center": ["CODE_WRITE", "PRODUCTION_CHANGE"]})
        report = _report()
        assert report["feature_build_allowed"] is True
        assert report["feature_ship_allowed"] is True

    def test_a_missing_policy_reports_the_defaults_and_refuses_nothing_silently(
        self, home
    ):
        """The case that would otherwise surface as a mysterious refusal."""
        report = _report()
        assert report["policy_file_present"] is False
        assert report["using_defaults"] is True
        assert report["code_write"] == []
        assert report["production_change"] == []
        assert report["feature_build_allowed"] is False
        assert report["feature_ship_allowed"] is False

    def test_the_human_output_says_what_to_do_about_it(self, home):
        result = CliRunner().invoke(wiz, ["authority"])
        assert result.exit_code == 0
        # Rich wraps, so assert on fragments a line break cannot split.
        flat = " ".join(result.output.split())
        assert "feature work will be refused" in flat
        assert "authority.json" in flat
        assert "will not edit it for you" in flat


class TestTheCeilingsAreReportedAsCeilings:
    """"Not granted today" and "can never be granted" are different facts."""

    def test_telegram_and_voice_can_never_change_production(self, home):
        _write_policy(home, {"control_center": ["PRODUCTION_CHANGE"]})
        report = _report()
        assert report["telegram_cannot_change_production"] is True
        assert report["voice_cannot_change_production"] is True

    def test_it_stays_true_even_when_the_config_asks_for_it(self, home):
        """A config asking for the impossible is clamped, and still reported."""
        _write_policy(
            home,
            {
                "telegram": ["PRODUCTION_CHANGE"],
                "voice": ["PRODUCTION_CHANGE"],
            },
        )
        report = _report()
        assert report["telegram_cannot_change_production"] is True
        assert report["voice_cannot_change_production"] is True
        assert "telegram" not in report["production_change"]
        assert "voice" not in report["production_change"]

    def test_it_would_report_a_weakened_ceiling(self, home, monkeypatch):
        """The check has to be capable of saying no, or it says nothing."""
        from openjarvis.wiz import authority as authority_mod

        weakened = dict(authority_mod.CHANNEL_CEILING)
        weakened[authority_mod.Channel.TELEGRAM] = authority_mod.expand(
            {authority_mod.Authority.PRODUCTION_CHANGE}
        )
        monkeypatch.setattr(authority_mod, "CHANNEL_CEILING", weakened)

        report = _report()
        assert report["telegram_cannot_change_production"] is False


class TestItIsSafeToRunOnTheLiveMachine:
    def test_it_reads_the_policy_and_nothing_else(self, home, monkeypatch):
        """No feature store, no checkout, no engineering target, no network.

        assemble() and build_wiz() open the feature database and the git
        checkout; a preflight that did that could not be run before deciding
        whether to enable anything.
        """
        import openjarvis.wiz.assemble as assemble_mod
        import openjarvis.wiz.runtime as runtime_mod

        def _forbidden(*_args, **_kwargs):
            raise AssertionError("the preflight built the runtime")

        monkeypatch.setattr(assemble_mod, "assemble", _forbidden)
        monkeypatch.setattr(runtime_mod, "build_wiz", _forbidden)

        _write_policy(home, {"control_center": ["CODE_WRITE"]})
        report = _report()
        assert report["code_write"] == ["control_center"]

    def test_it_does_not_write_anything(self, home):
        _write_policy(home, {"control_center": ["CODE_WRITE"]})
        before = (home / "authority.json").read_text()
        _report()
        assert (home / "authority.json").read_text() == before, (
            "the preflight edited the policy it was asked to inspect"
        )

    def test_it_prints_no_values_that_could_be_secrets(self, home):
        """The policy is a map from channel to consequence; it holds no secrets.

        Asserted anyway, because a report that grew a config dump later would
        be a quiet way to start printing them.
        """
        (home).mkdir(parents=True, exist_ok=True)
        (home / "authority.json").write_text(
            json.dumps(
                {
                    "grants": {"control_center": ["CODE_WRITE"]},
                    "unrelated_token": "ghp_FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE1234",
                }
            )
        )
        result = CliRunner().invoke(wiz, ["authority"])
        assert result.exit_code == 0
        assert "ghp_" not in result.output
        assert "FAKEFAKE" not in result.output
