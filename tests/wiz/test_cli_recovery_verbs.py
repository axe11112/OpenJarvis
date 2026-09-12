"""The operator recovery verbs are reachable from something an owner can run.

FeaturePipeline grew five operator verbs, each written to break a specific
lifecycle deadlock, each with tests -- and for a while none of them was called
from anywhere. Every reference outside their own definitions was a docstring
cross-reference, so the deadlocks they close stayed closed unless someone
opened a Python REPL against the live feature store.

The first test here is the one that matters: it fails if a verb exists on the
pipeline with no way to reach it. The rest check that the commands are thin
calls onto the canonical methods rather than a second implementation of them.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from click.testing import CliRunner

from openjarvis.cli.wiz_cmd import wiz
from openjarvis.wiz.features.pipeline import FeaturePipeline

#: Every pipeline method that only an operator can trigger, mapped to the CLI
#: command that triggers it. A new operator verb must be added here *and* given
#: a command; that is the point.
OPERATOR_VERBS: Dict[str, str] = {
    "reopen_for_planning": "reopen",
    "reopen_for_deploy": "reopen",
    "reopen_for_owner_authorized_rebuild": "reopen",
    "approve_manual_acceptance": "accept",
    "reverify_against_current_base": "refresh-base",
    "reconcile_after_ship": "reconcile",
    "reverify_production": "reconcile",
}


class _FakeFeature:
    def __init__(self, feature_id: str = "FEAT-00031"):
        self.id = feature_id
        self.state = type("S", (), {"value": "TESTING"})()


class _FakePipeline:
    """Records which canonical method the command reached, and with what."""

    def __init__(self, *, raises: Exception | None = None):
        self.calls: List[tuple] = []
        self._raises = raises

    def _record(self, name: str, feature_id: str, kwargs: Dict[str, Any]):
        self.calls.append((name, feature_id, kwargs))
        if self._raises is not None:
            raise self._raises
        return _FakeFeature(feature_id)

    def reopen_for_planning(self, feature_id, **kw):
        return self._record("reopen_for_planning", feature_id, kw)

    def reopen_for_deploy(self, feature_id, **kw):
        return self._record("reopen_for_deploy", feature_id, kw)

    def reopen_for_owner_authorized_rebuild(self, feature_id, **kw):
        return self._record("reopen_for_owner_authorized_rebuild", feature_id, kw)

    def approve_manual_acceptance(self, feature_id, **kw):
        return self._record("approve_manual_acceptance", feature_id, kw)

    def reverify_against_current_base(self, feature_id, **kw):
        return self._record("reverify_against_current_base", feature_id, kw)


@pytest.fixture
def pipeline(monkeypatch):
    fake = _FakePipeline()
    monkeypatch.setattr(
        "openjarvis.cli.wiz_cmd._pipeline_or_exit", lambda: fake, raising=True
    )
    return fake


class TestTheVerbsAreReachable:
    def test_every_operator_verb_has_a_command(self):
        """The regression guard: an unreachable recovery verb is not a
        recovery path, however well it is implemented and tested."""
        commands = set(wiz.commands)
        for verb, command in OPERATOR_VERBS.items():
            assert hasattr(FeaturePipeline, verb), (
                f"{verb} no longer exists; update OPERATOR_VERBS with it"
            )
            assert command in commands, (
                f"FeaturePipeline.{verb} has no way to be run: expected a "
                f"`jarvis wiz {command}` command. An operator verb nothing can "
                f"call is a deadlock, not a recovery path."
            )

    def test_no_operator_verb_is_left_out_of_the_map(self):
        """Catches a *new* verb added to the pipeline and wired to nothing."""
        suspicious = {
            name
            for name in vars(FeaturePipeline)
            if name.startswith(("reopen_for_", "approve_", "reverify_", "reconcile_"))
            and not name.startswith("_")
        }
        missing = suspicious - set(OPERATOR_VERBS)
        assert not missing, (
            f"new operator verb(s) {sorted(missing)} on FeaturePipeline with no "
            f"entry here and probably no way to run them"
        )


class TestReopen:
    @pytest.mark.parametrize(
        "target,expected",
        [
            ("planning", "reopen_for_planning"),
            ("deploy", "reopen_for_deploy"),
            ("rebuild", "reopen_for_owner_authorized_rebuild"),
        ],
    )
    def test_each_target_reaches_its_own_canonical_method(
        self, pipeline, target, expected
    ):
        result = CliRunner().invoke(
            wiz,
            ["reopen", "FEAT-00031", "--for", target, "--reason", "infra was broken"],
        )
        assert result.exit_code == 0, result.output
        assert [c[0] for c in pipeline.calls] == [expected]
        assert pipeline.calls[0][1] == "FEAT-00031"
        assert pipeline.calls[0][2]["reason"] == "infra was broken"

    def test_rebuild_refuses_without_a_reason(self, pipeline):
        """The one verb that spends an attempt past the budget asks why."""
        result = CliRunner().invoke(wiz, ["reopen", "FEAT-00031", "--for", "rebuild"])
        assert result.exit_code != 0
        assert "--reason is required" in result.output
        assert pipeline.calls == [], "it reached the pipeline anyway"

    def test_deploy_does_not_require_a_reason(self, pipeline):
        """Re-running the checks costs no Claude session, so it needs no ceremony."""
        result = CliRunner().invoke(wiz, ["reopen", "FEAT-00031", "--for", "deploy"])
        assert result.exit_code == 0, result.output
        assert pipeline.calls[0][0] == "reopen_for_deploy"

    def test_an_unknown_target_is_refused_by_the_parser(self, pipeline):
        result = CliRunner().invoke(
            wiz, ["reopen", "FEAT-00031", "--for", "everything"]
        )
        assert result.exit_code != 0
        assert pipeline.calls == []


class TestAccept:
    def test_it_reaches_approve_manual_acceptance(self, pipeline):
        result = CliRunner().invoke(
            wiz, ["accept", "FEAT-00031", "--reason", "checked the layout myself"]
        )
        assert result.exit_code == 0, result.output
        assert pipeline.calls[0][0] == "approve_manual_acceptance"
        assert pipeline.calls[0][2]["reason"] == "checked the layout myself"

    def test_a_reason_is_required(self, pipeline):
        """This records a person's personal judgement; it is not a --force."""
        result = CliRunner().invoke(wiz, ["accept", "FEAT-00031"])
        assert result.exit_code != 0
        assert pipeline.calls == []


class TestRefreshBase:
    def test_it_reaches_reverify_against_current_base(self, pipeline):
        result = CliRunner().invoke(
            wiz,
            [
                "refresh-base",
                "FEAT-00031",
                "--expected-head-sha",
                "a" * 40,
                "--reason",
                "main moved",
            ],
        )
        assert result.exit_code == 0, result.output
        name, feature_id, kwargs = pipeline.calls[0]
        assert name == "reverify_against_current_base"
        assert feature_id == "FEAT-00031"
        assert kwargs["expected_head_sha"] == "a" * 40
        assert kwargs["reason"] == "main moved"

    def test_the_expected_head_sha_is_not_optional(self, pipeline):
        """Naming the commit is the interlock: it stops this acting on a
        feature that moved between the owner reading it and running this."""
        result = CliRunner().invoke(
            wiz, ["refresh-base", "FEAT-00031", "--reason", "main moved"]
        )
        assert result.exit_code != 0
        assert pipeline.calls == []


class TestRefusalsReachTheOwner:
    def test_a_pipeline_refusal_is_printed_and_exits_non_zero(self, monkeypatch):
        """A refused recovery must not look like a successful one."""
        fake = _FakePipeline(
            raises=ValueError("FEAT-00031 is COMPLETE, not HUMAN_REQUIRED")
        )
        monkeypatch.setattr(
            "openjarvis.cli.wiz_cmd._pipeline_or_exit", lambda: fake, raising=True
        )
        result = CliRunner().invoke(
            wiz, ["reopen", "FEAT-00031", "--for", "deploy", "--reason", "x"]
        )
        assert result.exit_code != 0
        assert "is COMPLETE, not HUMAN_REQUIRED" in result.output
